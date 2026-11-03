"""批 6-B：submit_patches 入参偏离 schema 被拒时，事件与 span 留原始入参。

v24：3 条补丁 2 条 rejected=「submit_patches 入参偏离 schema」，
但 raw 没存库无法诊断。不改拒绝规则，只加证据。全部离线。
用法：.venv/bin/python tests/test_patch_reject.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _isolate  # noqa: F401, E402

from research_agent.graph.subgraphs import write as W    # noqa: E402

FAILED: list[str] = []


def check(label, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {label}"
          + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


class PatchLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def complete_json_forced(self, stage, prompt, name, schema, system=None):
        self.calls.append({"stage": stage})
        i = len([c for c in self.calls if c["stage"] == stage]) - 1
        seq = self.script if stage == "write_patch" else []
        r = seq[min(i, len(seq) - 1)] if seq else Exception("unexpected")
        if isinstance(r, Exception):
            raise r
        return r


def _task(tid):
    return {"id": tid, "covers": ["sec1"], "mandate": "m",
            "segments": ["S3·1"], "must_cover": [], "not_yours": []}


def _run(state, patch_script):
    llm = PatchLLM(patch_script)
    captured = {"run_calls": []}
    o_llm, o_run = W.LLM.complete_json_forced, W._run_task
    W.LLM.complete_json_forced = llm.complete_json_forced
    W._run_task = lambda s, t, revise=None: (
        captured["run_calls"].append(t["id"]), ({"sec1": "重写后正文"}, None))
    try:
        out = W.node_revise(state)
    finally:
        W.LLM.complete_json_forced = o_llm
        W._run_task = o_run
    return llm, captured, out


def main():
    state = {"session_id": None, "question": "q",
             "spec": {"thesis": "T"},
             "outline": [{"id": "sec1", "title": "第一节"}],
             "tasks": [_task("t1")], "drafts": {"t1": {"sec1": "原稿正文"}},
             "failed": [], "violations": {}, "segments": [],
             "on_topic": [], "src_lines": [], "rework_rounds": 0,
             "audit": {"targets": [{"task": "t1", "section": "sec1",
                                    "problem": "p", "fix": "f",
                                    "quote": "原稿", "action": "edit"}]}}
    bad_raw = {"patch": [{"item": 0, "old": "x", "new": "y"}],
               "extra": "模型把 patches 写成 patch 且漏了 unresolved",
               "deep": {"nested": [1, 2, 3]}}
    # 两次调用都偏离 → 第 2 次拒绝（patch_calls=2），落 patch_reject
    llm, cap, out = _run(state, [bad_raw, bad_raw])
    patch = (out["events"][0].get("patch") or {})
    check("rejected 里有这条（行为未变）",
          any(r.get("task") == "t1" and "入参偏离" in r.get("reason", "")
              for r in patch.get("rejected") or []),
          str(patch.get("rejected"))[:200])
    pr = patch.get("patch_reject") or []
    check("patch_reject 事件含 task/section/reason/raw",
          len(pr) == 1 and pr[0]["task"] == "t1"
          and pr[0]["section"] == "sec1"
          and "入参偏离" in pr[0]["reason"]
          and "patch" in pr[0]["raw"] and "unresolved" in pr[0]["raw"],
          str(pr)[:300])
    check("raw 截断 ≤3000", len(pr[0]["raw"]) <= 3000)
    check("回退整节重写仍发生（行为未变）",
          cap["run_calls"] == ["t1"], str(cap["run_calls"]))
    check("fallback_sections 记了 sec1",
          [t for t, s in patch.get("fallback_sections") or []
           if s == "sec1"], str(patch.get("fallback_sections"))[:100])

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项未过：{FAILED}")
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    main()
