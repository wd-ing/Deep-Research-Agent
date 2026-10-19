"""定点补丁返修的守门测试（离线）。

2026-10-19 加（坑 80）。返修不再一律整节重写：审查给 quote+action，
每个任务一次 `submit_patches` 强制调用产 old→new 替换，程序精确匹配
并校验；定位失败/校验不过/整节层面问题回退 `write_revise` 旧路径。

桩挂 `W.LLM.complete_json_forced`（按 stage=="write_patch" 区分）与
`W._run_task`（旧路径），与 test_auto_fix/test_audit_forced 同层。

用法：
    .venv/bin/python tests/test_patch_revise.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research_agent.graph.subgraphs import write as W   # noqa: E402

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}"
          + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


# ------------------------------------------------------------- 桩与状态

def _task(tid: str, covers=None, segs=None) -> dict:
    return {"id": tid, "covers": covers or ["sec1"], "mandate": "m",
            "segments": segs or ["S3·1", "S3·2"],
            "must_cover": [], "not_yours": []}


def _state(tasks, **kw) -> dict:
    st = {"session_id": None, "question": "q", "spec": {"thesis": "T"},
          "outline": [{"id": "sec1", "title": "第一节"},
                      {"id": "sec2", "title": "第二节"}],
          "tasks": tasks, "drafts": {}, "failed": [], "violations": {},
          "segments": [], "on_topic": [], "src_lines": [],
          "rework_rounds": 0}
    st.update(kw)
    return st


class PatchLLM:
    """按 stage 分流：write_patch 走脚本，其余环节记录后抛错。"""

    def __init__(self, script=None):
        self.script = script or []        # 每项 = dict 或 Exception
        self.calls: list[dict] = []

    def complete_json_forced(self, stage, prompt, name, schema,
                             system=None):
        self.calls.append({"stage": stage, "prompt": prompt,
                           "name": name})
        i = len([c for c in self.calls if c["stage"] == stage]) - 1
        seq = self.script if stage == "write_patch" else []
        r = seq[min(i, len(seq) - 1)] if seq else Exception("unexpected")
        if isinstance(r, Exception):
            raise r
        return r


class ReviseRun:
    """`_run_task` 桩：记录每次调用的 covers 与 asks，按脚本返回。"""

    def __init__(self, script=None):
        self.script = script or {}
        self.calls: list[dict] = []

    def __call__(self, state, task, revise=None):
        self.calls.append({"task": task["id"], "covers": task["covers"],
                           "revise": revise})
        seq = self.script.get(task["id"], [({}, "x")])
        return seq[min(len(self.calls) - 1, len(seq) - 1)]


def _run_revise(state, patch_script, run_script=None):
    """桩挂好后跑一次 node_revise；返回 (patch_llm, run, out)。"""
    llm, run = PatchLLM(patch_script), ReviseRun(run_script)
    o_llm, o_run = W.LLM.complete_json_forced, W._run_task
    W.LLM.complete_json_forced = llm.complete_json_forced
    W._run_task = run
    try:
        return llm, run, W.node_revise(state)
    finally:
        W.LLM.complete_json_forced = o_llm
        W._run_task = o_run


def _patch_ev(out):
    return ((out.get("events") or [{}])[0]).get("patch") or {}


def _tgt(tid, sec, quote="", action="edit", problem="p", fix="f"):
    return {"task": tid, "section": sec, "problem": problem, "fix": fix,
            "quote": quote, "action": action}


# ------------------------------------------------------------- ① 快乐路径

def t_happy_path():
    body = "甲句保持不变。乙句要改。丙句也不动。"
    st = _state([_task("t1")],
                drafts={"t1": {"sec1": body}},
                audit={"targets": [_tgt("t1", "sec1", "乙句要改。")]})
    llm, run, out = _run_revise(
        st, [{"patches": [{"item": 0, "old": "乙句要改。",
                            "new": "乙句改好了。"}],
              "unresolved": []}])
    new_body = out["drafts"]["t1"]["sec1"]
    check("① 补丁应用：只有那一句变了",
          new_body == body.replace("乙句要改。", "乙句改好了。"),
          repr(new_body))
    check("① 旧路径零调用", len(run.calls) == 0)
    ev = _patch_ev(out)
    check("① 事件 applied=1、patch_calls=1",
          ev.get("applied") == 1 and ev.get("patch_calls") == 1
          and ev.get("items") == 1, str(ev)[:160])


# --------------------------------------------------- ②③ 定位失败回退

def t_locate_failures():
    # old 在正文里找不到 → 拒收 → 该节回退旧路径。
    body = "甲句。乙句。丙句。"
    st = _state([_task("t1")],
                drafts={"t1": {"sec1": body}},
                audit={"targets": [_tgt("t1", "sec1", "乙句。", problem="改乙")]})
    llm, run, out = _run_revise(
        st, [{"patches": [{"item": 0, "old": "不存在的句子", "new": "x"}],
              "unresolved": []}],
        {"t1": [({"sec1": "重写后的正文"}, None)]})
    check("② old 找不到：回退旧路径恰好一次",
          len(run.calls) == 1 and run.calls[0]["task"] == "t1")
    check("② 回退 asks 含该条意见",
          "改乙" in (run.calls[0]["revise"] or {}).get("fix", ""))
    ev = _patch_ev(out)
    check("② 事件 rejected 有可读理由",
          any("old 未找到" in r.get("reason", "")
              for r in ev.get("rejected") or []), str(ev)[:200])
    check("② fallback_sections 含 [t1,sec1]",
          ["t1", "sec1"] in (ev.get("fallback_sections") or []), str(ev)[:200])

    # old 不唯一（正文里出现两次）→ 同样拒收回退。
    body2 = "重复句。中间隔开。重复句。"
    st = _state([_task("t1")],
                drafts={"t1": {"sec1": body2}},
                audit={"targets": [_tgt("t1", "sec1", "重复句。")]})
    # quote 出现两次 → 分流直接判不可补丁，连补丁调用都不发
    llm, run, out = _run_revise(st, [{"patches": [], "unresolved": []}],
                                {"t1": [({"sec1": "重写的"}, None)]})
    check("③ quote 不唯一：不发补丁调用直接回退",
          len([c for c in llm.calls if c["stage"] == "write_patch"]) == 0
          and len(run.calls) == 1)

    # quote 唯一但模型给的 old 出现两次 → 补丁调用发了，应用时拒收。
    body3 = "独有句。重复词出现。隔一下。重复词出现。"
    st = _state([_task("t1")],
                drafts={"t1": {"sec1": body3}},
                audit={"targets": [_tgt("t1", "sec1", "独有句。")]})
    llm, run, out = _run_revise(
        st, [{"patches": [{"item": 0, "old": "重复词出现。",
                            "new": "改掉。"}],
              "unresolved": []}],
        {"t1": [({"sec1": "重写的"}, None)]})
    ev = _patch_ev(out)
    check("③ old 不唯一：拒收回退且理由可读",
          len(run.calls) == 1
          and any("old 不唯一" in r.get("reason", "")
                  for r in ev.get("rejected") or []), str(ev)[:200])


# --------------------------------------------------- ④ 新文校验回退

def t_new_validation():
    cases = [
        ("越界引用", "乙句带 [S9·9] 出处。"),
        ("内部编号", "乙句改到 sec3 里讲。"),
        ("污染输出", "乙句 read_segments 痕迹。"),
        ("扩写过度", "乙句扩写" + "废话" * 200 + "。"),
    ]
    for name, new in cases:
        body = "甲句。乙句要改。丙句。"
        st = _state([_task("t1")],
                    drafts={"t1": {"sec1": body}},
                    audit={"targets": [_tgt("t1", "sec1", "乙句要改。")]})
        llm, run, out = _run_revise(
            st, [{"patches": [{"item": 0, "old": "乙句要改。",
                                "new": new}],
                  "unresolved": []}],
            {"t1": [({"sec1": "重写后的正文内容足够长"}, None)]})
        ev = _patch_ev(out)
        check(f"④ new 含{name}：拒收并回退旧路径",
              len(run.calls) == 1
              and any(name in r.get("reason", "")
                      for r in ev.get("rejected") or [])
              and out["drafts"]["t1"]["sec1"] == "重写后的正文内容足够长",
              str(ev.get("rejected"))[:120])


# --------------------------------------------- ⑤ 不可补丁项直接旧路径

def t_unsuitable_targets():
    body = "甲句。乙句。丙句。"
    variants = [
        ("rewrite_section", _tgt("t1", "sec1", "乙句。", "rewrite_section")),
        ("缺 action", {"task": "t1", "section": "sec1", "problem": "p",
                        "fix": "f", "quote": "乙句。"}),
        ("quote 空", _tgt("t1", "sec1", "", "edit")),
        ("缺 section", _tgt("t1", None, "乙句。", "edit")),
    ]
    for name, tgt in variants:
        st = _state([_task("t1")],
                    drafts={"t1": {"sec1": body}},
                    audit={"targets": [tgt]})
        llm, run, out = _run_revise(
            st, [{"patches": [], "unresolved": []}],
            {"t1": [({"sec1": "重写的"}, None)]})
        check(f"⑤ {name}：不发补丁调用，直接旧路径",
              len([c for c in llm.calls if c["stage"] == "write_patch"]) == 0
              and len(run.calls) == 1, str(run.calls)[:100])


# --------------------------------------------- ⑥ 补丁调用失败回退

def t_call_failures():
    body = "甲句。乙句要改。丙句。"
    tgt = _tgt("t1", "sec1", "乙句要改。")

    # 连续两次可重试异常 → 全部回退（调用恰好 2 次）。
    st = _state([_task("t1")], drafts={"t1": {"sec1": body}},
                audit={"targets": [tgt]})
    llm, run, out = _run_revise(
        st, [RuntimeError("timeout"), RuntimeError("timeout2")],
        {"t1": [({"sec1": "重写的"}, None)]})
    check("⑥ 两次可重试异常 → 回退（2 次调用）",
          len([c for c in llm.calls if c["stage"] == "write_patch"]) == 2
          and len(run.calls) == 1)

    # 两次无效结构 → 同样回退。
    st = _state([_task("t1")], drafts={"t1": {"sec1": body}},
                audit={"targets": [tgt]})
    llm, run, out = _run_revise(
        st, [{"patches": []}, "不是 dict"],
        {"t1": [({"sec1": "重写的"}, None)]})
    check("⑥ 两次无效结构 → 回退（2 次调用）",
          len([c for c in llm.calls if c["stage"] == "write_patch"]) == 2
          and len(run.calls) == 1)

    # HTTP 429 不可重试 → 恰好 1 次调用就回退。
    st = _state([_task("t1")], drafts={"t1": {"sec1": body}},
                audit={"targets": [tgt]})
    llm, run, out = _run_revise(
        st, [RuntimeError("MiniMax 429: rate limit")],
        {"t1": [({"sec1": "重写的"}, None)]})
    check("⑥ 429 不重试：恰好 1 次调用后回退",
          len([c for c in llm.calls if c["stage"] == "write_patch"]) == 1
          and len(run.calls) == 1)


# --------------------------------------------------- ⑦ 混合任务

def t_mixed_task():
    # 注意：正文/补丁文字里不能出现 tN/secN 字样——那是内部编号，
    # 会被护栏/校验当成缺陷处理。
    bodies = {"sec1": "甲句。这里要改的句。丙句。",
              "sec2": "丁句。戊句。己句。"}
    st = _state([_task("t1", covers=["sec1", "sec2"])],
                drafts={"t1": dict(bodies)},
                audit={"targets": [
                    _tgt("t1", "sec1", "这里要改的句。"),
                    _tgt("t1", "sec2", "", "rewrite_section", "整节重写")]})
    llm, run, out = _run_revise(
        st, [{"patches": [{"item": 0, "old": "这里要改的句。",
                            "new": "改好的句。"}],
              "unresolved": []}],
        {"t1": [({"sec2": "整节重写后的稿子"}, None)]})
    d = out["drafts"]["t1"]
    check("⑦ sec1 补丁生效、其余文字逐字节不变",
          d["sec1"] == bodies["sec1"].replace("这里要改的句。", "改好的句。"),
          repr(d["sec1"]))
    check("⑦ sec2 走旧路径且 covers 只有 sec2",
          len(run.calls) == 1 and run.calls[0]["covers"] == ["sec2"]
          and d["sec2"] == "整节重写后的稿子",
          str(run.calls[0]["covers"]) + " | " + repr(d["sec2"]))
    check("⑦ 补丁的 sec1 没被旧路径覆盖",
          "改好的句。" in d["sec1"])


# --------------------------------------------------- ⑧ unresolved

def t_unresolved():
    body = "甲句。改不了的句。丙句。"
    st = _state([_task("t1")],
                drafts={"t1": {"sec1": body}},
                audit={"targets": [_tgt("t1", "sec1", "改不了的句。",
                                        problem="这条改不动")]})
    llm, run, out = _run_revise(
        st, [{"patches": [],
              "unresolved": [{"item": 0, "reason": "材料里没有支撑"}]}])
    check("⑧ unresolved 进 state 字段",
          len(out.get("revise_unresolved") or []) == 1
          and out["revise_unresolved"][0]["task"] == "t1"
          and "材料" in out["revise_unresolved"][0]["reason"],
          str(out.get("revise_unresolved"))[:160])
    check("⑧ unresolved 不回退旧路径、原文不动",
          len(run.calls) == 0 and out["drafts"]["t1"]["sec1"] == body)
    # node_final 的存疑节要写出那行
    fin = W.node_final({**st, **out, "assembled": "正文",
                        "src_lines": [], "audit": {"remaining_issues": []}})
    md = fin.get("report_markdown") or ""
    check("⑧ node_final 渲染返修未能解决行",
          "返修未能解决:t1/sec1:这条改不动（材料里没有支撑）" in md,
          [x for x in md.split("\n") if "返修未能解决" in x])


# --------------------------------------------------- ⑨ 程序违规目标

def t_violation_items():
    sent = "某结论有出处 [S10·1]。"
    body = f"甲句。{sent}丙句。"
    st = _state([_task("t1")],
                drafts={"t1": {"sec1": body}},
                violations={"t1": [{"kind": "越界引用", "found": "S10·1"}]},
                audit={"targets": [{"task": "t1", "section": None,
                                    "problem": "程序检出 1 处引用/编号问题:越界引用:S10·1",
                                    "fix": "逐处处理", "_violation": True,
                                    "quote": "", "action": "edit"}]})
    llm, run, out = _run_revise(
        st, [{"patches": [{"item": 0, "old": sent, "new": "某结论有据 [S3·1]。"}],
              "unresolved": []}])
    prompt = llm.calls[0]["prompt"] if llm.calls else ""
    check("⑨ 违规目标展开成补丁项，quote 是整句",
          sent in prompt and "原句" in prompt, prompt[:200])
    check("⑨ 补丁应用后 violations 重算为空",
          out.get("violations") == {} and len(run.calls) == 0,
          str(out.get("violations"))[:120])
    check("⑨ 越界句被替换",
          out["drafts"]["t1"]["sec1"] == "甲句。某结论有据 [S3·1]。丙句。",
          repr(out["drafts"]["t1"]["sec1"]))


# --------------------------------------------------- ⑩ 事件与并发

def t_events_and_concurrency():
    # 正文里故意用「壹/贰」避开 tN/secN 内部编号字形。
    bodies = {"t1": "甲句。壹句要改的。丙句。",
              "t2": "丁句。贰句要改的。己句。"}
    st = _state([_task("t1"), _task("t2")],
                drafts={k: {"sec1": v} for k, v in bodies.items()},
                audit={"targets": [_tgt("t1", "sec1", "壹句要改的。"),
                                    _tgt("t2", "sec1", "贰句要改的。")]})
    llm, run, out = _run_revise(
        st, [{"patches": [{"item": 0, "old": "壹句要改的。",
                            "new": "壹句改好。"}],
              "unresolved": []},
             {"patches": [{"item": 0, "old": "贰句要改的。",
                            "new": "贰句改好。"}],
              "unresolved": []}])
    calls = [c for c in llm.calls if c["stage"] == "write_patch"]
    ev = _patch_ev(out)
    check("⑩ 两个并发任务各一次补丁调用",
          len(calls) == 2 and len(run.calls) == 0)
    check("⑩ 事件字段齐全",
          all(k in ev for k in
              ("items", "patch_calls", "applied", "rejected",
               "unresolved", "fallback_sections"))
          and ev["items"] == 2 and ev["patch_calls"] == 2
          and ev["applied"] == 2, str(ev)[:200])
    check("⑩ 两稿各自只改了自己那句",
          out["drafts"]["t1"]["sec1"] == bodies["t1"].replace(
              "壹句要改的。", "壹句改好。")
          and out["drafts"]["t2"]["sec1"] == bodies["t2"].replace(
              "贰句要改的。", "贰句改好。"))


# --------------------------------------------------- ⑪ 归一化

def t_normalize_patch_input():
    good = {"patches": [{"item": 0, "old": "a", "new": "b"}],
            "unresolved": [{"item": 1, "reason": "r"}]}
    d = W._normalize_patch_input(good)
    check("⑪ 直给结构原样收",
          d == good, str(d))
    # 外层单键包装 + 列表 {"item":[...]} 包装 + item 字符串数字
    wrapped = {"patch": {"patches": {"item": [{"item": "0", "old": "a",
                                               "new": "b"}]},
                          "unresolved": []}}
    d = W._normalize_patch_input(wrapped)
    check("⑪ 包装/字符串 item 容忍",
          d is not None and d["patches"][0]["item"] == 0
          and d["patches"][0]["old"] == "a", str(d)[:120])
    # 缺键拒绝
    for bad in ({"patches": []}, {"unresolved": []}, {},
                {"patch_result": {"patches": []}},
                [1, 2], "not json"):
        check(f"⑪ 拒绝 {str(bad)[:30]}",
              W._normalize_patch_input(bad) is None)
    # 空 patches/unresolved 但键齐 = 合法（模型全说改不了）
    d = W._normalize_patch_input({"patches": [], "unresolved": []})
    check("⑪ 双空列表合法",
          d == {"patches": [], "unresolved": []})


# --------------------------------------------------- ⑫ 向后兼容

def t_legacy_findings():
    # 老 fixture：findings 没有 quote/action → action 落 rewrite_section →
    # 直接旧路径，不发补丁调用。
    body = "甲句。乙句。丙句。"
    st = _state([_task("t1")],
                drafts={"t1": {"sec1": body}},
                audit={"targets": [{"task": "t1", "section": "sec1",
                                    "problem": "p", "fix": "f"}]})
    llm, run, out = _run_revise(
        st, [{"patches": [], "unresolved": []}],
        {"t1": [({"sec1": "旧路径重写"}, None)]})
    check("⑫ 无 quote/action 的目标走旧路径",
          len([c for c in llm.calls if c["stage"] == "write_patch"]) == 0
          and len(run.calls) == 1
          and out["drafts"]["t1"]["sec1"] == "旧路径重写")


def main() -> int:
    print("=== ① 快乐路径 ===")
    t_happy_path()
    print("\n=== ②③ 定位失败 ===")
    t_locate_failures()
    print("\n=== ④ new 校验 ===")
    t_new_validation()
    print("\n=== ⑤ 不可补丁项 ===")
    t_unsuitable_targets()
    print("\n=== ⑥ 调用失败 ===")
    t_call_failures()
    print("\n=== ⑦ 混合任务 ===")
    t_mixed_task()
    print("\n=== ⑧ unresolved ===")
    t_unresolved()
    print("\n=== ⑨ 程序违规目标 ===")
    t_violation_items()
    print("\n=== ⑩ 事件与并发 ===")
    t_events_and_concurrency()
    print("\n=== ⑪ 归一化 ===")
    t_normalize_patch_input()
    print("\n=== ⑫ 向后兼容 ===")
    t_legacy_findings()
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
