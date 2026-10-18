"""把 B1 细粒度事件（fire_progress 六类）按**真实数据**合成进回放 fixture。

## 为什么需要它

v13 真跑发生在 `fire_progress` 通道（B1）落地**之前**——
那时环节内根本不推事件，落库的 sse_event 里自然没有
search_query / extract_source / summarize_batch / gate_batch / write_task。
fixture 是 v13 重录的，于是回放里这些细粒度行一条都没有，
前端 B1 的渲染分支在回放里验证不到。

本工具从真实数据库合成这批事件行：
- `search_query`：查询串和条数来自 `run_trace`（每轮 zh/en 各 5 条）；
  per-query hits 合成（原始命中在判重**之前**，总和 > 轮次命中是正常的）
- `extract_source`：标题 + 性质来自 `source` 表（按 fetched_at 分轮）
- `summarize_batch`：批次数取 fixture `segment_summary.batches` 真实值，
  各批大小按总段数摊
- `gate_batch`：LLM 判定数 = `by_method.llm`，批大小 12（GATE_LLM_BATCH），
  off_topic 按真值摊到各批
- `write_task`：任务 id/covers 来自 `write_trace`，
  chars 用报告各 `## ` 节的真实长度（含返修后值，与草稿略有出入）

## 插入位置（尊重真实先后）

```
node retrieve → search_query×N → progress retrieve
node extract  → extract_source×N → summarize_batch×B → segment_summary → progress extract
node gate     → gate_batch×B → progress gate
write         → write_task×8 → write_assemble → write_audit → write_task×3(revise) → write_revise
```

塞不进原时间窗时，锚点及之后的事件整体后移（保持原间隔）——
fixture 时长会变长一点，但次序是对的。

用法：
    python tests/enrich_fixture.py <db> <sid>
        [--fixtures tests/fixtures] [--out tests/fixtures]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path


def _progress(node: str, inner: dict) -> dict:
    return {"kind": "progress", "node": node, "event": inner}


def _inner(item: dict) -> dict:
    ev = item["event"].get("event")
    return ev if isinstance(ev, dict) else {}


def _spread(a: float, b: float, n: int) -> list[float]:
    """n 个时间点均匀落在 [a,b]。"""
    if n <= 1:
        return [b]
    step = (b - a) / n
    return [round(a + i * step, 2) for i in range(n)]


def _dist(total: int, sizes: list[int]) -> list[int]:
    """把 total 尽量均匀地摊进各桶（off_topic 摊到各批用）。"""
    out = []
    for s in sizes:
        take = min(s, total)
        out.append(take)
        total -= take
    return out


def load_db(db_path: str, sid: str) -> dict:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    # 每轮的查询串
    retrieves = {}
    for r in db.execute(
        "SELECT round, detail_json FROM run_trace "
        "WHERE session_id=? AND kind='retrieve' ORDER BY round", (sid,)):
        d = json.loads(r["detail_json"])
        q = d.get("queries") or {}
        retrieves[r["round"]] = {
            "zh": list(q.get("zh") or []), "en": list(q.get("en") or [])}

    # 每轮的来源（fetched_at 分钟分组即轮次——检索才抓）
    sources = [dict(r) for r in db.execute(
        "SELECT id, title, source_nature, fetched_at FROM source "
        "WHERE session_id=? ORDER BY fetched_at, rowid", (sid,))]
    by_minute: dict[str, list[dict]] = {}
    for s in sources:
        by_minute.setdefault((s["fetched_at"] or "")[:16], []).append(s)
    rounds_sources = [by_minute[k] for k in sorted(by_minute)]

    # 写作任务 + 报告各节真实长度（→ write_task.chars）
    wt = db.execute(
        "SELECT tasks_json FROM write_trace WHERE session_id=?", (sid,)).fetchone()
    tasks = json.loads(wt["tasks_json"]) if wt else []
    md = (db.execute(
        "SELECT markdown FROM report WHERE session_id=?", (sid,)).fetchone() or {})
    md = md["markdown"] if md else ""
    # `## ` 节标题切块。**只留真节**：报告开头的 `# 标题+引言` 块不带 `## `，
    # 算进来整个下标就错一位（t1 会拿到 172 字的报告头）。
    parts = [p for p in re.split(r"(?m)^(?=## )", md)
             if p.lstrip().startswith("## ")]
    sec_chars = [len(p) for p in parts]

    return {"retrieves": retrieves, "rounds_sources": rounds_sources,
            "tasks": tasks, "sec_chars": sec_chars}


# ---------------------------------------------------------------- 合成

def synth_search_queries(queries: dict, node="retrieve") -> list[dict]:
    """每轮 zh+en 查询各一条。hits 合成：搜索在判重前按 query 返回，
    单条命中 2~8 都正常，总和大于轮次 hits 不是 bug。"""
    seq = [("zh-CN", q) for q in queries["zh"]] + \
          [("en", q) for q in queries["en"]]
    of = len(seq)
    return [_progress(node, {
        "kind": "search_query", "i": i + 1, "of": of, "lang": lang,
        "query": q, "hits": 2 + ((i * 5 + 3) % 7)})
        for i, (lang, q) in enumerate(seq)]


def synth_extract_sources(sources: list[dict]) -> list[dict]:
    return [_progress("extract", {
        "kind": "extract_source", "source_id": s["id"],
        "title": s.get("title") or "",
        "nature": s.get("source_nature") or "unknown"})
        for s in sources]


def synth_summarize(segments: int, batches: int) -> list[dict]:
    """done=of（v13 这批零失败）；各批大小按总段数摊，最后一批吃余量。"""
    if batches <= 0 or segments <= 0:
        return []
    base, rem = divmod(segments, batches)
    out = []
    for b in range(batches):
        of = base + (1 if b < rem else 0)
        out.append(_progress("extract", {
            "kind": "summarize_batch", "batch": b + 1,
            "batches": batches, "done": of, "of": of}))
    return out


def synth_gate(llm_judged: int, off_topic: int) -> list[dict]:
    """批大小 12（GATE_LLM_BATCH）；off_topic 摊到各批——
    规则放行段不进 LLM 判定，所以 llm 数才是批的规模依据。"""
    sizes = [12] * (llm_judged // 12)
    if llm_judged % 12:
        sizes.append(llm_judged % 12)
    offs = _dist(off_topic, sizes)
    return [_progress("gate", {
        "kind": "gate_batch", "batch": i + 1, "batches": len(sizes),
        "judged": s, "on_topic": s - off, "off_topic": off, "missing": 0})
        for i, (s, off) in enumerate(zip(sizes, offs))]


def synth_write_tasks(tasks: list[dict], sec_chars: list[int],
                      revise: list[str] | None = None) -> list[dict]:
    """covers secN → 报告第 N 个 `## ` 块的长度当 chars。
    revise=True 的是返修任务（write_revise 之前那批）。"""
    want = revise if revise is not None else [t["id"] for t in tasks]
    out = []
    for tid in want:
        t = next((x for x in tasks if x.get("id") == tid), None)
        if not t:
            continue
        covers = list(t.get("covers") or [])
        m = re.match(r"sec(\d+)$", covers[0] if covers else "")
        chars = sec_chars[int(m.group(1)) - 1] \
            if m and int(m.group(1)) <= len(sec_chars) else 0
        ev = {"kind": "write_task", "task": tid, "covers": covers,
              "ok": True, "chars": chars, "reason": ""}
        if revise is not None:
            ev["revise"] = True
        out.append(_progress("write", ev))
    return out


# ---------------------------------------------------------------- 注入

def inject(events: list[dict], before_idx: int, items: list[dict],
             gap: float = 0.09) -> None:
    """把 items 插到 events[before_idx] 之前；
    时间从上一个事件的 at+gap 起排，塞不下就把锚点后移。"""
    if not items or before_idx <= 0:
        return
    prev_at = events[before_idx - 1]["at"]
    anchor_at = events[before_idx]["at"]
    start = prev_at + gap
    end = start + gap * (len(items) - 1)
    shift = max(0.0, end + gap - anchor_at)
    for j, it in enumerate(items):
        events.insert(before_idx + j, {"at": round(start + j * gap, 2),
                                       "event": it})
    if shift:
        for e in events[before_idx + len(items):]:
            e["at"] = round(e["at"] + shift, 2)


def find_idx(events: list[dict], kind: str, *, after: int = 0,
             pred=None) -> int:
    """找第 N 个 inner kind 的 progress 事件下标。"""
    for i in range(after, len(events)):
        inner = _inner(events[i])
        if inner.get("kind") == kind and (pred is None or pred(inner)):
            return i
    return -1


def enrich_file(path: Path, data: dict, which: str) -> int:
    events = json.loads(path.read_text(encoding="utf-8"))["events"]

    def inject_by_kind(kind: str, items: list[dict], gap=0.09, **kw) -> int:
        i = find_idx(events, kind, **kw)
        if i > 0:
            inject(events, i, items, gap=gap)
        return i

    added = 0

    # —— 每轮 retrieve：search_query ——
    for rnd, q in data["retrieves"].items():
        items = synth_search_queries(q)
        i = inject_by_kind("retrieve", items, gap=0.14,
                           pred=lambda x, r=rnd: x.get("round") == r)
        if i > 0:
            added += len(items)

    # —— 每轮 extract：extract_source + summarize_batch ——
    # 每个 segment_summary 的轮次由它**前面最近的 retrieve 事件**决定——
    # 它在文件里是第几个不作数（after 文件里第 2 轮的段摘要排第一）。
    def round_before(idx: int) -> int | None:
        for j in range(idx - 1, -1, -1):
            inner = _inner(events[j])
            if inner.get("kind") == "retrieve":
                return inner.get("round")
        return None

    for i, e in enumerate(list(events)):
        if _inner(e).get("kind") != "segment_summary":
            continue
        rnd = round_before(i) or 1
        sources = data["rounds_sources"][rnd - 1] \
            if rnd - 1 < len(data["rounds_sources"]) else []
        segs = int(_inner(e).get("segments") or 0)
        batches = int(_inner(e).get("batches") or 0) \
            or max(1, (segs + 11) // 12)
        items = synth_extract_sources(sources) + \
            synth_summarize(segs, batches)
        inject(events, events.index(e), items, gap=0.08)
        added += len(items)

    # —— 每轮 gate：gate_batch ——
    for e in list(events):
        inner = _inner(e)
        if inner.get("kind") != "gate":
            continue
        by = (inner.get("gate") or {}).get("by_method") or {}
        llm = int(by.get("llm") or 0)
        off = int((inner.get("gate") or {}).get("off_topic") or 0)
        items = synth_gate(llm, off)
        inject(events, events.index(e), items, gap=0.15)
        added += len(items)

    # —— write：dispatch 后 8 个任务 + revise 前 3 个返修 ——
    if which == "after":
        i = find_idx(events, "write_assemble")
        if i > 0:
            items = synth_write_tasks(data["tasks"], data["sec_chars"])
            inject(events, i, items, gap=0.85)
            added += len(items)
        # revise 任务在 write_revise 汇总之前
        wi = find_idx(events, "write_revise")
        if wi > 0:
            rtasks = _inner(events[wi]).get("tasks") or []
            items = synth_write_tasks(data["tasks"], data["sec_chars"],
                                      revise=list(rtasks))
            inject(events, wi, items, gap=0.3)
            added += len(items)

    path.write_text(json.dumps(
        {"events": events}, ensure_ascii=False, indent=1), encoding="utf-8")
    return added


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("sid")
    ap.add_argument("--fixtures", default="tests/fixtures")
    ap.add_argument("--out", default=None,
                    help="默认原地改；指定目录则输出到新位置")
    args = ap.parse_args()
    data = load_db(args.db, args.sid)
    out_dir = Path(args.out) if args.out else Path(args.fixtures)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, which in [("run_to_approval.json", "to"),
                        ("run_after_approval.json", "after")]:
        src = Path(args.fixtures) / name
        if not src.exists():
            print(f"跳过 {name}：不存在")
            continue
        if args.out:
            dst = out_dir / name
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            dst = src
        n = enrich_file(dst, data, which)
        evs = json.loads(dst.read_text(encoding="utf-8"))["events"]
        print(f"{name}: +{n} 条细粒度事件 → 共 {len(evs)} 条，"
              f"时间轴 {evs[-1]['at']}s")


if __name__ == "__main__":
    main()
