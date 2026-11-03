"""`_minimax` 上的**截断检测**（2026-10-17 随换 provider 一起挖出来的）。

## 这个 bug 长什么样

切到 MiniMax 之后发现的，形状和本项目消灭过的那些一模一样——**静默**：

`_is_truncated` 原来只读两个字段，都只认 **OpenAI 形状**：

    finish_reason == "length"        （Anthropic 叫 "max_tokens"）
    usage["completion_tokens"]       （Anthropic 叫 "output_tokens"）

于是它在 MiniMax 上**恒返回 False**。更要命的是 `_minimax` 压根没调它——
原来只判 `if not text`，于是被 `max_tokens` 切断的正文
（`write_section` / `write_audit` 写到句子中间断掉）被当成**成功返回**：

    截断的正文 → return text   →  报告少一截  →  degraded: false  →  不报错

`_openai_compat` 两个分支里这道防线是有的，切 provider 时整段漏掉了。
所以本文件测两件事，缺一个都不算数：

    ① `_is_truncated` 认不认 Anthropic 形状的字段名
    ② `_minimax` **有没有真的调它**（①对了②没接上，一样静默）

## 桩的要求

`_minimax` 只走 `.post`（不实现流式），所以桩 `.post` 就够；
provider **钉死成 minimax**——不跟着 `defaults` 漂，
否则换默认 provider 时这个测试会静默变成测别的东西（坑 47/50 的同一条）。

用法：.venv/bin/python tests/test_minimax_truncation.py
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
    EmptyOutput, LLMClient, _is_truncated,
)

fails: list[str] = []


def check(tag: str, ok: bool, detail: str = "") -> None:
    print(f"{'✅' if ok else '❌'} {tag}" + (f"\n   {detail}" if detail else ""))
    if not ok:
        fails.append(tag)


class FakeResp:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.text = json_module.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class FakeClient:
    """按脚本依次返回预设响应，并记录实际发出的 payload。"""

    def __init__(self, script: list[dict]) -> None:
        self.script = list(script)
        self.sent: list[dict] = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.sent.append(json_module.loads(json_module.dumps(json)))
        return FakeResp(self.script.pop(0) if self.script else {"content": []})

    def stream(self, method, url, headers=None, json=None, timeout=None):
        raise AssertionError("`_minimax` 不该走流式——它只实现 `.post`")


def mm_text(text: str, stop_reason: str = "end_turn",
            out_tokens: int = 1000) -> dict:
    """Anthropic 形状的一次成功响应。字段名照 2026-10-17 实测报文。"""
    return {"stop_reason": stop_reason,
            "content": [{"type": "thinking", "thinking": ""},
                        {"type": "text", "text": text}],
            "usage": {"input_tokens": 500, "output_tokens": out_tokens}}


def mm_truncated(text: str, out_tokens: int = 8192) -> dict:
    """被 max_tokens 切断：正文有、但没写完。**这是最危险的那种失败。**"""
    return mm_text(text, stop_reason="max_tokens", out_tokens=out_tokens)


def pin_minimax(llm: LLMClient, stage: str):
    """把这个 stage 钉在 minimax 上——不依赖环境默认 provider。

    `stream` **显式关掉**：本文件测的是截断判断，而 `write_section` /
    `relevance` 在配置里是 `stream: true`，接上流式后 `_minimax` 会走
    `.stream()` —— 本文件的桩只有 `.post`，会直接 AssertionError。
    流式路径的截断检测由 `tests/test_minimax_stream.py` ③ 覆盖
    （同样的判定，走的是同一段代码）。
    """
    cfg = copy.copy(llm.registry.stage(stage))
    cfg.provider = "minimax"
    cfg.base_url = "https://api.minimaxi.com/anthropic"
    cfg.api_key_env = "MINIMAX_API_KEY"
    cfg.stream = False
    # 2026-11-04：环节迁 glm 后 guard.max_output_tokens == max_output_tokens
    # （16384=16384），② 的「截断后预算翻倍」没有余量可翻。
    # guard 浅拷贝再放大，不污染共享对象，也不依赖线上配置的余量。
    cfg.guard = copy.copy(cfg.guard)
    cfg.guard.max_output_tokens = cfg.max_output_tokens * 4
    llm.registry._cache[stage] = cfg
    return cfg


# ─────────────────────────── ① 字段名 ───────────────────────────
print("=== ① `_is_truncated` 认 Anthropic 形状 ===")
anth = [
    ("stop_reason=max_tokens（Anthropic 的截断标记）",
     {"output_tokens": 3000}, "max_tokens", 8192, True),
    ("output_tokens 顶满上限（平台不报 stop_reason）",
     {"output_tokens": 8192}, "end_turn", 8192, True),
    ("output_tokens 差 1 token", {"output_tokens": 8191}, "end_turn", 8192, False),
    ("正常 end_turn + 用量不高", {"output_tokens": 2100}, "end_turn", 8192, False),
    ("tool_use 正常结束", {"output_tokens": 66}, "tool_use", 8192, False),
    ("output_tokens 是 None", {"output_tokens": None}, "end_turn", 8192, False),
    # ⚠️ budget=0 只该让**用量兜底**失效，不能推翻平台明说的截断标记 ——
    #    `finish_reason=length` 在老形状里同样是先于 budget 判定返回 True。
    ("budget=0 + 平台明说 max_tokens → 仍然判截断",
     {"output_tokens": 99999}, "max_tokens", 0, True),
    ("budget=0 + 无标记 → 不误判（兜底失效）",
     {"output_tokens": 99999}, "end_turn", 0, False),
]
for tag, usage, stop, budget, want in anth:
    got = _is_truncated(usage, stop, budget)
    check(tag, got == want, f"期望 {want}｜实得 {got}")

# 老字段名不能被这次修改弄坏（信通院 / xj189 还在用）
check("老形状没被弄坏：finish_reason=length 仍然判截断",
      _is_truncated({"completion_tokens": 100}, "length", 8192) is True)
check("老形状没被弄坏：completion_tokens 顶满仍判截断",
      _is_truncated({"completion_tokens": 8192}, None, 8192) is True)
check("老形状没被弄坏：正常完成不判截断",
      _is_truncated({"completion_tokens": 3966}, "stop", 8192) is False)

# ─────────────────────────── ② 真的接上了吗 ───────────────────────────
print("\n=== ② `_minimax` 有没有真的调 `_is_truncated` ===")
HALF = "固态电池的核心困难在于固态电解质难以同时兼顾高离子电导率、良好的界面接触"

llm = LLMClient()
cfg = pin_minimax(llm, "write_section")
llm._client = FakeClient([mm_truncated(HALF), mm_text("这次写完了。")])
txt, _ = llm._minimax(cfg, "提示词", None, False)
sent = llm._client.sent
check("② 截断的那次没有被当成成功（重试了）",
      len(sent) == 2, f"实际发出 {len(sent)} 次")
check("② 最后拿到的是完整的那次", txt == "这次写完了。", repr(txt[:40]))
if len(sent) == 2:
    check("② 截断后预算翻倍（`_minimax` 保留的是翻倍，不是降 effort）",
          sent[1]["max_tokens"] > sent[0]["max_tokens"],
          f"{sent[0]['max_tokens']} → {sent[1]['max_tokens']}，"
          f"上限 guard.max_output_tokens={cfg.guard.max_output_tokens}")

# 🔴 这条是本测试存在的理由：**没有它，上面两条可以在 `_minimax`
# 根本没调 `_is_truncated` 的情况下照样通过**（返回值完全一样）。
llm_b = LLMClient()
cfg_b = pin_minimax(llm_b, "write_section")
llm_b._client = FakeClient([mm_truncated(HALF), mm_truncated(HALF),
                            mm_truncated(HALF)])
try:
    got, _ = llm_b._minimax(cfg_b, "提示词", None, False)
    check("🔴 全程截断必须显式失败，不许把半截正文交出去",
          False, f"居然返回了 {len(got)} 字符：{got[:40]!r}")
except EmptyOutput as e:
    check("🔴 全程截断必须显式失败，不许把半截正文交出去", True,
          f"EmptyOutput: {str(e)[:110]}")
except Exception as e:
    check("🔴 全程截断必须显式失败，不许把半截正文交出去", False,
          f"抛的是 {type(e).__name__} 而不是 EmptyOutput：{str(e)[:90]}")

# 错误文案要说清是「截断」不是「空返回」——两者根因不同，处置也不同。
# ⚠️ 这里必须**重新填一遍脚本**：上一版复用了 llm_b，脚本早被取空，
#   于是 `last_reason` 其实是 `empty`，而文案里那句固定解释
#   （"`truncated` = 顶到 max_tokens 被切断"）**始终含 "truncated"**，
#   检查照样通过 —— 断言蒙对了，一个字都没验到。
llm_b2 = LLMClient()
cfg_b2 = pin_minimax(llm_b2, "write_section")
llm_b2._client = FakeClient([mm_truncated(HALF)] * 3)
try:
    llm_b2._minimax(cfg_b2, "提示词", None, False)
    check("文案点明最后原因是 truncated 而不是 empty", False, "居然成功了")
except EmptyOutput as e:
    msg = str(e)
    check("文案点明最后原因是 truncated 而不是 empty",
          "最后原因 truncated" in msg and "最后原因 empty" not in msg,
          msg[:150])
except Exception as e:
    check("文案点明最后原因是 truncated 而不是 empty", False,
          f"抛的是 {type(e).__name__}：{str(e)[:90]}")

# ─────────────────────────── ③ 正常路径不许被误伤 ───────────────────────────
print("\n=== ③ 正常返回不触发重试 ===")
llm3 = LLMClient()
cfg3 = pin_minimax(llm3, "write_section")
llm3._client = FakeClient([mm_text("一段完整的小节正文。", out_tokens=2100)])
t3, _ = llm3._minimax(cfg3, "提示词", None, False)
check("③ 一次就成，不重试",
      t3 == "一段完整的小节正文。" and len(llm3._client.sent) == 1,
      f"{t3[:24]!r}，{len(llm3._client.sent)} 次")

# 关思考 + 小输出：M3 上最常见的形态，绝不能被当截断
llm4 = LLMClient()
cfg4 = pin_minimax(llm4, "relevance")
llm4._client = FakeClient([mm_text("短输出", out_tokens=42)])
t4, _ = llm4._minimax(cfg4, "提示词", None, True)
check("③ 关思考的小输出正常返回",
      t4 == "短输出" and len(llm4._client.sent) == 1,
      f"{t4[:24]!r}，{len(llm4._client.sent)} 次")

# ─────────────────────────── ④ 工具侧的截断 ───────────────────────────
print("\n=== ④ `_minimax_tools` 工具参数被截断 → 必须告警 ===")
print("   （这条**故意不硬失败**：Anthropic 返回的是已解析对象，"
      "JSON 永远合法，截断只表现为少几个字段——抛异常会把"
      "一份可用的任务表变成整环节失败）")


def mm_tool(truncated: bool) -> dict:
    return {"stop_reason": "max_tokens" if truncated else "tool_use",
            "content": [{"type": "tool_use", "id": "t1", "name": "submit_tasks",
                         "input": {"tasks": [{"title": "T", "covers": ["sec1"],
                                              "segments": ["S1·1"]}]}}],
            "usage": {"input_tokens": 300, "output_tokens": 65536}}


llm5 = LLMClient()
cfg5 = pin_minimax(llm5, "write_plan_tasks")
llm5._client = FakeClient([mm_tool(True)])
_blocks, _u = llm5._minimax_tools(cfg5, "sys", [{"role": "user", "content": "x"}],
                                 [{"name": "submit_tasks", "description": "d",
                                   "input_schema": {"type": "object"}}],
                                 tool_choice="submit_tasks")
check("④ 工具侧截断有告警", any("截断" in w for w in llm5.warnings),
      f"warnings={llm5.warnings}")

llm6 = LLMClient()
cfg6 = pin_minimax(llm6, "write_plan_tasks")
llm6._client = FakeClient([mm_tool(False)])
llm6._minimax_tools(cfg6, "sys", [{"role": "user", "content": "x"}],
                    [{"name": "submit_tasks", "description": "d",
                      "input_schema": {"type": "object"}}],
                    tool_choice="submit_tasks")
check("④ 正常工具调用不误报", not any("截断" in w for w in llm6.warnings),
      f"warnings={llm6.warnings}")

print("\n" + "=" * 60)
print(f"失败 {len(fails)} 项" + ("：" + "、".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
