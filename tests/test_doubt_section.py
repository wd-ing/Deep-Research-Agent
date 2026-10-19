"""坑 80 收口（离线测试）：

A. 存疑节卫生：
   · `_clip_sentence` 各分支——截断不留残句、硬截必带 …；
   · `返修未能解决` 行用节标题而非 tN/secN，且不在句中硬截；
   · 整个存疑节过 `_translate_ids_for_doubt`：secN→「标题」一节、
     tN→「首cover节标题」的写作任务，幂等。
B. rework_effective 新判定：
   · `revise_unresolved` 里的 (task,section) 不算 still_there；
   · 有真没改掉的时，只有「修过至少一条 + 模型 high 收敛」才算有效。

桩挂在 `W.LLM.complete_json_forced`（node_audit 调的那层）与
`W._prev_high`（它要读真实 DB，离线直接桩掉返回值）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import research_agent.graph.subgraphs.write as W   # noqa: E402

_ok = _fail = _n = 0


def check(name, cond, detail=""):
    global _ok, _fail, _n
    _n += 1
    if cond:
        _ok += 1
        print(f"  ✅ {_n:2d} {name}")
    else:
        _fail += 1
        print(f"  ❌ {_n:2d} {name}  -> {detail}")


# ================================================================ A. 存疑节

def t_clip_sentence():
    # 不超上限：原样
    check("A1 短文本原样", W._clip_sentence("短句。", 10) == "短句。")
    # 句末在 n*0.5 之后 → 在句末截，保留标点
    s = "甲。乙丙丁。戊己庚辛壬癸没有句号"
    out = W._clip_sentence(s, 8)          # s[:8]=甲。乙丙丁。戊己,最后 。在 idx5>4
    check("A1 句末处截断保留标点", out == "甲。乙丙丁。", repr(out))
    # 句末太靠前（≤n*0.5）→ 硬截 + …
    out = W._clip_sentence(s, 4)          # s[:4]=甲。乙丙,最后 。在 idx1 ≤2
    check("A1 句末太靠前 → 硬截带省略号",
          out == "甲。乙丙…", repr(out))
    # 完全没有句末 → 硬截 + …
    out = W._clip_sentence("甲乙丙丁戊己庚辛壬癸", 5)
    check("A1 无句末 → 硬截带省略号", out == "甲乙丙丁戊…", repr(out))
    # 换行也是句末
    out = W._clip_sentence("第一行\n第二行\n第三行尾巴", 9)
    check("A1 换行处截断", out == "第一行\n第二行\n", repr(out))
    # 空/None
    check("A1 None→空串", W._clip_sentence(None, 5) == "")


def _final_state(**kw):
    st = {"question": "q", "spec": {}, "assembled": "正文",
          "outline": [{"id": "sec3", "title": "界面瓶颈"},
                      {"id": "sec4", "title": "成本结构"},
                      {"id": "sec6", "title": "玩家路线"},
                      {"id": "sec8", "title": "标准话语权"}],
          "tasks": [{"id": "t4", "covers": ["sec4"], "segments": []},
                    {"id": "t6", "covers": ["sec6"], "segments": []},
                    {"id": "t8", "covers": ["sec8"], "segments": []}],
          "audit": {}, "violations": {}, "failed": [],
          "src_lines": ["- S1 某来源"], "n_no_summary": 0,
          "assemble_warnings": [], "uncovered_tasks": [],
          "revise_unresolved": []}
    st.update(kw)
    return st


def t_unresolved_line():
    st = _final_state(revise_unresolved=[
        {"task": "t6", "section": "sec6",
         "problem": "与 sec4 口径矛盾：sec4 以 S37·1 为据写兼容度 90%，"
                    "本节却称未找到独立信源，这一句之后还有很多没有句号的尾巴"
                    "甲乙丙丁戊己庚辛壬癸甲乙丙丁戊己庚辛壬癸甲乙丙丁戊己庚辛壬癸"
                    "甲乙丙丁戊己庚辛壬癸甲乙丙丁戊己庚辛壬癸甲乙丙丁戊己庚辛壬癸",
         "reason": "可引用的证据段中没有任何关于国轩「复合氧化物/聚合物路线」"
                   "的来源。无法为该口径找到任何证据支撑，材料不足无法硬改。"}])
    md = W.node_final(st)["report_markdown"]
    line = next(l for l in md.splitlines() if "返修未能解决" in l)
    check("A2 行首是节标题不是 t6/sec6",
          line.startswith("- 返修未能解决（「玩家路线」）:"), line[:60])
    check("A2 行里没有内部编号",
          "t6" not in line and "sec6" not in line and "sec4" not in line,
          line[:120])
    check("A2 problem 里的 sec4 也被翻译",
          "「成本结构」一节" in line, line[:160])
    check("A2 截断不留残句（句尾是 。…）之一）",
          line.rstrip()[-1] in "。…）", line[-20:])


def t_translate_ids():
    outline = [{"id": "sec4", "title": "成本结构"},
               {"id": "sec8", "title": "标准话语权"}]
    tasks = [{"id": "t4", "covers": ["sec4"]},
             {"id": "t9", "covers": []}]
    text = ("- [no_material] sec4 的材料不够，t4 只分到 8 段；"
            "sec99 不在大纲里；「sec4」已包过的别动；"
            "t1.5 版本号与 500t、test1 放行；任务 t9 无 covers 不动")
    out = W._translate_ids_for_doubt(text, outline, tasks)
    check("A3 sec4→「成本结构」一节", "「成本结构」一节" in out, out)
    check("A3 t4→「成本结构」的写作任务", "「成本结构」的写作任务" in out, out)
    check("A3 sec99 不在大纲不动", "sec99" in out)
    check("A3 已包「sec4」不二次替换", "「sec4」" in out, out)
    check("A3 t1.5/500t/test1 放行",
          "t1.5" in out and "500t" in out and "test1" in out, out)
    check("A3 无 covers 的 t9 不动", "任务 t9 无" in out, out)
    check("A3 幂等",
          W._translate_ids_for_doubt(out, outline, tasks) == out)


def t_final_doubt_section():
    st = _final_state(
        # node_final 读的是 remaining_issues——blocked 是在 node_audit 里
        # 被拼成 "[kind] issue" 进这个列表的，这里直接摆拼好的形态。
        audit={"spec_issue": "sec4 的 must_cover 与 sec3 的口径冲突",
               "remaining_issues": ["[no_material] sec8 的材料不够",
                                    "[no_material] sec8 要的历史节点在 t8 证据里没有"]},
        violations={"t4": [{"kind": "自创写法", "found": "[S29·1—S29·7]"}]})
    md = W.node_final(st)["report_markdown"]
    doubt = md.split("## 存疑与未完成", 1)[1]
    check("A4 spec_issue 里 sec4/sec3 翻译",
          "「成本结构」一节" in doubt and "「界面瓶颈」一节" in doubt, doubt[:200])
    check("A4 blocked issue 里的 sec8/t8 翻译",
          "「标准话语权」一节" in doubt and "「标准话语权」的写作任务" in doubt,
          doubt[:400])
    check("A4 violations 行 t4 翻译",
          "「成本结构」的写作任务" in doubt, doubt[:400])
    import re
    rest = re.findall(r"(?<![A-Za-z0-9])(?:t\d+[ab]?|sec\d+)(?![A-Za-z0-9])",
                      doubt)
    check("A4 存疑节无内部编号残留", rest == [], str(rest[:6]))


# ================================================================ B. rework_effective

class StubAuditLLM:
    def __init__(self, raw):
        self.raw = raw

    def complete_json_forced(self, stage, prompt, name, schema,
                             system=None):
        return self.raw


def _ck(task, sec, st):
    return {"task": task, "section": sec, "status": st, "note": "n"}


def _findings(n):
    # n 条 high → cur_high = n（任务 id 都得在 by_task 里）
    ids = ["t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8", "t9"][:n]
    return [{"task": t, "section": f"sec{t[1:]}", "severity": "high",
             "problem": "问题", "fix": "改法", "quote": "句", "action": "edit"}
            for t in ids]


def _run_audit(checks, unres=None, prev_high=8, cur_findings=3):
    state = {"question": "q", "spec": {"thesis": "T"}, "assembled": "稿",
             "session_id": None, "rework_rounds": 1,
             "tasks": [{"id": f"t{i}", "covers": [f"sec{i}"],
                        "segments": [], "must_cover": []}
                       for i in range(1, 10)],
             "violations": {},
             "revise_unresolved": unres or []}
    raw = {"need_rework": True, "reason": "r", "findings": _findings(cur_findings),
           "blocked": [], "rework_check": checks}
    orig_llm = W.LLM.complete_json_forced
    orig_prev = W._prev_high
    W.LLM.complete_json_forced = StubAuditLLM(raw).complete_json_forced
    W._prev_high = lambda sid, r: prev_high
    try:
        return W.node_audit(state)
    finally:
        W.LLM.complete_json_forced = orig_llm
        W._prev_high = orig_prev


# 真实形状：fixed 4 + changed_form 1 + still_there 3，
# 其中 t6/sec6、t4/sec4 两条 still 是补丁员已判 unresolved 的项。
CHECK_REAL = [_ck("t6", "sec6", "fixed"), _ck("t9", "sec10", "fixed"),
              _ck("t9", "sec10", "fixed"), _ck("t9", "sec10", "fixed"),
              _ck("t1", "sec1", "changed_form"),
              _ck("t4", "sec4", "still_there"),
              _ck("t6", "sec6", "still_there"),
              _ck("t7", "sec8", "still_there")]
UNRES_REAL = [{"task": "t6", "section": "sec6"},
              {"task": "t4", "section": "sec4"}]


def t_rework_effective():
    out = _run_audit(CHECK_REAL, UNRES_REAL, prev_high=8, cur_findings=3)
    ev = (out.get("events") or [{}])[0]
    check("B1 真实形状→effective（unresolved 不算 still）",
          out["rework_effective"] is True, repr(ev))
    check("B1 事件 still_open=2/still_unresolved=2/prev=8/cur=3",
          ev.get("still_open") == 2 and ev.get("still_unresolved") == 2
          and ev.get("prev_high") == 8 and ev.get("cur_high") == 3,
          repr(ev))
    check("B1 check_detail 仍带全部 8 条", len(ev.get("check_detail") or []) == 8)

    # 同形状但 unresolved 为空 → still_real=3,fixed 4,3<8 → True
    out = _run_audit(CHECK_REAL, [], prev_high=8, cur_findings=3)
    check("B2 无 unresolved 但收敛 → effective",
          out["rework_effective"] is True,
          repr((out.get("events") or [{}])[0]))

    # high 4→7 且有 still_real → False（复现发散形态）
    checks = [_ck("t1", "sec1", "fixed"), _ck("t2", "sec2", "still_there"),
              _ck("t3", "sec3", "still_there"), _ck("t4", "sec4", "still_there")]
    out = _run_audit(checks, [], prev_high=4, cur_findings=7)
    check("B3 high 4→7 发散 → 无效", out["rework_effective"] is False)

    # 全 fixed → True
    out = _run_audit([_ck("t1", "sec1", "fixed"), _ck("t2", "sec2", "fixed")],
                     [], prev_high=5, cur_findings=2)
    check("B4 全 fixed → effective", out["rework_effective"] is True)

    # prev_high 取不到 + still_real → 退回旧规则 → False
    out = _run_audit(checks, [], prev_high=None, cur_findings=3)
    check("B5 prev_high=None → 旧规则→无效",
          out["rework_effective"] is False)
    check("B5 事件 prev_high=None",
          (out.get("events") or [{}])[0].get("prev_high") is None)

    # still 全是 unresolved 项 → still_real 空 → True
    checks = [_ck("t4", "sec4", "still_there"),
              _ck("t6", "sec6", "changed_form")]
    out = _run_audit(checks, UNRES_REAL, prev_high=8, cur_findings=3)
    check("B6 still 全是 unresolved → effective",
          out["rework_effective"] is True)
    check("B6 still_open=0/still_unresolved=2",
          (out.get("events") or [{}])[0].get("still_open") == 0
          and (out.get("events") or [{}])[0].get("still_unresolved") == 2)

    # fixed_n==0 且有 still_real → False（一条都没修掉不能算收敛）
    checks = [_ck("t1", "sec1", "still_there"), _ck("t2", "sec2", "changed_form")]
    out = _run_audit(checks, [], prev_high=8, cur_findings=3)
    check("B7 fixed=0 且 still_real → 无效", out["rework_effective"] is False)

    # _prev_high 真函数：无 session/rounds=0 → None
    check("B8 _prev_high(None)→None", W._prev_high(None, 1) is None
          and W._prev_high("s", 0) is None)


if __name__ == "__main__":
    t_clip_sentence()
    t_unresolved_line()
    t_translate_ids()
    t_final_doubt_section()
    t_rework_effective()
    print(f"\n== 通过 {_ok}/{_n}，失败 {_fail} ==")
    sys.exit(1 if _fail else 0)
