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
import _isolate  # noqa: F401, E402

import os  # noqa: E402

# 全程打桩不打网络，但 provider 启动时要 `api_key_env` 有值。
# 两个键都给：`INTERN_AI_API_KEY` 给 ⑧⑫，`MINIMAX_API_KEY` 给 ⑨。
os.environ.setdefault("INTERN_AI_API_KEY", "stub-not-used")
os.environ.setdefault("MINIMAX_API_KEY", "stub-not-used")

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


def t9_minimax_forced_works():
    """MiniMax 的强制通路（2026-10-17 从「拒绝」改成「能用」）。

    原来这条断言的是**反向**：MiniMax 遇到 `tool_choice` 抛 ConfigError，
    理由是「没接强制、降级等于把保证变成也许」。后来实测证明它**认**强制：

        adaptive + tools + 强制 submit   → tool_use=['submit'] ✅
        adaptive + 两工具 + 强制 submit   → 正确选中 submit  ✅

    我一度测出「不支持」，是**变量混淆** —— 那次同时用了
    `thinking: disabled`，而「关思考 + 工具」本来就会让 M3 改成回文本。
    两个变量没分开，结论就反了。
    """
    import copy
    c = P.LLMClient()
    sent: list[dict] = []

    class Resp:  # Anthropic 形状的非流式响应（`_minimax_tools` 走 `.post`）
        status_code = 200

        @staticmethod
        def json():
            return {"content": [{"type": "tool_use", "id": "x",
                                 "name": "submit_tasks",
                                 "input": {"tasks": [{"covers": ["sec1"],
                                                      "mandate": "m",
                                                      "segments": ["S1·1"]}]}}],
                    "usage": {"output_tokens": 9}}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.append(json)
        return Resp()

    # ⚠️ **必须桩 `.post` 不是 `.stream`**：`_minimax_tools` **只实现非流式**
    # （`stream: true` 被静默忽略——见 `_minimax_tools` 的说明）。
    # 桩错方法的表现是「真实打网络」，拿到 401/404，看起来像代码坏了。
    c._client.post = fake_post

    base = c.registry.stage("write_plan_tasks")
    prof = (c.registry.raw["providers"])["minimax"]
    cfg = copy.copy(base)
    cfg.stage = "_t9"
    cfg.provider = "minimax"
    cfg.model = prof["model"]
    # ⚠️ **base_url / api_key_env 必须一起换**：只改 provider 而留着
    # 信通院的 base_url，会打出 `/v1/messages` 到 OpenAI 兼容端点 → 404。
    # 这个坑我踩了一次才写下来。
    cfg.base_url = prof["base_url"]
    cfg.api_key_env = prof["api_key_env"]
    cfg.stream = False
    cfg.reasoning_effort = "high"      # 关键：开思考，否则 M3 会改回文本
    c.registry._cache["_t9"] = cfg

    def fake_stream(method, url, headers=None, json=None, timeout=None):
        sent.append(json)
        return CM()

    c._client.post = fake_post
    try:
        r = c.complete_with_tools("_t9", "s", [{"role": "user", "content": "p"}],
                                  [FINAL], tool_choice="submit_tasks")
    except Exception as e:
        check("⑨ MiniMax 强制可用", False, f"{type(e).__name__}: {str(e)[:110]}")
        c.registry._cache.pop("_t9", None)
        c.close()
        return
    finally:
        c.close()
    calls = [b for b in r["blocks"] if b.get("type") == "tool_use"]
    check("⑨ MiniMax 强制可用：没有抛「拒绝」", True)
    check("⑨ 报文带 Anthropic 形状的强制",
          sent[0].get("tool_choice") == {"type": "tool", "name": "submit_tasks"},
          str(sent[0].get("tool_choice")))
    check("⑨ 强制时只发那一个工具",
          [t.get("name") for t in sent[0].get("tools") or []] == ["submit_tasks"],
          str(sent[0].get("tools")))
    check("⑨ 开思考（否则 M3 会改回文本）",
          sent[0].get("thinking") == {"type": "adaptive"},
          str(sent[0].get("thinking")))
    check("⑨ 拿到了工具调用", len(calls) == 1, f"tool_use={len(calls)}")
    if calls:
        check("⑨ 参数能解析出任务",
              isinstance(calls[0].get("input"), dict)
              and calls[0]["input"].get("tasks"), str(calls[0].get("input"))[:80])


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
    import copy
    c = P.LLMClient()
    sent: list[dict] = []
    # ⚠️ **必须钉住 provider**：本用例测的是 **OpenAI 兼容路径**的空 tools 行为。
    # 用 `registry.stage("write_plan_tasks")` 会跟着 `defaults.provider` 漂 ——
    # 2026-10-17 默认切成 minimax 之后，它就走了 `_minimax_tools`
    # （只实现非流式、用 `.post`），于是 `.stream` 桩形同虚设、**真的打了网络**，
    # 报出来的是一个与本用例毫无关系的 401。
    base = c.registry.stage("write_plan_tasks")
    prof = (c.registry.raw["providers"])["internai"]
    cfg = copy.copy(base)
    cfg.stage = "_t12"
    cfg.provider = "internai"
    cfg.model = prof["model"]
    cfg.base_url = prof["base_url"]
    cfg.api_key_env = prof["api_key_env"]
    cfg.stream = True
    c.registry._cache["_t12"] = cfg

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
        c.complete_with_tools("_t12", "sys",
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
    c2.registry._cache["_t12"] = cfg      # 同样要钉住，别让它漂回 minimax
    c2._client.stream = fake_stream
    sent.clear()
    try:
        c2.complete_with_tools("_t12", "sys",
                               [{"role": "user", "content": "p"}], [FINAL])
    finally:
        c2.close()
    check("⑫ 非空 tools 照发",
          sent[0].get("tools", [{}])[0].get("function", {}).get("name")
          == "submit_tasks", str(sent[0].get("tools"))[:80])


def t13_normalize_task_arrays():
    """`_normalize_tasks` 对 must_cover / not_yours 的字符串容错（坑 76）。

    v14 真跑：模型把 `must_cover` 回成普通字符串而不是数组，旧代码对它
    逐字迭代，「覆盖三条路线」被拆成 12 个单字——下游 mandate 全是碎字。
    修复是 `_as_str_list`：字符串先当 JSON 数组试解，解不开就当一整条。
    """
    from research_agent.graph.subgraphs import write as W
    outline = [{"id": "sec1"}]

    def norm(**over):
        t = {"id": "t1", "covers": ["sec1"], "mandate": "m",
             "segments": [], "must_cover": [], "not_yours": []}
        t.update(over)
        return W._normalize_tasks([t], outline, set())[0]

    check("⑬ must_cover 数组原样",
          norm(must_cover=["a", "b"])["must_cover"] == ["a", "b"])
    check("⑬ JSON 字符串还原成数组",
          norm(must_cover='["x","y"]')["must_cover"] == ["x", "y"])
    # 核心回归：普通字符串是**一条**，逐字拆出来的单字一个都不能出现。
    check("⑬ 普通字符串按一条收、不拆成单字",
          norm(must_cover="单条要求")["must_cover"] == ["单条要求"])
    check("⑬ 空串与 None 都归空",
          norm(must_cover="")["must_cover"] == []
          and norm(must_cover=None)["must_cover"] == [])
    check("⑬ 超 12 项截到 12",
          len(norm(must_cover=[f"r{i}" for i in range(20)]
                  )["must_cover"]) == 12)
    check("⑬ 单项超 200 字截到 200",
          len(norm(must_cover=["x" * 300])["must_cover"][0]) == 200)
    check("⑬ not_yours 同规则",
          norm(not_yours="别写路线对比")["not_yours"] == ["别写路线对比"])


def main() -> int:
    print("=== run_tool_loop：两阶段交卷 ===")
    for fn in (t1_early_submit_skips_forced_round, t2_text_then_forced,
               t3_evidence_then_text, t4_rounds_exhausted_still_submits,
               t5_platform_ignores_choice, t6_forced_round_raises,
               t7_no_final_tool_unchanged):
        fn()
    print("\n=== provider 层 ===")
    t8_choice_name_missing()
    t9_minimax_forced_works()
    t10_broken_args_raises()
    t12_empty_tools_must_be_omitted()
    print("\n=== node_plan_tasks 接线 ===")
    t11_node_wiring()
    print("\n=== _normalize_tasks 数组字段容错 ===")
    t13_normalize_task_arrays()
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
