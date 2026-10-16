"""走**生产代码路径**对新平台做一次冒烟。

⚠️ 预检（`preflight_intern_ai.py`）是裸 HTTP，**不能代表生产能用**：
它绕过了 StageConfig、guard、SSEAccum 的接线、`_opencode_tools` 的
messages 组装。裸通 ≠ 生产通。

这个脚本走 `LLMClient` 真调用，覆盖四种调用形态：
  ① complete_json         —— extract/summarize/relevance/assess 用（关思考）
  ② complete_json_forced  —— summarize 用（带 response_format）
  ③ complete（流式）      —— write_plan/write_audit 等 effort 环节
  ④ complete_with_tools   —— write_section 与 node_plan_tasks 的工具循环

**重点验两件预检验不到的事**：
  · `reasoning_effort: none` 真的关掉了思考（生产路径的 payload 组装）
  · 流式路径的 `reasoning_content` 被累加（防线二要靠它）

用法：.venv/bin/python tests/smoke_intern_ai.py
"""

from __future__ import annotations

import os
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

client = LLMClient()
fails: list[str] = []


def check(tag: str, ok: bool, detail: str) -> None:
    print(f"{'✅' if ok else '❌'} {tag}\n   {detail}\n")
    if not ok:
        fails.append(tag)


def show(cfg, name: str) -> str:
    return (f"provider={cfg.provider} model={cfg.model} effort={cfg.reasoning_effort!r} "
            f"max_out={cfg.max_output_tokens} stream={cfg.stream}")


# ① 关思考环节（reasoning_effort: none）
cfg = client.registry.stage("relevance")
print(f"① relevance\n   配置：{show(cfg, 'relevance')}")
t0 = time.time()
try:
    out = client.complete_json(
        "relevance",
        '判断下面这段是否与「固态电池技术路线」相关。只输出 JSON：'
        '{"relevant": true/false, "reason": "一句话"}',
        system="你是检索相关性闸门。")
    check("① complete_json + effort:none",
          isinstance(out, dict) and "relevant" in out,
          f"{time.time()-t0:.1f}s｜解析出 {type(out).__name__}: {str(out)[:100]}")
except Exception as e:
    check("① complete_json + effort:none", False,
          f"{type(e).__name__}: {str(e)[:300]}")

# ② forced（response_format）
cfg = client.registry.stage("summarize")
print(f"② summarize\n   配置：{show(cfg, 'summarize')}")
t0 = time.time()
try:
    out = client.complete_json_forced(
        "summarize",
        "为下面两段各写一句话概括：\n\n"
        "[0] 固态电池用固态电解质替代液态电解质\n[1] 硫化物路线界面阻抗较低",
        name="emit_summaries",
        schema={"type": "object", "properties": {"summaries": {
            "type": "array",
            "items": {"type": "object", "properties": {
                "index": {"type": "integer"},
                "summary": {"type": "string"}},
                "required": ["index", "summary"]}}},
            "required": ["summaries"]},
        system="你是段落概括器。")
    items = (out or {}).get("summaries") or []
    check("② complete_json_forced", len(items) == 2,
          f"{time.time()-t0:.1f}s｜{len(items)} 条｜{str(items)[:140]}")
except Exception as e:
    check("② complete_json_forced", False, f"{type(e).__name__}: {str(e)[:300]}")

# ③ 流式 + effort 环节（write_plan 的档位与流式开关）
cfg = client.registry.stage("write_plan")
print(f"③ write_plan\n   配置：{show(cfg, 'write_plan')}")
t0 = time.time()
try:
    text = client.complete("write_plan", "用三行说明什么是证据分派。",
                           system="你是研究写作规划者。")
    check("③ 流式 complete + effort:high", len(text.strip()) > 0,
          f"{time.time()-t0:.1f}s｜正文 {len(text)} 字")
except Exception as e:
    check("③ 流式 complete + effort:high", False,
          f"{type(e).__name__}: {str(e)[:300]}")

# ④ 工具循环 —— 直接对着那个「未验证的假设」去验：
#    生产路径能不能真的拿到 tool_use 块。
cfg = client.registry.stage("write_section")
print(f"④ write_section\n   配置：{show(cfg, 'write_section')}")
tools = [{"name": "read_segments",
          "description": "按段号读取证据原文",
          "input_schema": {"type": "object", "properties": {
              "ids": {"type": "array", "items": {"type": "string"}}},
              "required": ["ids"]}}]
t0 = time.time()
try:
    res = client.complete_with_tools(
        "write_section", "你是写作子 agent。",
        [{"role": "user", "content": "请调用 read_segments 读取 S1·1 和 S2·3，不要自己回答。"}],
        tools)
    blocks = res.get("blocks") or []
    tool_blocks = [b for b in blocks if b.get("type") == "tool_use"]
    check("④ 工具循环拿到 tool_use 块", bool(tool_blocks),
          f"{time.time()-t0:.1f}s｜{len(blocks)} 块，其中 tool_use {len(tool_blocks)} 个"
          + (f"｜{tool_blocks[0]['name']}({tool_blocks[0]['input']})" if tool_blocks else ""))
except Exception as e:
    check("④ 工具循环拿到 tool_use 块", False, f"{type(e).__name__}: {str(e)[:300]}")

print("=" * 60)
print(f"共 4 项，失败 {len(fails)} 项" + ("：" + "、".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
