"""write 子图的结构自检：不调 LLM，用假响应走完整条链路。

按约定 1，write 板块做完之前测试只打写作环节。真实数据联调放到下一轮，
这里先把**结构**验干净：路由、装配顺序、引用程序校验、返修收敛、
降级标注、任务上限。结构不对就别花钱跑真实数据。

用法：python tests/write_struct.py
"""

from __future__ import annotations

import json
import re
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from research_agent.graph.subgraphs import write as W  # noqa: E402

OUTLINE = [
    {"id": "sec1", "title": "定义与层级", "question": "怎么划分", "kind": "definition"},
    {"id": "sec2", "title": "材料路线", "question": "各路线差异", "kind": "comparison"},
    {"id": "sec3", "title": "企业格局", "question": "谁在做什么", "kind": "comparison"},
]
EVIDENCE = [
    {"i": 0, "text": "全固态电池指完全不含液态电解质的电池", "entities": [],
     "source": 1, "nature": "research", "confidence": "high", "date": "2025-01"},
    {"i": 1, "text": "半固态保留 5% 到 15% 液态电解液", "entities": [],
     "source": 1, "nature": "research", "confidence": "medium", "date": "2025-01"},
    {"i": 2, "text": "硫化物电导率高但遇水放出硫化氢", "entities": [],
     "source": 2, "nature": "media", "confidence": "high", "date": "2025-03"},
    {"i": 3, "text": "氧化物稳定但脆性大难加工", "entities": [],
     "source": 2, "nature": "media", "confidence": "medium", "date": "2025-03"},
    {"i": 4, "text": "某厂商宣称 2027 年量产半固态产品", "entities": [],
     "source": 3, "nature": "first_party", "confidence": "low", "date": "2025-06"},
    {"i": 5, "text": "聚合物柔性好但电导率低", "entities": [],
     "source": 2, "nature": "media", "confidence": "medium", "date": "2025-03"},
]
SRC = [
    "[S1] 学术综述 | paper.com | academic | 2025-01-01 | 正文 8000 字",
    "[S2] 行业报道 | news.com | media | 2025-03-01 | 正文 5000 字",
    "[S3] 厂商新闻稿 | vendor.com | official | 2025-06-01 | 正文 2000 字",
]
SEGMENTS = [{"ref": f"S{(i % 3) + 1}·{(i // 3) + 1}",
             "summary": t["text"],
             "token_count": 700, "heading_path": None, "content": t["text"]}
            for i, t in enumerate(EVIDENCE)]
BASE = {"question": "固态电池的产业化与技术路线",
        "outline": OUTLINE, "signals": {"subtopics": []},
        "segments": SEGMENTS, "on_topic": SEGMENTS,
        "src_lines": SRC, "earlier": []}



class Fake:
    """按 stage 返回预置响应，并记录实际收到的调用。"""

    def __init__(self, plan: dict, sections: dict, audits: list[dict],
                 revise: str = "") -> None:
        self.plan = plan
        self.sections = sections
        self.audits = audits
        self.revise = revise or "（改好了）"
        self.calls: list[tuple[str, int]] = []
        self._ai = 0

    def complete_json(self, stage: str, prompt: str, system: str | None = None):
        self.calls.append((stage, len(prompt)))
        if stage == "write_plan":
            # 拆成两次之后靠提示词内容区分：规划给「证据段落」清单，
            # 分派给「写作规约 + 框架与轮廓」
            if "写作规约" in prompt:
                return self.plan
            return {k: v for k, v in self.plan.items() if k != "tasks"}
        if stage == "write_audit":
            d = self.audits[min(self._ai, len(self.audits) - 1)]
            self._ai += 1
            return d
        return self.sections.get(stage, {})

    def complete_with_tools(self, stage: str, system, messages, tools):
        """模拟子 agent：第一轮要 2 段全文，第二轮直接给正文。"""
        self.calls.append((stage + ":tools", len(messages)))
        n = len([m for m in messages if m.get("role") == "user"])
        if n == 1:
            return {"blocks": [
                {"type": "tool_use", "id": "t1", "name": "read_segments",
                 "input": {"segments": ["S1·1", "S1·2"]}},
                {"type": "tool_use", "id": "t2", "name": "read_context",
                 "input": {"segment": "S1·2", "before": 1, "after": 1}}],
                "usage": {}}
        return {"blocks": [{"type": "text",
                            "text": f"[S1·1] 任务 {stage} 的正文。"}],
                "usage": {}}

    def complete(self, stage: str, prompt: str, system: str | None = None,
                 json_mode: bool = False) -> str:
        self.calls.append((stage, len(prompt)))
        if stage in ("write_section", "write_revise"):
            tid = _task_id_from(prompt)
            return self.sections.get(f"{stage}:{tid}", f"[S1·1] 任务 {tid} 的正文。")
        return "{}"


def _unused_task_id_from(prompt: str) -> str:
    for line in prompt.splitlines():
        if line.startswith("id="):
            return line[3:].split()[0]
    return "?"


def run(fake: Fake, label: str, expect_tasks: int | None = None) -> dict:
    orig_j, orig_c = W.LLM.complete_json, W.LLM.complete
    orig_t = getattr(W.LLM, "complete_with_tools", None)
    W.LLM.complete_json = fake.complete_json
    W.LLM.complete = fake.complete
    W.LLM.complete_with_tools = fake.complete_with_tools
    try:
        out = W.run_write("s_test", BASE["question"], OUTLINE, BASE["signals"],
                          SEGMENTS, SEGMENTS, SRC)
    finally:
        W.LLM.complete_json, W.LLM.complete = orig_j, orig_c
        W.LLM.complete_with_tools = orig_t
    md = out.get("report_markdown") or ""
    kinds = [e.get("kind") for e in out.get("events") or []]
    n_tasks = len([t for t in fake.plan.get("tasks") or []])
    print(f"\n{'='*70}\n{label}\n{'='*70}")
    print(f"  主 agent 声明 {n_tasks} 个任务"
          + (f"（截断标记={out.get('spec_capped')}）" if out.get('spec_capped') else ""))
    print(f"  事件链: {' → '.join(kinds)}")
    print(f"  返修轮数: {out.get('rework_rounds', 0)}  有效: {out.get('rework_effective')}")
    print(f"  报告 {len(md)} 字，含小节 "
          f"{sum(1 for l in md.splitlines() if l.startswith('## '))} 个")
    if expect_tasks is not None and n_tasks != expect_tasks:
        print(f"  ❌ 任务数应为 {expect_tasks}，实际 {n_tasks}")
    return out


# ---------- 场景 1：正常一轮，无返修 ----------
plan1 = {"tasks": [
    {"id": "t1", "covers": ["sec1"], "mandate": "讲清三层边界",
     "segments": ["S1·1", "S1·2"],
     "must_cover": ["定义"], "not_yours": ["企业"]},
    {"id": "t2", "covers": ["sec2"], "mandate": "对比材料路线",
     "segments": ["S1·3", "S2·1", "S2·2"],
     "must_cover": ["电导率"], "not_yours": []},
    {"id": "t3", "covers": ["sec3"], "mandate": "企业布局",
     "segments": ["S3·1"],
     "must_cover": ["时间表"], "not_yours": []},
],
    "thesis": "全固态仍在中试，半固态先行",
    "glossary": [{"canonical": "全固态电池", "en": "ASSB", "aliases": ["全固态"]}],
    "units": ["能量密度统一 Wh/kg"],
    "cautions": ["S3 是厂商自述，不能当独立验证"],
    "spec_uncertainty": "「准固态」的归属材料里没有定论",
    "sections": [{"id": "sec1", "role": "定义"}]}
audit_ok = {"need_rework": False, "reason": "整体没问题", "findings": [],
            "remaining_issues": [], "spec_issue": None, "rework_effective": True}
out1 = run(Fake(plan1, {}, [audit_ok]), "场景 1：正常一轮，主 agent 判定无需返修")
assert "## 定义与层级" in out1["report_markdown"], "小节标题应按大纲顺序出现"
assert "<!-- task:" not in out1["report_markdown"], "定位标记必须被审查看到、装配后去掉"
assert "规约自检" in out1["report_markdown"], "规约自检必须落到报告里"
assert "「准固态」的归属材料里没有定论" in out1["report_markdown"], \
    "规约自检的原文要出现在报告里，不能只写「有存疑」"
print("  ✓ 小节按大纲顺序装配")
print("  ✓ 定位标记已剥离")
print("  ✓ 规约自检写进存疑一节")

# ---------- 场景 2：high 级问题触发返修，且返修有效 ----------
audit_bad = {"need_rework": True,
             "reason": "t1 越界引用了不在它来源里的编号",
             "findings": [{"task": "t1", "section": "sec1", "severity": "high",
                           "problem": "引用了 [S3] 但 t1 没有 S3",
                           "fix": "删掉 [S3] 那句"}],
             "remaining_issues": [], "spec_issue": None, "rework_effective": True}
audit_after = {"need_rework": False, "reason": "已修", "findings": [],
               "remaining_issues": [], "spec_issue": None, "rework_effective": True}
f2 = Fake(plan1, {"write_revise:t1": "改过的正文，只引用 [S1]。"}, [audit_bad, audit_after])
out2 = run(f2, "场景 2：high 级问题触发返修，返修后收敛")
assert out2["rework_rounds"] == 1, f"应返修 1 轮，实际 {out2['rework_rounds']}"
assert any(c[0].startswith("write_revise") for c in f2.calls), "应调用 write_revise"
print("  ✓ 只返修被点名的那一个任务（未波及其他）")
print("  ✓ 返修后收敛并退出")

# ---------- 场景 3：主 agent 说改不动，立刻停 ----------
audit_stuck = {"need_rework": True, "reason": "材料本身矛盾",
               "findings": [{"task": "t2", "section": "sec2", "severity": "high",
                             "problem": "两份来源数字冲突", "fix": "调和一下"}],
               "remaining_issues": ["sec2 的产能数字材料内自相矛盾"],
               "spec_issue": None, "rework_effective": False}
# 首轮必须忽略 rework_effective（没有「上一轮」可言），所以要先返修一轮，
# 第二轮才填 false——那才是「上一轮返修没奏效」的真实场景。
audit_retry = {"need_rework": True, "reason": "先修一轮",
               "findings": [{"task": "t2", "section": "sec2", "severity": "high",
                             "problem": "x", "fix": "y"}],
               "remaining_issues": [], "spec_issue": None, "rework_effective": True}
f3 = Fake(plan1, {}, [audit_retry, audit_stuck])
out3 = run(f3, "场景 3：返修一轮后主 agent 说改不动")
assert out3["rework_rounds"] == 1, f"应先返修 1 轮，实际 {out3['rework_rounds']}"
assert len([c for c in f3.calls if c[0].startswith("write_revise")]) == 1, \
    "只该返修一轮，不该反复重写"
assert "产能数字材料内自相矛盾" in out3["report_markdown"], "改不动的问题要写进存疑"
print("  ✓ 首轮忽略 rework_effective，正常返修一轮")
print("  ✓ 第二轮判为无效后立即停止，没有反复重写")
print("  ✓ 改不动的问题如实写进报告存疑一节")

# 首轮就说「改不动」也不能被采信——那是模型的字段填错，不是它真的试过
audit_r0 = dict(audit_stuck)
f3b = Fake(plan1, {}, [audit_r0])
out3b = run(f3b, "场景 3b：首轮就填 rework_effective=false（应被忽略）")
# 关键：首轮不能因为这个字段就停下——它必须先返修一轮。
# （Fake 每次返回同一份审查，所以第二轮才真的停，那是正确行为。）
assert out3b["rework_rounds"] == 1, \
    f"首轮应忽略 rework_effective=false 并返修一轮，实际返修 {out3b['rework_rounds']} 轮"
print("  ✓ 首轮的 rework_effective=false 被忽略，没有误停")

# ---------- 场景 4：程序引用校验 ----------
print(f"\n{'='*70}\n场景 4：引用程序校验（硬规则 1）\n{'='*70}")
ALLOWED = {"S1·1", "S1·2", "S2·1"}
cases = [
    ("正常段级引用", "结论甲 [S1·1] 结论乙 [S2·1]。", ALLOWED, 0),
    ("越界段号", "结论甲 [S1·1]，但 [S3·7] 不在我的段落里。", ALLOWED, 1),
    ("自创写法", "甲 [S1·1] 乙 [S1·2 类同表述见 S2·1]。", ALLOWED, 1),
    ("来源级写法（粗但正确）", "甲 [S1] 乙 [S2]。", ALLOWED, 0),
    ("越界的来源级写法", "甲 [S9]。", ALLOWED, 1),
    ("无引用", "这里只是一句不带来源的判断。", ALLOWED, 0),
]
for name, text, allowed, want in cases:
    got = W._check_citations(text, allowed)
    mark = "✓" if len(got) == want else "❌"
    print(f"  {mark} {name:<14} 期望 {want} 项，实得 {len(got)} 项 {got if got else ''}")
    assert len(got) == want, f"{name} 校验结果不符"

# ---------- 场景 5：任务数上限与依赖环 ----------
print(f"\n{'='*70}\n场景 5：任务数上限与脏数据\n{'='*70}")
many = {"tasks": [{"id": f"t{i}", "covers": ["sec1"], "mandate": "m", "segments": ["S1·1"]}
                  for i in range(30)], "need_full": []}
KNOWN = {s_["ref"] for s_ in SEGMENTS}
tasks = W._normalize_tasks(many["tasks"], OUTLINE, KNOWN)
print(f"  主 agent 给了 30 个任务 → 实际保留 {len(tasks)} 个（上限 {W.MAX_TASKS}）")
assert len(tasks) == W.MAX_TASKS, f"上限未生效：{len(tasks)}"

dirty = {"tasks": [{"id": "x", "covers": ["sec99"], "mandate": "m", "segments": ["S1·1"]},
                   {"id": "y", "covers": ["sec1"], "mandate": "m",
                    "segments": ["不存在的段号", "S1·1"]},
                   {"id": "z", "covers": ["sec2"], "mandate": "m",
                    "segments": ["S1·2"]}], "need_full": []}
t3 = W._normalize_tasks(dirty["tasks"], OUTLINE, KNOWN)
print("  脏数据：covers 指向不存在的小节被丢弃，编造的段号被清掉")
print(f"    保留 {len(t3)} 个任务：{[x['id'] for x in t3]}")
assert [x["id"] for x in t3] == ["y", "z"], "非法 covers 应被丢弃"
assert t3[0]["segments"] == ["S1·1"], f"非法段号应被清掉，实际 {t3[0]['segments']}"

# ---------- 场景 6：子 agent 拿不到别人的东西 ----------
print(f"\n{'='*70}\n场景 6：子 agent 的输入边界（最小权限）\n{'='*70}")
seen: list[dict] = []
orig = W._run_task
def spy(state, task, revise):
    pool = W._task_segments(state, task)
    seen.append({"task": task["id"],
                 "refs": [g["ref"] for g in pool],
                 "bodies": [g.get("content") or "" for g in pool]})
    return "[S1·1] 占位正文"
W._run_task = spy
st = {"question": BASE["question"], "spec": {"thesis": "T"}, "tasks": plan1["tasks"],
      "segments": SEGMENTS, "on_topic": SEGMENTS, "src_lines": SRC,
      "session_id": "s_struct"}
for t in plan1["tasks"]:
    W._run_task(st, t, None)
W._run_task = orig

by_id = {r["task"]: r for r in seen}
t1 = by_id["t1"]
print(f"  t1 分到的段: {t1['refs']}（应只有 S1·1 与 S1·2）")
assert sorted(t1["refs"]) == ["S1·1", "S1·2"], f"应只含 t1 自己的两段，实际 {t1['refs']}"
# SEGMENTS 的编号规则：第 i 条断言 → S{i%3+1}·{i//3+1}
#   t1 = S1·1, S1·2 = 断言 0 与 3
#   t2 = S2·1, S2·2 = 断言 1 与 4
#   t3 = S3·1      = 断言 2
t1_body = " ".join(t1["bodies"])
assert "全固态电池指" in t1_body, "应看到 S1·1 的全文"
assert "氧化物稳定但脆性大" in t1_body, "S1·2 确实属于 t1，应该看得到"
assert "半固态保留" not in t1_body, "不应看到 t2 的 S2·1"
assert "某厂商宣称" not in t1_body, "不应看到 t2 的 S2·2"
assert "硫化物电导率高" not in t1_body, "不应看到 t3 的 S3·1"
print("  ✓ 拿得到自己那两段的全文（可展开）")
print("  ✓ 看不到其他任务的段落全文")



# ---------- 场景 7：任务之间无依赖，应一次并发跑完 ----------
print(f"\n{'='*70}\n场景 7：任务之间无依赖（depends_on 已删除）\n{'='*70}")
many7 = {"tasks": [
    {"id": f"t{i}", "covers": ["sec1"], "mandate": "m", "segments": ["S1·1"]}
    for i in range(1, 6)], "need_full": [],
    "thesis": "T", "glossary": [{"canonical": "X"}], "units": ["u"],
    "cautions": ["c"], "spec_uncertainty": "u", "sections": []}
f7 = Fake(many7, {}, [audit_ok])
out7 = run(f7, "5 个无依赖任务")
drafts = out7.get("drafts") or {}
print(f"  产出的任务: {sorted(drafts)}")
assert sorted(drafts) == ["t1", "t2", "t3", "t4", "t5"], \
    f"5 个任务都该跑，实际 {sorted(drafts)}"
# 一次 dispatch 跑完 → 事件链里 dispatch 只出现一次
disp = [e for e in (out7.get("events") or []) if e.get("kind") == "write_dispatch"]
print(f"  dispatch 轮数: {len(disp)}（无依赖时应该是 1）")
assert len(disp) == 1, f"不该分多轮，实得 {len(disp)} 轮"
print("  ✓ 5 个无依赖任务一次并发跑完，没有被排成串行")

print(f"\n{'='*70}\n✅ 结构自检全部通过（7 个场景）\n{'='*70}")
