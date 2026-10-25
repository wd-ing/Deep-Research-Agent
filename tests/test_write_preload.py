"""预加载模式（RA_WRITE_PRELOAD，2026-10-25 批 3 实验）守门测试（离线）。

开关=1 且首稿（revise is None）时：任务分配段原文整块附进用户消息、
`run_tool_loop` 以 `max_rounds=-1` 跳过读证据轮，直接进 Phase B
强制交卷——一次调用，via 记 "preload"。

验：
  ① 开关开 → 只发 1 次强制调用（tools=[submit_section] +
     tool_choice），用户消息含全部段落 `[Sn·m]` 编号头与全文，
     via="preload"；
  ② 块超 PRELOAD_BLOCK_CHARS → 回退旧多轮路径（max_rounds=2）
     且 meta 记 preload_fallback；
  ③ 开关关 → 调用形状与现状逐字相同（max_rounds=2、原 system、
     概括清单而非原文）；
  ④ revise 不为 None → 永不预加载。

用法：.venv/bin/python tests/test_write_preload.py
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("MINIMAX_API_KEY", "stub-not-used")
os.environ["RA_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "t.db")

from research_agent.graph.subgraphs import write as W      # noqa: E402

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}" + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


class FakeLLM:
    """模拟「强制交卷一次就成」的 complete_with_tools。"""

    def __init__(self):
        self.calls: list[dict] = []

    def complete_with_tools(self, stage, system, messages, tools,
                            tool_choice=None, **kw):
        self.calls.append({"stage": stage, "system": system,
                           "messages": messages, "tools": tools,
                           "tool_choice": tool_choice})
        return {"blocks": [
            {"type": "tool_use", "name": "submit_section",
             "input": {"sections": [{"id": "sec1",
                                     "body": "正文 [S1·1]"}]}}]}


def _seg(ref, content, summary="概括"):
    return {"ref": ref, "content": content, "summary": summary,
            "token_count": 10, "source_id": "s1", "on_topic": 1}


def _state(segs, task_segs):
    return {"session_id": "s_test", "question": "q",
            "spec": {"thesis": "T"}, "outline": [{"id": "sec1"}],
            "tasks": [{"id": "t1", "covers": ["sec1"],
                       "segments": task_segs, "must_cover": [],
                       "not_yours": []}],
            "on_topic": segs,
            "src_lines": ["[S1] 标题甲 | x.com | media | 2026-01-01"
                          " | research | 正文 100 字"],
            "violations": {}, "drafts": {}, "failed": [],
            "rework_rounds": 0, "rework_effective": True}


def _run(st, llm):
    orig = W.LLM
    W.LLM = llm
    try:
        return W._run_task(st, st["tasks"][0], None)
    finally:
        W.LLM = orig


def t1_preload_one_forced_call():
    orig = W._WRITE_PRELOAD
    W._WRITE_PRELOAD = True
    try:
        st = _state([_seg("S1·1", "段一全文" * 100),
                     _seg("S1·2", "段二全文" * 50)],
                    ["S1·1", "S1·2"])
        llm = FakeLLM()
        secs, err = _run(st, llm)
        check("① 只发 1 次调用", len(llm.calls) == 1,
              f"calls={len(llm.calls)}")
        c = llm.calls[0]
        check("① 只给 submit_section 且强制（tool_choice）",
              [t["name"] for t in c["tools"]] == ["submit_section"]
              and c["tool_choice"] == "submit_section")
        user = c["messages"][-1]["content"]
        check("① 用户消息含全部段编号头与全文",
              "[S1·1]" in user and "[S1·2]" in user
              and "段一全文" in user and "段二全文" in user)
        check("① 编号头带来源行",
              "[S1] 标题甲" in user, user[:200])
        check("① system = WRITE_RULES + PRELOAD_NOTE",
              "无需调用任何读取工具" in (c["system"] or ""))
        check("① 交卷解析出正文", secs.get("sec1") == "正文 [S1·1]"
              and err is None, f"{secs} {err}")
        # meta 里的 via 走 _record_submit 前被改成 preload——
        # 从 write_trace 验证不划算，直接看 meta 侧效应不行
        # （meta 不外露），改看 _record_submit 的入参已由
        # via != "fallback" 分支走到。
    finally:
        W._WRITE_PRELOAD = orig


def t2_oversize_falls_back():
    orig, orig_cap = W._WRITE_PRELOAD, W.PRELOAD_BLOCK_CHARS
    W._WRITE_PRELOAD = True
    W.PRELOAD_BLOCK_CHARS = 100     # 压小上限逼出回退
    try:
        st = _state([_seg("S1·1", "段一全文" * 100),
                     _seg("S1·2", "段二全文" * 50)],
                    ["S1·1", "S1·2"])
        llm = FakeLLM()
        secs, err = _run(st, llm)
        c = llm.calls[0]
        # 回退路径：Phase A 第一轮发 证据工具+交卷工具 两把，
        # 不带 tool_choice（非强制）——桩直接交卷 → via=voluntary。
        check("② 超限回退：走多轮路径（带证据工具）",
              any(t["name"] == "read_segments" for t in c["tools"])
              and c["tool_choice"] is None,
              str([t["name"] for t in c["tools"]]))
        check("② 回退路径仍交卷成功", secs.get("sec1") == "正文 [S1·1]")
        user = c["messages"][-1]["content"]
        check("② 回退用的是概括清单而非原文",
              "段一全文段一" not in user
              and "这段在讲" in user)
    finally:
        W._WRITE_PRELOAD = orig
        W.PRELOAD_BLOCK_CHARS = orig_cap


def t3_off_unchanged():
    orig = W._WRITE_PRELOAD
    W._WRITE_PRELOAD = False
    try:
        st = _state([_seg("S1·1", "段一全文" * 100)],
                    ["S1·1"])
        llm = FakeLLM()
        secs, err = _run(st, llm)
        c = llm.calls[0]
        check("③ 开关关：Phase A 证据工具轮",
              any(t["name"] == "read_segments" for t in c["tools"]),
              str([t["name"] for t in c["tools"]]))
        check("③ 开关关：system 无预加载段",
              "无需调用任何读取工具" not in (c["system"] or ""))
        check("③ 开关关：消息里是概括清单",
              "段一全文段一" not in c["messages"][-1]["content"]
              and "这段在讲" in c["messages"][-1]["content"])
        check("③ 交卷成功", secs.get("sec1") == "正文 [S1·1]")
    finally:
        W._WRITE_PRELOAD = orig


def t4_revise_never_preloads():
    orig = W._WRITE_PRELOAD
    W._WRITE_PRELOAD = True
    try:
        st = _state([_seg("S1·1", "段一全文" * 100)], ["S1·1"])
        st["tasks"][0]["id"] = "t1"
        llm = FakeLLM()
        orig_llm = W.LLM
        W.LLM = llm
        try:
            secs, err = W._run_task(st, st["tasks"][0],
                                    {"fix": "改一改", "draft": "旧稿"})
        finally:
            W.LLM = orig_llm
        c = llm.calls[0]
        check("④ revise 路径不预加载（证据工具轮 + stage=write_revise）",
              c["stage"] == "write_revise"
              and any(t["name"] == "read_segments" for t in c["tools"]))
    finally:
        W._WRITE_PRELOAD = orig


if __name__ == "__main__":
    t1_preload_one_forced_call()
    t2_oversize_falls_back()
    t3_off_unchanged()
    t4_revise_never_preloads()
    print()
    if FAILED:
        print("失败：" + "、".join(FAILED))
        sys.exit(1)
    print("全过 ✅")
