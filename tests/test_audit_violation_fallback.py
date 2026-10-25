"""审查失败时「只修违规」降级返修的守门测试（离线）。

2026-10-27 加（批 4-A）。v21 实测：write_audit 撞 429 兜底后，
程序检出的 6 条引用违规（越界引用/自创写法，确定性可修）
没人修，原样进了存疑节。

新行为：`node_audit` 失败原因属「模型偏离/空返回」类
（入参无法归一化的字符串、EmptyOutput、JSONDecodeError）
且 `state["violations"]` 非空 → 把违规任务转成 `_violation`
返修目标，`need_rework=True`（算第 1 轮返修）；
HTTP 类失败（429/额度/5xx/超时/ConfigError）与无违规时
保持原样直进 final。

⚠️ 桩挂在生产同一层 `W.LLM.complete_json_forced`；
   不发任何真实 LLM 调用。

用法：
    .venv/bin/python tests/test_audit_violation_fallback.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("MINIMAX_API_KEY", "stub-not-used")

from research_agent.llm import provider as P            # noqa: E402
from research_agent.graph.subgraphs import write as W   # noqa: E402
from research_agent.graph.subgraphs import _retry as R  # noqa: E402

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}" + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


class StubAuditLLM:
    def __init__(self, script):
        self.script = list(script)
        self.seen: list[dict] = []

    def complete_json_forced(self, stage, prompt, name, schema,
                             system=None):
        self.seen.append({"stage": stage})
        r = self.script[min(len(self.seen) - 1, len(self.script) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


VIOLATIONS = {
    "t2": [{"kind": "越界引用", "found": "S9·9"},
           {"kind": "自创写法", "found": "[S18 相关证据不可用]"}],
    "t5": [{"kind": "越界引用", "found": "S23·2"}],
    "t9": [{"kind": "越界引用", "found": "S33·1"}],
}


def _state(with_violations=True) -> dict:
    return {"question": "q", "spec": {"thesis": "T"},
            "assembled": "草稿正文",
            "violations": VIOLATIONS if with_violations else {},
            "session_id": None,
            "tasks": [{"id": f"t{i}", "covers": [f"sec{i}"],
                       "segments": [], "must_cover": []}
                      for i in range(1, 10)]}


def _run(script, with_violations=True):
    stub = StubAuditLLM(script)
    orig = W.LLM.complete_json_forced
    W.LLM.complete_json_forced = stub.complete_json_forced
    try:
        out = W.node_audit(_state(with_violations))
    finally:
        W.LLM.complete_json_forced = orig
    return stub, out


def _ev(out):
    return (out.get("events") or [{}])[0]


def t_empty_output_rework():
    # EmptyOutput 属「空返回」类：重试一次仍失败 → 有违规时降级返修。
    stub, out = _run([P.EmptyOutput("参数被截断"),
                      P.EmptyOutput("参数仍被截断")])
    ev = _ev(out)
    check("EmptyOutput+违规 → write_audit_error 且 need_rework=True",
          ev.get("kind") == "write_audit_error"
          and out["audit"].get("need_rework") is True,
          f"{ev.get('kind')} nr={out['audit'].get('need_rework')}")
    targets = out["audit"].get("targets") or []
    check("targets 全是 _violation 目标、逐任务一个（3 任务）",
          len(targets) == 3
          and all(t.get("_violation") for t in targets)
          and {t["task"] for t in targets} == {"t2", "t5", "t9"},
          str(targets)[:200])
    check("事件带 violation_only_rework=3；存疑语保留",
          ev.get("violation_only_rework") == 3
          and "审查环节失败" in str(out["audit"].get("remaining_issues")),
          str(ev)[:160])
    check("问题/修法文本沿用程序检出模板（含违规样本）",
          "越界引用:S9·9" in str(targets), str(targets[0])[:160])
    # 路由一致性：need_rework + rework_rounds=0 < MAX_REWORK → revise。
    check("route_after_audit → revise",
          W.route_after_audit({**_state(), "audit": out["audit"],
                               "rework_rounds": 0}) == "revise")


def t_normalize_fail_rework():
    # 两次都无法归一化（模型偏离 schema）→ 同样降级返修。
    stub, out = _run([{"foo": 1}, [1, 2]])
    ev = _ev(out)
    targets = out["audit"].get("targets") or []
    check("归一化失败+违规 → need_rework=True + _violation 目标",
          out["audit"].get("need_rework") is True
          and len(targets) == 3
          and all(t.get("_violation") for t in targets),
          f"{len(targets)} {out['audit'].get('need_rework')}")
    check("violation_only_rework=3 + normalize_fail 两条仍在",
          ev.get("violation_only_rework") == 3
          and len(ev.get("normalize_fail") or []) == 2,
          str(ev.get("violation_only_rework")))


def t_http_failures_stay():
    # 429/额度：不重试、不降级返修——与改前完全一致。
    stub, out = _run([RuntimeError("MiniMax 429: Token Plan 用量上限")])
    ev = _ev(out)
    check("429+违规 → need_rework=False、targets 空、violation_only=0",
          ev.get("kind") == "write_audit_error"
          and out["audit"].get("need_rework") is False
          and out["audit"].get("targets") == []
          and ev.get("violation_only_rework") == 0
          and len(stub.seen) == 1,
          f"{out['audit'].get('need_rework')} {ev.get('violation_only_rework')}")
    check("429 → route_after_audit 仍去 final",
          W.route_after_audit({**_state(), "audit": out["audit"],
                               "rework_rounds": 0}) == "final")

    # 5xx 过载（打满 3 次瞬时重试后仍败）也不触发——
    # revise 的模型调用多半同样失败。sleep 桩掉。
    o_wait, o_sleep = R._TRANSIENT_WAIT_S, W.time.sleep
    R._TRANSIENT_WAIT_S = 0
    W.time.sleep = lambda s: None
    try:
        stub, out = _run([RuntimeError("MiniMax 529: a"),
                          RuntimeError("MiniMax 529: b"),
                          RuntimeError("MiniMax 529: c")])
    finally:
        R._TRANSIENT_WAIT_S = o_wait
        W.time.sleep = o_sleep
    ev = _ev(out)
    check("连续 529+违规 → 3 次尝试后 need_rework=False",
          len(stub.seen) == 3
          and out["audit"].get("need_rework") is False
          and ev.get("violation_only_rework") == 0,
          f"{len(stub.seen)} {out['audit'].get('need_rework')}")

    # ConfigError（配置/密钥类）同属不重发没意义。
    stub, out = _run([P.ConfigError("未设置 MINIMAX_API_KEY")])
    check("ConfigError+违规 → need_rework=False",
          out["audit"].get("need_rework") is False
          and _ev(out).get("violation_only_rework") == 0)


def t_no_violations_stay():
    # 模型偏离类失败但**无违规** → 与改前一致（无目标可返）。
    stub, out = _run([P.EmptyOutput("空返回"),
                      P.EmptyOutput("仍空")],
                     with_violations=False)
    ev = _ev(out)
    check("EmptyOutput 无违规 → need_rework=False、violation_only=0",
          out["audit"].get("need_rework") is False
          and out["audit"].get("targets") == []
          and ev.get("violation_only_rework") == 0)
    stub, out = _run([{"foo": 1}, [1, 2]], with_violations=False)
    check("归一化失败无违规 → need_rework=False",
          out["audit"].get("need_rework") is False
          and _ev(out).get("violation_only_rework") == 0)


def t_violations_unknown_task_ignored():
    # 违规挂在不存在的任务 id 上：无目标 → 不返修（同正常路径口径）。
    stub, out = _run([P.EmptyOutput("a"), P.EmptyOutput("b")],
                     with_violations=True)
    # 先把 violations 换成无效任务 id 再跑
    orig_state = _state
    orig = W.LLM.complete_json_forced
    W.LLM.complete_json_forced = StubAuditLLM(
        [P.EmptyOutput("a"), P.EmptyOutput("b")]).complete_json_forced
    try:
        st = _state(True)
        st["violations"] = {"t404": [{"kind": "越界引用", "found": "S9·9"}]}
        out = W.node_audit(st)
    finally:
        W.LLM.complete_json_forced = orig
    check("违规任务不存在 → 0 目标、need_rework=False",
          out["audit"].get("need_rework") is False
          and _ev(out).get("violation_only_rework") == 0)


def main():
    t_empty_output_rework()
    t_normalize_fail_rework()
    t_http_failures_stay()
    t_no_violations_stay()
    t_violations_unknown_task_ignored()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项未过：{FAILED}")
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    main()
