"""连跑 N 次写入环节，确认换模型后稳定（不是碰巧成功一次）。"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from research_agent.graph import prompts                  # noqa: E402
from research_agent.llm.provider import LLMClient         # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 3
LLM = LLMClient()

import json, sqlite3  # noqa: E402
claims = []
if Path("/tmp/ra_e2e5.db").exists():
    c = sqlite3.connect("/tmp/ra_e2e5.db")
    claims = [r[0][:400] for r in
              c.execute("SELECT content FROM chunk WHERE session_id IS NOT NULL LIMIT 80")]

for i, n in enumerate([40, 25, 12][:N], 1):
    pool = (claims or [f"固态电池第 {k} 条断言。" * 12 for k in range(80)])[:n]
    body = json.dumps({
        "section": {"title": "三类电解质路线及复合路线的性能分歧",
                    "question": "硫化物、氧化物、聚合物在离子电导率、稳定性、"
                                "界面处理与加工兼容性上各有什么优势与障碍？"},
        "source_legend": [f"[S{k+1}] 来源{k+1} | example.com" for k in range(10)],
        "evidence": [{"text": t, "entities": [], "source": k % 10 + 1,
                      "nature": "media", "confidence": "medium", "date": "2025"}
                     for k, t in enumerate(pool)],
    }, ensure_ascii=False)[:24000]

    t0 = time.time()
    try:
        txt = LLM.complete("write", body, system=prompts.WRITE)
        dt = round(time.time() - t0, 1)
        has_cite = "[S" in txt
        print(f"第{i}次 证据{n:3d}条 输入{len(body):6d}字符 → {dt:6.1f}s "
              f"正文{len(txt):5d}字 带引用{'✓' if has_cite else '✗'}")
    except Exception as e:
        print(f"第{i}次 证据{n:3d}条 ✗ {type(e).__name__}: {str(e)[:100]}")
