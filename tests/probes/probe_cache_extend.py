"""定性：prompt 缓存能不能沿着「只追加的对话」往后延。

前两版测试都栽在同一处——扩展调用里**去掉了 tools**，而缓存前缀顺序是
tools → system → messages，tools 一去前缀就变了，当然读不到。
这一版：所有调用都带同一份 tools，只让 messages 变长。

另外实测 ConnectError 偶发（约两成），测量脚本自己重试，
免得把偶发当成结论。
"""

from __future__ import annotations

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

KEY = os.environ["MINIMAX_API_KEY"]
URL = "https://api.minimaxi.com/anthropic/v1/messages"
M31 = "MiniMax-M3.1-Flash-Preview"
PREFIX = ("以下是一段用于测试提示词缓存的稳定材料，每一段都关于固态电池的"
          "技术路线与产业化进展。" * 400)
TOOLS = [{
    "name": "get_evidence",
    "description": "按下标取事实断言的全文。",
    "input_schema": {"type": "object",
                     "properties": {"indices": {"type": "array",
                                                "items": {"type": "integer"}}},
                     "required": ["indices"]},
}]
SYS = "你在做提示词缓存测试，回答尽量简短。"


def call(label, messages, mark=False, tries=3):
    """tools 永远带同一份。mark 只给第一个 user 块加显式断点。"""
    for attempt in range(1, tries + 1):
        payload = {"model": M31, "max_tokens": 400, "temperature": 0.0,
                   "system": SYS, "tools": TOOLS, "messages": messages}
        if mark:
            first = dict(messages[0])
            first["content"] = [{"type": "text", "text": messages[0]["content"],
                                 "cache_control": {"type": "ephemeral"}}]
            payload["messages"] = [first] + list(messages[1:])
        t0 = time.time()
        try:
            r = httpx.post(URL, headers={"x-api-key": KEY,
                                        "Authorization": f"Bearer {KEY}",
                                        "Content-Type": "application/json",
                                        "anthropic-version": "2023-06-01"},
                           json=payload, timeout=180.0)
        except Exception as e:
            if attempt < tries:
                print(f"  {label:<38} …{type(e).__name__}，重试")
                time.sleep(2)
                continue
            print(f"  {label:<38} ✗ {type(e).__name__} ×{tries}")
            return {}
        if r.status_code != 200:
            print(f"  {label:<38} ✗ HTTP {r.status_code} {r.text[:90]}")
            return {}
        u = r.json().get("usage") or {}
        print(f"  {label:<38} {round(time.time()-t0,1):5.1f}s "
              f"入{u.get('input_tokens') or 0:>6} "
              f"建{(u.get('cache_creation_input_tokens') or 0):>6} "
              f"读{(u.get('cache_read_input_tokens') or 0):>6}")
        return {"usage": u, "blocks": r.json().get("content") or []}
    return {}


def strip_tool_use(blocks):
    """剥掉 tool_use。协议要求 tool_use 必须紧跟 tool_result，
    后面接普通文本轮次会被判 400（实测踩过）。"""
    return [{k: v for k, v in b.items() if k != "type"} | {"type": b["type"]}
            for b in blocks if b.get("type") in ("text", "thinking")]


print(f"前缀 {len(PREFIX)} 字符（约 {len(PREFIX)//2} token）\n")

M1 = [{"role": "user", "content": PREFIX + "\n\n用一句话说明硫化物路线的缺点。不要调用工具。"}]
print("--- 基线：重复同一前缀 ---")
r1 = call("1. P 单发", M1)
r2 = call("2. 完全相同的 P 再发", M1)

print("\n--- 变长：P 之后追加 assistant + user（tools 不变）---")
LONG = [M1[0],
        {"role": "assistant", "content": strip_tool_use(r1.get("blocks") or [])},
        {"role": "user", "content": "再补充一条：氧化物脆性大，加工困难。"}]
r3 = call("3. P+assistant+user（自动模式）", LONG)
r4 = call("4. 同 3，P 上带显式 cache_control", LONG, mark=True)

print("\n" + "=" * 58)


def rd(r):
    return (r.get("usage") or {}).get("cache_read_input_tokens") or 0


b2, b3, b4 = rd(r2), rd(r3), rd(r4)
print(f"重复同一前缀读到    {b2:>6}")
print(f"变长后（自动）读到  {b3:>6}")
print(f"变长后（显式）读到  {b4:>6}")
if b2 < 1000:
    print("\n→ 基线都没命中，本次数据不可用。")
elif b3 > 1000:
    print(f"\n→ 缓存能沿只追加的对话往后延（读到 {b3}）。我先前那句断言是错的。")
elif b4 > 1000:
    print(f"\n→ 自动模式不往后延，但显式 cache_control 断点能（读到 {b4}）。")
else:
    print("\n→ 两种都没延到。原因仍待查，不能下结论。")
