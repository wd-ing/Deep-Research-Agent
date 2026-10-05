"""探测 MiniMax 的 Anthropic 兼容端点是否支持 prompt 缓存。

背景：write 环节 12 个子 agent 共享一段约 2,140 token 的固定前缀
（写作规则 + 规约 + 他任务职责）。若缓存命中价是原价 25%，
12 次调用可省约 28% 的输入。前提是厂商支持，且额度按计费量扣。

Anthropic 协议的用法是给内容块加 cache_control: {"type":"ephemeral"}，
响应里应出现 cache_creation_input_tokens 与 cache_read_input_tokens。

本探测用真实素材拼一段 >4,000 token 的前缀，连发两次比较。
对照组：不带 cache_control 连发两次，看是否隐式缓存。
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
URL = "https://api.minimaxi.com/anthropic/v1/messages"
M31 = "MiniMax-M3.1-Flash-Preview"
M3 = "MiniMax-M3"


def build_prefix() -> str:
    """用真实抽取内容拼一段稳定前缀，体量贴近规约 + 全量证据摘要。"""
    c = sqlite3.connect("/tmp/ra_e2e7.db")
    rows = [r[0][:300] for r in c.execute(
        "SELECT content FROM chunk WHERE session_id IS NOT NULL LIMIT 40")]
    gl = [{"canonical": f"术语{i}", "en": f"term{i}", "aliases": [f"别名{i}a"]}
          for i in range(12)]
    parts = [prompts.WRITE,
             json.dumps({"glossary": gl,
                         "units": ["能量密度统一 Wh/kg", "产能须标注规划或有效"],
                         "cautions": ["聚合站转述不可当独立验证"]},
                        ensure_ascii=False)]
    parts += [f"[摘要{i}] {t}" for i, t in enumerate(rows)]
    return "\n\n".join(parts)


PREFIX = build_prefix()
TASK = "根据以上材料，为「固态电解质路线对比」这一小节写 300 字正文，每条事实后标 [S编号]。"

print(f"固定前缀 {len(PREFIX)} 字符（约 {len(PREFIX)//2} token，中文粗估）\n")


def call(model: str, cache: bool, tag: str) -> dict:
    sys_blocks = [{"type": "text", "text": PREFIX}]
    if cache:
        sys_blocks[0]["cache_control"] = {"type": "ephemeral"}
    payload = {
        "model": model,
        "max_tokens": 512,
        "temperature": 0.0,
        "system": sys_blocks,
        "messages": [{"role": "user", "content": TASK}],
    }
    t0 = time.time()
    try:
        r = httpx.post(URL, headers={"x-api-key": KEY,
                                    "Authorization": f"Bearer {KEY}",
                                    "Content-Type": "application/json",
                                    "anthropic-version": "2023-06-01"},
                       json=payload, timeout=180.0)
    except Exception as e:
        return {"tag": tag, "err": f"{type(e).__name__}: {str(e)[:60]}"}
    dt = round(time.time() - t0, 2)
    if r.status_code != 200:
        return {"tag": tag, "http": r.status_code, "body": r.text[:200], "sec": dt}
    u = r.json().get("usage") or {}
    return {
        "tag": tag, "sec": dt,
        "in": u.get("input_tokens"),
        "out": u.get("output_tokens"),
        "cache_create": u.get("cache_creation_input_tokens"),
        "cache_read": u.get("cache_read_input_tokens"),
        "usage_keys": sorted(u.keys()),
    }


def show(r: dict) -> None:
    if "err" in r or "http" in r:
        print(f"  {r['tag']:<26} ✗ {r.get('err') or (str(r.get('http')) + ' ' + str(r.get('body'))[:120])}")
        return
    print(f"  {r['tag']:<26} {r['sec']:>6.2f}s in={r['in']} out={r['out']} "
          f"建缓存={r['cache_create']} 读缓存={r['cache_read']}")
    if r["cache_read"] or r["cache_create"]:
        print(f"  {'':26} usage 字段: {r['usage_keys']}")


print("=== A. M3.1 + cache_control，连发两次 ===")
show(call(M31, True, "M3.1 第1次(建)"))
show(call(M31, True, "M3.1 第2次(应命中)"))

print("\n=== B. M3.1 不带 cache_control（对照组）===")
show(call(M31, False, "M3.1 无标记第1次"))
show(call(M31, False, "M3.1 无标记第2次"))

print("\n=== C. MiniMax-M3 + cache_control，连发两次 ===")
show(call(M3, True, "M3 第1次(建)"))
show(call(M3, True, "M3 第2次(应命中)"))
