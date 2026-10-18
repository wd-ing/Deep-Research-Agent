"""探针：批量闸门的一致性验证 —— 拿 v12 的段重判一遍，和库里的旧 verdict 对。

动机（阶段一改动 1）：`gate_segments` 从逐段调用改成 12 段/批。
输入没变（同一份 heading/summary/blob），但模型「看一批」和「看一段」
的判定可能漂移。这里拿 v12 里 192 段被 LLM 判过的段重放批量路径，
一致率 ≥95% 才认为批量化不改变闸门行为。

跑法：
    set -a; . ./.env; set +a
    .venv/bin/python tests/probe_gate_batch.py

输入：v12 参考库（复制到 /tmp 副本，记账落在副本上，不碰原件）。
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SRC_DB = Path(os.environ.get("PROBE_SRC_DB",
                             ROOT / "data" / "refs" / "ra_v12_minimax.db"))
WORK_DB = Path(tempfile.gettempdir()) / "ra_gate_batch_probe.db"
PROBE_SID = "probe_gate_batch"


def main() -> None:
    if not SRC_DB.exists():
        sys.exit(f"输入库不存在：{SRC_DB}")
    shutil.copy(SRC_DB, WORK_DB)
    os.environ["RA_DB_PATH"] = str(WORK_DB)

    db = sqlite3.connect(WORK_DB)
    db.row_factory = sqlite3.Row

    sess = db.execute("SELECT id, question, probe_json FROM session").fetchone()
    signals = (json.loads(sess["probe_json"] or "{}") or {}).get("signals") or {}

    segs = [dict(r) for r in db.execute("""
        SELECT id, source_id, heading_path, content, summary, token_count,
               on_topic, gate_method
        FROM chunk WHERE session_id=? ORDER BY rowid""", (sess["id"],))]
    print(f"{len(segs)} 段 · "
          f"原判定：{Counter(r['gate_method'] for r in segs)}", flush=True)

    # llm_call 有 session 外键：先补桩 session，否则记账被静默吞掉
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    db.execute("INSERT OR REPLACE INTO session (id,title,question,"
               "created_at,updated_at) VALUES (?,?,?,?,?)",
               (PROBE_SID, "批量闸门一致性探针", "replay gate", now, now))
    db.commit()

    from research_agent.graph.subgraphs import relevance
    from research_agent.llm.provider import session_scope

    kws = relevance.build_keywords(signals, sess["question"])
    with session_scope(PROBE_SID):
        gate = relevance.gate_segments(segs, kws, sess["question"][:80])

    # marks: [(on_topic, reason, method, chunk_id)]
    new_v = {m[3]: (m[0], m[1], m[2]) for m in gate["marks"]}

    # 只对「原来是 LLM 判的段」算一致率——规则判的段两边都是确定性逻辑
    agree = disagree = 0
    diffs = []
    for s in segs:
        if s["gate_method"] not in ("llm", "fallback"):
            continue
        old_on = bool(s["on_topic"])
        new_on, reason, method = new_v.get(s["id"], (None, "", "?"))
        if new_on == old_on:
            agree += 1
        else:
            disagree += 1
            diffs.append((s["id"], old_on, new_on,
                          (s["summary"] or s["content"] or "")[:60],
                          reason[:60]))
    n = agree + disagree
    print(f"\n=== LLM 判定的 {n} 段，批量 vs 原逐段一致率 ===")
    print(f"一致 {agree} · 不一致 {disagree} · 一致率 {agree/max(n,1):.1%}"
          f"（阈值 95%）")
    for d in diffs[:15]:
        print(f"  {d[0]}: {d[1]}→{d[2]}  「{d[3]}」  新理由:{d[4]}")

    # 规则层结果也该一致（同一份 keywords、确定性代码）——顺带核一遍
    rule_diff = sum(1 for s in segs
                    if s["gate_method"] in ("rule", "rule_weak")
                    and bool(s["on_topic"]) != bool(new_v.get(s["id"], (None,))[0]))
    print(f"规则判定的段不一致数：{rule_diff}（应为 0，否则是规则层变了）")

    print(f"\n=== 本次探针调用 ===")
    for r in db.execute("""
        SELECT status, COUNT(*) n, SUM(input_tokens) i,
               SUM(cache_read_tokens) c, SUM(output_tokens) o
        FROM llm_call WHERE session_id=? GROUP BY status""", (PROBE_SID,)):
        print(f"  {r['status']}: {r['n']} 次 · in={r['i'] or 0:,} "
              f"(cache {r['c'] or 0:,}) out={r['o'] or 0:,}")
    print(f"工作库：{WORK_DB}")


if __name__ == "__main__":
    main()
