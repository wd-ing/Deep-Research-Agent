"""批 5 守门（离线）：_reject_broken_args 三类文案+插桩、
submit_spec 路径、RA_SPEC_SLIM/RA_AUDIT_TRIM 开关。

不发任何真实调用。用法：.venv/bin/python tests/test_batch5_flags.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _isolate  # noqa: F401, E402
os.environ.setdefault("MINIMAX_API_KEY", "stub-not-used")

from research_agent.llm import provider as P            # noqa: E402
from research_agent.graph.subgraphs import write as W   # noqa: E402
from research_agent.graph import prompts                 # noqa: E402

FAILED: list[str] = []


def check(label, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {label}"
          + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


# ---------------- A：_reject_broken_args 三类文案 + 插桩字段 ----------------

def _reject(blocks, fr):
    try:
        P._reject_broken_args(blocks, "write_section", fr)
        return ""
    except P.EmptyOutput as e:
        return str(e)


def t_reject_wording():
    bad = {"type": "tool_use", "name": "submit_section", "input": {},
           "_args_broken": True,
           "_diag": {"args_chars": 1, "args_head": "{", "args_tail": "{",
                     "args_frag_events": 1, "json_delta_events": 1,
                     "message_delta": True, "message_stop": True,
                     "stop_reason": None, "stream_span_s": 45.81,
                     "idle_before_reject_s": 0.0, "stream_error": None}}
    e = _reject([bad], "max_tokens")
    check("max_tokens→预算文案", "max_tokens 截断" in e and "预算" in e)
    e = _reject([bad], "length")
    check("length→同判预算", "max_tokens" in e)
    e = _reject([bad], None)
    check("stop_reason 缺失→非预算文案", "不是预算问题" in e
          and "max_tokens 截断" not in e)
    e = _reject([bad], "tool_use")
    check("其他 stop_reason→非法 JSON 文案",
          "tool_use" in e and "不是预算问题" in e)
    check("诊断字段落 error（args_head/span/delta 计数）",
          '"args_head": "{"' in e and "stream_span_s" in e
          and "json_delta_events" in e, e[-200:])
    check("error 总长 ≤1500", len(e) <= 1500, str(len(e)))
    e = _reject([{"type": "tool_use", "name": "x", "input": {},
                  "_args_broken": True}], None)
    check("无 _diag 不炸且分类文案仍在", "不是预算问题" in e)


def t_accum_diag():
    acc = P.AnthropicSSEAccum()
    acc.feed({"type": "message_start",
              "message": {"usage": {"input_tokens": 10}}})
    acc.feed({"type": "content_block_start", "index": 0,
              "content_block": {"type": "tool_use", "id": "t1",
                                "name": "submit_section"}})
    acc.feed({"type": "content_block_delta", "index": 0,
              "delta": {"type": "input_json_delta",
                        "partial_json": "{xx not json"}})
    acc.feed({"type": "message_delta",
              "delta": {}, "usage": {"output_tokens": 5}})
    acc.feed({"type": "message_stop"})
    bl = acc.blocks()
    b = bl[0]
    check("Anthropic 流：broken 块带 _diag 全字段",
          b.get("_args_broken") and b.get("_diag", {}).get("args_chars") == 12
          and b["_diag"]["json_delta_events"] == 1
          and b["_diag"]["message_delta"] is True
          and b["_diag"]["message_stop"] is True
          and b["_diag"]["stop_reason"] is None,
          str(b.get("_diag"))[:200])
    acc2 = P.SSEAccum()
    acc2.feed({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"name": "s", "arguments": "{bad"}}]}}]})
    b2 = acc2.blocks()[0]
    check("OpenAI 流：broken 块带 _diag",
          b2.get("_args_broken")
          and b2["_diag"]["args_frag_events"] == 1
          and b2["_diag"]["args_head"] == "{bad", str(b2.get("_diag"))[:160])


# ---------------- B：submit_spec 路径 ----------------

class StubLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def complete_json_forced(self, stage, prompt, name, schema, system=None):
        self.calls.append({"stage": stage, "name": name,
                           "schema": schema, "system": system})
        r = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


SPEC_OK = {"thesis": "T", "sections": [{"id": "sec1"}],
           "spec_uncertainty": "u",
           "glossary": [], "units": [], "cautions": []}


def _state():
    return {"question": "q", "outline": [{"id": "sec1", "title": "t"}],
            "on_topic": [], "segments": [], "src_lines": [],
            "signals": {}, "session_id": None}


def _run_spec(script, slim=False):
    stub = StubLLM(script)
    o1 = W.LLM.complete_json_forced
    o2 = W._SPEC_SLIM
    W.LLM.complete_json_forced = stub.complete_json_forced
    W._SPEC_SLIM = slim
    try:
        out = W.node_plan_spec(_state())
    finally:
        W.LLM.complete_json_forced = o1
        W._SPEC_SLIM = o2
    return stub, out


def t_submit_spec():
    stub, out = _run_spec([SPEC_OK])
    check("submit_spec 成功：spec 键齐",
          out["spec"].get("thesis") == "T"
          and out["spec"].get("spec_uncertainty") == "u")
    c = stub.calls[0]
    # 批 6 起 schema 是按大纲 n 深拷贝加 minItems 的副本，不是常量本体
    check("走 complete_json_forced + submit_spec + SCHEMA 副本",
          c["name"] == "submit_spec"
          and c["schema"]["properties"]["sections"].get("minItems") == 1
          and "minItems" not in
          W.SPEC_SCHEMA["properties"]["sections"])
    check("系统提示词默认不含 SLIM_NOTE",
          prompts.WRITE_PLAN_SPEC_SLIM_NOTE not in c["system"])
    # 缺必填键 → 重试一次救回
    stub, out = _run_spec([{"thesis": "x"}, SPEC_OK])
    check("缺键→重试救回（2 次调用）",
          len(stub.calls) == 2 and out["spec"].get("thesis") == "T")
    # 两次都坏 → plan_failed 降级（与旧语义同）
    stub, out = _run_spec([{"x": 1}, {"y": 2}])
    check("两次坏→plan_failed 降级",
          "plan_failed" in out and out["spec" if "spec" in out else
                                      "plan_failed"] != "",
          str(list(out))[:80])
    check("坏入参事件是 write_plan_error",
          out.get("events", [{}])[0].get("kind") == "write_plan_error")
    # slim=1 → 系统提示词含 SLIM_NOTE 且原提示词未被改写
    stub, out = _run_spec([SPEC_OK], slim=True)
    check("SLIM=1 → 提示词=原文+NOTE",
          stub.calls[0]["system"].startswith(prompts.WRITE_PLAN_SPEC[:80])
          and prompts.WRITE_PLAN_SPEC_SLIM_NOTE in stub.calls[0]["system"])
    check("默认 WRITE_PLAN_SPEC 未被改字节（含 submit_spec 话术）",
          "submit_spec" in prompts.WRITE_PLAN_SPEC
          and "严格输出 JSON" not in prompts.WRITE_PLAN_SPEC)


# ---------------- C：RA_AUDIT_TRIM ----------------

def t_audit_trim():
    check("_AUDIT_TRIM 默认 True（RA_AUDIT_TRIM=0 退回）",
          W._AUDIT_TRIM is True)
    check("_SPEC_SLIM 默认 True（RA_SPEC_SLIM=0 退回）",
          W._SPEC_SLIM is True)
    check("WRITE_AUDIT_TRIM_NOTE 原文含 mid/low≤3、reason≤60",
          "合计最多 3 条" in prompts.WRITE_AUDIT_TRIM_NOTE
          and "60 字" in prompts.WRITE_AUDIT_TRIM_NOTE)
    # node_audit 的 system 拼装：桩 LLM 看 system 参数
    seen = {}

    class S:
        def complete_json_forced(self, stage, prompt, name, schema,
                                 system=None):
            seen["system"] = system
            return {"need_rework": False, "reason": "ok",
                    "findings": [], "blocked": []}

    st = {"question": "q", "spec": {}, "assembled": "draft",
          "violations": {}, "tasks": [], "session_id": None,
          "rework_rounds": 0}
    for flag, want in ((False, False), (True, True)):
        o1, o2 = W.LLM.complete_json_forced, W._AUDIT_TRIM
        W.LLM.complete_json_forced = S().complete_json_forced
        W._AUDIT_TRIM = flag
        try:
            W.node_audit(dict(st))
        finally:
            W.LLM.complete_json_forced = o1
            W._AUDIT_TRIM = o2
        check(f"AUDIT_TRIM={flag} → NOTE {'在' if want else '不在'}提示词",
              (prompts.WRITE_AUDIT_TRIM_NOTE in (seen.get("system") or ""))
              is want)
    check("WRITE_AUDIT 原文未被改", "输出篇幅约束" not in
          prompts.WRITE_AUDIT)


def main():
    t_reject_wording()
    t_accum_diag()
    t_submit_spec()
    t_audit_trim()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项未过：{FAILED}")
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    main()
