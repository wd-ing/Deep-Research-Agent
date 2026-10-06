"""端到端跑一次完整研究：probe → outline → [人工审批] → retrieve → extract
→ assess → apply_revision → write → finalize。

不走 HTTP，直接驱动主图，方便看每个节点的耗时与事件。

用法：
    python tests/e2e_run.py "你的问题" [db路径]
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# .env 里的 key 要在导入任何模块前读进来
for _line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        k, v = _line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

DB = sys.argv[2] if len(sys.argv) > 2 else "/tmp/ra_e2e_run.db"
os.environ["RA_DB_PATH"] = DB

QUESTION = sys.argv[1] if len(sys.argv) > 1 else "日本脑炎病毒疫苗（JEV）近年的研发进展与难点"

from langgraph.types import Command  # noqa: E402

from research_agent.db.repo import get_repo  # noqa: E402
from research_agent.graph.runner import get_graph  # noqa: E402

T0 = time.time()


def tick(msg: str) -> None:
    print(f"[{time.time() - T0:7.1f}s] {msg}", flush=True)


def dump_events(out: dict, tag: str) -> None:
    for ev in out.get("events") or []:
        s = json.dumps(ev, ensure_ascii=False)
        print(f"    · {s[:300]}", flush=True)


def main() -> None:
    repo = get_repo()
    sid = repo.create_session(QUESTION[:40], QUESTION)
    print(f"session_id = {sid}\ndb = {DB}\n问题 = {QUESTION}\n", flush=True)

    g = get_graph()
    cfg = {"configurable": {"thread_id": sid}}

    # ---- 第一段：跑到人工审批 ----
    tick("启动：probe …")
    out = g.invoke(
        {"session_id": sid, "question": QUESTION, "title": QUESTION[:40],
         "elapsed_s": 0.0, "cost_tokens": 0},
        config=cfg,
    )
    tick("已停在审批点")

    probe = out.get("probe") or {}
    print(f"\n  探针质量: {probe.get('quality')}  降级: {probe.get('degraded')}")
    print(f"  探针结论: {probe.get('headline')}")
    print(f"  歧义: {probe.get('ambiguity')}")
    outline = out.get("outline") or []
    print(f"\n  大纲 {len(outline)} 节:")
    for s in outline:
        gi = s.get("grounded_in")
        gi = f" | 锚定 {len(gi)} 项" if isinstance(gi, list) else ""
        print(f"    - [{s.get('id')}] {s.get('title')} ({s.get('kind')}){gi}")
        print(f"      Q: {s.get('question')}")
    q = out.get("plan_queries") or {}
    print(f"\n  中文查询 {len(q.get('zh') or [])} 条: {q.get('zh')}")
    print(f"  英文查询 {len(q.get('en') or [])} 条: {q.get('en')}")
    print(f"  召回旧段落: {len(out.get('recalled_chunks') or [])}")

    # 记录代价基线
    before = repo.session_cost(sid)
    print(f"\n  审批前累计 token: {before}")

    # ---- 第二段：批准，继续跑到结束 ----
    recall = out.get("recalled_chunks") or []
    tick("批准大纲，继续跑 retrieve → extract → …")
    out2 = g.invoke(
        Command(resume={"approved": True,
                        "approved_recalled_ids": [c["id"] for c in recall]}),
        config=cfg,
    )
    tick("全部节点跑完")

    print("\n---- 事件 ----")
    dump_events(out, "pre")
    dump_events(out2, "post")

    # ---- 结果核对 ----
    print("\n---- 落库核对 ----")
    n_src = repo.q1("SELECT COUNT(*) c FROM source WHERE session_id=?", (sid,))["c"]
    n_on = repo.q1("SELECT COUNT(*) c FROM source WHERE session_id=? AND on_topic=1",
                   (sid,))["c"]
    n_off = repo.q1("SELECT COUNT(*) c FROM source WHERE session_id=? AND on_topic=0",
                    (sid,))["c"]
    n_ch = repo.q1("SELECT COUNT(*) c FROM chunk WHERE session_id=?", (sid,))["c"]
    n_pend = repo.q1("SELECT COUNT(*) c FROM chunk WHERE session_id=? AND status='pending'",
                     (sid,))["c"]
    # R4 时代这里数的是「断言」（all_evidence 里 kind=='claims'）。
    # R5 起证据单位改成段落，落库是 chunk 表、闸门结果在 chunk.on_topic，
    # 断言那套已经不存在了——再数就是永远 0，看着像坏了。
    n_seg = repo.q1("SELECT COUNT(*) c FROM chunk WHERE session_id=? "
                    "AND summary IS NOT NULL AND summary!=''", (sid,))["c"]
    n_on_seg = repo.q1("SELECT COUNT(*) c FROM chunk WHERE session_id=? AND on_topic=1",
                       (sid,))["c"]
    n_no_sum = repo.q1("SELECT COUNT(*) c FROM chunk WHERE session_id=? "
                       "AND (summary IS NULL OR summary='')", (sid,))["c"]
    print(f"  来源 {n_src}（相关 {n_on} / 无关 {n_off}）")
    print(f"  分块 {n_ch}（仍 pending {n_pend}）")
    print(f"  段落 {n_seg}（通过闸门 {n_on_seg}，无概括 {n_no_sum}）")
    print(f"  会话状态: {repo.q1('SELECT status FROM session WHERE id=?', (sid,))['status']}")
    print(f"  总 token: {repo.session_cost(sid)}")

    md = out2.get("report_markdown") or ""
    print(f"\n---- 报告 {len(md)} 字 ----\n")
    print(md)

    with open(f"/tmp/e2e_report_{sid[:8]}.md", "w", encoding="utf-8") as f:
        f.write(md)
    print(f"\n（报告已存 /tmp/e2e_report_{sid[:8]}.md）")


if __name__ == "__main__":
    main()
