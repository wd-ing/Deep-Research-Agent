"""末轮审查跳过模型（审查策略 A）的守门测试（离线）。

2026-10-25 时间优化：`rework_rounds >= MAX_REWORK` 时 node_audit 不调
模型，走合成结论（`_audit_skip_final`）——末轮审查只负责把残留问题
收进存疑节，而 violations/revise_unresolved/blocked 都是程序产出的。
v18 基线三次审查 127/62/162s，末次是纯等待。

验：
  ① rounds==MAX_REWORK → 零 LLM 调用、audit.skipped、need_rework=False、
     上轮 blocked/remaining_issues 原样带进、路由去 final；
  ② 存疑节照常收 violations 与返修残留（返修后重算的 violations 在
     state 里——node_revise 已是重算版）；
  ③ rounds<MAX_REWORK → 行为与现状完全一致（模型照常被调，回归）。

用法：.venv/bin/python tests/test_audit_skip.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research_agent.graph.subgraphs import write as W   # noqa: E402

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}" + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


class CountLLM:
    """挂 complete_json_forced：数调用、可脚本化返回。"""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = 0

    def complete_json_forced(self, stage, prompt, name, schema,
                             system=None):
        self.calls += 1
        if not self.script:
            raise AssertionError("末轮审查不该调模型")
        return self.script.pop(0)


TASKS = [{"id": "t1", "covers": ["sec1"], "segments": [], "must_cover": []},
         {"id": "t2", "covers": ["sec2"], "segments": [], "must_cover": []}]
OUTLINE = [{"id": "sec1", "title": "术语边界"},
           {"id": "sec2", "title": "路线分歧"}]


def _state(rounds: int, **kw) -> dict:
    st = {"session_id": None, "question": "q", "spec": {"thesis": "T"},
          "assembled": "草稿正文 [S1·1]",
          "outline": OUTLINE, "tasks": TASKS,
          "rework_rounds": rounds, "rework_effective": True,
          "violations": {}, "revise_unresolved": [],
          "failed": [], "drafts": {}, "src_lines": [],
          "audit": {}}
    st.update(kw)
    return st


def _with_llm(stub, fn):
    orig = W.LLM.complete_json_forced
    W.LLM.complete_json_forced = stub.complete_json_forced
    try:
        return fn()
    finally:
        W.LLM.complete_json_forced = orig


def t1_final_audit_skipped():
    prev_audit = {
        "need_rework": True,
        "blocked": [{"task": "t1", "section": "sec1",
                     "kind": "no_material", "issue": "没材料证明 X"}],
        "remaining_issues": ["[no_material] 没材料证明 X"],
        "spec_issue": "规约问题 P",
        "findings": [{"task": "t1", "severity": "high"}],
        "targets": [{"task": "t1", "section": "sec1", "action": "edit"}],
        "model_high": 1,
    }
    violations = {"t1": [{"kind": "越界引用", "found": "S1·9"}]}
    unresolved = [{"task": "t2", "section": "sec2",
                   "problem": "改不了 Y", "reason": "没证据"}]
    st = _state(W.MAX_REWORK, audit=prev_audit,
                violations=violations, revise_unresolved=unresolved,
                rework_effective=False)

    stub = CountLLM()
    out = _with_llm(stub, lambda: W.node_audit(st))
    a = out["audit"]
    check("① 零 LLM 调用", stub.calls == 0, f"calls={stub.calls}")
    check("① skipped + need_rework=False + reason",
          a.get("skipped") is True and a.get("need_rework") is False
          and "返修上限" in (a.get("reason") or ""))
    check("① 上轮 blocked/remaining 带进", a["blocked"] == prev_audit["blocked"]
          and a["remaining_issues"] == prev_audit["remaining_issues"])
    check("① spec_issue 沿用", a.get("spec_issue") == "规约问题 P")
    check("① rework_effective 保持 state 值", a["rework_effective"] is False)
    ev = (out.get("events") or [{}])[0]
    check("① 事件是独立 kind（不是 write_audit）",
          ev.get("kind") == "write_audit_skipped", str(ev.get("kind")))

    # 路由：need_rework=False → final
    check("① 路由去 final",
          W.route_after_audit({**st, "audit": a}) == "final")

    # 存疑节：violations（返修后重算的 state 值）+ revise_unresolved + 上轮残留
    fin = W.node_final({**st, "audit": a})
    md = fin["report_markdown"]
    check("① 存疑节收 violations（t1 已翻译成节名）",
          "「术语边界」的写作任务 的引用存在问题" in md and "S1·9" in md)
    check("① 存疑节收返修残留（节名翻译过）",
          "返修未能解决（「路线分歧」）" in md, md[md.find("存疑"):][:200])
    check("① 存疑节收上轮 remaining_issues", "没材料证明 X" in md)
    check("① 「末轮未复核」不进正文", "末轮" not in md and "复核" not in md)


def t2_mid_round_unchanged():
    """rounds=1 < MAX_REWORK：模型照常调，返回什么算什么（回归）。"""
    stub = CountLLM([{"need_rework": False, "reason": "好",
                      "findings": [], "blocked": [],
                      "rework_check": [{"task": "t1", "section": "sec1",
                                        "status": "fixed"}],
                      "remaining_issues": []}])
    st = _state(1, audit={"model_high": 1, "targets": [{"task": "t1"}]})
    out = _with_llm(stub, lambda: W.node_audit(st))
    ev = (out.get("events") or [{}])[0]
    check("② 模型被调一次", stub.calls == 1)
    check("② 事件是 write_audit（非 skipped）",
          ev.get("kind") == "write_audit", str(ev.get("kind")))
    check("② audit 无 skipped 标记",
          out["audit"].get("skipped") is not True)


if __name__ == "__main__":
    t1_final_audit_skipped()
    t2_mid_round_unchanged()
    print()
    if FAILED:
        print("失败：" + "、".join(FAILED))
        sys.exit(1)
    print("全过 ✅")
