"""程序护栏 `_auto_fix_*` + 残留违规→返修目标的守门测试（离线）。

2026-10-19 加（坑 79）。三类真实终稿缺陷是确定性可查的，不再靠模型看：
  R1  多引用/区间括号规范化：`[S3·1、S3·2]`→`[S3·1][S3·2]`、区间展开
  R2  伪引用：`[S21 系报道口径,按规约……]`→`[S21]`（首 token 在分内才修）
  R3a `secN` → 人读的节名（只替换不删句：实质内容不许陪葬）
  R3b `tN`/`tNa` → 保守删整句（带 [S 引用/表格/标题/超长的句子不删）
修不了的一律不动，残留由 `_check_internal_ids`/`_check_citations` 报出，
`node_audit` 把残留变成返修目标（模型说「不用返修」也不算数）。

桩挂 `W._run_task`（dispatch/revise 调用层）与 `W.LLM.complete_json_forced`
（audit 调用层），与 test_dispatch_retry/test_audit_forced 同层。

用法：
    .venv/bin/python tests/test_auto_fix.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _isolate  # noqa: F401, E402

from research_agent.graph.subgraphs import write as W   # noqa: E402

FAILED: list[str] = []

# 小 outline 桩：给 R3a 换名用，取自真实稿的节名
TITLES = {"sec4": "成本与制造", "sec6": "企业路线",
          "sec8": "标准与专利", "sec10": "产业化阶段"}


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}" + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def fix(text: str, allowed, titles=TITLES, cur=None) -> str:
    out, _ = W._auto_fix_text(text, set(allowed), titles, cur)
    return out


def fix_all(text: str, allowed, titles=TITLES, cur=None):
    return W._auto_fix_text(text, set(allowed), titles, cur)


# --------------------------------------------------------------- ① 真实缺陷

def t_real_defects():
    # 任务编号元句删除（v14 真实形态，无 [S 引用）：整句删掉，前后句保留。
    text = "前文一句。S29·8 在 t6b 的任务范围内,本节仅在涉及标准与专利耦合时引用。下一句。"
    out = fix(text, {"S29·8"})
    check("① t6b 元说明句删净、前后句留",
          out == "前文一句。下一句。", repr(out))

    # secN 指代别节：句子是实质内容，**只换名不删**（v14 真实句）。
    text = ("- **设备/工艺细节**（温等静压、干法电极、辊压之争）："
            "按厂商口径与综述报道给出（S36、S38、S39），"
            "需在 sec6 与 sec8 中交叉印证。")
    out = fix(text, set())
    check("① 实质句不删：secN 换成节名",
          "需在 「企业路线」一节 与 「标准与专利」一节 中交叉印证" in out
          and "设备/工艺细节" in out, repr(out[-60:]))

    # 「另见 sec10」这种实质判断整句保留（首版 R3 的误伤案例）。
    text = ("在该条件下，2026—2027 年更可能是 pilot 与小批量产线元年，"
            "而非规模化商业化元年——具体时间线判断另见 sec10。")
    out = fix(text, set())
    check("① pilot 元年句保留、sec10 换名",
          "更可能是 pilot" in out and "「产业化阶段」一节" in out
          and "sec10" not in out, repr(out[-50:]))

    # 自指：cur_sec=sec6 时 sec6 → 本节（吞一个空格）。
    out = fix("sec6 的核心问题不是「谁家更好」。", set(), cur="sec6")
    check("① 自指 secN → 本节",
          out == "本节的核心问题不是「谁家更好」。", repr(out))

    # 伪引用（v14 真实形态）：首 token S21 有分段 → 整括号规范成 [S21]。
    text = "某结论 [S21 系报道口径,按规约交叉印证要求须与 S18、S20、S23、S24·1 交叉印证后再用] 成立。"
    out = fix(text, {"S21·1"})
    check("① 伪引用 → [S21]（首 token 来源级保留）",
          out == "某结论 [S21] 成立。", repr(out[:40]))

    # 区间引用（v14 真实形态）：展开后只留分内的段。
    text = "多点印证 [S29·1—S29·7]。"
    out = fix(text, {"S29·1", "S29·2", "S29·3"})
    check("① 区间 [S29·1—S29·7] → 只留分内三段",
          out == "多点印证 [S29·1][S29·2][S29·3]。", repr(out))


def t_bracket_rules():
    # 区间部分越界 → 只留 allowed 内的；全越界 → 不改动（留给残留）。
    out = fix("[S5·2—S5·6]", {"S5·4"})
    check("② 区间只留分内一段", out == "[S5·4]", repr(out))
    out = fix("[S5·2—S5·6]", {"S9·1"})
    check("② 区间全越界 → 原样不动", out == "[S5·2—S5·6]", repr(out))
    out = fix("[S5·2—7]", {"S5·3"})
    check("② 短写区间 S5·2—7 同样展开", out == "[S5·3]", repr(out))
    out = fix("[S5·1—S5·20]", {"S5·3"})
    check("② 超 12 段区间不展开", out == "[S5·1—S5·20]", repr(out))

    # 并列拆分 + 去重保序。
    check("③ 半角逗号并列拆开",
          fix("[S3·1, S3·2]", {"S3·1", "S3·2"}) == "[S3·1][S3·2]")
    check("③ 顿号并列拆开",
          fix("[S3·1、S3·2]", {"S3·1", "S3·2"}) == "[S3·1][S3·2]")
    check("③ 重复 token 去重",
          fix("[S3·1、S3·1、S3·2]", {"S3·1", "S3·2"}) == "[S3·1][S3·2]")
    check("③ 并列里越界的被剔除",
          fix("[S3·1, S9·9]", {"S3·1"}) == "[S3·1]")
    check("③ 规范单引用原样",
          fix("[S3·5]", {"S3·5"}) == "[S3·5]")

    # 伪引用首 token 越界 → 不删不改（留给残留检查）。
    text = "[S21 系报道口径,待交叉印证]"
    out = fix(text, {"S9·1"})
    check("④ 伪引用首 token 越界 → 原样不动",
          out == text, repr(out))


def t_sentence_rules():
    # R3b 删句豁免：带 [S 引用/表格行/标题行/>120 字 → 不删，残留能报。
    text = "[S3·1] 这段属于 t6 的范围说明。"
    out = fix(text, {"S3·1"})
    check("⑤ 带引用的 tN 句不删（可能是实质内容）",
          out == text, repr(out))
    check("⑤ 残留检查能报出「内部编号」",
          any(b["kind"] == "内部编号" for b in W._check_internal_ids(out)))

    text = "| 列 |\n| t6 负责这段 |\n正文。"
    out = fix(text, set())
    check("⑤ 表格行的 tN 不删", out == text, repr(out))
    check("⑤ 表格行残留能报出",
          any(b["kind"] == "内部编号" for b in W._check_internal_ids(out)))

    # 但表格行里的 secN 原地换名——替换不破坏表格结构。
    text = "| 指标 | 详见 sec4 |\n| a | b |"
    out = fix(text, set())
    check("⑤ 表格行 secN 原地换名",
          "详见 「成本与制造」一节" in out and "|" in out, repr(out[:40]))

    text = "# 小节 sec6 的说明\n正文。"
    out = fix(text, set())
    check("⑤ 标题行 secN 不动（留残留）",
          out == text and W._check_internal_ids(text), repr(out[:30]))

    # secN 不在大纲里 → 不换，残留报出来进返修。
    text = "细节见 sec99 的处理。"
    out = fix(text, set())
    check("⑤ 未知 secN 不换名且残留可报",
          "sec99" in out
          and any(b["kind"] == "内部编号" for b in W._check_internal_ids(out)),
          repr(out))

    long_sent = "这个超长句子里出现了 t3 的编号" + "，很长的补充说明" * 30 + "。"
    text = f"短句。{long_sent}下一句。"
    out = fix(text, set())
    check("⑤ >120 字的 tN 句不删", "t3" in out, repr(out[:30]))
    check("⑤ 长句残留能报出",
          any(b["kind"] == "内部编号" for b in W._check_internal_ids(out)))

    # 装配注释里的 t6/sec2 不算正文命中。
    text = "<!-- task:t6 section:sec2 -->\n正文句子。"
    out = fix(text, set())
    check("⑤ HTML 注释不被误改", out == text, repr(out))
    check("⑤ 注释里的编号不报错",
          W._check_internal_ids(text) == [])


def t_false_positive_guard():
    # 误伤防护：这些写法一个都不能命中。
    safe = ("结论 [S3·5] 有出处。第 t 次迭代提到 500t 载荷，"
            "GB/T 1234 标准与 S29t 批次一致，test1 用例通过，"
            "secure channel 与 second phase 也正常，版本 t1.5 发布。")
    out, fx = fix_all(safe, {"S3·5"})
    check("⑥ 误伤防护：全句原样",
          out == safe and fx == [], repr(out[:50]))
    check("⑥ 误伤防护：无内部编号残留",
          W._check_internal_ids(safe) == [])

    # 幂等：修过的稿子再跑一遍，零 fixes。
    text = "证据 [S3·1、S3·2]。t1 的任务范围说明。结尾。"
    once, _ = fix_all(text, {"S3·1", "S3·2"})
    twice, fx2 = fix_all(once, {"S3·1", "S3·2"})
    check("⑦ 幂等：二跑无变化", twice == once and fx2 == [], repr(twice))


# ------------------------------------------------------------- ② 节点接入

def _task(tid: str, covers=None, segs=None) -> dict:
    return {"id": tid, "covers": covers or ["sec1"], "mandate": "m",
            "segments": segs or ["S3·1", "S3·2"],
            "must_cover": [], "not_yours": []}


def _state(tasks, **kw) -> dict:
    st = {"session_id": None, "question": "q", "spec": {"thesis": "T"},
          "outline": [{"id": "sec1"}], "tasks": tasks,
          "drafts": {}, "failed": [], "violations": {},
          "segments": [], "on_topic": [], "src_lines": []}
    st.update(kw)
    return st


def _with_run_task(script, fn):
    calls = {}
    def fake(state, task, revise=None):
        calls[task["id"]] = calls.get(task["id"], 0) + 1
        seq = script.get(task["id"], [({}, "x")])
        return seq[min(calls[task["id"]] - 1, len(seq) - 1)]
    orig = W._run_task
    W._run_task = fake
    try:
        return calls, fn()
    finally:
        W._run_task = orig


def t_nodes_apply_fix():
    # dispatch：初稿里的两类缺陷在进 violations/审查前被修掉。
    body = "证据见 [S3·1、S3·2]。t1 的任务范围说明一句。正文收尾。"
    _, out = _with_run_task(
        {"t1": [({"sec1": body}, None)]},
        lambda: W.node_dispatch(_state([_task("t1")])))
    ev = (out.get("events") or [{}])[0]
    check("⑧ dispatch 修稿：auto_fixes==2 且样本能查",
          ev.get("auto_fixes") == 2 and bool(ev.get("auto_fix_sample")),
          str(ev.get("auto_fix_sample"))[:120])
    check("⑧ 修后正文：引用规范化、编号句删除",
          out["drafts"]["t1"]["sec1"] == "证据见 [S3·1][S3·2]。正文收尾。",
          repr(out["drafts"]["t1"]["sec1"]))

    # revise：返修稿同样过护栏（R1 括号 + R3b 删句 + R3a 换名三处）。
    st = _state([_task("t2")],
                drafts={"t2": {"sec1": "旧稿 [S3·1、S3·2]。"}},
                failed=[], audit={"targets": [{"task": "t2",
                                               "section": "sec1",
                                               "problem": "p",
                                               "fix": "f"}]},
                rework_rounds=0,
                outline=[{"id": "sec1", "title": "定义"},
                         {"id": "sec6", "title": "企业路线"}])
    _, out = _with_run_task(
        {"t2": [({"sec1": "新稿见 [S3·1、S3·2]，细节在 sec6。且含 t1 元说明句。收尾。"},
                 None)]},
        lambda: W.node_revise(st))
    ev = (out.get("events") or [{}])[0]
    check("⑧ revise 修稿：auto_fixes==3",
          ev.get("auto_fixes") == 3, str(ev.get("auto_fix_sample"))[:160])
    check("⑧ revise 修后正文：R1+R3a+R3b 都生效",
          out["drafts"]["t2"]["sec1"]
          == "新稿见 [S3·1][S3·2]，细节在 「企业路线」一节。收尾。",
          repr(out["drafts"]["t2"]["sec1"]))


# --------------------------------------------------- ③ 残留 → 返修目标

class StubAuditLLM:
    def __init__(self, raw):
        self.raw = raw

    def complete_json_forced(self, stage, prompt, name, schema,
                             system=None):
        return self.raw


def _audit_state(violations, **kw):
    st = {"question": "q", "spec": {"thesis": "T"},
          "assembled": "稿", "session_id": None, "rework_rounds": 0,
          "tasks": [_task("t1"), _task("t9")],
          "violations": violations}
    st.update(kw)
    return st


def _run_audit(raw, violations, **kw):
    orig = W.LLM.complete_json_forced
    W.LLM.complete_json_forced = StubAuditLLM(raw).complete_json_forced
    try:
        return W.node_audit(_audit_state(violations, **kw))
    finally:
        W.LLM.complete_json_forced = orig


PASS = {"need_rework": False, "reason": "ok", "findings": [], "blocked": []}


def t_violation_targets():
    viol = {"t1": [{"kind": "内部编号", "found": "…在 t6b 的任务范围内…"},
                   {"kind": "越界引用", "found": "S9·9"}]}
    # 模型说 need_rework=False → 残留违规仍必须进返修。
    out = _run_audit(dict(PASS), viol)
    a = out["audit"]
    ev = (out.get("events") or [{}])[0]
    check("⑨ 模型说 false，残留仍逼出返修",
          a["need_rework"] is True and len(a["targets"]) == 1
          and a["targets"][0]["section"] is None
          and a["targets"][0]["task"] == "t1", str(a["targets"])[:160])
    check("⑨ violation_targets=1 进事件与 audit",
          ev.get("violation_targets") == 1
          and a.get("violation_targets") == 1, str(ev)[:140])
    check("⑨ problem/fix 按模板生成",
          a["targets"][0]["problem"].startswith("程序检出 2 处引用/编号问题:")
          and "内部编号" in a["targets"][0]["problem"]
          and "删掉整句元说明" in a["targets"][0]["fix"],
          a["targets"][0]["problem"][:80])

    # 模型 high 已点名的任务不重复追加 target——但违规文本要并进
    # 该任务的第一条 target（返修提示里不能丢程序检出的问题）。
    raw = {"need_rework": True, "reason": "r", "blocked": [],
           "findings": [{"task": "t1", "section": "sec1",
                         "severity": "high", "problem": "p", "fix": "f"}]}
    out = _run_audit(raw, viol)
    a = out["audit"]
    tgt = a["targets"][0]
    check("⑨ 已点名任务：违规并进首个 target（不新增、不计数）",
          len(a["targets"]) == 1 and tgt["section"] == "sec1"
          and "p" in tgt["problem"]
          and "程序检出 2 处引用/编号问题" in tgt["problem"]
          and "删掉整句元说明" in tgt["fix"]
          and a.get("violation_targets") == 0,
          str(tgt)[:200])

    # 无违规、模型也放行 → need_rework False。
    out = _run_audit(dict(PASS), {})
    check("⑨ 干净稿子照旧放行",
          out["audit"]["need_rework"] is False, str(out["audit"])[:80])

    # 残留永远修不掉时：rework_rounds 到 MAX_REWORK 就收口。
    st = {"audit": {"need_rework": True},
          "rework_rounds": W.MAX_REWORK, "rework_effective": True}
    check("⑩ 残留修不掉：MAX_REWORK 后停",
          W.route_after_audit(st) == "final")
    st["rework_rounds"] = W.MAX_REWORK - 1
    check("⑩ 没修掉且轮次未满：继续返修",
          W.route_after_audit(st) == "revise")


def main() -> int:
    print("=== ① 真实缺陷原样 ===")
    t_real_defects()
    print("\n=== ②③④ 括号规则 ===")
    t_bracket_rules()
    print("\n=== ⑤ 删句豁免 ===")
    t_sentence_rules()
    print("\n=== ⑥⑦ 误伤防护与幂等 ===")
    t_false_positive_guard()
    print("\n=== ⑧ 节点接入 ===")
    t_nodes_apply_fix()
    print("\n=== ⑨⑩ 残留 → 返修目标 ===")
    t_violation_targets()
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
