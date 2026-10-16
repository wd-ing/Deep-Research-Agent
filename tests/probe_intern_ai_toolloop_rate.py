"""生产路径 `complete_with_tools` 的**实际工具调用率**，n≥5。

⚠️ **为什么不能只跑一次就下结论**：预检和逐变量隔离（A~F 六组）
每次都拿到了 tool_use，但走生产的 `complete_with_tools` 那一次拿到 0 个。
两者 payload 完全一致，所以差异**只可能来自模型自己的选择**。

而且代码里本来就写了这条：`complete_json_forced` 的 docstring 说
「必须用 tool_choice 强制指定。只把工具列进 tools 的话，
**模型可能选择直接回文本而不调工具**」。

`complete_with_tools`（工具循环用）**故意不加** `tool_choice` ——
它要的就是「模型自己决定要不要读原文」。所以「没调工具」在这个路径上
**可能是合法结果，不是故障**。但到底是合法还是退化，得看频率。

用法：.venv/bin/python tests/probe_intern_ai_toolloop_rate.py
"""

from __future__ import annotations

import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from research_agent.llm.provider import LLMClient  # noqa: E402

TOOLS = [{"name": "read_segments",
          "description": "按段号读取证据原文",
          "input_schema": {"type": "object", "properties": {
              "ids": {"type": "array", "items": {"type": "string"}}},
              "required": ["ids"]}}]

PROMPTS = [
    ("明确要求调工具", "请调用 read_segments 读取 S1·1 和 S2·3，不要自己回答。"),
    ("真实写作场景", "这一节要写「固态电池的产业化进展」。证据里有 S1·1、S2·3、S4·2 "
                "三段。你需要看它们的全文才能动笔——请先取回需要的证据。"),
]

client = LLMClient()
fails = []

for label, prompt in PROMPTS:
    cfg = client.registry.stage("write_section")
    print(f"=== {label}（n=5）===")
    print(f"    {cfg.provider} / {cfg.model} / effort={cfg.reasoning_effort} "
          f"/ stream={cfg.stream} / max_out={cfg.max_output_tokens}\n")
    hits, times = 0, []
    for i in range(5):
        t0 = time.time()
        try:
            res = client.complete_with_tools(
                "write_section", "你是写作子 agent。",
                [{"role": "user", "content": prompt}], TOOLS)
            blocks = res.get("blocks") or []
            tb = [b for b in blocks if b.get("type") == "tool_use"]
            txt = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            dt = round(time.time() - t0, 1)
            times.append(dt)
            mark = "✅" if tb else "⚠️ "
            if tb:
                hits += 1
            print(f"  {mark} #{i+1} {dt:>5.1f}s 块 {len(blocks)}｜tool_use {len(tb)}"
                  f"｜正文 {len(txt)} 字"
                  + (f"｜{tb[0]['name']}({tb[0]['input']})" if tb else ""))
            if not tb and txt:
                print(f"        退回成了纯文本: {txt[:150]!r}")
        except Exception as e:
            print(f"  ❌ #{i+1} {type(e).__name__}: {str(e)[:150]}")
    print(f"\n  → 工具调用率 {hits}/5；耗时中位 {statistics.median(times) if times else '-'}\n")

print("=" * 60)
print("判读：生产路径的 payload 本身没问题（逐变量隔离 F 组已证）。")
print("「没调工具」在这个路径上可能合法 —— complete_with_tools 故意不加")
print("tool_choice，因为它要的就是「模型自己决定要不要读原文」。")
sys.exit(0)
