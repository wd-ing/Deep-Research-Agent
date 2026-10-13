"""write 子图的结构自检：不调 LLM，用假响应走完整条链路。

按约定 1，write 板块做完之前测试只打写作环节。真实数据联调放到下一轮，
这里先把**结构**验干净：路由、装配顺序、引用程序校验、返修收敛、
降级标注、任务上限、多节归位、多目标返修、装配自检。结构不对就别花钱跑真实数据。

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

# write_trace.session_id 有指向 session 的外键，而这里用的是 s_test 这个
# 并不存在的会话。撞外键会让每次「子 agent 用了哪些工具」的记录都失败。
# 给它一份临时库并把会话建出来——顺便让工具记录这条路径真的被跑到，
# 也别去污染 data/research.db。
_TMP_DB = Path("/tmp/ra_write_struct.db")
os.environ["RA_DB_PATH"] = str(_TMP_DB)
import research_agent.db.repo as _repo_mod  # noqa: E402
_r = _repo_mod.get_repo()
if not _r.q1("SELECT id FROM session WHERE id=?", ("s_test",)):
    _r.x("INSERT INTO session (id,title,question,status,created_at,updated_at) "
         "VALUES (?,?,?,?,?,?)",
         ("s_test", "结构自检", "固态电池", "done", "2026-01-01T00:00:00+00:00",
          "2026-01-01T00:00:00+00:00"))

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
        "src_lines": SRC}



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
        # 任务头在第一条 user 消息里。工具轮之后 messages[-1] 已经是
        # tool_result，从那儿取只会拿到空的 covers。
        head = messages[0].get("content", "")
        tid = _task_id_from(head)
        covers = _covers_from(head)
        text = self.sections.get(f"{stage}:{tid}", "")
        if not text:
            text = _body_for(tid, covers)
        return {"blocks": [{"type": "text", "text": text}], "usage": {}}

    def complete(self, stage: str, prompt: str, system: str | None = None,
                 json_mode: bool = False) -> str:
        self.calls.append((stage, len(prompt)))
        if stage in ("write_section", "write_revise"):
            tid = _task_id_from(prompt)
            text = self.sections.get(f"{stage}:{tid}", "")
            return text or _body_for(tid, _covers_from(prompt))
        return "{}"


def _body_for(tid: str, covers: list[str]) -> str:
    """多节任务必须逐节标记，否则装配取不到各自那段。"""
    if not covers:
        return f"[S1·1] 任务 {tid} 的正文。"
    return "".join(f"<!-- section:{c} -->\n[S1·1] 任务 {tid} 的 {c} 正文。\n"
                   for c in covers)


def _flat(content) -> str:
    """user 消息的 content 可能是 str，也可能是内容块列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text") or "" for b in content
                         if isinstance(b, dict))
    return ""


def _task_id_from(prompt) -> str:
    for line in _flat(prompt).splitlines():
        if line.startswith("id="):
            return line[3:].split()[0]
    return "?"


def _covers_from(prompt) -> list[str]:
    """从「对应小节=['sec1', 'sec2']」解出小节 id 列表。"""
    m = re.search(r"对应小节=\[([^\]]*)\]", _flat(prompt))
    if not m:
        return []
    return [x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip()]


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

# ---------- 场景 3：逐条核对显示上轮问题仍在，立刻停 ----------
# 2026-10-08：`rework_effective` 不再由模型自填，改由 `rework_check` 逐条核对推出。
# 所以「改不动」在 mock 里要表达成「上轮那条 still_there」，而不是自填 false。
audit_stuck = {"need_rework": True, "reason": "材料本身矛盾",
               "findings": [{"task": "t2", "section": "sec2", "severity": "high",
                             "problem": "两份来源数字冲突", "fix": "调和一下"}],
               "blocked": [{"task": "t2", "section": "sec2",
                            "kind": "no_material",
                            "issue": "sec2 的产能数字材料内自相矛盾，返修改不了"}],
               "rework_check": [{"task": "t2", "section": "sec2",
                                 "status": "still_there"}],
               "spec_issue": None}
# 首轮不需要 rework_check（没有「上一轮」可言），所以要先返修一轮，
# 第二轮才出现 still_there——那才是「上一轮返修没解决」的真实场景。
audit_retry = {"need_rework": True, "reason": "先修一轮",
               "findings": [{"task": "t2", "section": "sec2", "severity": "high",
                             "problem": "x", "fix": "y"}],
               "blocked": [], "spec_issue": None}
f3 = Fake(plan1, {}, [audit_retry, audit_stuck])
out3 = run(f3, "场景 3：返修一轮后逐条核对显示仍在")
assert out3["rework_rounds"] == 1, f"应先返修 1 轮，实际 {out3['rework_rounds']}"
# 数返修**轮次**而不是调用次数：一次返修会走两轮工具循环
# （先要证据、再产出正文），调用次数不等于轮次。
rev_rounds = [e for e in (out3.get("events") or []) if e.get("kind") == "write_revise"]
assert out3["rework_rounds"] == 1 and len(rev_rounds) == 1, \
    f"只该返修一轮，实际 {len(rev_rounds)} 轮"
assert "产能数字材料内自相矛盾" in out3["report_markdown"], "改不动的问题要写进存疑"
print("  ✓ 首轮不需要逐条核对，正常返修一轮")
print("  ✓ 逐条核对显示仍在后立即停止，没有反复重写")
print("  ✓ blocked 内容如实写进报告存疑一节")

# 首轮就报 still_there 也不能被采信——那时根本没有「上一轮」
audit_r0 = dict(audit_stuck)
f3b = Fake(plan1, {}, [audit_r0])
out3b = run(f3b, "场景 3b：首轮就报 still_there（应被忽略）")
# 关键：首轮不能因为这个就停下——它必须先返修一轮。
assert out3b["rework_rounds"] == 1, \
    f"首轮应忽略 still_there 并返修一轮，实际返修 {out3b['rework_rounds']} 轮"
print("  ✓ 首轮的 still_there 被忽略，没有误停")

# 模型没给逐条核对结果时，不能当成「返修有效」——旧实现采信了瞎猜的 false
audit_nocheck = {"need_rework": True, "reason": "不核对",
                 "findings": [{"task": "t2", "section": "sec2", "severity": "high",
                               "problem": "x", "fix": "y"}],
                 "blocked": [], "rework_check": [], "spec_issue": None}
f3c = Fake(plan1, {}, [audit_retry, audit_nocheck])
out3c = run(f3c, "场景 3c：第二轮不给核对结果")
assert out3c["rework_rounds"] == 1, \
    f"没给核对结果应判为无效并停手，实际返修 {out3c['rework_rounds']} 轮"
print("  ✓ 没给逐条核对结果时按「无效」处理，不假装改好了")

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
    return ({"": "[S1·1] 占位正文"}, None)
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


# ---------- 场景 8：一个任务写两节，各归各位（R6 核心） ----------
print(f"\n{'='*70}\n场景 8：一个任务写两节（分节输出与归位）\n{'='*70}")
plan8 = {"tasks": [
    {"id": "t1", "covers": ["sec1"], "mandate": "定义", "segments": ["S1·1"]},
    {"id": "t8", "covers": ["sec2", "sec3"], "mandate": "两节紧密耦合",
     "segments": ["S1·2", "S2·1"]},
],
    "thesis": "T", "glossary": [{"canonical": "X"}], "units": ["u"],
    "cautions": ["c"], "spec_uncertainty": "u", "sections": []}
f8 = Fake(plan8, {}, [audit_ok])
out8 = run(f8, "一个任务负责 sec2 + sec3")

d8 = out8.get("drafts") or {}
secs8 = d8.get("t8") or {}
print(f"  t8 的产出小节: {sorted(secs8)}")
assert sorted(secs8) == ["sec2", "sec3"], \
    f"多节任务应逐节产出，实际 {sorted(secs8)}"
assert secs8["sec2"] != secs8["sec3"], "两节正文不能相同"
md8 = out8["report_markdown"]
assert md8.count("sec2 正文") == 1 and md8.count("sec3 正文") == 1, \
    "每节的内容只应出现一次"
assert "<!-- section:" not in md8, "分节标记不能留在最终报告里"
# 装配自检不该报警
w8 = [e for e in (out8.get("events") or []) if e.get("kind") == "write_assemble"]
assert not (w8 and w8[0].get("warnings")), \
    f"装配不该报警，实际 {w8[0] if w8 else None}"
print("  ✓ 两节各自归位，内容不重复")
print("  ✓ 分节标记已在装配时消费掉")

# 切片不按标记 → 如实报错，不把整份复制给两节
secs_cut, note_cut = W._split_sections("[S1·1] 只有一段文字", ["sec2", "sec3"])
assert secs_cut == {}, f"切不出分节就不能给内容，实际 {secs_cut}"
print(f"  ✓ 缺分节标记时如实报错：{note_cut[:40]}…")

# 部分缺节：给到的那节要留下，缺的节由装配标成「未能完成」
secs_part, _ = W._split_sections(
    "<!-- section:sec2 -->\n[S1·1] 甲\n<!-- section:sec9 -->\n[S1·1] 乙",
    ["sec2", "sec3"])
assert list(secs_part) == ["sec2"], f"只该留下 sec2，实际 {secs_part}"
print("  ✓ 只标了部分小节时，多余的按未知小节丢弃")

# ---------- 场景 9：同一任务被点名多节，一次问完（R6 核心） ----------
print(f"\n{'='*70}\n场景 9：返修时同一任务的多节问题一起问\n{'='*70}")
audit_two = {"need_rework": True, "reason": "t8 两节都要改",
             "findings": [
                 {"task": "t8", "section": "sec2", "severity": "high",
                  "problem": "sec2 越界", "fix": "sec2 改成只引 S1·2"},
                 {"task": "t8", "section": "sec3", "severity": "high",
                  "problem": "sec3 放错了内容", "fix": "sec3 换成企业格局"}],
             "remaining_issues": [], "spec_issue": None, "rework_effective": True}
f9 = Fake(plan8, {"write_revise:t8":
                  "<!-- section:sec2 -->\n[S1·2] 修好的 sec2。\n"
                  "<!-- section:sec3 -->\n[S1·2] 修好的 sec3。"},
          [audit_two, audit_ok])
out9 = run(f9, "t8 的 sec2 与 sec3 同时被点名")
secs9 = (out9.get("drafts") or {}).get("t8") or {}
print(f"  返修后 t8 的小节: {sorted(secs9)}")
assert sorted(secs9) == ["sec2", "sec3"], "两节都必须在"
assert "修好的 sec2" in secs9.get("sec2", ""), "sec2 的修改被覆盖了"
assert "修好的 sec3" in secs9.get("sec3", ""), "sec3 的修改丢了"
# 一次调用问完两节，不是分两次
rev9 = [e for e in (out9.get("events") or []) if e.get("kind") == "write_revise"]
assert len(rev9) == 1, f"应只有一轮返修，实际 {len(rev9)}"
assert rev9[0].get("tasks") == ["t8"], f"应只返修 t8，实际 {rev9[0].get('tasks')}"
print("  ✓ 两节的修改都保住了，没有互相覆盖")
print("  ✓ 同一任务一次问完，不是拆成两次")

# 只点名一节时，另一节必须原样保留
merged = W._merge_sections(
    {"sec2": "旧 sec2", "sec3": "旧 sec3"},
    {"sec2": "新 sec2"}, [{"section": "sec2"}])
assert merged == {"sec2": "新 sec2", "sec3": "旧 sec3"}, f"未点名的小节被冲掉了：{merged}"
# 返修写崩时整体保留原稿
kept = W._merge_sections({"sec2": "旧 sec2"}, {}, [{"section": "sec2"}])
assert kept == {"sec2": "旧 sec2"}, "空产出不该覆盖原稿"
print("  ✓ 未被点名的小节原样保留")
print("  ✓ 返修写崩时保留原稿，不拿空串覆盖")

# ---------- 场景 10：装配自检能抓到 R5 那种事故 ----------
print(f"\n{'='*70}\n场景 10：装配自检（重复正文 / 放错小节）\n{'='*70}")
dup = W._assemble_warnings(
    {"sec2": "<!-- task:t8 section:sec2 -->\n同一份正文",
     "sec3": "<!-- task:t8 section:sec3 -->\n同一份正文"},
    {"t8": {"sec2": "同一份正文", "sec3": "同一份正文"}},
    {"t8": {"covers": ["sec2", "sec3"]}})
print(f"  命中 {len(dup)} 条：{dup}")
assert any("逐字相同" in d for d in dup), f"没抓到重复正文：{dup}"
gap = W._assemble_warnings(
    {}, {"t8": {"sec2": "甲"}},
    {"t8": {"covers": ["sec2", "sec3"]}})
assert any("缺 sec3" in d for d in gap), f"没抓到多节任务缺节：{gap}"
print("  ✓ 两节正文相同 → 报警")
print("  ✓ 多节任务只产出一节 → 报警")
print("  ✓ 报警会一路写进报告的存疑一节")

# ---------- 场景 11：审查看得到证据量，首轮不记 rework_effective ----------
print(f"\n{'='*70}\n场景 11：审查的输入与留痕\n{'='*70}")


class SpyFake(Fake):
    """记下每次 complete_json 收到的提示词。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.prompts: list[str] = []

    def complete_json(self, stage, prompt, system=None):
        self.prompts.append(prompt)
        return super().complete_json(stage, prompt, system)


fake0 = SpyFake(plan1, {}, [audit_ok, audit_bad, audit_after])
# 留痕是按轮次去重追加的，前面场景已经写过 0/1 轮了，先清干净
_repo_mod.get_repo().x("DELETE FROM write_trace WHERE session_id=?", ("s_test",))
run(fake0, "审查拿到了任务分派摘要")
audits = [p for p in fake0.prompts if '"草稿"' in p]
assert audits, "审查提示没抓到"
print(f"  审查提示里含任务分派摘要: {any('证据段数' in p for p in audits)}")
assert any("证据段数" in p for p in audits), \
    "审查看不到每个任务分了几段证据，判不了「写砸了」还是「没材料可写」"

hist = _repo_mod.get_repo().write_trace("s_test").get("audit_history") or []
print(f"  audit_history 轮数: {len(hist)}（R5 那次只留得下 1 轮）")
assert hist, "审查留痕没落库"
assert hist[0]["rework_effective"] is None, \
    f"首轮该记 null 而不是 {hist[0]['rework_effective']!r}——那会让看历史的人误以为返修失败过"
print(f"  第 0 轮 rework_effective 记为 {hist[0]['rework_effective']}（不是模型随手填的值）")
print(f"  第 0 轮 targets: {hist[0]['targets'] or '（无）'}")
for h in hist[1:]:
    print(f"  第 {h['round']} 轮 rework_effective: {h['rework_effective']}（这一轮才有意义）")

# ---------- 场景 12：子 agent 自造的 `##` 由装配降级 ----------
print(f"\n{'='*70}\n场景 12：自造标题的降级（提示词管不住，用程序）\n{'='*70}")
RAW = ("## 子 agent 自造的标题\n正文一。\n\n"
       "### 模型自己写的三级标题\n正文二。\n\n"
       "#### 更深的四级\n正文三。\n"
       "## 另一个自造标题\n正文四。\n"
       "这不是标题行：## 出现在句子里 https://x.com/a##b\n")
got = W._demote_headings(RAW)
lines = got.splitlines()
print("  降级后：")
for line in lines:
    if line.startswith("#") or line.startswith("这不是"):
        print(f"    {line[:56]}")


def heads(text: str, n: int) -> list[str]:
    """取正好 n 级井号的行首标题。"""
    pat = re.compile(r"^" + "#" * n + r"(?!#)\s+(.*)$")
    return [m.group(1) for m in map(pat.match, text.splitlines()) if m]


assert heads(got, 2) == [], f"`##` 没被降级：{heads(got, 2)}"
assert heads(got, 3) == ["子 agent 自造的标题", "模型自己写的三级标题", "另一个自造标题"], \
    heads(got, 3)
assert heads(got, 4) == ["更深的四级"], f"更深的层级不该被拉平：{heads(got, 4)}"
assert "这不是标题行：## 出现在句子里 https://x.com/a##b" in lines, "非行首的 `##` 不该动"
print("  ✓ `##` 降成 `###`")
print("  ✓ 本来就是 `###` / `####` 的原样保留，没被拉平")
print("  ✓ 句子里出现的 `##` 不动（只改行首）")
print(f"  ✓ 上报计数：原文 {W._count_demoted(RAW)} 个待降级（`###` 不计）")
assert W._count_demoted(RAW) == 2, W._count_demoted(RAW)

# 端到端看一眼：装配后报告里不该再有子 agent 的 `##`
plan12 = {"tasks": [
    {"id": "t1", "covers": ["sec1"], "mandate": "定义", "segments": ["S1·1"]},
],
    "thesis": "T", "glossary": [{"canonical": "X"}], "units": ["u"],
    "cautions": ["c"], "spec_uncertainty": "u", "sections": []}
f12 = Fake(plan12, {"write_section:t1": "## 子 agent 又写了标题\n[S1·1] 正文。\n"}, [audit_ok])
out12 = run(f12, "装配把自造标题降级")
md12 = out12["report_markdown"]
head12 = [m.group().strip()[3:] for m in re.finditer(r"^## (?!#).*$", md12, re.M)]
print(f"  报告二级标题 {len(head12)} 个：{head12}")
outline_titles = [s["title"] for s in OUTLINE]
assert set(head12) == set(outline_titles) | {"存疑与未完成"}, \
    f"二级标题只该有大纲小节 + 存疑，实际 {head12}"
assert "子 agent 又写了标题" not in head12, "子 agent 的 `##` 没被降下去"
# 降级之后标题会被统一重编号（`### 1.1 xxx`），所以这里只查文字还在，
# 不能整串比对——编号是装配层加的，见 `_renumber_headings`。
assert any("子 agent 又写了标题" in h for h in heads(md12, 3)), \
    f"降级后的标题应该还在，实际三级标题 {heads(md12, 3)}"
ev12 = [e for e in out12["events"] if e.get("kind") == "write_assemble"]
print(f"  装配事件记录降级数：{ev12[0].get('headings_demoted')}")
assert ev12[0].get("headings_demoted") == 1
print("  ✓ 降级数量进了事件，改了什么看得见")

# ---------- 场景 12b：编号统一 + 引言标记（2026-10-11） ----------
print(f"\n{'='*70}\n场景 12b：标题编号重算与引言标记\n{'='*70}")
RN = W._renumber_stats
cases = [
    ("四种自造编号全被剥", 1,
     "### 一、甲\n### 4. 乙\n### 1.1 丙\n### sec3 丁\n正文。",
     ["1.1 甲", "1.2 乙", "1.3 丙", "1.4 丁"]),
    ("中文数字不跟空格也剥", 2, "### 一、甲\n正文。", ["2.1 甲"]),
    ("3D打印 / 500强 不被误剥", 3,
     "### 3D打印进展\n### 500强企业排名\n正文。", ["3.1 3D打印进展", "3.2 500强企业排名"]),
    ("深层连续编号", 4, "### 甲\n#### 甲一\n##### 甲一之一\n#### 甲二\n### 乙",
     ["4.1 甲", "4.1.1 甲一", "4.1.1.1 甲一之一", "4.1.2 甲二", "4.2 乙"]),
]
for name, no, src, want in cases:
    RN.update({"stripped": 0, "leads": 0})
    got = W._renumber_headings(src, no)
    hs = [h.split(" ", 1)[1] for h in heads(got, 3)] if name.startswith("3D") else None
    for w in want:
        assert w in got, f"{name}：期望有 `{w}`，实际 {got!r}"
    print(f"  ✓ {name}")

RN.update({"stripped": 0, "leads": 0})
W._renumber_headings("### 一、甲\n正文。", 1)
assert RN["stripped"] == 1, f"剥掉几个编号应该上报，实际 {RN['stripped']}"
print(f"  ✓ 剥编号的数量进统计：{RN['stripped']}")

for name, src in [("独占一行", "<!-- lead -->\n### 甲\n正文。"),
                  ("标记+文本", "<!-- lead --> 甲\n正文。"),
                  ("标记在###后", "### <!-- lead --> 甲\n正文。"),
                  ("标记+###前缀", "<!-- lead --> ### 甲\n正文。")]:
    RN.update({"stripped": 0, "leads": 0})
    got = W._renumber_headings(src, 1)
    assert "<!-- lead -->" not in got, f"{name}：标记没被消掉，{got!r}"
    assert "###" not in got, f"{name}：引言不该留标题符号，{got!r}"
    assert "甲" in got, f"{name}：文字被丢了，{got!r}"
    assert RN["leads"] == 1, f"{name}：leads 应为 1，实际 {RN['leads']}"
    print(f"  ✓ 引言标记·{name}")

got = W._renumber_headings("```\n### 代码块里不动\n```\n### 真标题", 1)
assert "代码块里不动" in got and "### 1.1 真标题" in got, got
print("  ✓ 代码块里的 # 不当标题处理")
assert W._renumber_headings("直接正文。", 1) == "直接正文。"
assert W._renumber_headings("", 1) == ""
print("  ✓ 无标题 / 空正文不受影响")

# ---------- 场景 13：规划失败时必须说人话 ----------
print(f"\n{'='*70}\n场景 13：规划失败的降级横幅\n{'='*70}")
PLAN_ERR = ("MiniMax 500: {\"type\":\"error\",\"error\":{\"type\":\"api_error\","
            "\"message\":\"output new_sensitive (1027)\"}}")
out13 = W.node_final({
    "question": "固态电池的产业化进展", "spec": {}, "assembled": "",
    "audit": {}, "violations": {}, "failed": [],
    "uncovered_tasks": [], "assemble_warnings": [],
    "plan_failed": PLAN_ERR, "src_lines": ["[S1] 某来源 | a.com"],
})
md13 = out13["report_markdown"]
banner_at = md13.find("本次研究没有产出正文")
thesis_at = md13.find("本文主张")
print(f"  横幅位置 {banner_at}，主张位置 {thesis_at}")
assert banner_at != -1, "规划失败却没有横幅"
assert banner_at < md13.find("## 存疑与未完成"), "横幅必须在存疑一节之前"
assert "没有产出正文" in md13 and PLAN_ERR[:30] in md13, \
    "横幅里必须写明失败原因，不能只说「失败了」"
assert "重新运行" in md13, "横幅要给出下一步"
print("  ✓ 标题下面立刻是失败横幅，不是空白")
print("  ✓ 横幅里有原始错误原因")
print("  ✓ 横幅在存疑一节之前（末尾才提就等于没提）")

# 正常路径不能被这条横幅污染
out_ok = W.node_final({
    "question": "Q", "spec": {"thesis": "T"}, "assembled": "## A\n正文",
    "audit": {}, "violations": {}, "failed": [],
    "uncovered_tasks": [], "assemble_warnings": [], "src_lines": []})
assert "没有产出正文" not in out_ok["report_markdown"], "正常报告不该有失败横幅"
assert "本文主张" in out_ok["report_markdown"]
print("  ✓ 正常路径不受影响")

print(f"\n{'='*70}\n✅ 结构自检全部通过（13 个场景）\n{'='*70}")
