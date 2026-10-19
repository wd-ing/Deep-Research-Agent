"""submit_section 结构化交卷的守门测试（离线，不需要网络）。

2026-10-19 加。验的是「写作正文走 submit_section 工具参数」这条链路的
**行为**：交卷解析、meta["via"] 记账、Phase B 同档位重试、fallback
污染检测、merge 保护、P2 的非强制轮上限与交卷 nudge——不是
「干净率会涨多少」，后者必须打真平台
（tests/exp_submit_section.py / exp_submit_prod.py 那批对照实验已经做过）。

⚠️ 桩一律挂在生产同一层：
  · `W.LLM.complete_with_tools` —— run_tool_loop 调的就是它
  · `llm._minimax_once`          —— payload 在这里定型，桩它最准
  · `registry._cache`            —— stage 配置钉死，不随 models.yaml 漂移

`_run_task` 与 `_record_submit` 会真的写 write_trace，所以 RA_DB_PATH
指到 /tmp——本文件约定：测试不写真实数据库（data/research.db）。

用法：
    .venv/bin/python tests/test_submit_section.py
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 全程打桩不打网络，但 provider 启动时要 api_key_env 有值。
os.environ.setdefault("MINIMAX_API_KEY", "stub-not-used")
os.environ.setdefault("INTERN_AI_API_KEY", "stub-not-used")
os.environ["RA_DB_PATH"] = "/tmp/ra_submit_section_test.db"

from research_agent.llm import provider as P            # noqa: E402
from research_agent.llm.toolloop import run_tool_loop   # noqa: E402
from research_agent.graph.subgraphs import write as W   # noqa: E402
from research_agent.db.repo import get_repo             # noqa: E402

# /tmp 是全新库：`get_repo()` 只建连接，表结构要靠迁移脚本落地
# （生产里由 run_write/server 跑 init_schema）。不跑的话 write_trace
# 表不存在，`_record_submit` 的 try/except 会把它**静默吞掉**——
# 那才是最难查的假绿。
get_repo().init_schema()

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}" + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def blocks(*items):
    return {"blocks": list(items), "usage": {}}


def use(name, payload, bid="u1"):
    return {"type": "tool_use", "id": bid, "name": name, "input": payload}


def txt(s):
    return {"type": "text", "text": s}


def sub_json(sections, notes=None):
    d = {"sections": sections}
    if notes is not None:
        d["notes"] = notes
    return json.dumps(d, ensure_ascii=False)


# --------------------------------------------------------------- ① _parse_submission

def t_parse_submission():
    secs, note, _ = W._parse_submission(
        sub_json([{"id": "sec1", "body": "正文甲"}]), ["sec1"])
    check("① 单节正常", secs == {"sec1": "正文甲"} and note is None,
          f"{secs} {note}")

    secs, note, _ = W._parse_submission(
        sub_json([{"id": "sec2", "body": "乙"}, {"id": "sec1", "body": "甲"}]),
        ["sec1", "sec2"])
    check("① 多节按 id 归位（与顺序无关）",
          secs == {"sec1": "甲", "sec2": "乙"} and note is None, str(secs))

    secs, _, _ = W._parse_submission(
        sub_json([{"id": "sec1", "body": "甲"}, {"id": "sec9", "body": "多余"}]),
        ["sec1"])
    check("① 多余 id 丢弃", secs == {"sec1": "甲"}, str(secs))

    # 单节任务 id 写错也收——「一项对一项」时映射无歧义（实测会写成任务 id）。
    secs, _, _ = W._parse_submission(
        sub_json([{"id": "t1", "body": "甲"}]), ["sec1"])
    check("① 单节 id 对不上映射 covers[0]", secs == {"sec1": "甲"}, str(secs))

    secs, note, _ = W._parse_submission(
        sub_json([{"id": "sec1", "body": "甲"}]), ["sec1", "sec2"])
    check("① 缺节出 note 且如实点名",
          secs == {"sec1": "甲"} and bool(note) and "sec2" in note, str(note))

    secs, note, _ = W._parse_submission(
        sub_json([{"id": "sec1", "body": "   "}]), ["sec1"])
    check("① body 全空→交卷无效", secs == {} and bool(note), f"{secs} {note}")

    # 不抛异常：上游（截断）可能给出坏 JSON，交回失败由上层走降级。
    try:
        secs, note, _ = W._parse_submission("{不是 json", ["sec1"])
        check("① 非法 JSON 不抛、判失败", secs == {} and bool(note), str(note))
    except Exception as e:
        check("① 非法 JSON 不抛、判失败", False, f"{type(e).__name__}: {e}")

    _, _, notes = W._parse_submission(
        sub_json([{"id": "sec1", "body": "甲"}], notes="引用核对过"), ["sec1"])
    check("① notes 单独带出", notes == "引用核对过", repr(notes))


# --------------------------------------------------------------- ② _run_task 端到端

SEGS = [{"ref": "S1·1", "summary": "固态电池的定义", "token_count": 100,
         "content": "固态电池使用固体电解质。"},
        {"ref": "S1·2", "summary": "三条技术路线", "token_count": 100,
         "content": "硫化物、氧化物、聚合物。"}]

T1 = {"id": "t1", "covers": ["sec1"], "mandate": "写清定义",
      "segments": ["S1·1"], "must_cover": [], "not_yours": []}
T2 = {"id": "t2", "covers": ["sec2", "sec3"], "mandate": "两节耦合",
      "segments": ["S1·2"], "must_cover": [], "not_yours": []}


def _state(sid: str | None = None) -> dict:
    # write_trace.session_id 有外键指到 session：不先建行，
    # save_write_trace 会 IntegrityError——被 best-effort 的 try/except
    # 静默吞掉，trace 里什么都查不到（本文件第一版就栽在这）。
    sid = sid or get_repo().create_session("t", "q")
    return {"question": "固态电池的路线", "spec": {"thesis": "T"},
            "tasks": [T1, T2], "segments": SEGS, "on_topic": SEGS,
            "src_lines": ["[S1] s | u | t | d | x"], "session_id": sid}


class StubLLM:
    """挂在 `complete_with_tools` 层；`kw` 原样记录，能区分
    「没传 thinking」与「传了 None」——Phase B 的兼容契约就在这个差别上。
    """

    def __init__(self, script):
        self.script = list(script)
        self.seen: list[dict] = []

    def complete_with_tools(self, stage, system, messages, tools, **kw):
        # messages 深拷贝存档：nudge 只该出现在 Phase B 的**副本**里，
        # 浅拷贝会和调用方共享 content 列表，原地改动就看不出来了。
        self.seen.append({"stage": stage, "kw": dict(kw),
                          "messages": copy.deepcopy(messages)})
        r = self.script[min(len(self.seen) - 1, len(self.script) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


def t_run_task():
    # 自愿交卷：第一轮就 submit_section；text 块里的前言必须被丢弃。
    stub = StubLLM([blocks(txt("下面是我写的正文，请查收。"),
                          use("submit_section",
                              {"sections": [{"id": "sec1",
                                             "body": "干净的正文 [S1·1]。"}],
                               "notes": "S1·1 足够支撑"}))])
    st = _state()
    sid = st["session_id"]
    orig = W.LLM.complete_with_tools
    W.LLM.complete_with_tools = stub.complete_with_tools
    try:
        secs, note = W._run_task(st, T1, None)
    finally:
        W.LLM.complete_with_tools = orig
    check("② 自愿交卷 drafts 取 body", secs == {"sec1": "干净的正文 [S1·1]。"},
          str(secs))
    check("② 对话文本里的前言不进正文",
          all("下面是我写" not in v for v in secs.values()))
    check("② 自愿交卷一轮收工", len(stub.seen) == 1, str(len(stub.seen)))

    tr = get_repo().write_trace(sid).get("stats") or {}
    check("② notes 进 trace、不进正文",
          tr.get("section_notes", {}).get("t1") == "S1·1 足够支撑"
          and "S1·1 足够支撑" not in secs.get("sec1", ""),
          str(tr.get("section_notes")))
    check("② via=voluntary 记账",
          tr.get("submit_via", {}).get("voluntary") == 1,
          str(tr.get("submit_via")))

    # 多节任务：sections[] 归位，不再需要 <!-- section: --> 标记。
    stub = StubLLM([blocks(use("submit_section",
                              {"sections": [{"id": "sec3", "body": "第三节内容。"},
                                            {"id": "sec2", "body": "第二节内容。"}]}))])
    W.LLM.complete_with_tools = stub.complete_with_tools
    try:
        secs, note = W._run_task(_state(), T2, None)
    finally:
        W.LLM.complete_with_tools = orig
    check("② 多节 sections[] 归位",
          secs == {"sec2": "第二节内容。", "sec3": "第三节内容。"} and note is None,
          f"{secs} {note}")


# --------------------------------------------------------------- ③ fallback 清洗

def t_fallback():
    # 伪工具调用文本：整体判失败，一个标点都不许进稿。
    secs, err = W._clean_fallback("]<]minimax[>[<tool_call>…</tool_call>]", ["sec1"])
    check("③ 伪工具调用整体失败", secs == {} and "污染" in (err or ""), str(err))

    # ```markdown 围栏 + `# secN` 头的干净正文：两件无歧义的事剥掉后可用。
    # 正文必须 ≥200 字符——fallback 有最小长度下限（真实验收里 30 字符
    # 的「正文」被照收过），样本短了测的就是另一条规则。
    long_body = "干净的正文 [S1·1]。" + "展开论述材料中的事实。" * 20
    fenced = "```markdown\n# sec3 固态电池\n\n" + long_body + "\n```"
    secs, err = W._clean_fallback(fenced, ["sec3"])
    check("③ 围栏+secN头的干净正文可用",
          secs == {"sec3": long_body} and err is None,
          f"{secs} {err}")

    secs, err = W._clean_fallback("下面是我重写的 sec2 正稿。\n\n正文 [S1·1]。", ["sec2"])
    check("③ 带前言判失败", secs == {} and "污染" in (err or ""), str(err))

    en = "\n".join([
        "Let me verify the citations and structure once more.",
        "Citation check: all references are within the assigned set.",
        "Constraints satisfied: no source list, no headings added."])
    secs, err = W._clean_fallback(en, ["sec1"])
    check("③ 英文推理整块判失败", secs == {} and "污染" in (err or ""), str(err))

    # 最小长度下限（2026-10-19 验收后加）：30 字符的干净文本也要拒收，
    # ≥200 的干净文本照常通过。
    secs, err = W._clean_fallback("一段很短但完全干净的正文。", ["sec1"])
    check("③ 30 字符干净文本判过短",
          secs == {} and "过短" in (err or ""), str(err))
    ok_body = "正常的正文 [S1·1]。" + "按材料口径展开。" * 30
    secs, err = W._clean_fallback(ok_body, ["sec1"])
    check("③ ≥200 字符干净文本通过",
          secs == {"sec1": ok_body} and err is None, f"{secs} {err}")

    # _run_task 层接线：模型到强制轮都在回文本 → via=fallback → 走清洗。
    stub = StubLLM([blocks(txt("]<]minimax[>[<tool_call> junk"))])
    orig = W.LLM.complete_with_tools
    W.LLM.complete_with_tools = stub.complete_with_tools
    try:
        secs, err = W._run_task(_state(), T1, None)
    finally:
        W.LLM.complete_with_tools = orig
    check("③ _run_task 污染文本不入稿", secs == {} and "污染" in (err or ""),
          f"{secs} {err}")
    # write_section 配了 forced_thinking=disabled → 强制轮失败后
    # 应出现一次同档位（thinking=False）的重试（第一次强制不传 thinking）。
    kws = [c["kw"] for c in stub.seen]
    check("③ 关思考首次不带 thinking、失败重试带 False",
          len(kws) >= 3 and "thinking" not in kws[1]
          and kws[2].get("thinking") is False, str(kws))


# --------------------------------------------------------------- ④ Phase B 同档位重试

class FakeTools:
    def specs(self):
        return [{"name": "read_segments", "description": "读原文",
                 "input_schema": {"type": "object"}}]

    def dispatch(self, calls):
        return [{"type": "tool_result", "tool_use_id": c.get("id"),
                 "content": "原文"} for c in calls]


class FakeLoopLLM(StubLLM):
    """run_tool_loop 用的桩：带一个只给 forced_thinking 的 registry。

    cfg=None 时 stage() 抛异常——覆盖「取不到配置 = 不重试」的分支。
    """

    def __init__(self, script, cfg=None):
        super().__init__(script)
        self._cfg = cfg

    @property
    def registry(self):
        cfg = self._cfg

        class _R:
            @staticmethod
            def stage(name):
                if cfg is None:
                    raise P.ConfigError("no stage")
                return cfg
        return _R()


CFG_DISABLED = SimpleNamespace(provider="minimax", reasoning_effort="low",
                               forced_thinking="disabled")
CFG_NONE = SimpleNamespace(provider="minimax", reasoning_effort="low",
                           forced_thinking=None)


def t_retry():
    final = W.SUBMIT_SECTION
    msgs = [{"role": "user", "content": "p"}]

    # 关思考第一次返回「编造的 read_context」→ 同档位（关思考）重试救回。
    llm = FakeLoopLLM([
        blocks(txt("先写了点文本")),
        blocks(use("read_context", {"segment": "S1·1"})),
        blocks(use("submit_section", {"sections": [{"id": "s", "body": "b"}]})),
    ], cfg=CFG_DISABLED)
    meta: dict = {}
    text, _, _ = run_tool_loop(llm, "write_section", "sys", list(msgs),
                               FakeTools(), final_tool=final, meta=meta)
    check("④ 编造工具后重试拿到交卷",
          json.loads(text)["sections"][0]["id"] == "s")
    check("④ meta[via]=forced_retry", meta.get("via") == "forced_retry",
          str(meta))
    check("④ 共 3 次调用", len(llm.seen) == 3, str(len(llm.seen)))
    if len(llm.seen) == 3:
        check("④ 首次强制没传 thinking 形参",
              "thinking" not in llm.seen[1]["kw"], str(llm.seen[1]["kw"]))
        check("④ 重试带 thinking=False（同档位）",
              llm.seen[2]["kw"].get("thinking") is False, str(llm.seen[2]["kw"]))

    # 抛异常（截断拒收等）同样触发重试。
    llm = FakeLoopLLM([
        blocks(txt("文本")),
        RuntimeError("arguments 被截断"),
        blocks(use("submit_section", {"sections": [{"id": "s", "body": "b"}]})),
    ], cfg=CFG_DISABLED)
    meta = {}
    text, _, _ = run_tool_loop(llm, "write_section", "sys", list(msgs),
                               FakeTools(), final_tool=final, meta=meta)
    check("④ 首次抛异常也重试", meta.get("via") == "forced_retry"
          and json.loads(text)["sections"][0]["id"] == "s", str(meta))

    # 第一次就成功：只多打一次强制调用，meta=forced。
    llm = FakeLoopLLM([
        blocks(txt("文本")),
        blocks(use("submit_section", {"sections": [{"id": "s", "body": "b"}]})),
    ], cfg=CFG_DISABLED)
    meta = {}
    text, _, _ = run_tool_loop(llm, "write_section", "sys", list(msgs),
                               FakeTools(), final_tool=final, meta=meta)
    check("④ 首次即成功 via=forced", meta.get("via") == "forced", str(meta))
    check("④ 首次即成功只 2 次调用", len(llm.seen) == 2, str(len(llm.seen)))
    if len(llm.seen) == 2:
        check("④ 成功的那次同样没传 thinking",
              "thinking" not in llm.seen[1]["kw"], str(llm.seen[1]["kw"]))

    # 没配 forced_thinking → 不重试，退回文本。
    llm = FakeLoopLLM([blocks(txt("手写正文")), blocks(txt("还是文本"))],
                      cfg=CFG_NONE)
    meta = {}
    text, _, _ = run_tool_loop(llm, "write_section", "sys", list(msgs),
                               FakeTools(), final_tool=final, meta=meta)
    check("④ 未配置 forced_thinking 不重试",
          len(llm.seen) == 2 and meta.get("via") == "fallback",
          f"{len(llm.seen)} {meta}")

    # registry 取不到配置 → 同样不重试、不抛错。
    llm = FakeLoopLLM([blocks(txt("a")), blocks(txt("b"))], cfg=None)
    meta = {}
    try:
        text, _, _ = run_tool_loop(llm, "write_section", "sys", list(msgs),
                                   FakeTools(), final_tool=final, meta=meta)
        check("④ 取不到配置按没配处理",
              len(llm.seen) == 2 and meta.get("via") == "fallback", str(meta))
    except Exception as e:
        check("④ 取不到配置按没配处理", False, f"{type(e).__name__}: {e}")


# --------------------------------------------------------------- ⑤ provider payload

def t_provider_payload():
    llm = P.LLMClient()
    sent: list[dict] = []

    def fake_once(cfg, url, headers, payload):
        sent.append(payload)
        return 200, "", {"content": [use("submit_section",
                                        {"sections": [{"id": "s", "body": "b"}]})],
                         "usage": {}}
    llm._minimax_once = fake_once  # payload 在这层定型，桩这里最准

    base = llm.registry.stage("write_section")   # 已带 forced_thinking=disabled
    cfg = copy.copy(base)
    cfg.stage = "_t5"
    cfg.provider = "minimax"
    cfg.base_url = "https://api.minimaxi.com/anthropic"
    cfg.api_key_env = "MINIMAX_API_KEY"
    cfg.stream = False
    llm.registry._cache["_t5"] = cfg
    msgs = [{"role": "user", "content": "p"}]
    try:
        llm.complete_with_tools("_t5", "sys", list(msgs), [W.SUBMIT_SECTION],
                                tool_choice="submit_section")
        check("⑤ 关思考强制轮 thinking=disabled",
              sent[0].get("thinking") == {"type": "disabled"},
              str(sent[0].get("thinking")))
        check("⑤ 关思考轮不带 output_config",
              "output_config" not in sent[0], str(sent[0].keys()))
        check("⑤ tool_choice 照发",
              sent[0].get("tool_choice") == {"type": "tool",
                                            "name": "submit_section"},
              str(sent[0].get("tool_choice")))

        sent.clear()
        llm.complete_with_tools("_t5", "sys", list(msgs), [W.SUBMIT_SECTION],
                                tool_choice="submit_section", thinking=True)
        check("⑤ thinking=True → adaptive（形参语义保留）",
              sent[0].get("thinking") == {"type": "adaptive"},
              str(sent[0].get("thinking")))

        # 无 tool_choice（普通轮/Phase A）：forced_thinking 不生效，跟配置走。
        cfg2 = copy.copy(cfg)
        cfg2.stage = "_t5b"
        cfg2.reasoning_effort = "low"
        llm.registry._cache["_t5b"] = cfg2
        sent.clear()
        llm.complete_with_tools("_t5b", "sys", list(msgs), [W.SUBMIT_SECTION])
        check("⑤ 非强制轮跟 reasoning_effort 走（adaptive）",
              sent[0].get("thinking") == {"type": "adaptive"},
              str(sent[0].get("thinking")))
    finally:
        llm.close()

    # validate()：非法值与非 minimax 都要拦。
    bad = copy.copy(cfg)
    bad.stage = "_t5c"
    bad.forced_thinking = False        # YAML 把 `off` 解析成的就是这个
    try:
        bad.validate()
        check("⑤ forced_thinking=off(布尔) 被拦", False, "没抛")
    except P.ConfigError:
        check("⑤ forced_thinking=off(布尔) 被拦", True)

    bad2 = copy.copy(cfg)
    bad2.stage = "_t5d"
    bad2.provider = "internai"
    bad2.forced_thinking = "disabled"
    try:
        bad2.validate()
        check("⑤ 非 minimax 配 forced_thinking 被拦", False, "没抛")
    except P.ConfigError:
        check("⑤ 非 minimax 配 forced_thinking 被拦", True)


# --------------------------------------------------------------- ⑥ _merge_sections 保护

def t_merge():
    old_body = "旧稿正文。" * 20          # 100 字干净旧稿
    old = {"sec1": old_body}
    tgt = [{"section": "sec1"}]

    rej: list = []
    m = W._merge_sections(old, {"sec1": "]<]minimax[>[<tool_call>…"}, tgt,
                          rejected=rej)
    check("⑥ 伪工具新稿→保留旧稿且记录",
          m["sec1"] == old_body and len(rej) == 1
          and rej[0]["section"] == "sec1", str(rej))

    rej = []
    m = W._merge_sections(old, {"sec1": "改了一小句。"}, tgt, rejected=rej)
    check("⑥ 新稿<40%→保留旧稿且记录",
          m["sec1"] == old_body and len(rej) == 1, str(rej))

    rej = []
    garbage = {"sec1": "]<]minimax[>garbage" + "垃圾" * 30}
    m = W._merge_sections(garbage, {"sec1": "短新稿。"}, tgt, rejected=rej)
    check("⑥ 旧稿是 STRONG 垃圾→接受短新稿",
          m["sec1"] == "短新稿。" and not rej, f"{m['sec1']} {rej}")

    rej = []
    new_body = "新的完整正文。" * 20
    m = W._merge_sections(old, {"sec1": new_body}, tgt, rejected=rej)
    check("⑥ 正常新稿覆盖", m["sec1"] == new_body and not rej, str(rej))

    rej = []
    m = W._merge_sections({"sec1": "甲旧" * 25, "sec2": "乙旧" * 25},
                          {"sec2": "乙新" * 30}, [{"section": "sec1"}],
                          rejected=rej)
    check("⑥ 未被点名的小节不动", m["sec2"] == "乙旧" * 25, str(m))


# --------------------------------------------------------------- ⑦ P2：round_max_tokens / final_nudge / Phase A 异常

def _flat(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text") or "" for b in content
                     if isinstance(b, dict))


def t_round_cap():
    """P2 机制一：非强制轮输出上限。钉 `_minimax_once` 看 payload 定型。"""
    llm = P.LLMClient()
    sent: list[dict] = []

    def fake_once(cfg, url, headers, payload):
        sent.append(payload)
        return 200, "", {"content": [use("submit_section",
                                        {"sections": [{"id": "s",
                                                       "body": "b"}]})],
                         "usage": {}}
    llm._minimax_once = fake_once

    base = llm.registry.stage("write_section")
    cfg = copy.copy(base)
    cfg.stage = "_t7"
    cfg.provider = "minimax"
    cfg.base_url = "https://api.minimaxi.com/anthropic"
    cfg.api_key_env = "MINIMAX_API_KEY"
    cfg.stream = False
    cfg.round_max_tokens = 2048
    llm.registry._cache["_t7"] = cfg
    msgs = [{"role": "user", "content": "p"}]
    try:
        llm.complete_with_tools("_t7", "sys", list(msgs), [W.SUBMIT_SECTION])
        check("⑦ 非强制轮压到 round_max_tokens",
              sent[0].get("max_tokens") == 2048,
              str(sent[0].get("max_tokens")))

        sent.clear()
        llm.complete_with_tools("_t7", "sys", list(msgs), [W.SUBMIT_SECTION],
                                tool_choice="submit_section")
        check("⑦ 强制交卷轮不受 round_max_tokens",
              sent[0].get("max_tokens") == cfg.max_output_tokens,
              str(sent[0].get("max_tokens")))

        cfg2 = copy.copy(cfg)
        cfg2.stage = "_t7b"
        cfg2.round_max_tokens = None
        llm.registry._cache["_t7b"] = cfg2
        sent.clear()
        llm.complete_with_tools("_t7b", "sys", list(msgs), [W.SUBMIT_SECTION])
        check("⑦ 未配置 round_max_tokens 时上限不变",
              sent[0].get("max_tokens") == cfg.max_output_tokens,
              str(sent[0].get("max_tokens")))

        cfg3 = copy.copy(cfg)
        cfg3.stage = "_t7c"
        cfg3.max_output_tokens = 1024
        llm.registry._cache["_t7c"] = cfg3
        sent.clear()
        llm.complete_with_tools("_t7c", "sys", list(msgs), [W.SUBMIT_SECTION])
        check("⑦ 环节上限比 round 还小时取 min",
              sent[0].get("max_tokens") == 1024,
              str(sent[0].get("max_tokens")))
    finally:
        llm.close()

    # validate()：0/负数/非 int/布尔（bool 是 int 子类）/非 minimax 都拦。
    for v in (0, -1, "2048", False):
        bad = copy.copy(cfg)
        bad.stage = "_t7d"
        bad.round_max_tokens = v
        try:
            bad.validate()
            ok = False
        except P.ConfigError:
            ok = True
        check(f"⑦ round_max_tokens={v!r} 被拦", ok)
    badp = copy.copy(cfg)
    badp.stage = "_t7e"
    badp.provider = "internai"
    badp.forced_thinking = None      # 只留 round_max_tokens 一项违规
    try:
        badp.validate()
        check("⑦ 非 minimax 配 round_max_tokens 被拦", False, "没抛")
    except P.ConfigError:
        check("⑦ 非 minimax 配 round_max_tokens 被拦", True)


def t_nudge():
    """P2 机制二：Phase B 在末条 user 消息后钉「立即交卷」。"""
    final = W.SUBMIT_SECTION
    msgs = [{"role": "user", "content": "p"}]

    # 末条 content 是 str → 拼接 "\n\n"+nudge；传入对象不许被原地改。
    llm = FakeLoopLLM([blocks(txt("文本")),
                       blocks(use("submit_section",
                                  {"sections": [{"id": "s", "body": "b"}]}))],
                      cfg=CFG_NONE)
    meta: dict = {}
    run_tool_loop(llm, "write_section", "sys", msgs, FakeTools(),
                  final_tool=final, meta=meta, final_nudge=W.SUBMIT_NUDGE)
    got = llm.seen[1]["messages"][-1]
    check("⑦ Phase B 带 nudge（str 末条拼接）",
          isinstance(got["content"], str)
          and got["content"].endswith("\n\n" + W.SUBMIT_NUDGE),
          got["content"][-40:])
    check("⑦ 传入的 messages 未被原地修改",
          msgs == [{"role": "user", "content": "p"}], str(msgs))

    # 末条 content 是 list（读了一轮证据后的 tool_result）→ 追加 text 块。
    llm = FakeLoopLLM([
        blocks(use("read_segments", {"segments": ["S1·1"]})),
        blocks(txt("想再读一轮")),
        blocks(use("submit_section",
                   {"sections": [{"id": "s", "body": "b"}]})),
    ], cfg=CFG_NONE)
    meta = {}
    run_tool_loop(llm, "write_section", "sys", list(msgs), FakeTools(),
                  final_tool=final, meta=meta, final_nudge=W.SUBMIT_NUDGE)
    got = llm.seen[2]["messages"][-1]
    check("⑦ Phase B 带 nudge（list 末条追加 text 块）",
          isinstance(got["content"], list)
          and got["content"][-1] == {"type": "text",
                                     "text": W.SUBMIT_NUDGE},
          str(got["content"][-1]))

    # 关思考重试那一次同样带 nudge。
    llm = FakeLoopLLM([
        blocks(txt("文本")),
        blocks(txt("还是文本")),
        blocks(use("submit_section",
                   {"sections": [{"id": "s", "body": "b"}]})),
    ], cfg=CFG_DISABLED)
    meta = {}
    run_tool_loop(llm, "write_section", "sys", list(msgs), FakeTools(),
                  final_tool=final, meta=meta, final_nudge=W.SUBMIT_NUDGE)
    check("⑦ 重试调用同样带 nudge",
          meta.get("via") == "forced_retry"
          and W.SUBMIT_NUDGE in _flat(llm.seen[2]["messages"][-1]["content"]),
          str(meta))

    # 不传 final_nudge 的调用方（write_plan_tasks 等）：Phase B messages
    # 与改动前逐字一致——nudge 是一处新开关，不许变成默认行为。
    llm = FakeLoopLLM([blocks(txt("a")), blocks(txt("b"))], cfg=CFG_NONE)
    meta = {}
    run_tool_loop(llm, "write_section", "sys", list(msgs), FakeTools(),
                  final_tool=final, meta=meta)
    check("⑦ 不传 final_nudge 时 Phase B messages 不变",
          llm.seen[1]["messages"][-1] == {"role": "user", "content": "p"},
          str(llm.seen[1]["messages"][-1]))


def t_phase_a_exception():
    """Phase A 抛异常（自愿交卷参数被 max_tokens 截断等）→ 直接进 Phase B。"""
    final = W.SUBMIT_SECTION
    msgs = [{"role": "user", "content": "p"}]

    llm = FakeLoopLLM([RuntimeError("args truncated"),
                       blocks(use("submit_section",
                                  {"sections": [{"id": "s", "body": "b"}]}))],
                      cfg=CFG_NONE)
    meta: dict = {}
    text, _, _ = run_tool_loop(llm, "write_section", "sys", list(msgs),
                               FakeTools(), final_tool=final, meta=meta)
    check("⑦ Phase A 首轮异常→Phase B 拿到交卷",
          meta.get("via") == "forced"
          and json.loads(text)["sections"][0]["id"] == "s", str(meta))

    # 读了一轮证据后第二轮抛：此时 messages 末条已是 tool_result，
    # Phase B 的前提（末条是 user）仍成立。
    llm = FakeLoopLLM([
        blocks(use("read_segments", {"segments": ["S1·1"]})),
        RuntimeError("boom"),
        blocks(use("submit_section",
                   {"sections": [{"id": "s", "body": "b"}]})),
    ], cfg=CFG_NONE)
    meta = {}
    text, _, _ = run_tool_loop(llm, "write_section", "sys", list(msgs),
                               FakeTools(), final_tool=final, meta=meta)
    check("⑦ 第二轮异常同样进 Phase B",
          meta.get("via") == "forced" and len(llm.seen) == 3,
          f"{meta} {len(llm.seen)}")

    # final_tool=None 的老路径：异常照常传播，不兜底。
    llm = FakeLoopLLM([RuntimeError("boom")])
    try:
        run_tool_loop(llm, "write_section", "sys", list(msgs), FakeTools())
        check("⑦ final_tool=None 时异常照常传播", False, "没抛")
    except RuntimeError:
        check("⑦ final_tool=None 时异常照常传播", True)


def t_run_task_nudge():
    """生产入口接线：_run_task 发给 Phase B 的末条 user 消息带 SUBMIT_NUDGE。"""
    stub = StubLLM([blocks(txt("正文 [S1·1]。"))])   # 一直回文本 → 进 Phase B
    orig = W.LLM.complete_with_tools
    W.LLM.complete_with_tools = stub.complete_with_tools
    try:
        W._run_task(_state(), T1, None)
    finally:
        W.LLM.complete_with_tools = orig
    check("⑦ _run_task 的 Phase A 首轮不带 nudge",
          len(stub.seen) >= 1
          and W.SUBMIT_NUDGE not in _flat(
              stub.seen[0]["messages"][-1].get("content")),
          _flat(stub.seen[0]["messages"][-1].get("content"))[-40:])
    # write_section 配了 forced_thinking=disabled：强制轮失败会再试一次，
    # Phase B 的两次调用（seen[1:]）的末条消息都该带 nudge。
    check("⑦ _run_task 的 Phase B 调用带 SUBMIT_NUDGE",
          len(stub.seen) >= 2
          and all(W.SUBMIT_NUDGE in _flat(c["messages"][-1].get("content"))
                  for c in stub.seen[1:]),
          str([_flat(c["messages"][-1].get("content"))[-30:]
               for c in stub.seen[1:]]))


def main() -> int:
    print("=== ① _parse_submission ===")
    t_parse_submission()
    print("\n=== ② _run_task（自愿交卷/多节/notes） ===")
    t_run_task()
    print("\n=== ③ fallback 清洗与污染检测 ===")
    t_fallback()
    print("\n=== ④ Phase B 同档位重试 ===")
    t_retry()
    print("\n=== ⑤ provider payload / validate ===")
    t_provider_payload()
    print("\n=== ⑥ _merge_sections 返修保护 ===")
    t_merge()
    print("\n=== ⑦ P2：round_max_tokens / final_nudge / Phase A 异常 ===")
    t_round_cap()
    t_nudge()
    t_phase_a_exception()
    t_run_task_nudge()
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
