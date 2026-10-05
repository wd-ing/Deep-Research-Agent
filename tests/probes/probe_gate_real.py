"""问题二实测：段级闸门在**真实探针轮廓**下的表现。

之前那次测试我传的是空轮廓 `{"subtopics": [], ...}`，没有关键词可用，
于是 165 段全部落到 LLM 判定——412 次调用、90k token。
那是个测试假象，但也暴露了脆弱性：**闸门成本完全取决于探针轮廓的质量**。

这次用真实运行的 probe_json（10,160 字符的轮廓），重跑同一个库的 177 个段落，
统计规则层能拦住多少、LLM 被调用多少次。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
os.environ.setdefault("RA_DB_PATH", "/tmp/ra_r5_e2e.db")

from research_agent.db.repo import get_repo  # noqa: E402
from research_agent.graph.subgraphs import relevance  # noqa: E402

DB = "/tmp/ra_r5_e2e.db"
SID = "s_a9f5bc761016"

c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row
signals = json.loads(
    c.execute("SELECT probe_json FROM session WHERE id=?", (SID,)).fetchone()[0]
)["signals"]
question = c.execute("SELECT question FROM session WHERE id=?", (SID,)).fetchone()[0]
segs = [dict(r) for r in c.execute(
    "SELECT id, source_id, heading_path, content, summary, token_count "
    "FROM chunk WHERE session_id=? ORDER BY rowid", (SID,))]

print(f"研究问题：{question}")
print(f"探针轮廓：{len(json.dumps(signals, ensure_ascii=False)):,} 字符")
print(f"  子主题 {len(signals.get('subtopics') or [])}、"
      f"实体 {len(signals.get('entities') or [])}、"
      f"争议 {len(signals.get('controversies') or [])}")
kw = relevance.build_keywords(signals, question)
print(f"  抽出关键词：strong {len(kw.get('strong') or [])} 个、"
      f"weak {len(kw.get('weak') or [])} 个")
print(f"  strong 样例：{(kw.get('strong') or [])[:8]}")
print(f"\n待判段落 {len(segs)} 个"
      f"（其中 {(len(segs) - sum(1 for s in segs if (s.get('summary') or '').strip()))} 个没有概括，"
      f"用正文开头兜底）\n")

repo = get_repo()
t0 = time.time()
gate = relevance.gate_segments(segs, kw, question[:80])
dt = time.time() - t0

by = gate["stats"]["by_method"]
print("=" * 68)
print("判定结果")
print("=" * 68)
for m, n in sorted(by.items(), key=lambda x: -x[1]):
    tag = "（零成本）" if m.startswith("rule") else "（要花钱）"
    print(f"  {m:<18} {n:>4} 段  {tag}")
print(f"\n  合计 {gate['stats']['segments']} 段，"
      f"保留 {gate['stats']['on_topic']}，"
      f"判为无关 {gate['stats']['off_topic']}")
print(f"  需要 LLM 判定：{sum(n for m, n in by.items() if not m.startswith('rule'))} 段")
print(f"  耗时 {dt:.1f} 秒")

# 记账：跑完看真实花费
rows = repo.q("SELECT stage, COUNT(*) n, SUM(input_tokens+output_tokens) tok "
              "FROM llm_call GROUP BY stage")
print("\n" + "=" * 68)
print("本次闸门调用的记账")
print("=" * 68)
for r in rows:
    print(f"  {r['stage']:<16} {r['n']:>4} 次  {r['tok']:>8,} token")
print("\n对照上一次（空轮廓）：412 次 / 89,987 token")
