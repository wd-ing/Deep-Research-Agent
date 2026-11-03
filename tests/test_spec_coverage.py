"""批 6-A：spec 覆盖保险三层（提示词动态 id / schema minItems / 缺项补占位）。

v24 回归：8 节大纲 submit_spec 只交 1 项 sections。全部离线。
用法：.venv/bin/python tests/test_spec_coverage.py
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _isolate  # noqa: F401, E402

from research_agent.graph.subgraphs import write as W    # noqa: E402
from research_agent.graph import prompts                 # noqa: E402

FAILED: list[str] = []


def check(label, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {label}"
          + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


class StubLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def complete_json_forced(self, stage, prompt, name, schema, system=None):
        self.calls.append({"schema": schema, "system": system})
        r = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


def _spec(sec_ids, **kw):
    d = {"thesis": "T", "spec_uncertainty": "u",
         "glossary": [], "units": [], "cautions": [],
         "sections": [{"id": i, "role": "r", "must_cover": [],
                       "not_yours": []} for i in sec_ids]}
    d.update(kw)
    return d


def _state(outline_ids):
    return {"question": "q",
            "outline": [{"id": i, "title": f"第{i}节"} for i in outline_ids],
            "on_topic": [], "segments": [], "src_lines": [],
            "signals": {}, "session_id": None}


def _run(script, outline_ids):
    stub = StubLLM(script)
    o = W.LLM.complete_json_forced
    W.LLM.complete_json_forced = stub.complete_json_forced
    try:
        out = W.node_plan_spec(_state(outline_ids))
    finally:
        W.LLM.complete_json_forced = o
    return stub, out


def main():
    IDS = ["sec1", "sec2", "sec3"]

    # (a) 首次缺 id、第二次齐全 → 不 stub，sections_missing=[]
    stub, out = _run([_spec(["sec1"]), _spec(IDS)], IDS)
    check("a 重试救回：2 次调用、不 stub",
          len(stub.calls) == 2 and "plan_failed" not in out)
    ev = out["events"][0]
    check("a sections_missing=[] 且未 stub",
          ev.get("sections_missing") == []
          and ev.get("sections_stubbed") is False, str(ev)[:150])

    # (b) 两次都缺 → 补占位、事件含缺失、不 plan_failed
    stub, out = _run([_spec(["sec1"]), _spec(["sec1", "sec2"])], IDS)
    check("b 两次缺仍不 plan_failed",
          "plan_failed" not in out and len(stub.calls) == 2)
    secs = {s["id"]: s for s in out["spec"]["sections"]}
    check("b 缺 id 补占位（sec3 空 role/空数组）",
          set(secs) == set(IDS) and secs["sec3"]["role"] == ""
          and secs["sec3"]["must_cover"] == []
          and secs["sec3"]["not_yours"] == [],
          str(list(secs)))
    ev = out["events"][0]
    check("b 事件 sections_missing=['sec3']、stubbed",
          ev.get("sections_missing") == ["sec3"]
          and ev.get("sections_stubbed") is True, str(ev)[:200])

    # (c) 一次齐全 → 一次调用
    stub, out = _run([_spec(IDS)], IDS)
    check("c 一次齐全：1 次调用",
          len(stub.calls) == 1 and out["events"][0]
          .get("sections_stubbed") is False)

    # (d) 大纲为空 → 旧行为（不追加 id 段、schema 是全局常量、不校验覆盖）
    stub, out = _run([_spec(["sec9"])], [])
    check("d 空大纲：1 次调用、接受",
          len(stub.calls) == 1 and "plan_failed" not in out)
    check("d 无动态 id 段、schema 未动",
          "必须逐个覆盖" not in stub.calls[0]["system"]
          and stub.calls[0]["schema"] is W.SPEC_SCHEMA)

    # (e) 提示词动态段 + minItems 随 n + 不污染全局常量
    stub, out = _run([_spec(IDS)], IDS)
    sys_prompt = stub.calls[0]["system"]
    check("e 提示词含动态 id 段",
          "必须逐个覆盖已批框架的全部小节" in sys_prompt
          and "共 3 个" in sys_prompt and "sec1、sec2、sec3" in sys_prompt,
          sys_prompt[-160:])
    sch = stub.calls[0]["schema"]
    check("e schema minItems=3（深拷贝）",
          sch["properties"]["sections"].get("minItems") == 3
          and sch is not W.SPEC_SCHEMA)
    check("e 全局 SPEC_SCHEMA 未被污染",
          "minItems" not in W.SPEC_SCHEMA["properties"]["sections"])
    # 深拷贝未改原常量（再验证一次 n 变化）
    sch2 = copy.deepcopy(W.SPEC_SCHEMA)
    check("e 常量不变式", "minItems" not in
          W.SPEC_SCHEMA["properties"]["sections"])

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项未过：{FAILED}")
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    main()
