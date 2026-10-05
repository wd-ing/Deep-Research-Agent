"""验证 MiniMax-M3.1-Flash-Preview 在 Anthropic 兼容端点是否支持 tool calling。

「看全文做成 tool、让 agent 自己决定要不要继续循环」这个设计成不成立，
取决于一件事：端点支不支持 tools 与 tool_use。

不支持 → 只能退回「一次性给全部」或「带上限的 need_full」。
支持   → 工具循环可做，而且对话是只追加的，正好吃 prompt 缓存。

同时测两件事：
  1. 单个 tool_use 能否一次要多个下标（参数是数组）
  2. 返回 tool_result 后能否继续再调一轮（多轮 tool loop）
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

KEY = os.environ["MINIMAX_API_KEY"]
URL = "https://api.minimaxi.com/anthropic/v1/messages"
M31 = "MiniMax-M3.1-Flash-Preview"

TOOLS = [{
    "name": "get_evidence",
    "description": "按下标取事实断言的全文。下标来自之前的证据摘要。",
    "input_schema": {
        "type": "object",
        "properties": {
            "indices": {"type": "array", "items": {"type": "integer"},
                        "description": "要查看的断言下标，可以一次给多个"}
        },
        "required": ["indices"],
    },
}]

EVIDENCE = []
c = sqlite3.connect("/tmp/ra_e2e7.db")
for i, (txt,) in enumerate(c.execute(
        "SELECT content FROM chunk WHERE session_id IS NOT NULL LIMIT 12")):
    EVIDENCE.append(txt or "")
BRIEF = {
    "研究问题": "固态电池的产业化进展，以及各家技术路线的分歧点",
    "已批准的框架": [{"id": "sec1", "title": "定义与层级", "question": "怎么划分"},
                     {"id": "sec2", "title": "材料路线", "question": "各路线差异"}],
    "断言摘要": [{"i": i, "摘要": t[:70]} for i, t in enumerate(EVIDENCE)],
}
SYS = """你在规划一份研究报告的写作规约。
你会拿到断言的**摘要**。如果某几条只有摘要你看不准，
可以调用 get_evidence 工具取它们的全文，取完再继续判断。
看完认为够了就直接给出 JSON 规约，不要再调工具。"""


def call(messages: list, label: str) -> dict:
    payload = {"model": M31, "max_tokens": 8192, "temperature": 0.0,
               "system": SYS, "tools": TOOLS, "messages": messages}
    t0 = time.time()
    try:
        r = httpx.post(URL, headers={"x-api-key": KEY,
                                    "Authorization": f"Bearer {KEY}",
                                    "Content-Type": "application/json",
                                    "anthropic-version": "2023-06-01"},
                       json=payload, timeout=240.0)
        dt = round(time.time() - t0, 1)
    except Exception as e:
        print(f"  {label:<26} ✗ {type(e).__name__}: {str(e)[:70]}")
        return {}
    if r.status_code != 200:
        print(f"  {label:<26} ✗ HTTP {r.status_code} {r.text[:180]}")
        return {}
    j = r.json()
    u = j.get("usage") or {}
    blocks = j.get("content") or []
    kinds = [b.get("type") for b in blocks]
    calls = [b for b in blocks if b.get("type") == "tool_use"]
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    print(f"  {label:<26} ✓ {dt:5.1f}s 块类型 {kinds} "
          f"出{u.get('output_tokens')} 缓存读{u.get('cache_read_input_tokens') or 0}")
    if calls:
        print(f"  {'':28}要的下标: {calls[0].get('input')}")
    elif text:
        print(f"  {'':28}正文: {text[:100]}")
    return {"blocks": blocks, "usage": u, "text": text, "calls": calls}


print("=== 探针 1：单轮 tool_use ===")
msgs = [{"role": "user", "content": json.dumps(BRIEF, ensure_ascii=False)}]
r1 = call(msgs, "第 1 轮")

if not r1.get("calls"):
    print("\n结论：模型没有发起 tool_use。"
          f"{'可能是不支持 tools，也可能是不想调。' if r1 else '端点直接报错。'}")
    sys.exit(0)

print("\n=== 探针 2：喂回 tool_result，问它还要不要再看 ===")
# 每个 tool_use 必须对应一个 tool_result，顺序一致。上一版只回了一个就报
# 400「tool call and result not match」——模型一轮里发起了两个 tool_use。
results = []
for blk in r1["calls"]:
    idxs = (blk.get("input") or {}).get("indices") or []
    results.append({
        "type": "tool_result",
        "tool_use_id": blk["id"],
        "content": json.dumps(
            [{"i": i, "全文": EVIDENCE[i][:400]}
             for i in idxs if 0 <= i < len(EVIDENCE)], ensure_ascii=False),
    })
print(f"  （回填 {len(results)} 个 tool_result，对应 {len(r1['calls'])} 个 tool_use）")
msgs2 = msgs + [
    {"role": "assistant", "content": r1["blocks"]},
    {"role": "user", "content": results},
]
r2 = call(msgs2, "第 2 轮（回填后）")

if r2.get("calls"):
    print("\n✅ 多轮 tool loop 可行")
    print(f"   第二轮又要了: {r2['calls'][0].get('input')}")
elif r2.get("text"):
    print("\n✅ tool_result 回填后模型直接给结论，工具循环完整可用")
    print(f"   结论开头: {r2['text'][:150]}")
else:
    print("\n⚠️ 第二轮既没调工具也没给文本，需要进一步看")

u1 = r1.get("usage") or {}
u2 = r2.get("usage") or {}
print(f"\n缓存：第1轮读 {u1.get('cache_read_input_tokens') or 0}，"
      f"第2轮读 {u2.get('cache_read_input_tokens') or 0}"
      f"（只追加的对话，理论上第 2 轮应能命中第 1 轮的前缀）")
