"""探针：重放 v12 里 98 段无概括的 chunk，验证 low effort + 有界重试能否救回。

动机（2026-10-18 定位）：
`data/refs/ra_v12_minimax.db` 那轮 summarize 12/32 调用 EmptyOutput
（「模型返回文本而不是工具调用」），98/246 段没概括。
但那轮跑在 SUMM_RETRY 加进来**之前**（75aab36 是跑完才提的），
线上重试的真实回收率没有实测过——离线 `test_summarize_retry` 只验了桩。

跑法：
    set -a; . ./.env; set +a
    RA_DB_PATH=/tmp/ra_summ_replay.db .venv/bin/python tests/probe_summarize_replay.py

输入：v12 参考库（只读，复制到 WORK_DB 后记账都落在副本上，不碰原件）。
判定：看 98 段里救回多少、重试发起几次、哪个来源的批次还在成片挂。
"""

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SRC_DB = Path(os.environ.get("PROBE_SRC_DB",
                             ROOT / "data" / "refs" / "ra_v12_minimax.db"))
WORK_DB = Path(tempfile.gettempdir()) / "ra_summ_replay.db"
PROBE_SID = "probe_summ_replay"


def main() -> None:
    if not SRC_DB.exists():
        sys.exit(f"输入库不存在：{SRC_DB}")
    # 记账会落到 RA_DB_PATH 指的库——用副本，v12 原件一个字节不动
    shutil.copy(SRC_DB, WORK_DB)
    os.environ["RA_DB_PATH"] = str(WORK_DB)

    db = sqlite3.connect(WORK_DB)
    db.row_factory = sqlite3.Row
    items = [dict(r) for r in db.execute("""
        SELECT id AS chunk_id, content AS text,
               COALESCE(heading_path,'') AS heading_path,
               source_id, on_topic
        FROM chunk
        WHERE summary IS NULL OR trim(summary)=''
        ORDER BY source_id, id""")]
    print(f"无概括段：{len(items)}（on_topic {sum(i['on_topic'] for i in items)}，"
          f"分布在 {len(set(i['source_id'] for i in items))} 个来源）", flush=True)
    if not items:
        sys.exit("没有可重放的段")

    # ⚠️ `llm_call.session_id` 有 REFERENCES session(id) 外键（repo 开
    # PRAGMA foreign_keys），用一个不存在的 sid 记账会全部被 FK 拒掉，
    # 而且 `_record` 的 `except: pass` 把这事吞得无声无息——先补一条
    # 桩 session，否则下面的「本次探针的 llm_call」永远是空的。
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    db.execute("INSERT OR REPLACE INTO session (id,title,question,"
               "created_at,updated_at) VALUES (?,?,?,?,?)",
               (PROBE_SID, "概括重放探针", "replay missing summaries", now, now))
    db.commit()

    from research_agent.graph.subgraphs.extract_batch import summarize_segments
    from research_agent.llm.provider import session_scope

    calls_before = db.execute(
        "SELECT COUNT(*) FROM llm_call WHERE session_id=?", (PROBE_SID,)).fetchone()[0]

    with session_scope(PROBE_SID):
        out = summarize_segments(items)

    got = out["summaries"]
    ev = out["events"][0] if out["events"] else {}
    print(f"\n=== 结果 ===")
    print(f"批数 {ev.get('batches')} · 救回 {len(got)}/{len(items)} · "
          f"彻底失败批 {out['failed']}", flush=True)
    print(f"重试发起 {ev.get('retried')} · 重试救回 {ev.get('recovered')} · "
          f"仍不完整批 {ev.get('still_incomplete')}", flush=True)

    # 救回/丢失按来源和 on_topic 拆开看——罪魁是那个 80 段的综述
    got_ids = set(got)
    print(f"\n=== 按来源拆 ===")
    from collections import Counter
    miss_by_src = Counter(i["source_id"] for i in items if i["chunk_id"] not in got_ids)
    hit_by_src = Counter(i["source_id"] for i in items if i["chunk_id"] in got_ids)
    for sid in sorted(set(hit_by_src) | set(miss_by_src)):
        print(f"  {sid}: 救回 {hit_by_src.get(sid,0)} · 仍丢 {miss_by_src.get(sid,0)}")
    lost_on_topic = sum(1 for i in items
                        if i["chunk_id"] not in got_ids and i["on_topic"])
    print(f"仍丢失的 on_topic 段：{lost_on_topic}")

    print(f"\n=== 本次探针的 llm_call ===")
    for r in db.execute("""
        SELECT created_at, status, SUBSTR(COALESCE(error,''),1,60) e,
               input_tokens, cache_read_tokens, output_tokens
        FROM llm_call WHERE session_id=? ORDER BY id""", (PROBE_SID,)):
        flag = "❌" if r["status"] == "error" else "  "
        print(f"  {flag} {str(r['created_at'])[11:19]}  in={r['input_tokens']:>6} "
              f"(cache {r['cache_read_tokens'] or 0:>6})  out={r['output_tokens']:>5}  {r['e']}")
    print(f"\n工作库：{WORK_DB}（v12 副本 + 本次调用记录）")


if __name__ == "__main__":
    main()
