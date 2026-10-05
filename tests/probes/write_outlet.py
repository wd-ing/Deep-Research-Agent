"""写入环节的出路实测。

已确认：MiniMax-M3.1-Flash-Preview 的 `thinking.budget_tokens` 完全无效
（0 / 1024 / 4096 / 不传，思考都是 ~35,000 字符），思考把 max_tokens 吃光，
正文 0 字符。所以问题不是「服务端不稳」，是**预算被思考饿死**。

这里对比几条可能的出路：
  A. 同模型但把 max_tokens 提到 32768 —— 够不够装下思考 + 正文
  B. 换 MiniMax-M3（同一账号可直连）
  C. 换 space-bunny-free（抽取环节证明稳定，但长文写作未验证）
  D. 砍输入规模（证据 40 条 → 12 条）看思考是否随之下降
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

MINIMAX = os.environ["MINIMAX_API_KEY"]
OPENCODE = os.environ["OPENCODE_API_KEY"]


def load_claims() -> list[str]:
    import sqlite3
    db = "/tmp/ra_e2e5.db"
    if not Path(db).exists():
        return []
    c = sqlite3.connect(db)
    return [r[0][:400] for r in
            c.execute("SELECT content FROM chunk WHERE session_id IS NOT NULL LIMIT 80")]


def make_body(claims: list[str], n: int) -> str:
    return json.dumps({
        "section": {"title": "三类电解质路线及复合路线的性能分歧",
                    "question": "硫化物、氧化物和聚合物路线在离子电导率、稳定性、"
                                "界面处理、加工兼容性和材料短板上各有什么优势与障碍？"},
        "source_legend": [f"[S{i+1}] 来源{i+1} | example.com" for i in range(10)],
        "evidence": [{"text": t, "entities": [], "source": i % 10 + 1,
                      "nature": "media", "confidence": "medium", "date": "2025"}
                     for i, t in enumerate(claims[:n])],
    }, ensure_ascii=False)[:24000]


def call_minimax(model: str, body: str, max_tokens: int) -> tuple:
    payload = {
        "model": model, "max_tokens": max_tokens, "temperature": 0.3,
        "system": prompts.WRITE,
        "messages": [{"role": "user", "content": body}],
    }
    r = httpx.post("https://api.minimaxi.com/anthropic/v1/messages",
                   headers={"x-api-key": MINIMAX, "Authorization": f"Bearer {MINIMAX}",
                            "Content-Type": "application/json",
                            "anthropic-version": "2023-06-01"},
                   json=payload, timeout=300.0)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code} {r.text[:150]}", 0, 0
    blocks = r.json().get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    think = "".join(b.get("thinking", "") for b in blocks if b.get("type") == "thinking")
    return text, None, len(think), len(text)


def call_opencode(model: str, body: str, max_tokens: int) -> tuple:
    r = httpx.post("https://opencode.ai/zen/v1/chat/completions",
                   headers={"Authorization": f"Bearer {OPENCODE}",
                            "Content-Type": "application/json"},
                   json={"model": model, "max_tokens": max_tokens, "temperature": 0.3,
                         "messages": [{"role": "system", "content": prompts.WRITE},
                                      {"role": "user", "content": body}]},
                   timeout=300.0)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code} {r.text[:150]}", 0, 0
    msg = (r.json()["choices"][0]["message"])
    return (msg.get("content") or ""), None, 0, len(msg.get("content") or "")


claims = load_claims() or [f"固态电池技术路线第 {i} 条断言样例。" * 14 for i in range(80)]
print(f"素材 {len(claims)} 条\n")

CASES = [
    ("A M3.1 + max_tokens 32768", "minimax", "MiniMax-M3.1-Flash-Preview", 32768, 40),
    ("B MiniMax-M3 + 16384",      "minimax", "MiniMax-M3",                  16384, 40),
    ("C space-bunny-free + 8192", "opencode", "space-bunny-free",            8192, 40),
    ("D M3.1 + 32768 / 只给 12 条", "minimax", "MiniMax-M3.1-Flash-Preview", 32768, 12),
]

for label, prov, model, mt, n in CASES:
    body = make_body(claims, n)
    t0 = time.time()
    try:
        fn = call_minimax if prov == "minimax" else call_opencode
        text, err, think, tl = fn(model, body, mt)
        dt = round(time.time() - t0, 1)
        if err:
            print(f"{label:30} ✗ {err}  ({dt}s)")
        else:
            ok = "✓" if text and text.strip() else "✗ 空"
            print(f"{label:30} {ok} {dt:6.1f}s 输入 {len(body):5d} 字符 "
                  f"思考 {think:6d} 正文 {tl:5d}")
            if text and text.strip():
                print(f"{'':32}开头: {text[:80].strip()}")
    except Exception as e:
        print(f"{label:30} ✗ {type(e).__name__}: {str(e)[:90]}")
