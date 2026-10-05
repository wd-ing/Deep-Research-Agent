"""从上次崩掉的节点续跑，不重跑已完成的检索与抽取。

用法：python tests/e2e_resume.py <session_id> [db路径]
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
os.environ["RA_DB_PATH"] = sys.argv[2] if len(sys.argv) > 2 else "/tmp/ra_e2e_run.db"

from research_agent.db.repo import get_repo  # noqa: E402
from research_agent.graph.runner import get_graph  # noqa: E402

T0 = time.time()


def tick(m: str) -> None:
    print(f"[{time.time() - T0:7.1f}s] {m}", flush=True)


def main() -> None:
    repo = get_repo()
    s = repo.q1("SELECT * FROM session WHERE id=?", (SID,))
    if not s:
        sys.exit(f"会话 {SID} 不存在")
    print(f"续跑 {SID} | {s['question']}\n", flush=True)

    g = get_graph()
    tick("从 checkpoint 恢复（None = 重跑上一个未完成的节点）")
    out = g.invoke(None, config={"configurable": {"thread_id": SID}})
    tick("跑完")

    print("\n---- 事件 ----")
    for ev in out.get("events") or []:
        print(f"    · {json.dumps(ev, ensure_ascii=False)[:300]}")

    n_src = repo.q1("SELECT COUNT(*) c FROM source WHERE session_id=?", (SID,))["c"]
    n_on = repo.q1("SELECT COUNT(*) c FROM source WHERE session_id=? AND on_topic=1",
                   (SID,))["c"]
    n_ch = repo.q1("SELECT COUNT(*) c FROM chunk WHERE session_id=?", (SID,))["c"]
    n_pend = repo.q1("SELECT COUNT(*) c FROM chunk WHERE session_id=? AND status='pending'",
                     (SID,))["c"]
    n_cl = sum(len(e.get("claims") or [])
               for e in (out.get("all_evidence") or []) if e.get("kind") == "claims")
    n_cl_on = sum(len(e.get("claims") or [])
                  for e in (out.get("all_evidence") or [])
                  if e.get("kind") == "claims" and e.get("on_topic", True))
    print("\n---- 落库 ----")
    print(f"  来源 {n_src}（相关 {n_on}）")
    print(f"  分块 {n_ch}（pending {n_pend}）")
    print(f"  断言 {n_cl}，其中进入报告池 {n_cl_on}")
    print(f"  会话状态: {repo.q1('SELECT status FROM session WHERE id=?', (SID,))['status']}")
    print(f"  总 token: {repo.session_cost(SID)}")

    md = out.get("report_markdown") or ""
    print(f"\n---- 报告 {len(md)} 字 ----\n{md}")
    Path(f"/tmp/e2e_report_{SID[:8]}.md").write_text(md, encoding="utf-8")
    print(f"\n（已存 /tmp/e2e_report_{SID[:8]}.md）")


if __name__ == "__main__":
    main()
