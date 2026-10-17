"""guard 语义（2026-10-17 改）的守门测试。

改动前：空返回/截断 → **翻倍预算**重试
改动后：空返回/截断 → **降 reasoning_effort**，预算**一动不动**

为什么方向变了
--------------
`max_tokens` 是思考和正文的**共享天花板**，思考吃满 ⇒ 正文必然为 0
（实测 `write_plan` @16,384：reasoning_tokens 恰好 16,384、正文 0 字）。
所以「撞了就加钱」是把墙抬高、让模型想得更远再撞一次 ——
实测 16,384→65,536 之后思考量跟着涨到 26,002 才开始自然停止。

**这次要守住的两个不变量**：
  1. 重试时 `max_tokens` **必须不变**（不能再有 `budget * 2`）
  2. 重试时 `reasoning_effort` **必须降档**（high→medium→low）

离线桩测，不打网络。用法：.venv/bin/python tests/test_guard_effort_ladder.py
"""

from __future__ import annotations

import json as json_module
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# provider 启动时要 `api_key_env` 有值（`_headers` / `_opencode` 都会读），
# 但本测试全程是桩、不打网络，所以这里只需要「有值」这个事实。
os.environ.setdefault("INTERN_AI_API_KEY", "stub-not-used")

from research_agent.llm.provider import (  # noqa: E402
    LLMClient, _lower_effort,
)

fails: list[str] = []


def check(tag: str, ok: bool, detail: str = "") -> None:
    print(f"{'✅' if ok else '❌'} {tag}" + (f"\n   {detail}" if detail else ""))
    if not ok:
        fails.append(tag)


class FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload)
        self.headers = {}

    def json(self):
        return self._payload

    def iter_lines(self):
        # ⚠️ **流式形状**：`SSEAccum`/`_sse_lines` 读的是 `choices[].delta`，
        # 不是非流式的 `choices[].message`。桩一开始发成了 message，
        # 于是流式分支永远收不到 content、每次都判「空返回」——
        # **症状和真 bug 一模一样**（这正是本项目反复栽的那类：形状不对但静默）。
        ch = self._payload["choices"][0]
        delta = {"role": "assistant",
                 "content": ch.get("message", {}).get("content"),
                 "reasoning_content": ch.get("message", {}).get("reasoning_content")}
        yield f"data: {json.dumps({'choices': [{'delta': delta}]})}"
        if self._payload.get("usage"):
            yield f"data: {json.dumps({'usage': self._payload['usage']})}"
        if ch.get("finish_reason"):
            yield f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': ch['finish_reason']}]})}"
        yield "data: [DONE]"

    def read(self):
        return self.text.encode()


class FakeStreamCtx:
    def __init__(self, payload: dict) -> None:
        self._r = FakeResp(payload)

    def __enter__(self):
        return self._r

    def __exit__(self, *a):
        return False


class FakeClient:
    """按脚本依次返回预设响应，并记录每次实际发出的 payload。"""

    def __init__(self, script: list[dict]) -> None:
        self.script = list(script)
        self.sent: list[dict] = []

    def _next(self, payload: dict) -> FakeResp:
        self.sent.append(json_module.loads(json_module.dumps(payload)))
        return FakeResp(self.script.pop(0) if self.script else {"choices": []})

    def post(self, url, headers=None, json=None, timeout=None):
        return self._next(json)

    def stream(self, method, url, headers=None, json=None, timeout=None):
        # ⚠️ 这里**必须**走 `_next` 取脚本里的下一个响应。
        # 第一版直接 `FakeStreamCtx(json)`，把**请求** payload 当成了响应 ——
        # 于是响应里没有 `choices`，`KeyError` 冒出来，症状完全不像桩的问题。
        self.sent.append(json_module.loads(json_module.dumps(json)))
        return FakeStreamCtx(self.script.pop(0) if self.script else {"choices": []})


def ok_resp(text: str = "有内容") -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text,
                                     "reasoning_content": ""},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50}}


def empty_resp(completion_tokens: int = 16384) -> dict:
    """空返回：思考吃满、正文 0。"""
    return {"choices": [{"message": {"role": "assistant", "content": "",
                                     "reasoning_content": "想了很久"},
                         "finish_reason": "length"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": completion_tokens,
                      "completion_tokens_details": {"reasoning_tokens": completion_tokens}}}


print("=== ① effort 降档梯子 ===")
check("high → medium → low → None",
      [_lower_effort("high"), _lower_effort("medium"), _lower_effort("low")] == ["medium", "low", None])
check("disabled/none 不再往下走（它们不是可靠的『关思考』）",
      _lower_effort("disabled") is None and _lower_effort("none") is None)

print("\n=== ② 非流式：预算不变、effort 降档 ===")
llm = LLMClient()
llm._client = FakeClient([empty_resp(), ok_resp()])
txt, _ = llm._opencode(llm.registry.stage("write_plan_tasks"), "提示词", None, True)
sent = llm._client.sent
check("两次都发出去了", len(sent) == 2, f"实际 {len(sent)} 次")
if len(sent) == 2:
    check("① max_tokens 两次完全相同（不再翻倍）",
          sent[0]["max_tokens"] == sent[1]["max_tokens"],
          f"{sent[0]['max_tokens']} → {sent[1]['max_tokens']}"
          f"（基线 {llm.registry.stage('write_plan_tasks').max_output_tokens}）")
    check("② reasoning_effort high → medium",
          sent[0].get("reasoning_effort") == "high"
          and sent[1].get("reasoning_effort") == "medium",
          f"{sent[0].get('reasoning_effort')} → {sent[1].get('reasoning_effort')}")
check("② 降档后拿到内容了", txt.strip() == "有内容", repr(txt[:30]))

print("\n=== ③ 梯子走完就停，不无限重试 ===")
llm2 = LLMClient()
# high → medium → low 三次全空：梯子到底后必须停
llm2._client = FakeClient([empty_resp(), empty_resp(), empty_resp(), empty_resp()])
try:
    llm2._opencode(llm2.registry.stage("write_plan_tasks"), "提示词", None, True)
    check("③ 梯子走完后抛错（而不是无限重试）", False, "居然返回了")
except Exception as e:
    efforts = [p.get("reasoning_effort") for p in llm2._client.sent]
    budgets = {p["max_tokens"] for p in llm2._client.sent}
    check("③ 梯子走完后抛错（而不是无限重试）", True,
          f"{type(e).__name__}: {str(e)[:90]}")
    check("③ 预算全程只有一个值",
          len(budgets) == 1, f"出现过的 max_tokens: {budgets}")
    check("③ effort 依次 high→medium→low",
          efforts[:3] == ["high", "medium", "low"], f"实际 {efforts}")
    check("③ 最多 3 次（梯子长度），不是 retry_on_empty 之外再多试",
          len(llm2._client.sent) <= 3, f"实际 {len(llm2._client.sent)} 次")

print("\n=== ④ 流式分支同规则 ===")
llm3 = LLMClient()
llm3._client = FakeClient([empty_resp(), empty_resp(), ok_resp()])
cfg = llm3.registry.stage("write_plan_tasks")
llm3.registry._cache["write_plan_tasks"].stream = True   # 临时开流式
txt3, _ = llm3._opencode(cfg, "提示词", None, True)
s3 = llm3._client.sent
check("④ 流式两次尝试后成功", txt3.strip() == "有内容", repr(txt3[:30]))
check("④ 流式预算也不变",
      len({p["max_tokens"] for p in s3}) == 1,
      f"出现过的 max_tokens: {[p['max_tokens'] for p in s3]}")
check("④ 流式 effort 降档",
      [p.get("reasoning_effort") for p in s3][:2] == ["high", "medium"],
      f"{[p.get('reasoning_effort') for p in s3]}")

print("\n=== ⑤ 非流式成功路径不触发重试 ===")
llm4 = LLMClient()
llm4._client = FakeClient([ok_resp("一次就成")])
t4, _ = llm4._opencode(llm4.registry.stage("write_plan_tasks"), "提示词", None, True)
check("⑤ 一次成功就返回", t4 == "一次就成" and len(llm4._client.sent) == 1,
      f"{t4!r}，{len(llm4._client.sent)} 次调用")

print("\n" + "=" * 60)
print(f"失败 {len(fails)} 项" + ("：" + "、".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
