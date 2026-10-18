"""只重跑写作环节：从 checkpoints.db 里 next==("write",) 的那个
checkpoint 继续走，write → finalize → summary，前面跑过的节点不重复。

v13 全量跑里 write_plan_spec 吐了坏 JSON（char 6962），整条写作线
退化。修复（b99ddeb 有界重试）落地后，用这个脚本在同一份会话数据上
只重跑写作，避免再花一次检索+抽取的钱。

用法：
    python tests/rerun_write.py <session_id> [db路径]

⚠️ checkpoint 在 data/checkpoints.db（固定路径，不随 RA_DB_PATH 变），
   会话的业务数据（source/chunk/on_topic/report）在 RA_DB_PATH 指的库里。
   两个库都得是对的。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        k, v = _line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

SID = sys.argv[1]
DB = sys.argv[2] if len(sys.argv) > 2 else "/tmp/ra_v13_gate.db"
os.environ["RA_DB_PATH"] = DB

from research_agent.db.repo import get_repo  # noqa: E402
from research_agent.graph.runner import get_graph  # noqa: E402

T0 = time.time()


def tick(msg: str) -> None:
    print(f"[{time.time() - T0:7.1f}s] {msg}", flush=True)


def main() -> None:
    repo = get_repo()
    row = repo.q1("SELECT id, question, status FROM session WHERE id=?", (SID,))
    if not row:
        sys.exit(f"session {SID} 不在 {DB}")
    print(f"session = {SID}  db = {DB}  status = {row['status']}", flush=True)

    g = get_graph()
    cfg = {"configurable": {"thread_id": SID}}

    # get_state_history 从新到旧，找最后一个 next 含 "write" 的快照
    # ——也就是「马上要跑 write」的那个位置。
    target = None
    for snap in g.get_state_history(cfg):
        nxt = tuple(snap.next or ())
        if "write" in nxt:
            target = snap
            break
        # 更早的快照不用看了：历史是逆序的，越翻越旧
    if not target:
        sys.exit("没找到 next==('write',) 的 checkpoint——这个会话的写作"
                 "可能没走到入口，或 thread 不是它")

    vals = target.values
    tick(f"从 checkpoint 继续：next={target.next}  "
         f"elapsed_s={vals.get('elapsed_s', 0):.0f}  "
         f"outline={len(vals.get('outline') or [])} 节")
    # 带着 checkpoint_id invoke(None) = 从那个分叉点重跑后续节点
    cfg2 = {"configurable": {
        "thread_id": SID,
        "checkpoint_id": target.config["configurable"]["checkpoint_id"],
    }}
    out = g.invoke(None, config=cfg2)
    tick("write → finalize → summary 重跑完")

    for ev in out.get("events") or []:
        print(f"    · {json.dumps(ev, ensure_ascii=False)[:300]}", flush=True)

    md = out.get("report_markdown") or ""
    n_calls = repo.q1(
        "SELECT COUNT(*) c FROM llm_call WHERE session_id=? AND stage LIKE 'write%'",
        (SID,))["c"]
    print(f"\n报告 {len(md)} 字 | 本会话 write* 环节累计调用 {n_calls} 次"
          f" | 总 token {repo.session_cost(SID)}")
    path = f"/tmp/e2e_report_{SID[:8]}_rewrite.md"
    Path(path).write_text(md, encoding="utf-8")
    print(f"（报告已存 {path}）", flush=True)


if __name__ == "__main__":
    main()
