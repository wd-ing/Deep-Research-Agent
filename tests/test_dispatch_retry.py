"""dispatch 内重试 + revise 清陈旧 failed 的守门测试（离线，不需要网络）。

2026-10-19 加（坑 78）。验两条真实运行里暴露的行为：
  ① `node_dispatch` 对「交卷但正文全空」这类偶发失败重试一次——
     真实运行里一次偶发空交卷就让整节丢稿，同一任务复测 6/6 成功；
  ② `node_revise` 救回任务后，把 `failed` 里的旧记录清掉——
     原来不清，终稿「存疑与未完成」会照旧记录写「未能完成」，
     而正文其实已经在了。

桩挂 `_run_task` 层（dispatch/revise 调的就是它），按 task id 回脚本，
并记录调用次数——「恰好调用 N 次」是本文件的核心断言。

用法：
    .venv/bin/python tests/test_dispatch_retry.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research_agent.graph.subgraphs import write as W   # noqa: E402

FAILED: list[str] = []

BODY = "正文 [S1·1]。" + "按材料口径展开。" * 30      # >200 字符


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}" + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def _task(tid: str, covers=None) -> dict:
    return {"id": tid, "covers": covers or ["sec1"], "mandate": "m",
            "segments": ["S1·1"], "must_cover": [], "not_yours": []}


def _state(tasks, **kw) -> dict:
    st = {"session_id": None, "question": "q", "spec": {"thesis": "T"},
          "outline": [{"id": "sec1"}, {"id": "sec2"}],
          "tasks": tasks, "drafts": {}, "failed": [],
          "segments": [], "on_topic": [], "src_lines": []}
    st.update(kw)
    return st


class ScriptLLM:
    """按 task id 回脚本；记录每个任务被调几次（核心断言对象）。"""

    def __init__(self, script: dict):
        self.script = script
        self.calls: dict[str, int] = {}

    def run(self, state, task, revise=None):
        tid = task["id"]
        self.calls[tid] = self.calls.get(tid, 0) + 1
        seq = self.script.get(tid) or [({}, "empty output")]
        return seq[min(self.calls[tid] - 1, len(seq) - 1)]


def _with_stub(script, fn):
    stub = ScriptLLM(script)
    orig = W._run_task
    W._run_task = stub.run
    try:
        out = fn()
    finally:
        W._run_task = orig
    return stub, out


def t_dispatch_retry():
    # ① 首跑空交卷、重试成功 → 进 drafts、failed 空、retried/recovered=1
    stub, out = _with_stub(
        {"t1": [({}, "交卷但正文全空"), ({"sec1": BODY}, None)]},
        lambda: W.node_dispatch(_state([_task("t1")])))
    ev = (out.get("events") or [{}])[0]
    check("① 空交卷重试成功：进 drafts 且 failed 空",
          out["drafts"].get("t1") == {"sec1": BODY}
          and out["failed"] == [], f"{out['failed']}")
    check("① 事件 retried=1 recovered=1",
          ev.get("retried") == 1 and ev.get("recovered") == 1, str(ev))
    check("① _run_task 恰好被调 2 次",
          stub.calls.get("t1") == 2, str(stub.calls))

    # ② 首跑和重试都失败 → 进 failed，恰好调 2 次，reason 是最后一次的
    stub, out = _with_stub(
        {"t1": [({}, "交卷但正文全空"), ({}, "未交卷且文本被污染：empty")]},
        lambda: W.node_dispatch(_state([_task("t1")])))
    ev = (out.get("events") or [{}])[0]
    check("② 两次都失败：进 failed 且 reason 是末次的",
          len(out["failed"]) == 1
          and out["failed"][0]["reason"] == "未交卷且文本被污染：empty",
          str(out["failed"]))
    check("② 恰好调 2 次（不无限重试）",
          stub.calls.get("t1") == 2 and ev.get("retried") == 1
          and ev.get("recovered") == 0, str(stub.calls))

    # ③ 429/额度类错误不重试：恰好调 1 次
    stub, out = _with_stub(
        {"t1": [({}, "调用失败：MiniMax 429: Token Plan 速率限制")]},
        lambda: W.node_dispatch(_state([_task("t1")])))
    ev = (out.get("events") or [{}])[0]
    check("③ 429 不重试（1 次调用、进 failed）",
          stub.calls.get("t1") == 1 and len(out["failed"]) == 1
          and ev.get("retried") == 0, f"{stub.calls} {ev}")

    # ④ 混合：t1 一次成功、t2 重试救回、t3 429 不重试
    stub, out = _with_stub(
        {"t1": [({"sec1": BODY}, None)],
         "t2": [({}, "交卷但正文全空"), ({"sec2": BODY}, None)],
         "t3": [({}, "调用失败：MiniMax 429: 限流")]},
        lambda: W.node_dispatch(_state(
            [_task("t1"), _task("t2", ["sec2"]), _task("t3", ["sec3"])])))
    ev = (out.get("events") or [{}])[0]
    check("④ 混合：t1/t2 进 drafts，只 t3 在 failed",
          set(out["drafts"]) == {"t1", "t2"}
          and [f["task"] for f in out["failed"]] == ["t3"],
          f"drafts={sorted(out['drafts'])} failed={out['failed']}")
    check("④ 调用次数：1 / 2 / 1，retried=1 recovered=1",
          stub.calls == {"t1": 1, "t2": 2, "t3": 1}
          and ev.get("retried") == 1 and ev.get("recovered") == 1,
          f"{stub.calls} {ev.get('retried')}/{ev.get('recovered')}")


def t_revise_clears_failed():
    # ⑤ revise 救回 t2 → failed 清掉；没救回的 t9 保留。
    st = _state([_task("t2", ["sec2"])],
                drafts={},                  # t2 挂在 failed 里、本来就没稿
                failed=[{"task": "t2", "reason": "交卷但正文全空"},
                        {"task": "t9", "reason": "MiniMax 429: 限流"}],
                audit={"targets": [{"task": "t2", "section": "sec2",
                                    "problem": "p", "fix": "f"}]},
                rework_rounds=0)
    stub, out = _with_stub({"t2": [({"sec2": BODY}, None)]},
                           lambda: W.node_revise(st))
    ev = (out.get("events") or [{}])[0]
    check("⑤ 救回后 failed 只留未救回的",
          [f["task"] for f in out["failed"]] == ["t9"], str(out["failed"]))
    check("⑤ 事件 cleared_failed=[t2]",
          ev.get("cleared_failed") == ["t2"], str(ev)[:200])
    check("⑤ 新正文真的并进了 drafts",
          out["drafts"]["t2"]["sec2"] == BODY, str(out["drafts"]))
    cleared_failed = out["failed"]          # ← ⑥ 要用的是清理后的这份

    # revise 没救回（空产出）→ t2 仍留在 failed。
    stub, out = _with_stub({"t2": [({}, "交卷但正文全空"),
                                   ({}, "交卷但正文全空")]},
                           lambda: W.node_revise(st))
    check("⑤ 返修也失败：t2 仍挂在 failed",
          [f["task"] for f in out["failed"]] == ["t2", "t9"],
          str(out["failed"]))

    # ⑥ 清理后 node_final 不再把已救回的任务写成「未能完成」。
    st2 = _state([_task("t2"), _task("t9")],
                 drafts={"t2": {"sec2": BODY}}, failed=cleared_failed,
                 assembled="sec2 正文", audit={}, spec={"thesis": "T"})
    md = W.node_final(st2)["report_markdown"]
    check("⑥ 终稿不再把救回的 t2 写成未能完成",
          "t2" not in (md.split("以下任务未能完成：")[-1]
                       if "以下任务未能完成" in md else ""),
          md[-300:])
    check("⑥ 没救回的 t9 仍如实列出",
          "t9" in md and "以下任务未能完成" in md, md[-200:])


def main() -> int:
    print("=== dispatch 偶发失败重试 ===")
    t_dispatch_retry()
    print("\n=== revise 清理陈旧 failed ===")
    t_revise_clears_failed()
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
