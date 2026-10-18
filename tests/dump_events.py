"""把某次真实运行的 SSE 事件流导出成回放 fixture。

事件从 sse_event 表（migration 0009 起 `_emit` 自动落库）读，
格式与 tests/fixtures/*.json 一致：
`{"note": ..., "events": [{"at": <相对秒>, "event": <完整事件>}]}`
——可以直接存进 tests/fixtures/ 给 replay_server.py 用。

用法：
    python tests/dump_events.py <session_id> [db路径] [--out 文件]
    python tests/dump_events.py s_xxx /tmp/ra.db --from-node write
        # --from-node：只导出从某个 node 事件起的部分
        #   （分段 fixture 用：审批前 / 审批后两份就是这么切的）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("session_id")
    ap.add_argument("db", nargs="?", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--from-node", default=None,
                    help="从该节点事件开始导出（之前的丢弃）")
    ap.add_argument("--to-node", default=None,
                    help="到该节点事件为止（含）")
    args = ap.parse_args()

    if args.db:
        os.environ["RA_DB_PATH"] = args.db
    from research_agent.db.repo import get_repo
    repo = get_repo()

    rows = repo.q(
        "SELECT at, event_json FROM sse_event WHERE session_id=? ORDER BY id",
        (args.session_id,))
    if not rows:
        sys.exit(f"{args.session_id} 没有落库事件——这条会话跑的时候 "
                 f"0009 迁移可能还没生效（或根本不是这个库）")

    events = [json.loads(r["event_json"]) for r in rows]
    ats = [r["at"] for r in rows]

    # 按 node 边界裁剪（给分段 fixture 用）
    def node_of(ev: dict) -> str | None:
        return ev.get("node") if ev.get("kind") == "node" else None
    if args.from_node:
        idx = next((i for i, e in enumerate(events) if node_of(e) == args.from_node), None)
        if idx is None:
            sys.exit(f"事件流里没有 node={args.from_node}")
        events, ats = events[idx:], ats[idx:]
    if args.to_node:
        idx = next((i for i, e in enumerate(events) if node_of(e) == args.to_node), None)
        if idx is not None:
            events, ats = events[:idx + 1], ats[:idx + 1]

    t0 = datetime.fromisoformat(ats[0]).timestamp()
    out = {
        "note": f"真实运行录下的 SSE，session {args.session_id}，"
                f"tests/dump_events.py 导出",
        "events": [
            {"at": round(datetime.fromisoformat(at).timestamp() - t0, 1),
             "event": ev}
            for at, ev in zip(ats, events)
        ],
    }
    text = json.dumps(out, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"{len(events)} 条事件 → {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
