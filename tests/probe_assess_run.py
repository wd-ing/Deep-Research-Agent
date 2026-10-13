"""只测 probe → outline → retrieve → extract → assess，不走 write。

为什么要单独拉出来
------------------
write 阶段一轮要 15–20 分钟，而用户明说「测试只打要改的环节」。
这次要验的是三处，都在 write 之前：
  1. 探针降级链（深读页为空时回退用轻扫标题）
  2. 529 这类可重试 5xx 会不会被退避重试
  3. assess 会不会真的点名到具体主体、第 2 轮查询会不会换

所以这里直接驱动主图到收工为止，**不走 write / finalize**。
省掉的不是时间，是随机性——write 那 20 分钟里任何一处抖动都会让对比失效。

**2026-10-08：审批点后置**。第一次 invoke 不再停在 outline 之后，
而是跑到「第一次 assess + 框架按材料校准」之后才 interrupt。
所以本 harness 现在还会打印**框架被怎么校准的**（收窄/并入/删去）。

跑法
----
1. 先跑到人工审批点，打印探针与大纲
2. 批准后循环调用，把每轮 assess 的决策打出来
3. 一旦路由要往前走（去 write）就停，不真的写报告

用法：.venv/bin/python tests/probe_assess_run.py "问题" [db路径]
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

QUESTION = sys.argv[1] if len(sys.argv) > 1 else \
    "固态电池的产业化进展，以及各家技术路线的分歧点"
DB = sys.argv[2] if len(sys.argv) > 2 else "/tmp/ra_probe_assess.db"
os.environ["RA_DB_PATH"] = DB

from langgraph.types import Command  # noqa: E402

from research_agent.db.repo import get_repo  # noqa: E402
from research_agent.graph.runner import get_graph  # noqa: E402
from research_agent.graph.nodes import route_after_assess  # noqa: E402
from research_agent.graph.subgraphs import write as write_subgraph  # noqa: E402

# ---- 把 write 打桩，让它瞬间返回 ----
# 第一版想靠「路由要往前走就 break」来停住 write，但 LangGraph 的 invoke 不会
# 在节点边界交还控制权：路由一旦决定去 write，图就把整条链跑完了。
# 实测那次 write 跑了 10 分钟、491k token，而这次测试根本不关心它。
_write_orig = write_subgraph.run_write


def _stub_write(**kw):
    return {"report_markdown": "（本测试跳过 write）", "events": [
        {"kind": "write", "note": "stubbed out by probe_assess_run.py"}]}


write_subgraph.run_write = _stub_write

T0 = time.time()


def tick(m: str) -> None:
    print(f"[{time.time()-T0:7.1f}s] {m}", flush=True)


def show_probe(out: dict) -> None:
    p = out.get("probe") or {}
    sig = (p.get("signals") or {})
    print(f"\n  探针 quality={p.get('quality')} degraded={p.get('degraded')}")
    print(f"  轮廓来源: {p.get('signals_from')}"
          f"{'  ← 深读没跑起来，用了轻扫标题' if p.get('signals_from')=='titles' else ''}")
    print(f"  subtopics {len(sig.get('subtopics') or [])}、"
          f"entities {len(sig.get('entities') or [])}、"
          f"controversies {len(sig.get('controversies') or [])}")
    print(f"  headline: {str(p.get('headline'))[:140]}")
    if p.get("degraded"):
        print("  ⚠️ 探针降级了——这会让下游大纲变薄")


def show_outline(out: dict) -> None:
    o = out.get("outline") or []
    print(f"\n  大纲 {len(o)} 节")
    for s in o:
        gi = s.get("grounded_in")
        print(f"    [{s.get('id')}] {s.get('title')} "
              f"({'锚定 '+str(len(gi))+' 项' if isinstance(gi,list) and gi else '无锚定'})")
    if len(o) <= 2:
        print("  🚨 大纲退化到 ≤2 节——检索目标会很窄，报告会明显偏薄")


def show_trace(repo, sid: str, seen: set) -> None:
    for e in repo.run_trace(sid):
        key = (e["round"], e["kind"], e["at"])
        if key in seen:
            continue
        seen.add(key)
        if e["kind"] == "assess":
            print(f"\n  ── 第 {e['round']} 轮 assess ──")
            print(f"     段落 {e.get('segments_total')}，本轮新增 {e.get('new_segments')}，"
                  f"连续无新增 {e.get('streak')}")
            print(f"     has_enough={e.get('has_enough')}，缺口 {len(e.get('gaps') or [])} 个")
            thin = [s["id"] for s in (e.get("sections") or []) if not s.get("hits")]
            if thin:
                print(f"     命中 0 的薄弱小节：{thin}")
            for g in (e.get("gaps") or [])[:6]:
                print(f"     缺口 [{g.get('urgency')}] {g.get('section_id')}: "
                      f"{str(g.get('what_missing'))[:78]}")
            qs = e.get("queries") or {}
            if qs.get("zh"):
                print(f"     下一轮查询：{qs['zh'][:3]}")
            if e.get("dropped_repeat"):
                print(f"     🚫 丢弃已搜过的词 {len(e['dropped_repeat'])} 条")
            print(f"     ⏹ 若现在收工：{e.get('stop_would_be') or '（会继续检索）'}")
        elif e["kind"] == "retrieve":
            print(f"\n  ── 第 {e['round']} 轮 retrieve ──")
            print(f"     搜 {(e.get('queries') or {}).get('zh')}")
            print(f"     命中 {e.get('hits')}，接纳 {e.get('accepted')}，"
                  f"累计已抓 {e.get('fetched_total')}")
            if e.get("repeat_queries"):
                print(f"     🚨🚨 与上轮重复 {len(e['repeat_queries'])} 条 —— "
                      f"这正是要修的 bug")


def _show_confidence(state) -> None:
    """审批前 tell 用户这份框架是「材料撑得住」还是「尽力而为」。"""
    o = state.get("outline") or []
    log = state.get("outline_revision_log") or []
    if not log:
        print("     框架未修订（assess 没给 outline_review，或全部判「保持」）")
        return
    e = log[-1]
    print(f"\n  ── 框架按材料校准了一轮（第 {e.get('round')} 轮）──")
    if e.get("narrowed"):
        for n in e["narrowed"]:
            print(f"     收窄 {n['id']} → {n['to']}")
    if e.get("merged"):
        print(f"     并入 {e['merged']}")
    if e.get("removed"):
        print(f"     删去 {e['removed']}")
    if e.get("skipped"):
        print(f"     ⚠️ 本次修订被放弃：{e.get('reason')}")
    print(f"     修订后 {len(o)} 节（原 {len(o) + len(e.get('removed') or [])} 节）")


def main() -> None:
    repo = get_repo()
    sid = repo.create_session(QUESTION[:40], QUESTION)
    print(f"session_id = {sid}\ndb = {DB}\n问题 = {QUESTION}\n", flush=True)

    g = get_graph()
    cfg = {"configurable": {"thread_id": sid}}

    tick("启动：probe → outline → retrieve → extract → assess …")
    out = g.invoke({"session_id": sid, "question": QUESTION, "title": QUESTION[:40],
                    "elapsed_s": 0.0, "cost_tokens": 0}, config=cfg)
    # 2026-10-08 起审批点后置：不再停在 outline 之后，
    # 而是**第一次 assess + 框架校准之后**才 interrupt。
    # 所以第一次 invoke 会跑完若干轮检索才停下来。
    tick("已停在审批点（材料已就位、框架已按材料校准）")
    show_probe(out)
    show_outline(out)
    _show_confidence(out)

    seen: set = set()
    tick("批准，继续 assess → （够料就收工 / 没够就再搜）")
    state = out
    rounds = 0
    while True:
        state = g.invoke(
            Command(resume={"approved": True}),
            config=cfg)
        show_trace(repo, sid, seen)
        rounds += 1
        # 已审批：只剩「继续搜」和「收工」，框架不会再被改
        nxt = route_after_assess(state)
        tick(f"路由决定：{nxt}")
        if nxt != "retrieve":
            aev = [e for e in (state.get("events") or [])
                   if e.get("kind") == "assess"]
            why = aev[-1].get("stop_would_be") if aev else "（无 assess 事件）"
            print(f"\n  ⏹ 收工。停止原因：{why}")
            print(f"     write 已被打桩，本测试不关心报告质量。")
            break
        if rounds >= 6:
            print("  ⏹ 轮数超上限，主动停")
            break

    print("\n──── 落库核对 ────")
    n_src = repo.q1("SELECT COUNT(*) c FROM source WHERE session_id=?", (sid,))["c"]
    n_ch = repo.q1("SELECT COUNT(*) c FROM chunk WHERE session_id=?", (sid,))["c"]
    n_on = repo.q1("SELECT COUNT(*) c FROM chunk WHERE session_id=? AND on_topic=1", (sid,))["c"]
    n_ns = repo.q1("SELECT COUNT(*) c FROM chunk WHERE session_id=? "
                   "AND (summary IS NULL OR summary='')", (sid,))["c"]
    print(f"  来源 {n_src}、段落 {n_ch}（过闸门 {n_on}，无概括 {n_ns}）")
    print(f"  检索轮数 {state.get('round')}，URL 计数 {state.get('url_count')}"
          f"（上限 {__import__('research_agent.graph.nodes', fromlist=['HARD']).HARD['max_urls']}）")
    print(f"  连续无新增 {state.get('consecutive_no_new')}，"
          f"stop_reason={state.get('stop_reason') or '（未判定够）'}")
    print(f"  耗时 {time.time()-T0:.0f} 秒，总 token {repo.session_cost(sid)}")

    print("\n──── 事件 ----")
    for e in (state.get("events") or []):
        print("  ·", json.dumps(e, ensure_ascii=False)[:230])
    print(f"\n  全量 trace 可查：SELECT * FROM run_trace WHERE session_id='{sid}'")


if __name__ == "__main__":
    main()