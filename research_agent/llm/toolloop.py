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
import time
from typing import Callable, Protocol

from ..trace import record_span


def _toolround(stage: str, meta: dict | None, round_no, t0: float,
               llm_ms: int | None, tool_ms: int | None) -> None:
    """记一轮工具循环：LLM 多久、工具执行多久，分开记。

    `via`（自愿/强制交卷）在交卷发生的那一轮才被写进 `meta`——
    早几轮记 None 是正常的，靠任务级 span 或末尾轮次的 via 对齐。
    """
    record_span("toolround", stage, t0, time.time(), {
        "task": (meta or {}).get("task_id"),
        "round": round_no,
        "llm_ms": llm_ms,
        "tool_ms": tool_ms,
        "via": (meta or {}).get("via"),
    })


class ToolProvider(Protocol):
    """一组工具。specs 给出 schema，dispatch 执行调用。"""

    def specs(self) -> list[dict]: ...

    def dispatch(self, calls: list[dict]) -> list[dict]: ...


def _check_minimax_thinking_tools(llm, stage: str, sends_tools: bool) -> None:
    """🔴 **「关思考 + 要发工具」在 MiniMax 上是错的**——共用护栏。

    实测（2026-10-17，MiniMax-M3，每格 n=10，`tool_choice` 形状一致）：

        提示词点名工具 + 开思考       → 真调 10/10，零编造
        提示词点名工具 + 关思考       → 真调 5~6/10，回文本 4/10，
                                      其中出现过「已经调用 lookup」+ 编造查询结果
        只说「查一下 X」不点名 + 关思考 → 真调 3/3

    ⇒ 危险格是「**提示词点名工具** + 关思考」，真凶是**没有思考**，
      不是 `tool_choice`（强制 6/10 对不强制 5/10，在噪声内）。
    ⇒ 本 guard 比真实危险条件**更宽**：它把「不点名 + 关思考」那 3/3 正常的
      组合也挡住了。宁可多挡——多挡只是多花 token，漏挡是假证据进报告。

    ## 为什么抽成共用函数（2026-10-18）

    这段逻辑原来只写在 `run_tool_loop` 里。而 `summarize` 走的是
    `complete_json_forced`（`extract_batch` 调的），**完全没被覆盖** ——
    在它 24 次全部 404 之前，这条组合是「不报错、不拦、也不管」的。
    两条发工具的路只护一条，正是这轮反复栽的同一个形态。

    ## 为什么拦在调用点而不是 `validate()`

    只有**真的要发工具的那一次调用**才知道这件事。配置层看不到
    「这个环节用不用工具」——`complete_json` 那条路不发工具，
    关思考完全没问题。
    """
    if not sends_tools:
        return
    try:
        cfg = llm.registry.stage(stage)
    except Exception:
        return
    if cfg.provider == "minimax" and not cfg.reasoning_effort:
        raise RuntimeError(
            f"环节 {stage} 在 MiniMax 上同时是「关思考」和「要发工具」——"
            "**实测真调工具只有 5~6/10，出现过「声称已调用」并编造查询结果**，"
            "必须开思考（给这个环节配 `reasoning_effort: low` 或更高；"
            "开思考后同一形状实测 10/10）。"
            "没有这个问题的模型不受影响。"
        )


def run_tool_loop(
    llm,
    stage: str,
    system: str,
    messages: list[dict],
    provider: ToolProvider,
    max_rounds: int = 3,
    final_tool: dict | None = None,
    meta: dict | None = None,
    final_nudge: str | None = None,
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

    ## `meta`（2026-10-19 加）

    传一个 dict 就回填 `meta["via"]`，记正文是从哪条通道拿到的：
    `voluntary`（Phase A 自愿交卷）/ `forced`（Phase B 首次）/
    `forced_retry`（Phase B 同档位重试）/ `fallback`（没拿到交卷，
    返回的是自由文本）。下游靠它决定要不要走文本清洗——
    交卷产物可信，自由文本要按污染通道处理。

    ## `final_nudge`（2026-10-19，P2 固化）

    关思考的强制轮里，模型仍会觉得「证据没读完」而去调 read_segments
    而不是交卷——在末条 user 消息后追加一句「立即交卷」把它钉回任务
    （真实验收实测：nudge 让 t7 从 ~一半不交卷 → 8/8）。
    只作用于发给 Phase B 的**副本**，返回的 messages 保持原样。
    不传的调用方（如 write_plan_tasks）行为完全不变。
    """
    messages = list(messages)
    log: list[list[dict]] = []
    text = ""
    final_name = (final_tool or {}).get("name")

    # 🔴 **关思考 + 发工具，在 MiniMax 上是错的**（2026-10-17 实测）。
    # 分变量复测（每格 n=10，`tool_choice` 形状一致）：
    #
    #   提示词点名工具 + 开思考   → 真调 10/10，零编造
    #   提示词点名工具 + 关思考   → 真调 5~6/10，回文本 4/10，
    #                              其中出现过「已经调用 lookup」+ 编造查询结果
    #   只说「查一下 X」不点名 + 关思考 → 真调 3/3
    #
    # ⇒ 危险格是「**提示词点名工具** + 关思考」，真凶是**没有思考**，
    #   不是 `tool_choice`（强制 6/10 对不强制 5/10，在噪声内）。
    # ⇒ 本 guard 比真实危险条件**更宽**：它把「不点名 + 关思考」那 3/3 正常的
    #   组合也挡住了。宁可多挡 —— 多挡只是多花 token，漏挡是假证据进报告。
    #
    # 为什么拦在这里而不是 `validate()`：只有**真的要发工具的那一次调用**
    # 才知道这件事。配置层看不到「这个环节用不用工具」。
    # （原先 `validate()` 里那条「minimax 不认 reasoning_effort」的拦截，
    #   在 `_minimax` 学会翻译之后变成了挡住合法配置，已撤销。）
    #
    # ⚠️ **2026-10-18：逻辑抽到 `_check_minimax_thinking_tools()`，
    # `complete_json_forced` 也调它。** 之前这条只写在 `run_tool_loop` 里，
    # 而 `summarize` 走的是 `complete_json_forced`（`extract_batch`）——
    # 护栏覆盖不到。**只补一条路不补另一条，是这轮反复栽的同一个坑。**
    _check_minimax_thinking_tools(llm, stage,
                                 bool(final_tool or provider.specs()))

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
        _rt0 = time.time()
        _llm_ms = _tool_ms = None
        try:
            _ct0 = time.time()
            resp = llm.complete_with_tools(stage, system, messages, tools)
            _llm_ms = int((time.time() - _ct0) * 1000)
        except Exception as e:
            _toolround(stage, meta, round_no, _rt0, _llm_ms, _tool_ms)
            if final_tool is None:
                # 老路径没有结构化交卷兜底，异常照常向上传播。
                raise
            # 自愿交卷的参数也可能被 max_tokens 截断、在 provider 层抛
            # （EmptyOutput / _args_broken）——那不是「环节不可用」，
            # 只是「这一轮没写成」。置空 text 后跳出循环进 Phase B：
            # 比向上抛、让调用方退回单次调用多留一次结构化机会。
            # ⚠️ 前提：异常前没动 messages，末条仍是 user，Phase B 可用。
            print(f"⚠️  [{stage}] 第 {round_no + 1} 轮调用失败"
                  f"（{type(e).__name__}: {str(e)[:80]}），进强制交卷",
                  flush=True)
            text = ""
            break
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
                        if meta is not None:
                            meta["via"] = "voluntary"
                        _toolround(stage, meta, round_no, _rt0,
                                   _llm_ms, _tool_ms)
                        return done, messages, log

        log.append(calls)
        if not calls:
            _toolround(stage, meta, round_no, _rt0, _llm_ms, _tool_ms)
            break

        messages.append({"role": "assistant", "content": blocks})
        # 协议硬要求：assistant 里有 tool_use，user 就必须紧跟 tool_result，
        # 数量与顺序一一对应，少一个就 400。
        _dt0 = time.time()
        results = list(provider.dispatch(calls))
        _tool_ms = int((time.time() - _dt0) * 1000)
        if last_round:
            # 额度用尽要说清楚。但**不能另起一个纯文本 user 消息**——
            # tool_use 之后只接受 tool_result，纯文本会被判
            # "tool call result does not follow tool call"（实测踩过）。
            # 所以它必须作为同一轮里的一个 text 块一起发。
            results.append({"type": "text", "text":
                           f"工具调用次数已用尽（{max_rounds} 轮）。请基于已获得的信息"
                           f"给出最终结果，不要再要求读证据。"})
        messages.append({"role": "user", "content": results})
        _toolround(stage, meta, round_no, _rt0, _llm_ms, _tool_ms)

    if final_tool is None:
        return text, messages, log

    # ---- Phase B：强制交卷 ----
    # `final_nudge`（P2，2026-10-19）：关思考的强制轮里模型仍会觉得
    # 「证据没读完」而去调 read_segments 不交卷（验收实测 ~2/8）。
    # 末条 user 消息后钉一句「立即交卷」把它拉回任务（P2 实测 t7 8/8）。
    # 只改发给 Phase B 的**副本**：传入的 messages 及其内部对象
    # 不原地改，返回给调用方的 messages 也不带这段 nudge。
    b_msgs = messages
    if final_nudge and messages and messages[-1].get("role") == "user":
        _last = messages[-1]
        _content = _last.get("content")
        if isinstance(_content, list):
            _content = list(_content) + [{"type": "text",
                                          "text": final_nudge}]
        else:
            _content = f"{_content or ''}\n\n{final_nudge}"
        b_msgs = list(messages[:-1]) + [{**_last, "content": _content}]
    err: Exception | None = None
    _bt0 = time.time()
    _b_ms = None
    try:
        # ⚠️ 第一次刻意**不传 `thinking`**：交给 provider 按环节配置决定
        # （`forced_thinking: disabled` 的环节这一轮会关思考）。
        # 也让不认 `thinking` 形参的老测试桩不受影响。
        _ct0 = time.time()
        resp = llm.complete_with_tools(stage, system, b_msgs, [final_tool],
                                       tool_choice=final_name)
        _b_ms = int((time.time() - _ct0) * 1000)
        blocks = resp.get("blocks") or []
        for b in blocks:
            if b.get("type") == "tool_use" and b.get("name") == final_name:
                done = _submitted(b)
                if done:
                    if meta is not None:
                        meta["via"] = "forced"
                    _toolround(stage, meta, "submit", _bt0, _b_ms, None)
                    return done, messages, log
        # 强制了却没拿到工具调用——平台无视了 tool_choice。保留上一轮的文本。
        text = "".join(x.get("text", "") for x in blocks
                       if x.get("type") == "text") or text
    except Exception as e:
        err = e
    _toolround(stage, meta, "submit", _bt0, _b_ms, None)

    # 强制轮没交卷 → 只在环节**显式配了** `forced_thinking: disabled` 时
    # 用**同一档位**重试**一次**（2026-10-19 加，当日真实验收数据订正）。
    # 依据：MiniMax 关思考 + 点名工具的编造调用是 ~10% 的随机事件，
    # 第二次关思考重试成功概率高得多；而**开思考的强制轮会先把预算
    # 烧在 thinking 上**——v13 t7 生产验收实测：第一次强制轮编造了
    # `read_context`，thinking=True 重试思考 8192 token 吃光
    # max_tokens、一个字没写，两轮合计烧 ~17k 后照样 fallback。
    # 所以重试是同档位再来一次，不是「回退到稳的那档」。
    # 没配这个开关的环节维持原行为，不为它们凭空多打一次调用；
    # 取不到配置（桩 llm、registry 异常）也按「没配」处理，不因此抛错。
    retry = False
    try:
        retry = getattr(llm.registry.stage(stage), "forced_thinking",
                        None) == "disabled"
    except Exception:
        retry = False
    if retry:
        _rt0 = time.time()
        _r_ms = None
        try:
            _ct0 = time.time()
            resp = llm.complete_with_tools(stage, system, b_msgs,
                                           [final_tool],
                                           tool_choice=final_name,
                                           thinking=False)
            _r_ms = int((time.time() - _ct0) * 1000)
            blocks = resp.get("blocks") or []
            for b in blocks:
                if b.get("type") == "tool_use" and b.get("name") == final_name:
                    done = _submitted(b)
                    if done:
                        if meta is not None:
                            meta["via"] = "forced_retry"
                        _toolround(stage, meta, "submit_retry", _rt0,
                                   _r_ms, None)
                        return done, messages, log
            text = "".join(x.get("text", "") for x in blocks
                           if x.get("type") == "text") or text
        except Exception as e2:
            err = e2
        _toolround(stage, meta, "submit_retry", _rt0, _r_ms, None)
        if err is None:
            err = RuntimeError(f"两轮强制交卷都没拿到 {final_name}")

    if err is not None:
        print(f"⚠️  [{stage}] 强制交卷失败，退回上一轮文本: {err}", flush=True)
    if meta is not None:
        meta["via"] = "fallback"
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
