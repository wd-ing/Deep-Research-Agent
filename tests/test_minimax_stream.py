"""`_minimax` 的**流式路径**（2026-10-17）。

## 为什么要这个文件

`_minimax` 打的是 Anthropic Messages 端点，以前**只实现非流式**，
于是配置里 8 个 `stream: true` 的环节全部静默降级 —— 配置说有流式，
实际没有，不报错、不告警（与坑 47「限流器 key 不匹配」同一形态：
装上了、看着在用、其实从没跑过）。

流式的**报文形状完全不同于非流式**：一串 `event`/`data` 对，
而不是一个 JSON 对象。所以很容易出现「非流式那一半补了、工具那一半没补」。
本文件把两条路都钉住。

## 桩的要求

`_minimax_once` 按 `payload["stream"]` 决定走 `.stream()` 还是 `.post()`，
所以**两个方法都要桩，且必须记录走的是哪一个** ——
只桩一个方法的话，测试会走到「另一条路」，而结果**长得一模一样**
（正文照样拿到了），测试照样绿。这正是本项目反复栽的那类。

provider **钉死 minimax**：不跟着 `defaults` 漂（坑 47/50）。

用法：.venv/bin/python tests/test_minimax_stream.py
"""

from __future__ import annotations

import copy
import json as json_module
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _isolate  # noqa: F401, E402
os.environ.setdefault("MINIMAX_API_KEY", "test-key-not-real")   # 桩，不打网络

from research_agent.llm.provider import (  # noqa: E402
    AnthropicSSEAccum, EmptyOutput, LLMClient,
)

fails: list[str] = []


def check(tag: str, ok: bool, detail: str = "") -> None:
    print(f"{'✅' if ok else '❌'} {tag}" + (f"\n   {detail}" if detail else ""))
    if not ok:
        fails.append(tag)


# ───────────────────────── 累加器（纯离线） ─────────────────────────
print("=== ① AnthropicSSEAccum 形状 ===")

a = AnthropicSSEAccum()
for f in [
    {"type": "message_start", "message": {"usage": {"input_tokens": 43, "output_tokens": 0}}},
    {"type": "ping"},                       # 实测有，必须被忽略而不是炸掉
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "固态"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "电池"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
     "usage": {"output_tokens": 42}},
    {"type": "message_stop"},
]:
    a.feed(f)
check("① 关思考：正文拼回来了", a.text_only() == "固态电池",
      f"{a.text_only()!r}")
check("① stop_reason 抓到了", a.stop_reason == "end_turn", repr(a.stop_reason))
check("① usage 里 output_tokens 取的是 message_delta 的最终值",
      a.usage.get("output_tokens") == 42 and a.usage.get("input_tokens") == 43,
      str(a.usage))

# ⚠️ 实测里 thinking 在 index 0、text 在 index 1。
# 「取 index 0 的 text」在这里会拿到空串，而且**不报错**。
b = AnthropicSSEAccum()
for f in [
    {"type": "content_block_start", "index": 0,
     "content_block": {"type": "thinking", "thinking": ""}},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "thinking_delta", "thinking": "先想"}},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "正文"}},
]:
    b.feed(f)
bl = b.blocks()
check("① thinking 与 text 分处两个 index，两个都要拿到",
      [x["type"] for x in bl] == ["thinking", "text"]
      and bl[0]["thinking"] == "先想" and bl[1]["text"] == "正文",
      str(bl))

c = AnthropicSSEAccum()
for f in [
    {"type": "content_block_start", "index": 0,
     "content_block": {"type": "tool_use", "id": "t1", "name": "submit_tasks"}},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "input_json_delta", "partial_json": '{"tasks": [{"ti'}},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "input_json_delta", "partial_json": 'tle": "A"}]}'}},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
     "usage": {"output_tokens": 47}},
]:
    c.feed(f)
tb = c.blocks()
check("① 工具参数分片拼成合法 JSON",
      len(tb) == 1 and tb[0]["type"] == "tool_use"
      and tb[0]["input"] == {"tasks": [{"title": "A"}]} and not tb[0].get("_args_broken"),
      str(tb))

d = AnthropicSSEAccum()
for f in [
    {"type": "content_block_start", "index": 0,
     "content_block": {"type": "tool_use", "id": "t1", "name": "submit_tasks"}},
    # 半截 JSON —— 被 max_tokens 切断时的真实样子
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "input_json_delta", "partial_json": '{"tasks": [{"title"'}},
    {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}},
]:
    d.feed(f)
db = d.blocks()
check("① 半截参数打 _args_broken（不许冒充 {})",
      db and db[0].get("_args_broken") is True and db[0]["input"] == {},
      str(db))

e = AnthropicSSEAccum()
e.feed({"type": "error", "error": {"type": "overloaded_error", "message": "炸了"}})
check("① error 事件被记下来（不许吞）", bool(e.error), str(e.error))

f_acc = AnthropicSSEAccum()
f_acc.feed({"type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""}})
check("① 空流（只有 start 没有 delta）不炸，text 为空",
      f_acc.text_only() == "" and len(f_acc.blocks()) == 0)


# ───────────────────────── 走哪条路 ─────────────────────────
print("\n=== ② `_minimax` 按配置选路，而不是无视配置 ===")


def mm_events(text="一段正文", stop="end_turn", out=1000, thinking=False):
    """拼成一串 Anthropic SSE 的 `data:` 行（实测形状）。"""
    ev = [{"type": "message_start",
           "message": {"usage": {"input_tokens": 500, "output_tokens": 0}}}]
    idx = 0
    if thinking:
        ev.append({"type": "content_block_start", "index": idx,
                   "content_block": {"type": "thinking", "thinking": ""}})
        ev.append({"type": "content_block_delta", "index": idx,
                   "delta": {"type": "thinking_delta", "thinking": "想"}})
        idx += 1
    ev.append({"type": "content_block_start", "index": idx,
               "content_block": {"type": "text", "text": ""}})
    ev.append({"type": "content_block_delta", "index": idx,
               "delta": {"type": "text_delta", "text": text}})
    ev.append({"type": "content_block_stop", "index": idx})
    ev.append({"type": "message_delta", "delta": {"stop_reason": stop},
               "usage": {"output_tokens": out}})
    ev.append({"type": "message_stop"})
    return ev


class FakeStreamCtx:
    def __init__(self, events, status=200, body=""):
        self.events = events
        self.status_code = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self):
        for ev in self.events:
            # Anthropic 的报文是 `event:` + `data:` 两行一组的；
            # `_sse_lines` 只读 `data:`，所以这里两行都给
            yield f"event: {ev.get('type')}"
            yield f"data: {json_module.dumps(ev, ensure_ascii=False)}"
        yield "data: [DONE]"

    def read(self):
        # ⚠️ 非 200 时 `_minimax_once` 靠 `read()` 取错误正文。
        # 第一版这里返回空串，于是 RuntimeError 变成「MiniMax 400: 」
        # —— **断言测不到东西，却看起来像是代码的问题**。
        # 桩的形状不对，报文就会缺一块（坑：桩要接在正确的层上）。
        return self._body.encode()


class FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.text = json_module.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._p


class FakeClient:
    """两个方法都桩，并记录**走的是哪一个**。

    ⚠️ 少桩一个方法 → 测试会静默走到另一条路，而返回结果长得一样，
    测试照样绿。所以 `calls` 必须记下走法。
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[str] = []      # "post" / "stream"
        self.sent: list[dict] = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append("post")
        self.sent.append(json_module.loads(json_module.dumps(json)))
        item = self.script.pop(0) if self.script else {}
        return FakeResp(item.get("body", {"content": []}), item.get("status", 200))

    def stream(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append("stream")
        self.sent.append(json_module.loads(json_module.dumps(json)))
        item = self.script.pop(0) if self.script else {}
        return FakeStreamCtx(item.get("events", []), item.get("status", 200),
                               json_module.dumps(item.get("body", {}), ensure_ascii=False))


def pin(llm, stage):
    cfg = copy.copy(llm.registry.stage(stage))
    cfg.provider = "minimax"
    cfg.base_url = "https://api.minimaxi.com/anthropic"
    cfg.api_key_env = "MINIMAX_API_KEY"
    llm.registry._cache[stage] = cfg
    return cfg


llm = LLMClient()
cfg = pin(llm, "write_section")
cfg.stream = True
llm._client = FakeClient([{"events": mm_events("流式拿到的正文", out=2100)}])
txt, usage = llm._minimax(cfg, "提示词", None, False)
check("② stream:true 真的走了 .stream()",
      llm._client.calls == ["stream"], f"实走 {llm._client.calls}")
check("② 流式正文拿到了", txt == "流式拿到的正文", repr(txt))
check("② 请求里带上了 stream:true",
      llm._client.sent[0].get("stream") is True, str(llm._client.sent[0].get("stream")))
check("② usage 带回了 output_tokens（记账/截断判断要用）",
      usage.get("output_tokens") == 2100, str(usage))

llm2 = LLMClient()
cfg2 = pin(llm2, "write_section")
cfg2.stream = False
llm2._client = FakeClient([{"body": {"content": [{"type": "text", "text": "非流式正文"}],
                                    "stop_reason": "end_turn",
                                    "usage": {"input_tokens": 5, "output_tokens": 9}}}])
txt2, _ = llm2._minimax(cfg2, "提示词", None, False)
check("② stream:false 仍然走 .post()（没被顺手改成流式）",
      llm2._client.calls == ["post"], f"实走 {llm2._client.calls}")
check("② 非流式没被破坏", txt2 == "非流式正文", repr(txt2))


# ───────────────────────── 流式下的截断 ─────────────────────────
print("\n=== ③ 流式下截断检测照样生效 ===")
llm3 = LLMClient()
cfg3 = pin(llm3, "write_section")
cfg3.stream = True
# write_section 的 base 已与 guard 上限同为 16384（批 4），翻倍无处可翻。
# 这里压低 base 测「翻倍机制」本身，不是钉配置值。
cfg3.max_output_tokens = 4096
llm3._client = FakeClient([
    {"events": mm_events("写到一半", stop="max_tokens", out=16384)},
    {"events": mm_events("这次写完了", out=2100)},
])
txt3, _ = llm3._minimax(cfg3, "提示词", None, False)
check("③ 流式被截断的那次被重试了", len(llm3._client.calls) == 2,
      f"实发 {len(llm3._client.calls)} 次")
check("③ 最后拿到完整的那次", txt3 == "这次写完了", repr(txt3))
if len(llm3._client.sent) == 2:
    check("③ 截断后预算翻倍",
          llm3._client.sent[1]["max_tokens"] > llm3._client.sent[0]["max_tokens"],
          f"{llm3._client.sent[0]['max_tokens']} → {llm3._client.sent[1]['max_tokens']}")

llm4 = LLMClient()
cfg4 = pin(llm4, "write_section")
cfg4.stream = True
llm4._client = FakeClient([{"events": mm_events("半截", stop="max_tokens", out=16384)}] * 3)
try:
    got, _ = llm4._minimax(cfg4, "提示词", None, False)
    check("③ 流式全程截断必须显式失败", False, f"居然返回了 {got[:30]!r}")
except EmptyOutput as e:
    check("③ 流式全程截断必须显式失败", True, f"EmptyOutput: {str(e)[:90]}")

# 流式 HTTP 错误：不能变成「流到一半没下文」
# ⚠️ 分两种：可退避的（429）和不可退避的（400）。
#    上一版只给了一份脚本，429 被退避重试两次后以 EmptyOutput 收场，
#    断言「消息里含 429」就挂了 —— 那是在断言一个**重试后**的结果。
llm5 = LLMClient()
cfg5 = pin(llm5, "write_section")
cfg5.stream = True
llm5._client = FakeClient([{"status": 400, "body": {"error": "bad request"}}] * 5)
try:
    llm5._minimax(cfg5, "提示词", None, False)
    check("③ 流式遇到不可退避的 HTTP 错误必须立刻响", False, "居然成功了")
except RuntimeError as e:
    check("③ 流式遇到不可退避的 HTTP 错误必须立刻响",
          "400" in str(e) and "bad request" in str(e),
          f"{type(e).__name__}: {str(e)[:100]}")
except Exception as e:
    check("③ 流式遇到不可退避的 HTTP 错误必须立刻响", False,
          f"抛的是 {type(e).__name__}：{str(e)[:90]}")

llm5b = LLMClient()
cfg5b = pin(llm5b, "write_section")
cfg5b.stream = True
llm5b._client = FakeClient([{"status": 429, "body": {"error": "rate limited"}},
                            {"events": mm_events("退避后拿到了", out=2100)}])
t5b, _ = llm5b._minimax(cfg5b, "提示词", None, False)
check("③ 流式撞 429 会退避重试并恢复",
      t5b == "退避后拿到了" and len(llm5b._client.calls) == 2,
      f"{t5b[:20]!r}，实发 {len(llm5b._client.calls)} 次")


# ───────────────────────── 工具那一半也得走流式 ─────────────────────────
print("\n=== ④ `_minimax_tools` 同样接上流式 ===")
TOOL = [{"name": "submit_tasks", "description": "d",
         "input_schema": {"type": "object"}}]


def tool_events(truncated=False, broken=None):
    """`truncated` → `stop_reason=max_tokens`；
    `broken` → 最后一片是半截（拼出来不是合法 JSON）。**两者独立**：
    截断但 JSON 完整 ⇒ 只告警；截断且 JSON 坏了 ⇒ 必须响。"""
    if broken is None:
        broken = truncated
    second = 'itle": "A"' if broken else 'itle": "A"}]}'
    return [
        {"type": "message_start", "message": {"usage": {"input_tokens": 300}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "t1", "name": "submit_tasks"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"tasks": [{"t'}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": second}},
        {"type": "message_delta",
         "delta": {"stop_reason": "max_tokens" if truncated else "tool_use"},
         "usage": {"output_tokens": 65536 if truncated else 500}},
        {"type": "message_stop"},
    ]


llm6 = LLMClient()
cfg6 = pin(llm6, "write_plan_tasks")
cfg6.stream = True
llm6._client = FakeClient([{"events": tool_events(False)}])
blocks6, usage6 = llm6._minimax_tools(cfg6, "sys", [{"role": "user", "content": "x"}],
                                      TOOL, tool_choice="submit_tasks")
check("④ 工具路径 stream:true 走 .stream()",
      llm6._client.calls == ["stream"], f"实走 {llm6._client.calls}")
check("④ 工具块拼回来了",
      len(blocks6) == 1 and blocks6[0]["name"] == "submit_tasks"
      and blocks6[0]["input"] == {"tasks": [{"title": "A"}]}, str(blocks6))
check("④ 正常工具调用不误报截断",
      not any("截断" in w for w in llm6.warnings), str(llm6.warnings))

# 截断但 JSON 完整 → 只告警（抛异常会把一份可用的任务表变成整环节失败）
llm7 = LLMClient()
cfg7 = pin(llm7, "write_plan_tasks")
cfg7.stream = True
llm7._client = FakeClient([{"events": tool_events(truncated=True, broken=False)}])
blocks7, _ = llm7._minimax_tools(cfg7, "sys", [{"role": "user", "content": "x"}],
                                 TOOL, tool_choice="submit_tasks")
check("④ 流式截断但参数完整 → 告警且仍交出结果",
      any("截断" in w for w in llm7.warnings) and len(blocks7) == 1,
      f"warnings={llm7.warnings}｜blocks={blocks7}")

# 截断且 JSON 坏了 → `_args_broken` ⇒ 必须响，不许给 {}
llm7b = LLMClient()
cfg7b = pin(llm7b, "write_plan_tasks")
cfg7b.stream = True
llm7b._client = FakeClient([{"events": tool_events(truncated=True, broken=True)}])
try:
    llm7b._minimax_tools(cfg7b, "sys", [{"role": "user", "content": "x"}],
                         TOOL, tool_choice="submit_tasks")
    check("④ 半截工具参数必须响（_reject_broken_args）", False, "居然成功了")
except EmptyOutput as e:
    check("④ 半截工具参数必须响（_reject_broken_args）", True, str(e)[:100])
except Exception as e:
    check("④ 半截工具参数必须响（_reject_broken_args）", False,
          f"抛的是 {type(e).__name__}：{str(e)[:90]}")

llm8 = LLMClient()
cfg8 = pin(llm8, "write_plan_tasks")
cfg8.stream = False
llm8._client = FakeClient([{"body": {"content": [{"type": "tool_use", "id": "t1",
                                                 "name": "submit_tasks",
                                                 "input": {"tasks": []}}],
                                    "stop_reason": "tool_use",
                                    "usage": {"output_tokens": 12}}}])
llm8._minimax_tools(cfg8, "sys", [{"role": "user", "content": "x"}], TOOL,
                    tool_choice="submit_tasks")
check("④ 工具路径 stream:false 仍走 .post()",
      llm8._client.calls == ["post"], f"实走 {llm8._client.calls}")

print("\n" + "=" * 60)
print(f"失败 {len(fails)} 项" + ("：" + "、".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
