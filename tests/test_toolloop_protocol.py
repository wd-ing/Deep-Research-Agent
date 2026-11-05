"""工具循环的协议转换（Anthropic 对话 → OpenAI 形状）。

⚠️ **这个 bug 是在真平台上跑出来的，不是读代码看出来的**：
信通院 discovery-api 严格校验内容块，`write_section` 实测 10 次里
**4 次** 400 然后静默降级成单次调用。xj189 不校验，所以一直藏着。

所以这个脚本分两半，**第二半必须打真平台才算数**：

  ① 离线：`_anthropic_msgs_to_openai` 的形状对不对（多 tool_use、
     只有 tool_use 没正文、user 轮里 tool_result + text 混排…）
  ② 在线：真的跑一个**两轮**工具循环，证明第二轮不再 400

**预检只测第一轮是不够的** —— 这次的坑正是预检 9/9 全过、
生产照样挂。凡是涉及「多轮协议」的，预检必须自己把对话拼出来跑第二轮。

用法：
    .venv/bin/python tests/test_toolloop_protocol.py          # 离线 + 在线
    .venv/bin/python tests/test_toolloop_protocol.py --offline
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import os as _os
if not _os.environ.get("ARK_API_KEY"):
    print("SKIP: 需要 ARK_API_KEY（真模型调用）")
    raise SystemExit(0)
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _isolate  # noqa: F401, E402

from research_agent.llm.provider import _anthropic_msgs_to_openai  # noqa: E402

fails: list[str] = []


def check(tag: str, ok: bool, detail: str = "") -> None:
    print(f"{'✅' if ok else '❌'} {tag}" + (f"\n   {detail}" if detail else ""))
    if not ok:
        fails.append(tag)


def roundtrip(msgs):
    return _anthropic_msgs_to_openai(msgs)


print("=== ① 离线：协议转换形状 ===")

# 第一轮：纯字符串，转换必须是恒等的
first = [{"role": "user", "content": "请调用 read_segments 读取 S1·1。"}]
check("第一轮纯字符串 → 恒等", roundtrip(first) == first, str(roundtrip(first)))

# 一个 tool_use、没有正文 → content 必须是 None（不是 ""）
one = [{"role": "user", "content": "去读"},
       {"role": "assistant", "content": [
           {"type": "tool_use", "id": "chatcmpl-tool-a", "name": "read_segments",
            "input": {"segments": ["S1·1"]}}]},
       {"role": "user", "content": [
           {"type": "tool_result", "tool_use_id": "chatcmpl-tool-a",
            "content": "正文…"}]}]
got = roundtrip(one)
a = got[1]
check("assistant 只有 tool_use 时 content 是 None（不是空串）",
      a.get("content") is None and len(a.get("tool_calls") or []) == 1,
      json.dumps(a, ensure_ascii=False))
check("arguments 是 JSON **字符串**（OpenAI 要求）",
      isinstance(a["tool_calls"][0]["function"]["arguments"], str)
      and json.loads(a["tool_calls"][0]["function"]["arguments"])
      == {"segments": ["S1·1"]},
      f"type={type(a['tool_calls'][0]['function']['arguments']).__name__}")
check("tool_result 变成独立的 role:tool 消息",
      len(got) == 3 and got[2]["role"] == "tool"
      and got[2]["tool_call_id"] == "chatcmpl-tool-a",
      json.dumps(got[2], ensure_ascii=False))
check("tool_call_id 与 tool_use 的 id 对上",
      got[2]["tool_call_id"] == a["tool_calls"][0]["id"],
      f"{got[2]['tool_call_id']!r} == {a['tool_calls'][0]['id']!r}")

# 一轮多个 tool_use → 每个一条 tool 消息，顺序一一对应
multi = [{"role": "assistant", "content": [
    {"type": "tool_use", "id": "t1", "name": "read_segments", "input": {"segments": ["S1·1"]}},
    {"type": "tool_use", "id": "t2", "name": "read_source", "input": {"source": "S2"}}]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "A"},
        {"type": "tool_result", "tool_use_id": "t2", "content": "B"}]}]
g = roundtrip(multi)
check("一轮多个 tool_use → 2 条 tool_calls + 2 条 tool 消息（顺序对应）",
      len(g[0]["tool_calls"]) == 2 and
      [m["role"] for m in g[1:]] == ["tool", "tool"] and
      [m["tool_call_id"] for m in g[1:]] == ["t1", "t2"] and
      [m["content"] for m in g[1:]] == ["A", "B"],
      json.dumps(g, ensure_ascii=False)[:260])

# tool_use 和正文混排
mixed = [{"role": "assistant", "content": [
    {"type": "text", "text": "我先读一下。"},
    {"type": "tool_use", "id": "t1", "name": "read_segments", "input": {"segments": ["S1·1"]}}]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "A"}]}]
g = roundtrip(mixed)
check("tool_use + 正文混排：正文保留、tool_calls 也在",
      g[0]["content"] == "我先读一下。" and len(g[0]["tool_calls"]) == 1,
      json.dumps(g[0], ensure_ascii=False))

# tool_result + text 混排（额度用尽那条提示就是这个形态）
budget = [{"role": "assistant", "content": [
    {"type": "tool_use", "id": "t1", "name": "read_segments", "input": {}}]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "A"},
        {"type": "text", "text": "工具调用次数已用尽。请基于已获得的信息给出最终结果。"}]}]
g = roundtrip(budget)
check("tool_result 与 text 混排：text 排在 tool 消息**之后**",
      [m["role"] for m in g[1:]] == ["tool", "user"]
      and "已用尽" in g[2]["content"],
      f"角色序列: {[m['role'] for m in g]}")

# tool_result 的 content 是块数组时也要能处理
arr = [{"role": "assistant", "content": [
    {"type": "tool_use", "id": "t1", "name": "x", "input": {}}]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": [{"type": "text", "text": "块里的正文"}]}]}]
g = roundtrip(arr)
check("tool_result.content 是内容块数组时拼成字符串",
      len(g) == 2 and g[1]["role"] == "tool"
      and g[1]["content"] == "块里的正文",
      f"{len(g)} 条消息｜{g[1]['role']}: {g[1]['content']!r}")

# 空输入
check("空 messages / 空内容不炸", roundtrip([]) == []
      and roundtrip([{"role": "user", "content": None}])
      == [{"role": "user", "content": None}])

# ─────────────────────── ② 在线：真跑两轮 ───────────────────────
ap = argparse.ArgumentParser()
ap.add_argument("--offline", action="store_true")
args = ap.parse_args()

if args.offline:
    print("\n跳过在线部分（--offline）")
else:
    print("\n=== ② 在线：真跑两轮工具循环（这一段才算数）===")
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    from research_agent.llm.provider import LLMClient  # noqa: E402

    TOOLS = [{"name": "echo_segments",
              "description": "回显请求读取的段号（测试用假工具）",
              "input_schema": {"type": "object", "properties": {
                  "segments": {"type": "array", "items": {"type": "string"}}},
                  "required": ["segments"]}}]

    class FakeProvider:
        def specs(self): return TOOLS
        def dispatch(self, calls):
            return [{"type": "tool_result", "tool_use_id": c.get("id"),
                     "content": f"已取回 {json.dumps(c.get('input'), ensure_ascii=False)}"}
                    for c in calls]

    from research_agent.llm.toolloop import run_tool_loop  # noqa: E402

    client = LLMClient()
    cfg = client.registry.stage("write_section")
    print(f"   {cfg.provider} / {cfg.model} / effort={cfg.reasoning_effort} "
          f"/ stream={cfg.stream}\n")
    try:
        text, msgs, log = run_tool_loop(
            client, "write_section",
            "你是写作子 agent。",
            [{"role": "user",
              "content": "请先调用 echo_segments 读取 S1·1 和 S2·3，"
                         "拿到之后再回答，不要凭空作答。"}],
            FakeProvider(), max_rounds=2)
        rounds_with_calls = sum(1 for r in log if r)
        check("两轮工具循环跑通（第二轮不再 400）",
              len(log) >= 2 and text.strip(),
              f"共 {len(log)} 轮，其中 {rounds_with_calls} 轮发了工具调用｜"
              f"最终正文 {len(text)} 字：{text[:80]!r}")
        check("确实发生过「回传工具结果」的轮次（这是之前 400 的那一轮）",
              rounds_with_calls >= 1,
              f"每轮工具调用数: {[len(r) for r in log]}")
    except Exception as e:
        check("两轮工具循环跑通", False, f"{type(e).__name__}: {str(e)[:400]}")

print("\n" + "=" * 60)
print(f"失败 {len(fails)} 项" + ("：" + "、".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
