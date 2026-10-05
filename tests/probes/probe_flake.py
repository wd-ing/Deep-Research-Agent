"""量一下探针①丢失 depth 的真实频率。

同一段搜索结果反复问 N 次，看有多少次拿不到可用的 depth。
这是决定「加重试」还是「拆调用 / 改结构」的依据，不靠猜。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from research_agent.graph import prompts                        # noqa: E402
from research_agent.graph.subgraphs.probe import _default_queries  # noqa: E402
from research_agent.llm.provider import LLMClient               # noqa: E402
from research_agent.search.searxng import SearXNGProvider       # noqa: E402
from research_agent.search.tavily import TavilyProvider        # noqa: E402

Q = sys.argv[1] if len(sys.argv) > 1 else "固态电池的产业化进展，以及各家技术路线的分歧点"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 6

LLM = LLMClient()
sx, tv = SearXNGProvider(), TavilyProvider()
qs = _default_queries(Q)
hits: list = []
for x in qs["zh"][:3]:
    try:
        hits += sx.search(x, language="zh-CN", limit=8)
    except Exception as e:
        print(f"searxng 失败: {e}")
for x in qs["en"][:3]:
    try:
        hits += tv.search(x, language="en", limit=6)
    except Exception as e:
        print(f"tavily 失败: {e}")

blob = "\n".join(f"[{i}] {h.title}\n    {(h.snippet or '')[:200]}\n    {h.url}"
                 for i, h in enumerate(hits[:30]))
print(f"命中 {len(hits)} 条，跑 {N} 次\n")

ok = bad = 0
for i in range(N):
    try:
        d = LLM.complete_json("probe", f"研究对象：{Q}\n\n搜索结果（只有标题和摘要）：\n{blob}",
                              system=prompts.PROBE_LIGHT)
    except Exception as e:
        bad += 1
        print(f"  第{i+1}次 异常: {type(e).__name__}: {str(e)[:100]}")
        continue
    depth = d.get("depth") or {}
    q = depth.get("quality")
    if q in ("substantive", "thin", "farm"):
        ok += 1
        print(f"  第{i+1}次 ✓ {q}，深读 {len(depth.get('worth_reading_urls') or [])} 条")
    else:
        bad += 1
        print(f"  第{i+1}次 ✗ 顶层键 {list(d.keys())}，depth={str(depth)[:80]}")

print(f"\n可用 {ok}/{N}，丢失 {bad}/{N}")
