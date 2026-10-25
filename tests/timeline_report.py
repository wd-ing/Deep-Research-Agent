"""时间线报告：`trace_span` + `llm_call` 离线对账（2026-10-21，埋点轮）。

回答的问题：
  · 写作子图那两段 ~45-50s「非 LLM 静默」到底被什么 span 覆盖；
  · latency_ms 里有多少是限流排队（wait_ms vs ttfb_ms vs 服务时间）；
  · 检索节点内部搜/抓/分块/写库各占多少；
  · checkpoint 写入（尤其 ~1.1MB 的子图 checkpoint）吃了几秒。

用法：
    .venv/bin/python tests/timeline_report.py <db路径> [session_id]

session_id 不给时用 trace_span/llm_call 里**最近**（started_at 最大）的那个。
老库（没有 trace_span / llm_call 新列）自动降级成「无埋点数据」，不崩。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

NO_TRACE = "（无埋点数据：本库没有 trace_span 表或该会话没有 span 行）"
NO_LLM = "（无埋点数据：llm_call 缺 started_at/wait_ms/ttfb_ms 列或无行）"


def _conn(db: str) -> sqlite3.Connection:
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _cols(c: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _has(c: sqlite3.Connection, table: str) -> bool:
    try:
        c.execute(f"SELECT 1 FROM {table} LIMIT 1")
        return True
    except sqlite3.Error:
        return False


def _med(v: list[float]) -> float:
    v = sorted(v)
    return v[len(v) // 2] if v else 0.0


def _hm(t: float, t0: float) -> str:
    """相对 t0 的 mm:ss."""
    d = t - t0
    sign = "-" if d < 0 else ""
    d = abs(d)
    return f"{sign}{int(d // 60):02d}:{d % 60:05.2f}"


def _ts(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}"


def load(c: sqlite3.Connection, sid: str | None):
    """取会话的 spans 与 llm_call 行；返回 (sid, spans, calls, flags)。"""
    have_span = _has(c, "trace_span")
    lc_cols = _cols(c, "llm_call")
    have_llm = {"started_at", "wait_ms", "ttfb_ms"} <= lc_cols

    spans: list[sqlite3.Row] = []
    if have_span:
        if sid is None:
            r = c.execute(
                "SELECT session_id FROM trace_span"
                " ORDER BY started_at DESC LIMIT 1").fetchone()
            sid = r["session_id"] if r else None
        if sid:
            spans = c.execute(
                "SELECT * FROM trace_span WHERE session_id=?"
                " ORDER BY started_at", (sid,)).fetchall()
    if sid is None:
        r = c.execute(
            "SELECT session_id FROM llm_call WHERE session_id IS NOT NULL"
            " ORDER BY created_at DESC LIMIT 1").fetchone()
        sid = r["session_id"] if r else None

    calls: list[sqlite3.Row] = []
    if sid:
        cols = "id,session_id,stage,model,input_tokens,output_tokens," \
               "latency_ms,status,error,created_at"
        if have_llm:
            cols += ",started_at,wait_ms,ttfb_ms"
        try:
            calls = c.execute(
                f"SELECT {cols} FROM llm_call WHERE session_id=?"
                " ORDER BY created_at", (sid,)).fetchall()
        except sqlite3.Error:
            calls = []
    return sid, spans, calls, have_span, have_llm


def _cell(r, key):
    """Row/dict 都可能缺列（老库没有新列）——统一返回 None。"""
    try:
        return r[key]
    except (KeyError, IndexError):
        return None


def _span_iv(row) -> tuple[float, float]:
    return float(row["started_at"]), float(row["ended_at"])


def _call_iv(r) -> tuple[float, float] | None:
    """llm_call 的在飞区间。started_at+latency_ms 才有；没有就 None。"""
    s = _cell(r, "started_at")
    if s is None:
        return None
    lat = (_cell(r, "latency_ms") or 0) / 1000.0
    return float(s), float(s) + lat


def section_phases(spans, calls, t0):
    """1) 阶段表：node + subnode 按开始时间，含占比。"""
    print("\n=== 1. 阶段表（绝对起止 + 相对 + 占比）===")
    nodes = [s for s in spans if s["kind"] == "node"]
    subs = [s for s in spans if s["kind"] == "subnode"]
    if not nodes and not subs:
        print(f"（该会话没有 node/subnode span；"
              f"trace_span 总行数 {len(spans)}）")
        return
    total = max(float(s["ended_at"]) for s in nodes or subs) - t0 if (nodes or subs) else 1
    total = max(total, 0.001)
    print(f"{'name':<26} {'start':>9} {'end':>9} {'dur':>8} {'%':>5}  abs")
    for s in sorted(nodes + subs, key=lambda r: (r["started_at"],
                                                r["kind"] != "node")):
        st, en = _span_iv(s)
        mark = "" if s["kind"] == "node" else "  └ "
        print(f"{mark}{s['name']:<24} {_hm(st, t0):>9} {_hm(en, t0):>9}"
              f" {en - st:7.2f}s {100 * (en - st) / total:4.1f}%"
              f"  {_ts(st)}→{_ts(en)}")


def section_concurrency(calls):
    """2) LLM 并发度剖面：5 秒桶里同时在飞的调用数。"""
    print("\n=== 2. LLM 并发度剖面（5s 桶内同时在飞的调用数）===")
    ivs = [iv for r in calls if (iv := _call_iv(r))]
    if not ivs:
        print(NO_LLM)
        return
    lo = min(iv[0] for iv in ivs)
    hi = max(iv[1] for iv in ivs)
    B = 5.0
    buckets: dict[int, int] = {}
    for s, e in ivs:
        for b in range(int((s - lo) // B), int((e - lo) // B) + 1):
            buckets[b] = buckets.get(b, 0) + 1
    busy_b = [v for v in buckets.values()]
    maxc = max(buckets.values()) if buckets else 0
    # 平均并行度 = 总在飞时间 / 有飞行的墙钟
    flight = sum(e - s for s, e in ivs)
    wall = hi - lo or 1
    print(f"在飞区间 {len(ivs)} 个，墙钟 {wall:.1f}s，"
          f"总在飞 {flight:.1f}s → 平均并行度 {flight / wall:.2f}，峰值 {maxc}")
    print(f"{'bucket':>8} {'inflight':>8}")
    for b in sorted(buckets):
        t = lo + b * B
        print(f"{_hm(t, lo):>8} {buckets[b]:>8}  {_ts(t)}")


def section_silence(spans, calls, threshold=5.0):
    """3) 静默分析：没有 LLM 在飞的 ≥5s 区间，谁覆盖了它，剩多少没解释。"""
    print(f"\n=== 3. 静默分析（无 LLM 在飞 ≥{threshold:.0f}s 的区间）===")
    ivs = sorted(iv for r in calls if (iv := _call_iv(r)))
    nodes = sorted((_span_iv(s), s) for s in spans if s["kind"] in
                   ("node", "subnode", "checkpoint", "toolround",
                    "retrieve", "llm_wait"))
    if not ivs:
        print(NO_LLM)
        return

    # 合并有飞行的区间，取空隙
    merged: list[list[float]] = []
    for s, e in ivs:
        if merged and s <= merged[-1][1] + 0.01:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    gaps = []
    prev_end = merged[0][0]
    for s, e in merged:
        if s - prev_end >= threshold:
            gaps.append((prev_end, s))
        prev_end = max(prev_end, e)
    if not gaps:
        print(f"没有 ≥{threshold:.0f}s 的静默区间。")
        return

    for gi, (gs, ge) in enumerate(gaps, 1):
        dur = ge - gs
        print(f"\n[gap {gi}] {_ts(gs)} → {_ts(ge)}  （{dur:.1f}s 无 LLM 在飞）")
        covered = []
        for (s, e), row in nodes:
            # 与 gap 相交的 span（允许 ±0.5s 的边界抖动）
            if e >= gs - 0.5 and s <= ge + 0.5:
                covered.append((s, e, row))
        if not covered:
            print("    覆盖它的 span：无")
        else:
            for s, e, row in covered:
                # 只算落在 gap 里的那段
                ov = min(e, ge) - max(s, gs)
                d = {}
                try:
                    d = json.loads(row["detail_json"] or "{}")
                except Exception:
                    pass
                extra = ""
                if row["kind"] == "checkpoint":
                    extra = (f" bytes={d.get('bytes')} ns={d.get('checkpoint_ns')}"
                             f" thread={d.get('thread_id')}")
                elif row["kind"] == "toolround":
                    extra = (f" task={d.get('task')} r={d.get('round')}"
                             f" llm_ms={d.get('llm_ms')} tool_ms={d.get('tool_ms')}")
                elif row["kind"] == "retrieve":
                    extra = (f" {json.dumps(d, ensure_ascii=False)[:100]}"
                             if d else "")
                print(f"    {row['kind']:<10} {row['name']:<22}"
                      f" {_ts(s)}→{_ts(e)}  overlap={max(ov, 0):.1f}s{extra}")
        # 未解释：把重叠区间合并后与 gap 求差
        intervals = sorted((max(s, gs), min(e, ge)) for s, e, _ in covered
                           if min(e, ge) - max(s, gs) > 0)
        merged_iv: list[list[float]] = []
        for s, e in intervals:
            if merged_iv and s <= merged_iv[-1][1]:
                merged_iv[-1][1] = max(merged_iv[-1][1], e)
            else:
                merged_iv.append([s, e])
        explained = sum(e - s for s, e in merged_iv)
        print(f"    → span 覆盖 {explained:.1f}s，"
              f"未解释 {dur - explained:.1f}s（含 span 间缝隙；"
              f"未解释部分大概率是 LangGraph 调度/序列化/事件循环）")


def section_write_gaps(spans, calls):
    """重点点名：写作子图的两处静默（独立于 LLM 在飞间隙，始终打印）。

    两种口径都对：
      · 子图节点级：audit 子节点开始前，上一个 write.* 子节点结束后隔了多久；
      · LLM 调用级：前一个 write_section/write_revise 调用的 latency 终点 →
        下一个 write_audit 请求的 started_at —— 这才是坑 64 的 45~50s 本体。
    """
    print("\n=== 3b. 写作静默对位（dispatch/revise → audit）===")
    sub = [(_span_iv(s)[0], _span_iv(s)[1], str(s["name"]))
           for s in spans if s["kind"] == "subnode"]
    audits = [(s, e) for s, e, n in sub if n == "write.audit"]
    if audits:
        for i, (as_, ae) in enumerate(audits):
            before = [x for x in sub
                      if x[1] <= as_ + 0.01 and x[2] != "write.audit"]
            prev = max(before, key=lambda x: x[1]) if before else None
            if prev:
                gap = as_ - prev[1]
                flag = "  ⚠️ 静默" if gap >= 1.0 else ""
                print(f"  [子图] audit#{i + 1}：{prev[2]} 结束 {_ts(prev[1])}"
                      f" → audit 开始 {_ts(as_)}，间隔 {gap:.2f}s{flag}")
    else:
        print("  [子图]（没有 write.* 子图 span）")

    # LLM 调用级：write_audit 的 started_at 减去之前最后一个写作调用结束时刻
    acalls = [(_cell(r, "started_at"),
               (_cell(r, "latency_ms") or 0) / 1000.0)
              for r in calls
              if r["stage"] == "write_audit" and _cell(r, "started_at")]
    if not acalls:
        print("  [LLM]（write_audit 没有 started_at——老库口径缺数据）")
        return
    prev_w = [(_cell(r, "started_at"),
               (_cell(r, "latency_ms") or 0) / 1000.0, r["stage"])
              for r in calls
              if r["stage"] and str(r["stage"]).startswith("write")
              and r["stage"] != "write_audit" and _cell(r, "started_at")]
    for i, (as_, _) in enumerate(acalls):
        before = [(s, e, st) for s, e, st in prev_w if s + e <= as_ + 0.01]
        prev = max(before, key=lambda x: x[0] + x[1]) if before else None
        if prev:
            gap = as_ - (prev[0] + prev[1])
            flag = "  ⚠️ 静默" if gap >= 1.0 else ""
            print(f"  [LLM] audit#{i + 1}：{prev[2]} 调用结束 "
                  f"{_ts(prev[0] + prev[1])} → audit 请求 {_ts(as_)}，"
                  f"间隔 {gap:.2f}s{flag}")


def section_llm_stats(calls):
    """4) 各 LLM stage 统计。"""
    print("\n=== 4. LLM stage 统计 ===")
    ok = [r for r in calls if r["status"] == "ok"]
    if not ok:
        print(NO_LLM)
        return
    by_stage: dict[str, list] = {}
    for r in ok:
        by_stage.setdefault(r["stage"], []).append(r)
    print(f"{'stage':<18} {'n':>3} {'lat_med':>8} {'lat_max':>8}"
          f" {'wait_tot':>9} {'ttfb_med':>8} {'in_tok':>8} {'out_tok':>8}"
          f" {'tok/s':>6}")
    for stage, rs in sorted(by_stage.items()):
        lat = [(r["latency_ms"] or 0) / 1000 for r in rs]
        try:
            waits = [r["wait_ms"] or 0 for r in rs]
            ttfbs = [r["ttfb_ms"] for r in rs if r["ttfb_ms"] is not None]
        except (KeyError, IndexError):
            waits, ttfbs = [], []
        it = sum(r["input_tokens"] or 0 for r in rs)
        ot = sum(r["output_tokens"] or 0 for r in rs)
        tps = ot / sum(lat) if sum(lat) else 0
        print(f"{stage:<18} {len(rs):>3} {_med(lat):7.2f}s {max(lat):7.2f}s"
              f" {sum(waits) / 1000:8.2f}s"
              f" {(f'{_med(ttfbs) / 1000:.2f}s' if ttfbs else '   —'):>8}"
              f" {it:>8} {ot:>8} {tps:6.1f}")
    errs = [r for r in calls if r["status"] != "ok"]
    if errs:
        print(f"\n非 ok 行 {len(errs)} 条（retry/error）：")
        for r in errs[:15]:
            print(f"    {r['stage']:<18} {r['status']:<6} {(r['error'] or '')[:90]}")


def section_checkpoints(spans):
    """5) checkpoint 写入汇总。"""
    print("\n=== 5. checkpoint 写入 ===")
    cps = [s for s in spans if s["kind"] == "checkpoint"]
    if not cps:
        print(f"（该会话没有 checkpoint span；总行数 {len(spans)}）")
        return
    # langgraph 子图 checkpoint 的 ns 形如 "write:abc…"（父图节点名:子图 id），
    # 主图的是裸 checkpoint id（无冒号）。靠这一点分主图/子图。
    by_scope: dict[str, list] = {"main": [], "subgraph": []}
    for s in cps:
        try:
            ns = (json.loads(s["detail_json"] or "{}").get("checkpoint_ns")
                  or "")
        except Exception:
            ns = ""
        by_scope["subgraph" if ":" in ns else "main"].append(s)
    print(f"{'scope':<10} {'n':>4} {'total':>8} {'max':>8} {'bytes':>10}")
    for scope, rs in by_scope.items():
        if not rs:
            continue
        durs = [s["ended_at"] - s["started_at"] for s in rs]
        byt = 0
        known = 0
        for s in rs:
            try:
                b = json.loads(s["detail_json"] or "{}").get("bytes")
                if b:
                    byt += b
                    known += 1
            except Exception:
                pass
        print(f"{scope:<10} {len(rs):>4} {sum(durs):7.2f}s {max(durs):7.3f}s"
              f" {byt:>9} ({known} 条有字节数)")
    # 最慢的 5 条单独列
    top = sorted(cps, key=lambda s: s["ended_at"] - s["started_at"],
                 reverse=True)[:5]
    if top:
        print("最慢的 5 次：")
        for s in top:
            d = {}
            try:
                d = json.loads(s["detail_json"] or "{}")
            except Exception:
                pass
            print(f"    {s['name']:<12} {s['ended_at'] - s['started_at']:6.3f}s"
                  f"  bytes={d.get('bytes')} ns={str(d.get('checkpoint_ns'))[:40]}"
                  f" {_ts(s['started_at'])}")


def section_waterfall(spans):
    """6) 关键路径瀑布：主图 + write 子图按时间排。"""
    print("\n=== 6. 关键路径瀑布（主图节点 + write 子图）===")
    rows = [s for s in spans if s["kind"] == "node"
            or (s["kind"] == "subnode" and str(s["name"]).startswith("write."))]
    if not rows:
        print(f"（该会话没有 node/write.* span；总行数 {len(spans)}）")
        return
    t0 = min(s["started_at"] for s in rows)
    print(f"{'rel_start':>9} {'dur':>8}  name")
    for s in sorted(rows, key=lambda r: r["started_at"]):
        st, en = _span_iv(s)
        ind = "" if s["kind"] == "node" else "    "
        print(f"{_hm(st, t0):>9} {en - st:7.2f}s  {ind}{s['name']}")


def main() -> None:
    db = sys.argv[1] if len(sys.argv) > 1 else "data/research.db"
    sid = sys.argv[2] if len(sys.argv) > 2 else None
    c = _conn(db)
    try:
        sid, spans, calls, have_span, have_llm = load(c, sid)
    finally:
        c.close()
    print(f"db={db}  session={sid}")
    print(f"trace_span {'有' if have_span else '无'}；"
          f"llm_call 新列 {'有' if have_llm else '无'}；"
          f"spans={len(spans)} calls={len(calls)}")
    if not spans and not calls:
        print("\n没有可分析的数据。")
        return
    t0 = min([s["started_at"] for s in spans] or
             [iv[0] for r in calls if (iv := _call_iv(r))] or [time.time()])
    section_phases(spans, calls, t0)
    section_concurrency(calls)
    section_silence(spans, calls)
    section_write_gaps(spans, calls)
    section_llm_stats(calls)
    section_checkpoints(spans)
    section_waterfall(spans)


if __name__ == "__main__":
    main()
