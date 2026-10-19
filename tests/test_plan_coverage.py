"""`node_plan_tasks` 小节覆盖校验的守门测试（离线，不需要网络）。

2026-10-20 加。E1 提示词实验实测：12 次真实规划里 2 次模型交了
「1 个任务、covers 只有 sec1、其余 8 节没有任何任务」的合法但无用的
计划（/tmp/exp_ab/e1_raw/A_4.json、B_5.json），`plan_failed` 为 null、
没有任何告警——生产里直接变成 8 节「没有任务覆盖本节」。

现在 node_plan_tasks 在 `_normalize_tasks` 后核对 outline 覆盖：
有漏 → 带「上一次交卷有问题」清单重试一次，两次取覆盖更多的那次
（并列取第二次）；全覆盖路径调用次数不变。

用法：
    .venv/bin/python tests/test_plan_coverage.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import os  # noqa: E402

os.environ.setdefault("MINIMAX_API_KEY", "stub-not-used")

from research_agent.graph.subgraphs import write as W  # noqa: E402

OUTLINE = [{"id": "sec1"}, {"id": "sec2"}, {"id": "sec3"}]


def _task(tid: str, covers: list[str]) -> dict:
    return {"id": tid, "covers": covers, "mandate": "m",
            "segments": ["S1·1"], "must_cover": [], "not_yours": []}


ONLY_SEC1 = {"tasks": [_task("t1", ["sec1"])]}
PARTIAL_2 = {"tasks": [_task("t1", ["sec1"]), _task("t2", ["sec2"])]}
FULL = {"tasks": [_task("t1", ["sec1"]), _task("t2", ["sec2"]),
                  _task("t3", ["sec3"])]}
# covers 写两节的单任务：sec1+sec2 覆盖两节，剩 sec3 漏。
FUSED = {"tasks": [_task("t1", ["sec1", "sec2"])]}
FUSED_ALL = {"tasks": [_task("t1", ["sec1", "sec2", "sec3"])]}


def _state(sid: str = "") -> dict:
    return {"session_id": sid, "question": "q",
            "spec": {"thesis": "T"}, "outline": list(OUTLINE),
            "on_topic": [{"ref": "S1·1", "summary": "a"}]}


class FakeCompleteJson:
    """`LLM.complete_json` 桩：按脚本返回 dict 或抛异常。"""

    def __init__(self, script: list):
        self.script = list(script)
        self.prompts: list[str] = []

    def __call__(self, stage, prompt, system=None):
        self.prompts.append(prompt)
        r = self.script[min(len(self.prompts) - 1, len(self.script) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


class FakeLoop:
    """`run_tool_loop` 桩：回交卷 JSON，记录 messages 末位 user prompt。"""

    def __init__(self, script: list):
        self.script = list(script)
        self.user_prompts: list[str] = []

    def __call__(self, llm, stage, system, messages, provider,
                 max_rounds=3, final_tool=None):
        self.user_prompts.append(messages[-1]["content"])
        r = self.script[min(len(self.user_prompts) - 1,
                            len(self.script) - 1)]
        if isinstance(r, Exception):
            raise r
        return json.dumps(r, ensure_ascii=False), [], [[]]


FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}"
          + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def t1_partial_then_full_retries():
    """首次只盖 sec1 → 重试后全覆盖：用第二次结果、retried=True、调 2 次。"""
    loop = FakeLoop([ONLY_SEC1, FULL])
    cj = FakeCompleteJson([FULL])
    orig_loop, orig_cj = W.run_tool_loop, W.LLM.complete_json
    W.run_tool_loop, W.LLM.complete_json = loop, cj
    try:
        out = W.node_plan_tasks(_state("s_x"))
    finally:
        W.run_tool_loop, W.LLM.complete_json = orig_loop, orig_cj
    ev = (out.get("events") or [{}])[0]
    check("① 工具循环调了 2 次", len(loop.user_prompts) == 2,
          f"实际 {len(loop.user_prompts)}")
    check("① 没用 complete_json 兜底", len(cj.prompts) == 0)
    check("① 取了第二次（全覆盖）",
          sorted(t["covers"] for t in out["tasks"])
          == [["sec1"], ["sec2"], ["sec3"]],
          json.dumps([t["covers"] for t in out["tasks"]]))
    check("① plan_retried=True", ev.get("plan_retried") is True)
    check("① uncovered_sections 已清空",
          ev.get("uncovered_sections") == [], str(ev.get("uncovered_sections")))
    check("① 重试 prompt 带了漏节清单",
          "上一次交卷有问题" in loop.user_prompts[1]
          and "sec2" in loop.user_prompts[1]
          and "sec3" in loop.user_prompts[1]
          and "sec1" in loop.user_prompts[1],
          loop.user_prompts[1][-200:])


def t2_both_partial_keeps_better():
    """两次都漏 → 取覆盖更多的一次，事件记 uncovered_sections。"""
    # 第一次只盖 sec1（漏 2 节），第二次盖 sec1+sec2（漏 1 节）→ 取第二次。
    cj = FakeCompleteJson([ONLY_SEC1, PARTIAL_2])
    orig = W.LLM.complete_json
    W.LLM.complete_json = cj
    try:
        out = W.node_plan_tasks(_state(""))
    finally:
        W.LLM.complete_json = orig
    ev = (out.get("events") or [{}])[0]
    check("② 取了覆盖更多的一次",
          sorted(t["covers"] for t in out["tasks"])
          == [["sec1"], ["sec2"]],
          json.dumps([t["covers"] for t in out["tasks"]]))
    check("② uncovered_sections 记着 sec3",
          ev.get("uncovered_sections") == ["sec3"],
          str(ev.get("uncovered_sections")))
    check("② plan_retried=True", ev.get("plan_retried") is True)
    check("② 共调 2 次 complete_json", len(cj.prompts) == 2,
          f"实际 {len(cj.prompts)}")


def t3_full_coverage_single_call():
    """首次全覆盖 → 不多调：complete_json 恰好 1 次。"""
    cj = FakeCompleteJson([FULL])
    orig = W.LLM.complete_json
    W.LLM.complete_json = cj
    try:
        out = W.node_plan_tasks(_state(""))
    finally:
        W.LLM.complete_json = orig
    ev = (out.get("events") or [{}])[0]
    check("③ 全覆盖只调 1 次", len(cj.prompts) == 1,
          f"实际 {len(cj.prompts)}")
    check("③ plan_retried=False", ev.get("plan_retried") is False)
    check("③ uncovered_sections 为空",
          ev.get("uncovered_sections") == [])
    check("③ 任务齐 3 个", len(out.get("tasks") or []) == 3)


def t4_retry_raises_keeps_first():
    """重试两次调用都抛 → 保留首次结果，不崩、不 plan_failed。"""
    cj = FakeCompleteJson(
        [ONLY_SEC1, RuntimeError("MiniMax 529"), RuntimeError("boom")])
    orig = W.LLM.complete_json
    W.LLM.complete_json = cj
    try:
        out = W.node_plan_tasks(_state(""))
    finally:
        W.LLM.complete_json = orig
    ev = (out.get("events") or [{}])[0]
    check("④ 没崩、没 plan_failed", out.get("plan_failed") is None,
          f"plan_failed={out.get('plan_failed')}")
    check("④ 保留了首次结果",
          [t["covers"] for t in out["tasks"]] == [["sec1"]])
    check("④ plan_retried=True", ev.get("plan_retried") is True)
    check("④ uncovered 仍是 sec2/sec3",
          ev.get("uncovered_sections") == ["sec2", "sec3"])


def t5_fused_task_covers_two():
    """一个任务 covers 两节 → 两节都算覆盖；sec3 漏 → 触发重试。"""
    # 第一次 covers=[sec1,sec2]（漏 sec3）；第二次单任务盖三节 → 取第二次。
    cj = FakeCompleteJson([FUSED, FUSED_ALL])
    orig = W.LLM.complete_json
    W.LLM.complete_json = cj
    try:
        out = W.node_plan_tasks(_state(""))
    finally:
        W.LLM.complete_json = orig
    ev = (out.get("events") or [{}])[0]
    check("⑤ covers 两节算覆盖两节、重试后取第二次",
          out["tasks"][0]["covers"] == ["sec1", "sec2", "sec3"])
    check("⑤ uncovered 清空", ev.get("uncovered_sections") == [])


def t6_retry_prompt_verbatim():
    """重试追加文案逐字钉死（含 covered/uncovered 填值）。"""
    loop = FakeLoop([ONLY_SEC1, FULL])
    cj = FakeCompleteJson([FULL])
    orig_loop, orig_cj = W.run_tool_loop, W.LLM.complete_json
    W.run_tool_loop, W.LLM.complete_json = loop, cj
    try:
        W.node_plan_tasks(_state("s_x"))
    finally:
        W.run_tool_loop, W.LLM.complete_json = orig_loop, orig_cj
    tail = loop.user_prompts[1]
    frag = ("【上一次交卷有问题】你上次交的任务只覆盖了 sec1，"
            "下面这些小节没有任何任务：sec2、sec3。\n"
            "**每个小节至少要被一个任务的 `covers` 覆盖**"
            "（可以合并、可以拆分，但不能漏）。证据极少的小节照样建任务，"
            "在 `mandate` 里写明「材料只够讲 X，其余如实标注缺口」。"
            "请重新交卷，覆盖全部小节。")
    check("⑥ 重试文案逐字在 user prompt 末尾", frag in tail,
          tail[-300:])


def main() -> int:
    print("=== node_plan_tasks 小节覆盖校验 ===")
    for fn in (t1_partial_then_full_retries, t2_both_partial_keeps_better,
               t3_full_coverage_single_call, t4_retry_raises_keeps_first,
               t5_fused_task_covers_two, t6_retry_prompt_verbatim):
        fn()
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
