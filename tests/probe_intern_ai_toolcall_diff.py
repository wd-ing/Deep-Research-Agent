"""定位：预检的手搓格式能拿到 tool_call，生产路径拿到 0 个。**逐变量隔离。**

⚠️ **这是「预检通过但生产失败」的第二次形态**（第一次见
`to_openai_tools` 的 docstring）。所以预检不算数 ——
只有走 `LLMClient` 的真实结果算数。

生产路径与预检的差异，逐个加上去看哪个把工具调用弄没了：
    A 基线     手搓格式、无 stream、无 effort、无 temperature  ← 已知可用的形态
    B          A + temperature
    C          A + reasoning_effort=low
    D          A + stream + stream_options
    E          A + system 消息
    F 生产     全部（即 write_section 的真实 payload）

用法：.venv/bin/python tests/probe_intern_ai_toolcall_diff.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from research_agent.llm.provider import SSEAccum, _sse_lines, to_openai_tools  # noqa: E402

BASE = "https://discovery-api.intern-ai.org.cn/v1"
MODEL = "deepseek-v4-flash-0731"

# 生产真正会发的工具定义：Anthropic 形状 → 经 to_openai_tools 转换
ANTHROPIC_TOOLS = [{"name": "read_segments",
                    "description": "按段号读取证据原文",
                    "input_schema": {"type": "object", "properties": {
                        "ids": {"type": "array", "items": {"type": "string"}}},
                        "required": ["ids"]}}]

PROMPT = "请调用 read_segments 读取 S1·1 和 S2·3，不要自己回答。"


def run(client: httpx.Client, tag: str, payload: dict) -> dict:
    """发一次，把「拿到了几个 tool_use」和「正文长什么样」都带回来。"""
    stream = bool(payload.get("stream"))
    t0 = time.time()
    try:
        if stream:
            with client.stream("POST", f"{BASE}/chat/completions", json=payload,
                               timeout=180) as r:
                if r.status_code != 200:
                    body = r.read().decode("utf-8", "replace")
                    return {"tag": tag, "ok": False, "err": f"HTTP {r.status_code}: {body[:200]}"}
                acc = SSEAccum()
                for obj in _sse_lines(r):
                    acc.feed(obj)
            blocks = acc.blocks()
            text = acc.text_only()
            rc = "".join(acc.reasoning)
        else:
            r = client.post(f"{BASE}/chat/completions", json=payload, timeout=180)
            if r.status_code != 200:
                return {"tag": tag, "ok": False, "err": f"HTTP {r.status_code}: {r.text[:200]}"}
            d = r.json()
            ch = (d.get("choices") or [{}])[0]
            msg = ch.get("message") or {}
            tcs = msg.get("tool_calls") or []
            blocks = [{"type": "tool_use", "id": t.get("id"),
                       "name": (t.get("function") or {}).get("name"),
                       "input": (t.get("function") or {}).get("arguments")}
                      for t in tcs]
            text = msg.get("content") or ""
            rc = msg.get("reasoning_content") or ""
    except Exception as e:
        return {"tag": tag, "ok": False, "err": f"{type(e).__name__}: {str(e)[:200]}"}

    n_tool = sum(1 for b in blocks if b.get("type") == "tool_use")
    return {"tag": tag, "ok": True, "t": round(time.time() - t0, 1),
            "n_tool": n_tool, "n_blocks": len(blocks),
            "text": text, "rc": rc, "blocks": blocks}


def main() -> int:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    h = {"Authorization": "Bearer " + os.environ["INTERN_AI_API_KEY"],
         "Content-Type": "application/json"}

    openai_tools = to_openai_tools(ANTHROPIC_TOOLS)
    print("生产实际发送的 tools 形状：")
    print("  " + json.dumps(openai_tools, ensure_ascii=False))
    print()

    base = {"model": MODEL, "max_tokens": 8192, "tools": openai_tools,
            "messages": [{"role": "user", "content": PROMPT}]}
    cases = [
        ("A 基线（= 预检形态）", {}),
        ("B +temperature=0.2", {"temperature": 0.2}),
        ("C +reasoning_effort=low", {"reasoning_effort": "low"}),
        ("D +stream", {"stream": True, "stream_options": {"include_usage": True}}),
        ("E +system 消息", {"__sys": True}),
        ("F 生产全量", {"temperature": 0.2, "reasoning_effort": "low",
                      "stream": True, "stream_options": {"include_usage": True},
                      "__sys": True}),
    ]

    results = []
    with httpx.Client(trust_env=False, headers=h,
                      timeout=httpx.Timeout(180.0, connect=20.0)) as c:
        for tag, extra in cases:
            payload = dict(base)
            msgs = [{"role": "user", "content": PROMPT}]
            if extra.pop("__sys", False):
                msgs = [{"role": "system", "content": "你是写作子 agent。"}] + msgs
            payload.update(extra)
            payload["messages"] = msgs
            r = run(c, tag, payload)
            results.append(r)
            if not r["ok"]:
                print(f"❌ {tag:24s} {r['err']}")
                continue
            verdict = "✅ 拿到工具" if r["n_tool"] else "⚠️ **没拿到工具**"
            print(f"{verdict}  {tag:24s} {r['t']:>5.1f}s "
                  f"块 {r['n_blocks']}｜正文 {len(r['text'])} 字｜思考 {len(r['rc'])} 字")
            if not r["n_tool"] and r["text"]:
                print(f"      正文内容: {r['text'][:200]!r}")
            elif r["n_tool"]:
                b = [x for x in r["blocks"] if x["type"] == "tool_use"][0]
                print(f"      工具: {b['name']}({b['input']})")

    print("\n--- 汇总 ---")
    for r in results:
        if r["ok"]:
            print(f"  {r['tag']:24s} tool_use {r['n_tool']} 个")
    print(json.dumps([{k: v for k, v in r.items() if k != "blocks"} for r in results],
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
