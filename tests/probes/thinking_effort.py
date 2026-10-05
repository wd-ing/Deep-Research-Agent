"""只测量，不改配置。

用户提供的官方文档说明：MiniMax-M3.1-Flash-Preview 的思考深度由
`output_config.effort` 控制（low/medium/high/xhigh/max，省略默认 max，none 报 400），
而 `thinking.type` 的合法值只有 `adaptive` 与 `disabled`。

我此前测的 `thinking.budget_tokens: 0 / 1024 / 4096 / 不传` 四种设置，
用的 `type: "enabled"` 本来就不在合法枚举里 —— 所以那四次其实是同一种情况，
都是「等同于省略 → adaptive + 默认 effort=max」，难怪思考长度清一色 ~35,000 字符。

这里验证真正的旋钮：显式发 `thinking.type=adaptive` + `output_config.effort=low`，
看思考长度能不能被压下来、正文能不能出来。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from research_agent.graph import prompts  # noqa: E402

KEY = os.environ["MINIMAX_API_KEY"]
MODEL = "MiniMax-M3.1-Flash-Preview"

claims = []
if Path("/tmp/ra_e2e7.db").exists():
    c = sqlite3.connect("/tmp/ra_e2e7.db")
    claims = [r[0][:400] for r in
              c.execute("SELECT content FROM chunk WHERE session_id IS NOT NULL LIMIT 80")]
claims = claims or [f"固态电池第 {i} 条断言样例。" * 12 for i in range(80)]

body = json.dumps({
    "section": {"title": "三类电解质路线及复合路线的性能分歧",
                "question": "硫化物、氧化物、聚合物在离子电导率、稳定性、"
                            "界面处理与加工兼容性上各有什么优势与障碍？"},
    "source_legend": [f"[S{i+1}] 来源{i+1} | example.com" for i in range(10)],
    "evidence": [{"text": t, "entities": [], "source": i % 10 + 1,
                  "nature": "media", "confidence": "medium", "date": "2025"}
                 for i, t in enumerate(claims[:40])],
}, ensure_ascii=False)[:24000]
print(f"模型 {MODEL}，输入 {len(body)} 字符，证据 40 条\n")

# 依次用文档里的合法写法：先 adaptive + 低 effort，再 adaptive + 默认
CASES = [
    ("adaptive + effort=low",  {"type": "adaptive"}, {"effort": "low"}),
    ("adaptive + effort=medium", {"type": "adaptive"}, {"effort": "medium"}),
    ("adaptive，不给 effort（=默认 max）", {"type": "adaptive"}, None),
]

for label, thinking, oc in CASES:
    payload = {
        "model": MODEL, "max_tokens": 16384, "temperature": 0.3,
        "system": prompts.WRITE,
        "messages": [{"role": "user", "content": body}],
        "thinking": thinking,
    }
    if oc:
        payload["output_config"] = oc

    t0 = time.time()
    try:
        r = httpx.post("https://api.minimaxi.com/anthropic/v1/messages",
                       headers={"x-api-key": KEY, "Authorization": f"Bearer {KEY}",
                                "Content-Type": "application/json",
                                "anthropic-version": "2023-06-01"},
                       json=payload, timeout=300.0)
        dt = round(time.time() - t0, 1)
        if r.status_code != 200:
            print(f"{label:34} ✗ HTTP {r.status_code}  {r.text[:150]}")
            continue
        blocks = r.json().get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        think = "".join(b.get("thinking", "") for b in blocks if b.get("type") == "thinking")
        print(f"{label:34} {'✓' if text.strip() else '✗空'} {dt:6.1f}s "
              f"思考 {len(think):6d} 字符 / 正文 {len(text):5d} 字符")
        if text.strip():
            print(f"{'':36}开头: {text[:70].strip()}")
    except Exception as e:
        print(f"{label:34} ✗ {type(e).__name__}: {str(e)[:90]}")
