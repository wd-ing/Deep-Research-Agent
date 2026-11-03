"""无概括段落不再被静默排除的守门测试（2026-10-20）。

背景：MiniMax 529 过载期 summarize 批次整批失败，`_brief` 的
`if summary.strip()` 把 44/162 个相关段落滤出规划材料——分不到
任务、写手永远看不到。现在 `_seg_summary` 拿原文开头/标题顶替，
`_brief`/写手清单/审查清单三处统一走它，事件带
`fallback_summary_segments` 计数。

不联网：`_brief`/`_seg_summary`/`_seg_fallback_count` 是纯函数；
写手 listing 那处直接断言 `_run_task` 拼的 prompt 文本。

用法：
    .venv/bin/python tests/test_fallback_summary.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("MINIMAX_API_KEY", "stub-not-used")
os.environ["RA_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "t.db")

from research_agent.graph.subgraphs import write as W  # noqa: E402

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}"
          + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


SEG_OK = {"ref": "S1·1", "heading_path": "一、概况", "summary": "正常概括",
          "content": "正文全文" * 100, "token_count": 100}
SEG_NOSUM = {"ref": "S1·2", "heading_path": "二、路线", "summary": "",
             "content": "第一段话。\n\n第二段话。" + "长正文" * 200,
             "token_count": 600}
SEG_NOBODY = {"ref": "S1·3", "heading_path": "三、分歧", "summary": None,
              "content": "", "token_count": 10}


def _state(segs) -> dict:
    return {"question": "q", "outline": [{"id": "sec1", "title": "t",
                                          "question": "q1",
                                          "kind": "evidence"}],
            "signals": {}, "src_lines": [],
            "on_topic": list(segs), "session_id": ""}


def t1_seg_summary():
    check("① 有概括原样返回",
          W._seg_summary(SEG_OK) == "正常概括")
    got = W._seg_summary(SEG_NOSUM)
    check("① 无概括 → [无概括·取原文开头] + 前80字去换行",
          got.startswith("[无概括·取原文开头] ")
          and "\n" not in got
          and "第一段话。 第二段话。" in got,
          got[:100])
    check("① 前缀后正文 ≤80 字",
          len(got) - len("[无概括·取原文开头] ") <= 80,
          f"len={len(got)}")
    check("① 无概括也无原文 → [无概括] + heading_path",
          W._seg_summary(SEG_NOBODY) == "[无概括] 三、分歧",
          W._seg_summary(SEG_NOBODY))
    check("① fallback 计数只数无概括段",
          W._seg_fallback_count([SEG_OK, SEG_NOSUM, SEG_NOBODY]) == 2)


def t2_brief_keeps_all():
    b = W._brief(_state([SEG_OK, SEG_NOSUM, SEG_NOBODY]))
    ev = b["证据段落"]
    check("② _brief 不再过滤无概括段（3/3 都在）",
          len(ev) == 3, str(len(ev)))
    check("② 无概括段带前缀标记",
          ev[1]["这段在讲"].startswith("[无概括·取原文开头]")
          and ev[2]["这段在讲"] == "[无概括] 三、分歧")
    check("② 有概括段不受影响",
          ev[0]["这段在讲"] == "正常概括")
    check("② 证据段落说明提示读全文",
          "无概括" in b.get("证据段落说明", "")
          and "read_segments" in b["证据段落说明"],
          b.get("证据段落说明"))


def t3_writer_listing():
    """写手拿到的证据清单里，无概括段同样带前缀（不是被丢/是 null）。"""
    captured: dict = {}

    class FakeLoop:
        def __call__(self, llm, stage, system, messages, tools, **kw):
            captured["prompt"] = messages[0]["content"]
            return ("", {}, [])          # via=fallback → 走 _clean_fallback

    import research_agent.llm.toolloop as TL
    orig_loop, orig_tools = W.run_tool_loop, None
    # _run_task 内部 `from ...tools.evidence import EvidenceTools`
    # 且要 sid 非空才走工具循环；给 sid 但让 EvidenceTools 构造成功
    # （repo 已在 RA_DB_PATH 临时库上初始化）。
    os.environ.setdefault("MINIMAX_API_KEY", "stub")
    state = _state([SEG_OK, SEG_NOSUM])
    state["session_id"] = "s_test"
    state["spec"] = {}
    state["tasks"] = [{"id": "t1", "covers": ["sec1"],
                       "segments": ["S1·1", "S1·2"], "mandate": "写",
                       "must_cover": [], "not_yours": []}]
    task = state["tasks"][0]
    W.run_tool_loop = FakeLoop()
    orig_preload = W._WRITE_PRELOAD
    try:
        # 默认路径是预加载（RA_WRITE_PRELOAD=1）：写手直接拿原文，不看概括
        # listing，所以「listing 里的无概括前缀」只在回退路径（预加载关闭 /
        # 块超限 / 返修）才存在。2026-11-03 起分两路断言，避免测试跟着
        # 默认值漂（原来只测 listing，默认开预加载后静默变成断言过期）。
        W._WRITE_PRELOAD = False
        W._run_task(state, task, None)
        p = captured.get("prompt") or ""
        W._WRITE_PRELOAD = True
        captured.clear()
        W._run_task(state, task, None)
        pre = captured.get("prompt") or ""
    finally:
        W.run_tool_loop = orig_loop
        W._WRITE_PRELOAD = orig_preload
    check("③ 回退路径 listing 含 [无概括 前缀",
          "[无概括·取原文开头]" in p,
          p[p.find("无概括") - 60:p.find("无概括") + 80]
          if "无概括" in p else p[:200])
    check("③ 回退路径 有概括段的概括原文也在",
          "正常概括" in p)
    check("③ 预加载路径 写手拿到两段原文（无概括段不会丢）",
          "原文已全部附上" in pre and "[S1·1]" in pre and "[S1·2]" in pre
          and "第一段话。" in pre and "正文全文" in pre,
          pre[-200:])
    check("③ 预加载路径 不走概括 listing",
          "[无概括" not in pre and "只有概括" not in pre)


def t4_plan_event_count():
    """node_plan_tasks 事件带 fallback_summary_segments。"""
    # 桩掉 LLM.complete_json_forced / run_tool_loop 太重——只断言事件字段
    # 由 n_fb 计数生成：直接调 node_plan_tasks 并让 _obtain 走
    # complete_json 路径（sid 空 → 无工具循环）。
    state = _state([SEG_OK, SEG_NOSUM, SEG_NOSUM])
    state["spec"] = {"thesis": "t"}

    class FakeLLM:
        calls = 0

        def complete_json(self, stage, prompt, system=None):
            FakeLLM.calls += 1
            return {"tasks": [{"covers": ["sec1"], "mandate": "m",
                               "segments": ["S1·1"]}]}

    orig = W.LLM
    W.LLM = FakeLLM()
    try:
        out = W.node_plan_tasks(state)
    finally:
        W.LLM = orig
    ev = [e for e in (out.get("events") or [])
          if e.get("kind") == "write_plan_tasks"][0]
    check("④ 事件 fallback_summary_segments=2",
          ev.get("fallback_summary_segments") == 2, str(ev))


def main() -> int:
    print("=== 无概括段落不再静默排除 ===")
    for fn in (t1_seg_summary, t2_brief_keeps_all,
               t3_writer_listing, t4_plan_event_count):
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
