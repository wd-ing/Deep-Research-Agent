"""工具循环执行器。

有了工具不等于能循环。Anthropic 协议上循环有三个坑，都实测踩过：

  1. 一轮可能发**多个** tool_use，每个都要回一个 tool_result，
     少一个或顺序不对就 400 `tool call and result not match`。
  2. tool_use 后面不能直接跟普通文本轮次，必须先 tool_result。
  3. 对话是只追加的，缓存能沿着它往后延（实测 9,262 → 9,351），
     所以循环不是「每轮全价」。

本模块只管协议，工具的语义交给 handlers。
"""

from __future__ import annotations

from typing import Callable, Protocol


class ToolProvider(Protocol):
    """一组工具。specs 给出 schema，dispatch 执行调用。"""

    def specs(self) -> list[dict]: ...
    def dispatch(self, calls: list[dict]) -> list[dict]: ...


def run_tool_loop(
    llm,
    stage: str,
    system: str,
    messages: list[dict],
    provider: ToolProvider,
    max_rounds: int = 3,
) -> tuple[str, list[dict], list[list[dict]]]:
    """跑到模型不再调工具为止。

    返回 (最终文本, 消息历史, 每轮的调用记录)。
    调用记录留给 write_trace，测试期要能查「它到底去看了什么」。

    max_rounds 只防**循环**，不防总量——限制总量会让 agent 不知道自己漏了什么，
    那比多读几段糟得多。
    """
    messages = list(messages)
    log: list[list[dict]] = []
    text = ""

    for round_no in range(max_rounds + 1):
        last_round = round_no >= max_rounds
        # 最后一轮**不带工具**：它发不出 tool_use，只能收尾
        tools = [] if last_round else provider.specs()
        resp = llm.complete_with_tools(stage, system, messages, tools)
        blocks = resp.get("blocks") or []
        text = "".join(b.get("text", "") for b in blocks
                       if b.get("type") == "text")
        calls = [b for b in blocks if b.get("type") == "tool_use"]
        log.append(calls)

        if not calls:
            return text, messages, log

        messages.append({"role": "assistant", "content": blocks})
        # 协议硬要求：assistant 里有 tool_use，user 就必须紧跟 tool_result，
        # 数量与顺序一一对应，少一个就 400。
        results = list(provider.dispatch(calls))
        if last_round:
            # 额度用尽要说清楚。但**不能另起一个纯文本 user 消息**——
            # tool_use 之后只接受 tool_result，纯文本会被判
            # "tool call result does not follow tool call"（实测踩过）。
            # 所以它必须作为同一轮里的一个 text 块一起发。
            results.append({"type": "text", "text":
                           f"工具调用次数已用尽（{max_rounds} 轮）。请基于已获得的信息"
                           f"给出最终结果，不要再要求读证据。"})
        messages.append({"role": "user", "content": results})

    return text, messages, log
