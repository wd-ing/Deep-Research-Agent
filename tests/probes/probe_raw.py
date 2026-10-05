"""复现探针①为什么没给出 depth。打印原始返回，不做任何加工。"""

from __future__ import annotations

import json
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

from research_agent.graph import prompts                       # noqa: E402
from research_agent.graph.subgraphs.probe import _default_queries  # noqa: E402
from research_agent.llm.provider import LLMClient                     # noqa: E402

LLM = LLMClient()
from research_agent.search.searxng import SearXNGProvider        # noqa: E402
from research_agent.search.tavily import TavilyProvider         # noqa: E402

Q = sys.argv[1] if len(sys.argv) > 1 else "固态电池的产业化进展，以及各家技术路线的分歧点"

sx, tv = SearXNGProvider(), TavilyProvider()
qs = _default_queries(Q)

hits: list = []
for x in qs["zh"][:3]:
    try:
        hits += sx.search(x, language="zh-CN", limit=8)
    except Exception as e:
        print(f"  searxng 失败 {x}: {e}")
for x in qs["en"][:3]:
    try:
        hits += tv.search(x, language="en", limit=6)
    except Exception as e:
        print(f"  tavily 失败 {x}: {e}")

print(f"命中 {len(hits)} 条")
blob = "\n".join(f"[{i}] {h.title}\n    {(h.snippet or '')[:200]}\n    {h.url}"
                 for i, h in enumerate(hits[:30]))

print("\n===== 原始返回（未经解析） =====")
raw = LLM.complete("probe", f"研究对象：{Q}\n\n搜索结果（只有标题和摘要）：\n{blob}",
                   system=prompts.PROBE_LIGHT)
print(raw[:6000])

print("\n===== complete_json 解析结果 =====")
data = LLM.complete_json("probe", f"研究对象：{Q}\n\n搜索结果（只有标题和摘要）：\n{blob}",
                         system=prompts.PROBE_LIGHT)
print("顶层键:", list(data.keys()))
print("depth:", json.dumps(data.get("depth"), ensure_ascii=False)[:500])
print("ambiguity.is_ambiguous:", (data.get("ambiguity") or {}).get("is_ambiguous"))
print("retry_queries:", json.dumps(data.get("retry_queries"), ensure_ascii=False)[:300])
