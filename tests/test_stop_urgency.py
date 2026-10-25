"""第 3 轮起的 urgency 停止线 + `_stop_kind` 分层映射的守门测试（离线）。

2026-10-25 时间优化：已完成 ≥2 轮检索后，最近一次 assess 的
`gaps_by_urgency.high <= 1` → 停（stop_layer="urgency"）。
依据 v18 基线：第 2 轮 high=1，第 3 轮只新增 9 段、0 段相关——
剩下的 medium/low 是「网上没有」，不是「再搜就有」。

`_predict_stop` 是停止判据唯一真相源（`_should_stop` 只问它）；
`_stop_kind` 必须把 urgency 映射到 satisfied——材料够用不是撞顶，
审批页不该显示「材料不足」警告。

用法：.venv/bin/python tests/test_stop_urgency.py
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

from research_agent.graph import nodes as N   # noqa: E402

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}" + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def _st(**kw) -> dict:
    st = {"plan_queries": {"zh": ["q"], "en": []},
          "round": 0, "consecutive_no_new": 0,
          "url_count": 0, "elapsed_s": 0}
    st.update(kw)
    return st


def t1_round2_high1_stops():
    st = _st(round=2, gaps_by_urgency={"high": 1, "medium": 1, "low": 6})
    reason = N._predict_stop(st)
    check("① round=2/high=1 → 停（urgency 文案）",
          reason == "剩余缺口紧迫度低（high≤1），不再追加检索",
          reason)
    check("① _should_stop 一致", N._should_stop(st) is True)
    check("① _stop_kind 兜底 → satisfied（不落 forced）",
          N._stop_kind({**st, "stop_reason": reason}) == "satisfied")
    check("① 显式 stop_layer=urgency → satisfied",
          N._stop_kind({**st, "stop_reason": reason,
                        "stop_layer": "urgency"}) == "satisfied")


def t2_high2_continues():
    st = _st(round=2, gaps_by_urgency={"high": 2, "medium": 1})
    check("② round=2/high=2 → 继续",
          N._predict_stop(st) == "" and N._should_stop(st) is False)


def t3_round1_immune():
    st = _st(round=1, gaps_by_urgency={"high": 0})
    check("③ round=1/high=0 → 不触发此条",
          N._predict_stop(st) == "", N._predict_stop(st))
    st = _st(round=0, gaps_by_urgency={"high": 0})
    check("③ round=0 → 不触发", N._predict_stop(st) == "")


def t4_missing_field_safe():
    st = _st(round=3)          # 没跑过 assess：字段缺省不触发
    check("④ 字段缺省 → 不误判紧迫度低", N._predict_stop(st) == "")


def t5_existing_stops_intact():
    """原有停止条件全部照过。"""
    st = _st(round=1, consecutive_no_new=2,
             gaps_by_urgency={"high": 5})
    r = N._predict_stop(st)
    check("⑤ 连续无新增仍停（硬条件）", r.startswith("硬条件：连续"), r)
    check("⑤ 无新增是 mechanism → forced",
          N._stop_kind({**st, "stop_reason": r}) == "forced")

    st = _st(round=N.HARD["max_rounds"])
    check("⑤ 轮数到顶仍停", "轮数到顶" in N._predict_stop(st))

    st = _st(url_count=N.HARD["max_urls"])
    check("⑤ URL 到顶仍停", "URL 数到顶" in N._predict_stop(st))

    st = _st(elapsed_s=N.HARD["timeout_min"] * 60)
    check("⑤ 超时仍停", "超时" in N._predict_stop(st))

    st = _st(stop_reason="assess 说够了")
    check("⑤ stop_reason 已写 → 原样回传",
          N._predict_stop(st) == "assess 说够了")
    check("⑤ assess 说够了 → satisfied",
          N._stop_kind(st) == "satisfied")

    st = _st()
    check("⑤ 干净状态继续", N._predict_stop(st) == "")

    st = _st(plan_queries={})
    check("⑤ 无查询词仍停", N._predict_stop(st) == "没有可用的查询词")


def t6_missing_high_key_matrix():
    """B2-3 修复形态（v19 现场暴露的 bug）：`_count_urgency` 只输出
    出现过的桶——{medium:3,low:6} 没有 high 键 = 0 个高紧迫缺口，
    是「最该停」的情形；旧 `.get("high", 99)` 把它放跑了。
    此处把 _predict_stop 的全部形态钉死。"""
    URG = "剩余缺口紧迫度低（high≤1），不再追加检索"
    cases = [
        ("{medium,low}无high键@2 → 停", 2, {"medium": 3, "low": 6}, URG),
        ("空dict{}@2 → 停（0 high）", 2, {}, URG),
        ("{high:1}@2 → 停", 2, {"high": 1, "low": 4}, URG),
        ("{high:2}@2 → 继续", 2, {"high": 2, "low": 4}, ""),
        ("{medium,low}@1 → 不触发", 1, {"medium": 3, "low": 6}, ""),
        ("{}@1 → 不触发", 1, {}, ""),
        ("字段缺省@2 → 不触发", 2, "__ABSENT__", ""),
        ("显式None@2 → 不触发", 2, None, ""),
    ]
    for label, rnd, gu, want in cases:
        kw = {"round": rnd}
        if gu != "__ABSENT__":
            kw["gaps_by_urgency"] = gu
        got = N._predict_stop(_st(**kw))
        check(f"⑥ {label}", got == want, f"got={got!r}")


def _assess_state(rnd: int) -> dict:
    return {"session_id": "s_urg", "round": rnd, "question": "q",
            "outline": [{"id": "sec1", "title": "固态电池", "question": ""}],
            "used_queries": [], "plan_queries": {},
            "consecutive_no_new": 0, "last_round_segments": 1,
            "plan_approved": False}


def _run_assess(data: dict, rnd: int = 2):
    """桩段落与 assess 模型，跑真 node_assess 拿 stop_* 出参。"""
    segs = [{"ref": "S1·1", "heading_path": "", "summary": "s1"},
            {"ref": "S1·2", "heading_path": "", "summary": "s2"}]
    orig_segs, orig_cj = N._segments_for_assess, N.LLM.complete_json
    N._segments_for_assess = lambda repo, sid: segs
    N.LLM.complete_json = lambda stage, prompt, system=None: data
    try:
        return N.node_assess(_assess_state(rnd))
    finally:
        N._segments_for_assess, N.LLM.complete_json = orig_segs, orig_cj


def t7_node_assess_urgency_matrix():
    """node_assess 停止链同口径（v19 现场数据形态逐行复现）。"""
    URG = "剩余缺口紧迫度低（high≤1），不再追加检索"
    gaps_ml = ([{"section_id": "sec1", "urgency": "medium",
                "what_missing": "x"}] * 3
               + [{"section_id": "sec1", "urgency": "low",
                   "what_missing": "y"}] * 6)

    # v19 现场：round=2 gaps {medium:3,low:6}（无 high 键）→ 该停没停
    out = _run_assess({"has_enough": False, "gaps": gaps_ml,
                       "next_queries": {"zh": ["新词"], "en": []}})
    check("⑦ node_assess round=2 无high键 → 停",
          out.get("stop_reason") == URG
          and out.get("stop_layer") == "urgency",
          f"{out.get('stop_reason')!r}/{out.get('stop_layer')!r}")

    # gaps=[] → 0 high → 停
    out = _run_assess({"has_enough": False, "gaps": [],
                       "next_queries": {"zh": ["新词"], "en": []}})
    check("⑦ gaps=[] → 停", out.get("stop_reason") == URG,
          out.get("stop_reason"))

    # high=2 → 继续
    out = _run_assess({"has_enough": False,
                       "gaps": [{"section_id": "s", "urgency": "high",
                                 "what_missing": "w"}] * 2,
                       "next_queries": {"zh": ["新词"], "en": []}})
    check("⑦ high=2 → 继续", out.get("stop_reason") == "",
          repr(out.get("stop_reason")))

    # data={}（调用失败态）→ 绝不按紧迫度低收工
    out = _run_assess({})
    check("⑦ data 空（失败态）→ 不触发", out.get("stop_reason") == "",
          repr(out.get("stop_reason")))

    # round=1 免疫
    out = _run_assess({"has_enough": False, "gaps": gaps_ml,
                       "next_queries": {"zh": ["新词"], "en": []}}, rnd=1)
    check("⑦ round=1 无high键 → 不触发", out.get("stop_reason") == "",
          repr(out.get("stop_reason")))


if __name__ == "__main__":
    t1_round2_high1_stops()
    t2_high2_continues()
    t3_round1_immune()
    t4_missing_field_safe()
    t5_existing_stops_intact()
    t6_missing_high_key_matrix()
    t7_node_assess_urgency_matrix()
    print()
    if FAILED:
        print("失败：" + "、".join(FAILED))
        sys.exit(1)
    print("全过 ✅")
