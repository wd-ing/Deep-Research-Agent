"""合并闸门（B2-5）守门测试（离线）：summarize 同批判 relevant。

2026-10-25 加。规则：**规则层优先**——命中专有名词的段不采信
prejudged；规则没放行的段用 `merged_gate`（{chunk_id: bool}）作闸门
结果；prejudged 缺 key / 偏离的段照旧落 LLM 批量判定兜底。

用法：.venv/bin/python tests/test_merged_gate.py
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("MINIMAX_API_KEY", "stub-not-used")
os.environ["RA_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "t.db")

from research_agent.graph.subgraphs import extract_batch as EB  # noqa: E402
from research_agent.graph.subgraphs import relevance as RV       # noqa: E402

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}" + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def _seg(cid: str, text: str = "", summary: str = "", heading: str = ""):
    return {"id": cid, "heading_path": heading, "summary": summary,
            "content": text, "title": ""}


KW = {"strong": ["固态电池", "宁德时代"], "weak": []}


def t1_prejudged_only_for_rule_misses():
    """规则放行不吃 prejudged；规则没放行的吃；缺 key 的走 LLM。"""
    llm_calls: list[str] = []
    orig = RV.LLM.complete_json
    # 兜底 LLM 全判 true（能走到它说明 prejudged 没覆盖）；verdicts
    # 铺满下标避免空 verdicts 把段推进 stragglers 重判。
    RV.LLM.complete_json = lambda stage, prompt, system=None: (
        llm_calls.append(prompt)
        or {"verdicts": [{"index": i, "on_topic": True}
                         for i in range(24)]})
    segs = [
        _seg("c_rule", text="固态电池", summary="讲固态电池路线"),   # rule 放行
        _seg("c_merged_on", text="别的领域", summary="无关内容"),   # merged=True
        _seg("c_merged_off", text="别的领域", summary="无关内容"),  # merged=False
        _seg("c_missing", text="别的领域", summary="无关内容"),     # 无 verdict → LLM
    ]
    pre = {"c_rule": False, "c_merged_on": True, "c_merged_off": False}
    try:
        out = RV.gate_segments(segs, KW, "固态电池产业化", prejudged=pre)
    finally:
        RV.LLM.complete_json = orig
    marks = {m[3]: m for m in out["marks"]}
    check("① 规则放行的段不采信 merged=False",
          marks["c_rule"][0] == 1 and marks["c_rule"][2] == "rule",
          str(marks["c_rule"]))
    check("① 规则没放行 + merged=True → on/method=merged",
          marks["c_merged_on"][0] == 1 and marks["c_merged_on"][2] == "merged")
    check("① 规则没放行 + merged=False → off/method=merged",
          marks["c_merged_off"][0] == 0 and marks["c_merged_off"][2] == "merged")
    check("① 缺 verdict 的段落 LLM 兜底（进了批判）",
          len(llm_calls) == 1, f"llm_calls={len(llm_calls)}")
    check("① keep 含 rule + merged_on + LLM兜底，不含 merged_off",
          {s["id"] for s in out["keep"]}
          == {"c_rule", "c_merged_on", "c_missing"})


def t2_no_prejudged_unchanged():
    """prejudged=None（旧路径/字段缺失）行为与改前一致。"""
    llm_calls: list[str] = []
    orig = RV.LLM.complete_json
    RV.LLM.complete_json = lambda stage, prompt, system=None: (
        llm_calls.append(prompt)
        or {"verdicts": [{"index": i, "on_topic": True}
                         for i in range(24)]})
    segs = [_seg("c_a", summary="无关"), _seg("c_b", summary="无关")]
    try:
        out = RV.gate_segments(segs, KW, "q", prejudged=None)
    finally:
        RV.LLM.complete_json = orig
    check("② 无 prejudged：非规则段全走 LLM", len(llm_calls) == 1)
    check("② 统计口径不变", out["stats"]["segments"] == 2)


def t3_summarize_merged_output():
    """topic_label 时：schema 带 relevant、产出 relevant 映射、
    非 bool 偏离被丢弃（缺 key → 下游兜底）。"""
    calls: list[dict] = []
    orig = EB.LLM.complete_json_forced

    def fake(stage, prompt, name, schema, system=None):
        calls.append({"prompt": prompt, "schema": schema, "system": system})
        return {"summaries": [
            {"index": 0, "summary": "s0", "relevant": True},
            {"index": 1, "summary": "s1", "relevant": "false"},  # 字符串收
            {"index": 2, "summary": "s2", "relevant": "maybe"},  # 偏离丢
            {"index": 3, "summary": "s3"},                        # 缺键丢
        ]}

    EB.LLM.complete_json_forced = fake
    items = [{"chunk_id": f"c{i}", "text": "t" * 50, "heading_path": ""}
             for i in range(4)]
    try:
        out = EB.summarize_segments(items, topic_label="固态电池")
    finally:
        EB.LLM.complete_json_forced = orig

    check("③ schema 带 relevant 且 required",
          "relevant" in calls[0]["schema"]["properties"]["summaries"]
          ["items"]["properties"]
          and "relevant" in calls[0]["schema"]["properties"]["summaries"]
          ["items"]["required"],
          str(calls[0]["schema"])[:200])
    check("③ 提示词带研究对象", "固态电池" in calls[0]["prompt"])
    check("③ system = SUMMARIZE + GATE_NOTE",
          "relevant" in (calls[0]["system"] or ""))
    check("③ summaries 四段全产出",
          len(out["summaries"]) == 4, str(out["summaries"]))
    check("③ relevant：true/字符串false收下、maybe与缺键丢",
          out["relevant"] == {"c0": True, "c1": False},
          str(out["relevant"]))
    check("③ 事件带 merged_gate/relevant_judged",
          out["events"][0].get("merged_gate") is True
          and out["events"][0].get("relevant_judged") == 2)


def t4_summarize_no_label_unchanged():
    """不给 label：schema 不变、relevant 空（老调用方不受影响）。"""
    calls: list[dict] = []
    orig = EB.LLM.complete_json_forced
    EB.LLM.complete_json_forced = lambda stage, prompt, name, schema, system=None: (
        calls.append({"schema": schema, "system": system})
        or {"summaries": [{"index": 0, "summary": "x"}]})
    try:
        out = EB.summarize_segments(
            [{"chunk_id": "c0", "text": "t" * 50, "heading_path": ""}])
    finally:
        EB.LLM.complete_json_forced = orig
    check("④ 无 label：schema 不带 relevant",
          "relevant" not in calls[0]["schema"]["properties"]["summaries"]
          ["items"]["properties"])
    check("④ 无 label：relevant 空表",
          out["relevant"] == {}, str(out["relevant"]))
    check("④ 无 label：system 是原 SUMMARIZE",
          "relevant" not in (calls[0]["system"] or ""))


def t5_norm_relevant():
    check("⑤ _norm_relevant 归一化",
          EB._norm_relevant(True) is True
          and EB._norm_relevant("false") is False
          and EB._norm_relevant("maybe") is None
          and EB._norm_relevant(1) is None)


if __name__ == "__main__":
    t1_prejudged_only_for_rule_misses()
    t2_no_prejudged_unchanged()
    t3_summarize_merged_output()
    t4_summarize_no_label_unchanged()
    t5_norm_relevant()
    print()
    if FAILED:
        print("失败：" + "、".join(FAILED))
        sys.exit(1)
    print("全过 ✅")
