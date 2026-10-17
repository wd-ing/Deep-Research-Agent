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

import json
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
    final_tool: dict | None = None,
) -> tuple[str, list[dict], list[list[dict]]]:
    """跑到模型不再调工具为止。

    返回 (最终文本, 消息历史, 每轮的调用记录)。
    调用记录留给 write_trace，测试期要能查「它到底去看了什么」。

    max_rounds 只防**循环**，不防总量——限制总量会让 agent 不知道自己漏了什么，
    那比多读几段糟得多。

    ## `final_tool`（2026-10-17 加）

    传一个「交卷」工具（Anthropic 形状 `{name, description, input_schema}`），
    最终答案就**一定**是它的 `arguments`，而不是模型手写的 JSON 文本。

    以前最后一轮是 `tools = []`——不带任何工具，于是模型必须**手写整份
    JSON**。分派环节那份 JSON 实测 7,135 token，而纯提示词路径的长 JSON
    会中途崩（`complete_json_forced` 里记着 xj189 9% 失败率、报错在
    第 630 字符而非结尾）。用户原话：「结构化输出只是为了让他长输出更稳定，
    不是指望结构化输出能解决崩掉的问题」——所以这一层是稳定性，不是兜底。

    两阶段：

      · **证据轮**：给 `provider.specs()`，并**同时**列出 `final_tool`。
        模型自己判断「够了可以交」，常见情况就一次调用搞定。
      · **提交轮**（`Phase B`）：只有 `final_tool` + `tool_choice` 强制。
        无论上面是回文本、还是轮次耗尽、还是平台无视了 `tool_choice`，
        这一轮都必须存在——**它是「一定拿到结构化结果」这件事的唯一保证**。

    ⚠️ 提交轮失败**不抛**，退回上一轮的文本让调用方自己 `_parse_json`。
    理由同 `node_plan_tasks` 的退路：分派不是非走这条路不可，
    但「保证」失效必须看得见，所以会打一条告警而不是无声降级。
    """
    messages = list(messages)
    log: list[list[dict]] = []
    text = ""
    final_name = (final_tool or {}).get("name")

    # 🔴 **关思考 + 发工具，在 MiniMax 上是错的**（2026-10-17 实测）：
    #     thinking.type=disabled + tools → 模型**不调工具**，
    #     回一段文本说「已经按你的要求调用 lookup 查『固态电池』」，
    #     **然后编出一整段查询结果**（含小标题和释义）。
    #     它把「没查」伪装成「查过了」—— 比不调用危险得多，
    #     因为下游会以为证据已经看过。
    #
    # 为什么拦在这里而不是 `validate()`：只有**真的要发工具的那一次调用**
    # 才知道这件事。配置层看不到「这个环节用不用工具」。
    # （原先 `validate()` 里那条「minimax 不认 reasoning_effort」的拦截，
    #   在 `_minimax` 学会翻译之后变成了挡住合法配置，已撤销。）
    try:
        _cfg = llm.registry.stage(stage)
    except Exception:
        _cfg = None
    if (_cfg is not None and _cfg.provider == "minimax"
            and not _cfg.reasoning_effort and (final_tool or provider.specs())):
        raise RuntimeError(
            f"环节 {stage} 在 MiniMax 上同时是「关思考」和「要发工具」——"
            "**这个组合实测会让模型假装调用工具并编造结果**，"
            "必须开思考（给这个环节配 `reasoning_effort: low` 或更高）。"
            "没有这个问题的模型不受影响。"
        )

    for round_no in range(max_rounds + 1):
        last_round = round_no >= max_rounds
        if final_tool is None:
            # 老行为：最后一轮**不带工具**，它发不出 tool_use，只能收尾。
            tools = [] if last_round else list(provider.specs())
        else:
            # 有交卷工具时**任何一轮都不发空数组**：
            #   · 最后一轮改成只给 `submit_tasks`（不强制）。
            #     理由有二：空数组在信通院直接 400（`_opencode_tools` 有记录），
            #     而提示词里写着「调用 submit_tasks 交卷」——手上却没有这个工具，
            #     模型会**凭空编一个同名调用**出来（实测 A 组就是这么发生的）。
            #   · 之前几轮给「证据工具 + submit_tasks」，让模型自己判断够了没。
            tools = ([final_tool] if last_round
                     else list(provider.specs()) + [final_tool])
        resp = llm.complete_with_tools(stage, system, messages, tools)
        blocks = resp.get("blocks") or []
        text = "".join(b.get("text", "") for b in blocks
                       if b.get("type") == "text")
        calls = [b for b in blocks if b.get("type") == "tool_use"]

        # 模型自己交卷了：拿它的 arguments 收工，不必再走强制那一轮。
        if final_name:
            for c in calls:
                if c.get("name") == final_name:
                    done = _submitted(c)
                    if done:
                        return done, messages, log

        log.append(calls)
        if not calls:
            break

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

    if final_tool is None:
        return text, messages, log

    # ---- Phase B：强制交卷 ----
    try:
        resp = llm.complete_with_tools(stage, system, messages, [final_tool],
                                       tool_choice=final_name)
        blocks = resp.get("blocks") or []
        for b in blocks:
            if b.get("type") == "tool_use" and b.get("name") == final_name:
                done = _submitted(b)
                if done:
                    return done, messages, log
        # 强制了却没拿到工具调用——平台无视了 tool_choice。保留上一轮的文本。
        text = "".join(x.get("text", "") for x in blocks
                       if x.get("type") == "text") or text
    except Exception as e:
        print(f"⚠️  [{stage}] 强制交卷失败，退回上一轮文本: {e}", flush=True)

    return text, messages, log


def _submitted(block: dict) -> str:
    """工具 `arguments` → 交给调用方的文本。

    存成 JSON 文本而不是把 dict 直接返回，是为了让下游**只有一条解析路径**：
    `node_plan_tasks` 的 `_parse_json(text)` 同时覆盖「手写 JSON」和
    「工具交卷」两种来源，生产解析函数因此始终被真正跑到，
    而不是只在桩里跑过。
    """
    payload = block.get("input")
    if not isinstance(payload, dict) or not payload:
        return ""
    return json.dumps(payload, ensure_ascii=False)
