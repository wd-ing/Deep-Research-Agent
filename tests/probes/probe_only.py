"""单跑探针子图，验证「判不出来」不再被当成「信息稀薄」。

用法：python tests/probe_only.py "研究对象" [预期]
预期可选：ok（应判出质量） / malformed（模拟模型不按格式返回）
"""

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

from research_agent.graph.subgraphs import probe as P  # noqa: E402

Q = sys.argv[1] if len(sys.argv) > 1 else "固态电池的产业化进展，以及各家技术路线的分歧点"
MODE = sys.argv[2] if len(sys.argv) > 2 else "ok"

if MODE == "malformed":
    # 模拟模型只返回了 ambiguity、没给 depth 的那次真实故障
    orig = P.LLM.complete_json

    def broken(stage, prompt, system=None):
        d = orig(stage, prompt, system=system)
        if stage == "probe_light" and "搜索结果" in prompt:
            d.pop("depth", None)
        return d

    P.LLM.complete_json = broken

g = P.build_probe_graph().compile()
out = g.invoke({"question": Q, "attempt": 0})

print("\n==== 事件 ====")
for ev in out.get("events") or []:
    print(" ", json.dumps(ev, ensure_ascii=False)[:220])

print("\n==== 出口 ====")
for k in ("judged", "quality", "degraded", "headline"):
    print(f"  {k}: {out.get(k)}")
sig = out.get("signals") or {}
for k in ("subtopics", "entities", "jargon", "controversies", "common_questions"):
    print(f"  {k}: {len(sig.get(k) or [])} 条")
print(f"  读到深读页: {len(out.get('deep_pages') or [])}")
