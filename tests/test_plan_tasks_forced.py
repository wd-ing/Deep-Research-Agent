"""`write_plan_tasks` 强制工具交卷的守门测试（离线，不需要网络）。

2026-10-17 加。验的是「最后一轮改成 `tool_choice` 强制交卷」这条链路的
**行为**，不是「空返回率会降多少」——后者必须打真平台（`probe` 那边）。

⚠️ 这里踩过一个坑值得记下来：接线改完，`probe_plan_tasks_toolloop.py`
里那个 `fake_loop(llm, stage, system, messages, provider, max_rounds=3)`
没跟着加 `final_tool` 形参，于是 `node_plan_tasks` 抛 `TypeError`，
**被它自己的 try/except 接住、退回单次调用、照样打印「✅ 走工具循环」**。
签名不匹配的桩不会让测试变红，只会让它测的东西悄悄换成退路。
所以本文件每个用例都断言**真的走了哪条路**，不只断言结果好看。

另外提醒自己：空集合恒为真。`assertTrue(text)` 在「什么都没返回」时也过，
所以下面一律断言**具体内容**（段号、小节 id、调用次数、tool_choice 取值）。

用法：
    .venv/bin/python tests/test_plan_tasks_forced.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research_agent.llm import provider as P  # noqa: E402
from research_agent.llm.toolloop import run_tool_loop  # noqa: E402

FINAL = {"name": "submit_tasks", "description": "交卷",
         "input_schema": {"type": "object",
                          "properties": {"tasks": {"type": "array"}},
                          "required": ["tasks"]}}

TASKS = {"tasks": [{"id": "t1", "covers": ["sec1"], "mandate": "比较路线",
                   "segments": ["S1·1"], "must_cover": [], "not_yours": []}]}


# --------------------------------------------------------------- 桩

class FakeLLM:
    """按脚本逐轮返回块，并记录每次调用的**形状**。

    `script` 是 `complete_with_tools` 的返回值序列；用完再要就返回
    最后一条（模拟模型开始胡说）。
    """

    def __init__(self, script: list[dict]):
        self.script = list(script)
        self.seen: list[dict] = []

    def complete_with_tools(self, stage, system, messages, tools,
                            tool_choice=None):
        self.seen.append({
            "tools": [t.get("name") for t in tools or []],
            "tool_choice": tool_choice,
            "messages": messages,
        })
        r = self.script[min(len(self.seen) - 1, len(self.script) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


class FakeTools:
    def specs(self):
        return [{"name": "read_segments", "description": "读原文",
                 "input_schema": {"type": "object"}}]

    def dispatch(self, calls):
        return [{"type": "tool_result", "tool_use_id": c.get("id"),
                 "content": "原文"} for c in calls]


def blocks(*items):
    return {"blocks": list(items), "usage": {"completion_tokens": 10}}


def use(name, payload, bid="u1"):
    return {"type": "tool_use", "id": bid, "name": name, "input": payload}


def txt(s):
    return {"type": "text", "text": s}


HAND_JSON = json.dumps(TASKS, ensure_ascii=False)

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}" + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


# --------------------------------------------------------------- 用例

def t1_early_submit_skips_forced_round():
    """模型自己就调了 submit_tasks → 不该再多打一次强制调用。"""
    llm = FakeLLM([blocks(use("submit_tasks", TASKS))])
    text, _, log = run_tool_loop(llm, "write_plan_tasks", "sys",
                                 [{"role": "user", "content": "p"}],
                                 FakeTools(), final_tool=FINAL)
    check("① 早交卷只调 1 次", len(llm.seen) == 1, f"实际 {len(llm.seen)}")
    check("① 早交卷不传 tool_choice", llm.seen[0]["tool_choice"] is None)
    check("① 证据工具仍在列",
          llm.seen[0]["tools"] == ["read_segments", "submit_tasks"],
          str(llm.seen[0]["tools"]))
    check("① 返回的是 arguments", json.loads(text)["tasks"][0]["id"] == "t1")
    check("① 交卷不算证据读取", log == [], f"log={log}")


def t2_text_then_forced():
    """第一轮回手写 JSON → 提交轮强制 tool_choice，产物应是 arguments。"""
    llm = FakeLLM([blocks(txt(HAND_JSON)), blocks(use("submit_tasks", TASKS))])
    text, _, _ = run_tool_loop(llm, "write_plan_tasks", "sys",
                               [{"role": "user", "content": "p"}],
                               FakeTools(), final_tool=FINAL)
    check("② 走了 2 轮", len(llm.seen) == 2, f"实际 {len(llm.seen)}")
    check("② 第二轮强制 submit_tasks",
          llm.seen[1]["tool_choice"] == "submit_tasks",
          str(llm.seen[1]["tool_choice"]))
    check("② 第二轮只发一个工具", llm.seen[1]["tools"] == ["submit_tasks"],
          str(llm.seen[1]["tools"]))
    check("② 产物来自 arguments 而非手写文本",
          json.loads(text) == TASKS)


def t3_evidence_then_text():
    """读了证据再回文本 → 证据轮有 tool_result，提交轮照样强制。"""
    llm = FakeLLM([
        blocks(use("read_segments", {"segments": ["S1·1"]})),
        blocks(txt(HAND_JSON)),
        blocks(use("submit_tasks", TASKS)),
    ])
    text, msgs, log = run_tool_loop(llm, "write_plan_tasks", "sys",
                                    [{"role": "user", "content": "p"}],
                                    FakeTools(), max_rounds=3,
                                    final_tool=FINAL)
    check("③ 走了 3 轮", len(llm.seen) == 3, f"实际 {len(llm.seen)}")
    # log 是**逐轮**的调用记录，回文本的那一轮也占一行（空的）。
    # 计数用 `sum(len(r) for r in log)`，空行不影响 tool_reads。
    check("③ 记下 1 次证据读取",
          sum(len(r) for r in log) == 1, f"log={log}")
    # 协议：assistant(tool_use) 之后必须紧跟 user(tool_result)
    kinds = [(m["role"], [b.get("type") for b in m["content"]])
             if isinstance(m["content"], list) else (m["role"], "str")
             for m in msgs]
    check("③ 消息历史合法（tool_use 后紧跟 tool_result）",
          kinds[1] == ("assistant", ["tool_use"])
          and kinds[2] == ("user", ["tool_result"]), str(kinds))
    check("③ 提交轮强制", llm.seen[2]["tool_choice"] == "submit_tasks")
    check("③ 提交轮拿得到前两轮的上下文",
          len(llm.seen[2]["messages"]) == 3, f"messages={len(llm.seen[2]['messages'])}")
    check("③ 产物正确", json.loads(text) == TASKS)


def t4_rounds_exhausted_still_submits():
    """轮次耗尽（一直读原文）→ 提交轮仍然必须发生。"""
    llm = FakeLLM([
        blocks(use("read_segments", {"segments": ["S1·1"]}, "u1")),
        blocks(use("read_segments", {"segments": ["S1·2"]}, "u2")),
        blocks(txt(""), use("read_segments", {"segments": ["S1·3"]}, "u3")),
        blocks(use("submit_tasks", TASKS)),
    ])
    text, msgs, log = run_tool_loop(llm, "write_plan_tasks", "sys",
                                    [{"role": "user", "content": "p"}],
                                    FakeTools(), max_rounds=2,
                                    final_tool=FINAL)
    check("④ 耗尽后仍强制交卷", json.loads(text) == TASKS)
    # 最后一轮（round_no >= max_rounds）撤掉**证据工具**，只留交卷工具：
    #   · 发空数组会被信通院 400（见场景 ⑫）
    #   · 提示词写着「调用 submit_tasks 交卷」而手上没这个工具，
    #     模型会凭空编一个同名调用（线上 A 组实测到过）
    check("④ 最后一轮只剩交卷工具", llm.seen[2]["tools"] == ["submit_tasks"],
          str(llm.seen[2]["tools"]))
    check("④ 提交轮只发 submit_tasks", llm.seen[3]["tools"] == ["submit_tasks"])
    check("④ 额度用尽提示作为 text 块跟在 tool_result 后",
          [b.get("type") for b in msgs[-1]["content"]] == ["tool_result", "text"],
          str([b.get("type") for b in msgs[-1]["content"]]))


def t5_platform_ignores_choice():
    """平台无视 tool_choice 回文本 → 退回上一轮文本，不炸。"""
    llm = FakeLLM([blocks(txt(HAND_JSON)), blocks(txt(HAND_JSON))])
    text, _, _ = run_tool_loop(llm, "write_plan_tasks", "sys",
                               [{"role": "user", "content": "p"}],
                               FakeTools(), final_tool=FINAL)
    check("⑤ 退回上一轮文本且仍可解析", json.loads(text) == TASKS)


def t6_forced_round_raises():
    """提交轮抛异常 → 不许炸掉整步，退回上一轮文本。"""
    llm = FakeLLM([blocks(txt(HAND_JSON)), RuntimeError("平台 500")])
    text, _, _ = run_tool_loop(llm, "write_plan_tasks", "sys",
                               [{"role": "user", "content": "p"}],
                               FakeTools(), final_tool=FINAL)
    check("⑥ 异常被吞且退回文本", json.loads(text) == TASKS)


def t7_no_final_tool_unchanged():
    """不传 final_tool → 行为与改造前一模一样（回归）。"""
    llm = FakeLLM([blocks(txt(HAND_JSON))])
    text, _, log = run_tool_loop(llm, "write_plan_tasks", "sys",
                                 [{"role": "user", "content": "p"}],
                                 FakeTools())
    check("⑦ 回归：1 次调用", len(llm.seen) == 1)
    check("⑦ 回归：最后一轮不带工具", llm.seen[0]["tools"] == ["read_segments"])
    check("⑦ 回归：返回手写文本", text == HAND_JSON)


def t8_choice_name_missing():
    """强制一个 tools 里没有的名字 → 必须显式 ConfigError。"""
    c = P.LLMClient()
    try:
        c.complete_with_tools("write_plan_tasks", None, [], [FINAL],
                              tool_choice="submit_nope")
        check("⑧ 名字对不上时报错", False, "没抛")
    except P.ConfigError as e:
        check("⑧ 名字对不上时报错", "submit_nope" in str(e), str(e)[:80])
    finally:
        c.close()


def t9_minimax_refuses_to_pretend():
    """MiniMax 没接 tool_choice → 必须报错，不许降级成非强制。"""
    c = P.LLMClient()
    cfg = P.StageConfig(
        stage="probe", provider="minimax", model="m",
        max_output_tokens=1024, temperature=0.3, reasoning_effort=None,
        thinking_effort=None, timeout_s=30.0)
    c.registry._cache["probe"] = cfg
    try:
        c.complete_with_tools("probe", None, [], [FINAL], tool_choice="submit_tasks")
        check("⑨ MiniMax 显式拒绝", False, "没抛")
    except P.ConfigError as e:
        check("⑨ MiniMax 显式拒绝", "tool_choice" in str(e), str(e)[:80])
    finally:
        c.registry._cache.pop("probe", None)
        c.close()


def t10_broken_args_raises():
    """参数被截断 → 必须显式失败，不许把 `{}` 当结果交出去。"""
    try:
        P._reject_broken_args(
            [{"type": "tool_use", "name": "submit_tasks", "input": {},
              "_args_broken": True}], "write_plan_tasks", "length")
        check("⑩ 破参数被拒", False, "没抛")
    except P.EmptyOutput as e:
        check("⑩ 破参数被拒", "max_tokens" in str(e), str(e)[:60])
    # 干净块不许误伤
    P._reject_broken_args([{"type": "tool_use", "name": "submit_tasks",
                            "input": {"tasks": []}}], "write_plan_tasks", "stop")
    check("⑩ 干净块不误伤", True)


def t11_node_wiring():
    """`node_plan_tasks` 真的把 final_tool 传下去了，且产物被真解析。"""
    from research_agent.graph.subgraphs import write as W
    orig = W.run_tool_loop
    seen: dict = {}

    def fake_loop(llm, stage, system, messages, provider, max_rounds=3,
                  final_tool=None):
        # ⚠️ 形参必须和调用点一致。早先这里漏了 `final_tool`，
        # 调用点抛 TypeError 被 `node_plan_tasks` 的 except 接住，
        # 退到单次调用，而测试**照样通过**——测的东西悄悄换了。
        seen["stage"] = stage
        seen["final_tool"] = final_tool
        return json.dumps(TASKS, ensure_ascii=False), [], [[]]

    W.run_tool_loop = fake_loop
    try:
        out = W.node_plan_tasks({
            "session_id": "s_x", "question": "q",
            "spec": {"thesis": "T"}, "outline": [{"id": "sec1"}],
            "on_topic": [{"ref": "S1·1", "summary": "a"}],
        })
    finally:
        W.run_tool_loop = orig
    tasks = out.get("tasks") or []
    ev = (out.get("events") or [{}])[0]
    check("⑪ 走的确实是工具循环", ev.get("kind") == "write_plan_tasks",
          f"kind={ev.get('kind')} failed={out.get('plan_failed')}")
    check("⑪ stage 名是拆开后的 write_plan_tasks", seen.get("stage") == "write_plan_tasks")
    check("⑪ final_tool 传下去了",
          (seen.get("final_tool") or {}).get("name") == "submit_tasks",
          str((seen.get("final_tool") or {}).get("name")))
    check("⑪ 任务真的落下来了（不是空集合冒充通过）",
          len(tasks) == 1 and tasks[0]["covers"] == ["sec1"]
          and tasks[0]["segments"] == ["S1·1"],
          json.dumps(tasks, ensure_ascii=False)[:160])


def t12_empty_tools_must_be_omitted():
    """🔴 `tools: []` 必须整个省略 —— 信通院直接 400（线上实测撞到）。

    2026-10-17 探针实测：
        internai 400 upstream_request_rejected
        "`tools` must not be an empty array. Either provide at least
         one tool or omit the field entirely."

    `run_tool_loop` 的最后一轮按设计就是「不带工具」，而 `_opencode_tools`
    之前老老实实发 `tools: []`。xj189 不校验块形状，这个洞从 2026-10-15
    工具循环加回来起就一直藏着 —— 与坑 41 同一形态：**平台的宽松把协议
    错误盖住了**。换成严格校验的平台才暴露。
    """
    c = P.LLMClient()
    sent: list[dict] = []
    # 只发不发真的请求，密钥存不存在无所谓——但 `_opencode_tools` 会先查它，
    # 所以塞个占位值。用**当前 stage 声明的那个**环境变量名，别写死。
    import os
    os.environ.setdefault(c.registry.stage("write_plan_tasks").api_key_env or "X",
                          "dummy")

    SSE = [
        'data: {"choices":[{"delta":{"content":"ok"}}]}',
        'data: {"choices":[{"finish_reason":"stop"}],'
        '"usage":{"completion_tokens":3}}',
        "data: [DONE]",
    ]

    class Resp:  # 假的流式响应（`_sse_lines` 走 `resp.iter_lines()`）
        status_code = 200

        def iter_lines(self):
            return iter(SSE)

        def read(self):
            return b""

    class CM:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return Resp()

        def __exit__(self, *a):
            return False

    def fake_stream(method, url, headers=None, json=None, timeout=None):
        sent.append(json)
        return CM()

    # ⚠️ `write_plan_tasks` 是 `stream: true`，走的是 `.stream()` 不是 `.post()`。
    # 第一版只桩了 `.post`，于是测试**真的发了一次请求**（拿到 401）。
    # 这本身就是坑 47 的又一次现身：**桩没接在正确的位置，测试会变成真调用**。
    orig = c._client.stream
    c._client.stream = fake_stream
    try:
        c.complete_with_tools("write_plan_tasks", "sys",
                              [{"role": "user", "content": "p"}], [])
    finally:
        c._client.stream = orig
        c.close()
    check("⑫ 空 tools 不发 tools 字段", "tools" not in sent[0],
          f"实际发了 tools={sent[0].get('tools')}")
    # 有工具时必须照发，不能被这个修复误伤。
    # ⚠️ 这里必须是 `[FINAL]` 而不是 `FINAL`：传 dict 的话迭代出的是它的**键**，
    # `to_openai_tools` 全跳过 → `tools: []` —— 正好复现出那个 400 的报文形状。
    # 这个坑我自己踩了一次，所以留了注释。
    c2 = P.LLMClient()
    c2._client.stream = fake_stream
    sent.clear()
    try:
        c2.complete_with_tools("write_plan_tasks", "sys",
                               [{"role": "user", "content": "p"}], [FINAL])
    finally:
        c2.close()
    check("⑫ 非空 tools 照发",
          sent[0].get("tools", [{}])[0].get("function", {}).get("name")
          == "submit_tasks", str(sent[0].get("tools"))[:80])


def main() -> int:
    print("=== run_tool_loop：两阶段交卷 ===")
    for fn in (t1_early_submit_skips_forced_round, t2_text_then_forced,
               t3_evidence_then_text, t4_rounds_exhausted_still_submits,
               t5_platform_ignores_choice, t6_forced_round_raises,
               t7_no_final_tool_unchanged):
        fn()
    print("\n=== provider 层 ===")
    t8_choice_name_missing()
    t9_minimax_refuses_to_pretend()
    t10_broken_args_raises()
    t12_empty_tools_must_be_omitted()
    print("\n=== node_plan_tasks 接线 ===")
    t11_node_wiring()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败：")
        for f in FAILED:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
