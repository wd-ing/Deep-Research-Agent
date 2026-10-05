"""实测 MiniMax 的 thinking 预算到底有没有约束力。

背景：写报告时 `thinking_budget: 0` 发出的是
`{"type": "enabled", "budget_tokens": 0}`，实测思考长度 34,182 字符，
服务端随后掐断连接。也就是说 budget_tokens: 0 并没有「不思考」的意思，
模型自己飘到哪算哪。

这里用**真实写入规模的输入**对比四种设置，看哪一种能把思考压住且不掐断：
    0        —— 现状
    1024     —— 给一个小的正数
    4096     —— 中等
    不传      —— 完全不提 thinking 字段
"""

from __future__ import annotations

import json
import os
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
URL = "https://api.minimaxi.com/anthropic/v1/messages"
MODEL = "MiniMax-M3.1-Flash-Preview"

# 用上次真实抽取到的断言做输入，贴近线上规模
claims = []
db = "/tmp/ra_e2e5.db"
if Path(db).exists():
    import sqlite3
    c = sqlite3.connect(db)
    for (s,) in c.execute("SELECT content FROM chunk WHERE session_id IS NOT NULL LIMIT 60"):
        claims.append(s[:400])
if not claims:
    claims = [f"关于固态电池技术路线的第 {i} 条事实断言样例文字。" * 12 for i in range(60)]

body = json.dumps({
    "section": {"title": "三类电解质路线及复合路线的性能分歧",
                "question": "硫化物、氧化物和聚合物路线在离子电导率、稳定性、"
                            "界面处理、加工兼容性和材料短板上各有什么优势与障碍？"},
    "source_legend": [f"[S{i+1}] 来源{i+1} | example.com" for i in range(10)],
    "evidence": [{"text": t, "entities": [], "source": i % 10 + 1,
                  "nature": "media", "confidence": "medium", "date": "2025"}
                 for i, t in enumerate(claims[:40])],
}, ensure_ascii=False)[:24000]

print(f"输入 {len(body)} 字符，证据 {min(40, len(claims))} 条\n")

CASES = [("0（现状）", 0), ("1024", 1024), ("4096", 4096), ("不传", None)]

for label, budget in CASES:
    payload = {
        "model": MODEL,
        "max_tokens": 8192,
        "temperature": 0.3,
        "system": prompts.WRITE,
        "messages": [{"role": "user", "content": body}],
    }
    if budget is not None:
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget}

    t0 = time.time()
    try:
        r = httpx.post(URL, headers={"x-api-key": KEY, "Authorization": f"Bearer {KEY}",
                                     "Content-Type": "application/json",
                                     "anthropic-version": "2023-06-01"},
                       json=payload, timeout=180.0)
        dt = round(time.time() - t0, 1)
        if r.status_code != 200:
            print(f"{label:10} ✗ HTTP {r.status_code} {r.text[:120]}")
            continue
        blocks = r.json().get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        think = "".join(b.get("thinking", "") for b in blocks if b.get("type") == "thinking")
        usage = r.json().get("usage") or {}
        print(f"{label:10} {'✓' if text else '✗ 空'} {dt:6.1f}s "
              f"思考 {len(think):6d} 字符 / 正文 {len(text):5d} 字符 "
              f"out_tokens {usage.get('output_tokens')}")
        if text:
            print(f"{'':10}   开头: {text[:70].strip()}")
    except Exception as e:
        print(f"{label:10} ✗ {type(e).__name__}: {str(e)[:100]}")
