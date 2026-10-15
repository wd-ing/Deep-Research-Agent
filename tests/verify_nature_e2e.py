"""端到端验一次：抽取阶段的来源性质判定，在**真实管线**里不再整批丢。

**为什么需要它**
--------------
`tests/probe_nature_batch.py` 是函数级证据（完美桩）。它能证明
`_run_nature_batch` 自己的下标体系没分叉，但证不了**真实管线**里
「41 个来源 → 6 批 → 全部判出」这条路真的通，也证不了
`unjudged_sources` 这个计数真的从 extract 一路走到事件里。

**这次跑的是什么**
----------------
真节点（`nodes.node_probe` / `node_outline` / `node_retrieve` /
`node_extract` / `node_assess`）、真检索、真 LLM、真落库。
只是**不往下走写作**——写作占 v5 输入 token 的 69%（write_section 428k），
而这次要验的东西全在写作之前。

审批点本来就在第一次 assess **之后**（`runner.py:63`），所以跑到 assess
天然就还没进审批、更没进写作。不需要人工介入。

**规模**：把 `HARD` 临时调小（`max_rounds=1`、`max_urls=20`），
目标是拿到 **10~20 个来源 = 2~3 批** —— 正好是原来会整批丢的那种规模。
跑完不需要还原，脚本只在内存里改这份 dict。

**验什么**（全绿才算过）
  ① 来源数 > 8（否则压根没触发多批次，验了等于没验）
  ② `source_nature` 为空的来源数 = 0
  ③ extract 事件里 `unjudged_sources` = 0
  ④ 批次数 > 1（确认第 2 批真的跑过）

跑：
    .venv/bin/python tests/verify_nature_e2e.py "你的问题"
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

DB = "/tmp/ra_verify_nature.db"
os.environ["RA_DB_PATH"] = DB
QUESTION = (sys.argv[1] if len(sys.argv) > 1
            else "钠离子电池的商业化时间表，以及主要厂商的量产进度")

from langgraph.graph import END, START, StateGraph   # noqa: E402

from research_agent.db.repo import get_repo           # noqa: E402
from research_agent.graph import nodes                # noqa: E402
from research_agent.graph.state import ResearchState  # noqa: E402

# 只在内存里改这份 dict —— 生产代码不动，也不需要跑完再还原
nodes.HARD["max_rounds"] = 1
nodes.HARD["max_urls"] = 20

T0 = time.time()


def tick(msg: str) -> None:
    print(f"[{time.time() - T0:6.1f}s] {msg}", flush=True)


def build_partial_graph():
    """跑到 assess 为止。跑的都是 `nodes` 里的**生产节点**，
    只是不往下接写作——写作占 v5 输入 token 的 69%，而验的东西都在它前面。"""
    g = StateGraph(ResearchState)
    g.add_node("probe", nodes.node_probe)
    g.add_node("outline", nodes.node_outline)
    g.add_node("retrieve", nodes.node_retrieve)
    g.add_node("extract", nodes.node_extract)
    g.add_node("assess", nodes.node_assess)
    g.add_edge(START, "probe")
    g.add_edge("probe", "outline")
    g.add_edge("outline", "retrieve")
    g.add_edge("retrieve", "extract")
    g.add_edge("extract", "assess")
    g.add_edge("assess", END)
    return g.compile()


def main() -> None:
    repo = get_repo()
    sid = repo.create_session(QUESTION[:40], QUESTION)
    print(f"session_id = {sid}\ndb = {DB}\n问题 = {QUESTION}")
    print(f"HARD: max_rounds={nodes.HARD['max_rounds']} "
          f"max_urls={nodes.HARD['max_urls']}\n", flush=True)

    tick("启动（probe → outline → retrieve → extract → assess）")
    out = build_partial_graph().invoke(
        {"session_id": sid, "question": QUESTION, "title": QUESTION[:40],
         "elapsed_s": 0.0, "cost_tokens": 0},
    )
    tick("跑完")

    events = out.get("events") or []
    ex_events = [e for e in events if e.get("kind") == "extract"]

    print("\n---- 判定结果 ----")
    for r in repo.q(
            "SELECT COALESCE(source_nature,'<<NULL>>') v, COUNT(*) n FROM source "
            "WHERE session_id=? GROUP BY 1 ORDER BY n DESC", (sid,)):
        print(f"  {r['v']:<12} {r['n']}")

    print("\n---- extract 事件 ----")
    for e in ex_events:
        print("  " + json.dumps({k: v for k, v in e.items()
                                 if k != "gate"}, ensure_ascii=False)[:300])

    ok = verify(repo, sid, ex_events)

    # 把真实事件留给工作流视图探针：这是「真实 SSE 里长什么样」的唯一一份样本
    dump = ROOT / "tests" / "fixtures" / "verify_nature_e2e.json"
    dump.write_text(json.dumps(
        [{"at": round(time.time() - T0, 1), "event": e} for e in events],
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n真实事件已写到 {dump.relative_to(ROOT)}（{len(events)} 条）")

    cost = repo.session_cost(sid)
    print(f"本次用量：{cost}")
    print("\n结论：" + ("全部通过 ✅" if ok else "有项没过 ❌"))
    sys.exit(0 if ok else 1)


def verify(repo, sid: str, ex_events: list | None = None) -> bool:
    """对着库里的真实数据跑一遍验收。抽成函数是为了能 `--recheck` 复用，
    不必为了复查一次检查定义就重跑一遍研究（那要 58k token）。"""
    n_src = repo.q1("SELECT COUNT(*) c FROM source WHERE session_id=?", (sid,))["c"]
    # ⚠️ **NULL 和 'unknown' 必须是两回事**——
    # NULL/'' = 代码把它丢了（真 bug）；'unknown' = 模型看了材料判不出来（合法结论）。
    # 第一次写这个脚本时我把两者混在一起当失败，于是把「模型判不出来」误报成
    # 「代码又丢了」，差点去改一个没坏的东西。
    n_lost = repo.q1(
        "SELECT COUNT(*) c FROM source WHERE session_id=? "
        "AND COALESCE(source_nature,'')=''", (sid,))["c"]
    n_unknown = repo.q1(
        "SELECT COUNT(*) c FROM source WHERE session_id=? "
        "AND source_nature='unknown'", (sid,))["c"]
    judged = n_src - n_lost
    # 批次数从 llm_call 数，不看 extract_batch 事件——那些事件被
    # `extract_all` 里的 `out.update(sum_out)` 覆盖掉了，根本到不了事件列表里。
    n_extract_calls = repo.q1(
        "SELECT COUNT(*) c FROM llm_call WHERE session_id=? AND stage='extract'",
        (sid,))["c"]
    if ex_events is None:
        # ⚠️ 事件**不落库**（SSE progress 只在连接期间存在，见 HANDOVER 坑 25），
        # 所以 --recheck 验不了这条。**不能拿 `all([])` 冒充通过**——
        # 空集合恒为真，那是一条什么都没查的检查披着 ✅ 外衣。
        # 真要复验，跑一次完整流程（--recheck 之外的路径）。
        print("  ⏭️ ④ extract 事件 unjudged_sources —— 跳过：事件不落库，"
              "空集合恒为真，不能算通过（需完整跑一次）")
        checks = [
            ("① 来源数 > 8（否则没触发多批次，验了等于没验）",
             n_src > 8, f"来源 {n_src} 个"),
            ("② extract 判定调用 > 1（确认第 2 批真的跑过）",
             n_extract_calls > 1, f"{n_extract_calls} 次"),
            ("③ 被代码丢掉的来源（source_nature 为空）= 0",
             n_lost == 0, f"{n_lost} 个"),
            ("⑤ 每个来源都有明确结论（判出 + 判不出来 == 来源数）",
             judged == n_src, f"有结论 {judged} / 来源 {n_src}"
                              f"（其中模型判不出来 {n_unknown} 个，不算失败）"),
        ]
    else:
        unjudged_ev = [e.get("unjudged_sources", 0) for e in ex_events]
        print("\n---- 验收 ----")
        checks = [
            ("① 来源数 > 8（否则没触发多批次，验了等于没验）",
             n_src > 8, f"来源 {n_src} 个"),
            ("② extract 判定调用 > 1（确认第 2 批真的跑过）",
             n_extract_calls > 1, f"{n_extract_calls} 次"),
            ("③ 被代码丢掉的来源（source_nature 为空）= 0",
             n_lost == 0, f"{n_lost} 个"),
            ("④ extract 事件 unjudged_sources = 0",
             all(u == 0 for u in unjudged_ev), f"{unjudged_ev}"),
            ("⑤ 每个来源都有明确结论（判出 + 判不出来 == 来源数）",
             judged == n_src, f"有结论 {judged} / 来源 {n_src}"
                              f"（其中模型判不出来 {n_unknown} 个，不算失败）"),
        ]

    print()
    ok = True
    for name, passed, detail in checks:
        print(f"  {'✅' if passed else '❌'} {name}  —— {detail}")
        ok = ok and passed
    if n_unknown:
        print(f"\n  注：{n_unknown} 个来源是模型**判不出来**（值 'unknown'，带判定理由），"
              f"不是被代码丢掉。这两者以前在界面上长得一模一样。")
    return ok


def main() -> None:
    repo = get_repo()

    # --recheck：对已经跑完的会话重算一遍验收，不必为复查检查定义重跑研究
    if "--recheck" in sys.argv:
        sid = sys.argv[sys.argv.index("--recheck") + 1]
        print(f"复查会话 {sid}（库 {DB}）")
        ok = verify(repo, sid)
        sys.exit(0 if ok else 1)

    sid = repo.create_session(QUESTION[:40], QUESTION)
    print(f"session_id = {sid}\ndb = {DB}\n问题 = {QUESTION}")
    print(f"HARD: max_rounds={nodes.HARD['max_rounds']} "
          f"max_urls={nodes.HARD['max_urls']}\n", flush=True)


if __name__ == "__main__":
    main()
