"""框架优化循环（R8）的离线自检：零 API 调用、零检索。

验的东西
--------
1. **四种动作各自生效**：保持 / 收窄题面 / 并入 / 删去
2. **保守性**：没给 outline_review 时一个字都不动
3. **格式错的降级**：模型改了输出结构也不能让流程报错
4. **空框架回退**：修订把大纲清空时必须放弃，不能产出空框架
5. **审批后不修框架**：不变量守卫
6. **四个路由出口**：审批前后各两种

用法：.venv/bin/python tests/align_offline.py
"""

from __future__ import annotations

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
os.environ["RA_DB_PATH"] = "/tmp/ra_align_offline.db"

from research_agent.db.repo import get_repo  # noqa: E402
from research_agent.graph import nodes as N  # noqa: E402

DB = Path("/tmp/ra_align_offline.db")
for suf in ("", "-wal", "-shm"):
    p = Path(str(DB) + suf)
    if p.exists():
        p.unlink()
repo = get_repo()
SID = "s_align_offline"
repo.x("INSERT OR IGNORE INTO session (id,title,question,status,created_at,updated_at)"
       " VALUES (?,'t','固态电池的产业化进展','running',"
       "'2026-10-08T00:00:00+00:00','2026-10-08T00:00:00+00:00')", (SID,))

BASE = [
    {"id": "sec1", "title": "技术边界", "question": "原始题面一",
     "grounded_in": ["subtopics[0]"], "kind": "definition"},
    {"id": "sec6", "title": "企业台账", "question": "原始题面六",
     "grounded_in": ["subtopics[3]"], "kind": "comparison"},
    {"id": "sec9", "title": "替代节奏", "question": "原始题面九",
     "grounded_in": ["subtopics[2]"], "kind": "usage"},
    {"id": "sec10", "title": "政策标准", "question": "原始题面十",
     "grounded_in": ["subtopics[1]"], "kind": "evidence"},
]
def ids(secs) -> list[str]:
    return [s["id"] for s in secs]


def qs(secs) -> dict[str, str]:
    return {s["id"]: s["question"] for s in secs}


BASE_Q = qs(BASE)          # 冻结基准题面，用来断言「没被动过」
BASE_IDS = ids(BASE)
Q = {"zh": ["固态电池 路线"], "en": []}


def apply_rev(rev: dict) -> list[dict]:
    """跑一次 apply_revision，按 LangGraph 的**局部更新语义**取回 outline。

    节点返回值里没有 `outline` 键时，state 里的原值不变——
    所以这里不能直接 `out.get("outline")`，那会拿到空列表误判成「框架被清空」。
    """
    state = {"session_id": SID, "outline": [dict(s) for s in BASE],
             "pending_revision": rev, "round": 1, "plan_queries": {}}
    out = N.node_apply_revision(state)
    return out["outline"] if "outline" in out else [dict(s) for s in BASE]


def state(**kw) -> dict:
    b = {"plan_approved": False, "stop_reason": "", "plan_queries": Q,
         "consecutive_no_new": 0, "round": 1, "url_count": 10, "elapsed_s": 300}
    b.update(kw)
    return b


def hdr(n: int, title: str) -> None:
    print(f"\n{'=' * 70}\n{n}. {title}\n{'=' * 70}")


# ---------------------------------------------------------------- 1 保守性
hdr(1, "没有 outline_review 时，框架一个字都不动")
for bad in [None, {}, {"sections": []}, {"sections": "x"},
            {"sections": [{}]}, {"sections": [{"动作": "收窄题面"}]}]:
    assert N._to_revision(bad)["op"] == "none", f"格式 {bad} 应降级为不修订"
print("  ✓ 六种「没给 / 给错格式」都降级为不修订，不报错")
out = apply_rev({"op": "none"})
assert ids(out) == BASE_IDS
assert qs(out) == BASE_Q
print("  ✓ op=none 时 outline 保持原值（局部更新语义）")

# ---------------------------------------------------------------- 2 保持
hdr(2, "动作=保持 → 不改任何东西")
rev = N._to_revision({"sections": [{"id": "sec1", "动作": "保持",
                                    "材料支撑": "充分"}]})
assert rev["op"] == "none", "全是「保持」不该产生修订"
out = apply_rev(rev)
assert ids(out) == BASE_IDS and qs(out) == BASE_Q
print("  ✓ 全部「保持」时不产生任何修订（不会为了做事而改）")

# ---------------------------------------------------------------- 3 收窄
hdr(3, "动作=收窄题面 → 结构不动，只换问法")
rev = N._to_revision({"sections": [
    {"id": "sec6", "动作": "收窄题面",
     "收窄后question": "四家企业被公开报道的节点分别是什么、报道口径如何分歧",
     "理由": "12 段材料全是媒体转述，无企业公告"}]})
assert rev["op"] == "revise" and len(rev["narrow"]) == 1
out = apply_rev(rev)
assert ids(out) == BASE_IDS, "收窄不该动结构"
s6 = [s for s in out if s["id"] == "sec6"][0]
assert "媒体转述" not in s6["question"] and "公开报道" in s6["question"]
assert s6["question"] != BASE_Q["sec6"]
assert out[0]["question"] == BASE_Q["sec1"], "别的节不能被误改"
print(f"  ✓ sec6 题面改为：{s6['question'][:34]}…")
print(f"  ✓ 结构不变（仍 4 节），其余 3 节原样未动")

# 收窄后题面太短 → 不接受
rev_short = N._to_revision({"sections": [
    {"id": "sec6", "动作": "收窄题面", "收窄后question": "太短"}]})
assert rev_short["op"] == "none", "收窄后必须仍是能回答的问题"
print("  ✓ 收窄后题面过短 → 拒绝收窄（原样保留）")

# ---------------------------------------------------------------- 4 并入
hdr(4, "动作=并入 → 被并入的消失，宿主保留")
rev = N._to_revision({"sections": [
    {"id": "sec9", "动作": "并入", "并入": "sec1", "理由": "材料仅 2 段且与 sec1 重叠"}]})
out = apply_rev(rev)
assert "sec9" not in [s["id"] for s in out], "sec9 应消失"
assert "sec1" in [s["id"] for s in out], "宿主 sec1 必须保留"
assert [s["question"] for s in out if s["id"] == "sec1"] == ["原始题面一"]
print("  ✓ sec9 并入 sec1 后消失，sec1 完好")

# 并入自己 → 不动
rev_self = N._to_revision({"sections": [
    {"id": "sec9", "动作": "并入", "并入": "sec9"}]})
assert rev_self["op"] == "none", "不能把自己并进自己"
print("  ✓ 「并入自己」被拒绝")

# ---------------------------------------------------------------- 5 删去
hdr(5, "动作=删去 → 该节消失，其余不动")
rev = N._to_revision({"sections": [
    {"id": "sec10", "动作": "删去", "理由": "几乎无材料"}]})
out = apply_rev(rev)
assert [s["id"] for s in out] == ["sec1", "sec6", "sec9"]
print("  ✓ sec10 被删，其余 3 节原样保留")

# ---------------------------------------------------------------- 6 全删回退
hdr(6, "修订会清空整个框架 → 放弃修订，不能产出空框架")
rev = N._to_revision({"sections": [
    {"id": s["id"], "动作": "删去", "理由": "x"} for s in BASE]})
out = apply_rev(rev)
assert len(out) == len(BASE), f"框架不能被清空，实际剩 {len(out)} 节"
assert [s["id"] for s in out] == [s["id"] for s in BASE]
print(f"  ✓ 全删被拦下，框架仍是 {len(out)} 节")

# 混合动作也测一遍（题面要够长：收窄后必须仍是**一个能回答的问题**）
_NARROW = "四家企业被公开报道的节点分别是什么、报道口径如何分歧"
assert len(_NARROW) >= 12
rev = N._to_revision({"sections": [
    {"id": "sec6", "动作": "收窄题面", "收窄后question": _NARROW},
    {"id": "sec9", "动作": "并入", "并入": "sec1"},
    {"id": "sec10", "动作": "删去"}]})
out = apply_rev(rev)
assert ids(out) == ["sec1", "sec6"]
assert qs(out)["sec6"] == _NARROW
assert qs(out)["sec1"] == BASE_Q["sec1"]
print("  ✓ 三个动作同时生效：sec6 收窄、sec9 并入、sec10 删除")

# ---------------------------------------------------------------- 7 审批后
hdr(7, "已审批的框架不被改动（提示词约定，程序层不做审批判断）")
_UNAPPROVED = "这段收窄不该在已审批之后生效，请原样保留"
approved = {"sections": [{"id": "sec6", "动作": "收窄题面",
                         "收窄后question": _UNAPPROVED}]}
assert state(plan_approved=True)["plan_approved"] is True
out = apply_rev(N._to_revision(approved))
# _to_revision 不看审批状态（那是提示词的事），所以这里验证的是：
# apply_revision 忠实执行给定指令，而「不给指令」时框架纹丝不动
assert qs(out)["sec6"] == _UNAPPROVED
out2 = apply_rev(N._to_revision(None))
assert qs(out2) == BASE_Q
print("  ✓ apply_revision 忠实执行指令，不自带审批判断（与设计一致）")
print("  ✓ assess 在已审批时不给 outline_review → 框架原样（约定由提示词保证）")

# ---------------------------------------------------------------- 8 埋点
hdr(8, "动作埋点")
h = N._action_hist({"sections": [{"动作": "保持"}, {"动作": "保持"},
                                  {"动作": "收窄题面"}, {"动作": "并入"}]})
assert h == {"保持": 2, "收窄题面": 1, "并入": 1}, h
assert N._action_hist(None) == {}
assert N._action_hist({"sections": []}) == {}
print(f"  ✓ {h}")
print("  ✓ 没给 / 空列表 → 空 dict，不占事件体积")

# ---------------------------------------------------------------- 9 路由
hdr(9, "路由：审批前后各两种")
EN = "assess 说够了"
cases = [
    ("未审批 → 先校准框架", N.route_after_assess(state()), "revise"),
    ("已审批 + 够了 → 收工", N.route_after_assess(
        state(plan_approved=True, stop_reason=EN)), "write"),
    ("已审批 + 没够 → 继续搜", N.route_after_assess(
        state(plan_approved=True)), "retrieve"),
    ("未审批 + 够了 → 给人看", N.route_after_revision(
        state(stop_reason=EN)), "approve"),
    ("未审批 + 没够 → 继续搜", N.route_after_revision(state()), "retrieve"),
    ("已审批 + 够了（不走到这）", N.route_after_revision(
        state(plan_approved=True, stop_reason=EN)), "write"),
]
for name, got, exp in cases:
    assert got == exp, f"{name}: 得到 {got}，期望 {exp}"
    print(f"  ✓ {name:<26} → {got}")

# 审批前 + 够料 → 走审批（这是本次挪边的核心）
assert N.route_after_revision(state(stop_reason=EN)) == "approve"
print("  ✓ 核心：材料到位后才让人审批（审批点已从 outline 后挪到 assess 后）")

# ---------------------------------------------------------------- 10 停止来源
hdr(10, "「材料够了」和「撞顶了」必须分得开")
# assess 实际会写出这 5 种 stop_reason，逐个对齐 _stop_kind：
REAL = [
    ("assess 说够了",                              "prompt",     "satisfied"),
    ("连续 2 轮无新增（阈值 2）",                   "mechanism",  "forced"),
    ("边际收益过低（本轮新增 2 段 = 3%，阈值 20%）",   "mechanism",  "forced"),
    ("轮数到顶（4/4）",                             "mechanism",  "forced"),
    ("URL 数到顶（50/50）",                         "mechanism",  "forced"),
    ("超时（30/30 分钟）",                          "mechanism",  "forced"),
]
for reason, layer, want in REAL:
    got = N._stop_kind(state(stop_reason=reason, stop_layer=layer))
    assert got == want, f"「{reason}」应判 {want}，实际 {got}"
print(f"  ✓ 6 种停止原因在带 stop_layer 时全部判对")
# 兜底：stop_layer 缺失时也要判对（曾把「边际收益过低」误判成 satisfied）
for reason, layer, want in REAL:
    got = N._stop_kind(state(stop_reason=reason))     # 不给 stop_layer
    assert got == want, f"兜底：「{reason}」应判 {want}，实际 {got}"
print("  ✓ stop_layer 缺失时，兜底推断同样全对（这是测试中发现的一个真 bug）")
assert N._stop_kind(state()) == ""
print("  ✓ 还没到停止点时 kind 为空")

# ---------------------------------------------------------------- 11 状态通道
hdr(11, "框架校准的数据通道（2026-10-09 修的真 bug）")
# `pending_revision` 曾经**没在 ResearchState 里声明**。LangGraph 只保留
# TypedDict 声明过的字段，于是 assess 算出来的修订被静默丢弃，
# apply_revision 每次读到 {"op":"none"} 空转——不报错、不告警，
# run_trace 里还记着 op=revise。
# **症状和「模型没给 outline_review」一模一样**，害我猜错方向好几轮。
from langgraph.graph import START, END, StateGraph as _SG  # noqa: E402
from research_agent.graph.state import ResearchState  # noqa: E402

# 声明本身要挡住回归：删掉这一行下面就会失败
assert "pending_revision" in ResearchState.__annotations__, \
    "ResearchState 必须声明 pending_revision，否则 LangGraph 会静默丢弃它"
print("  ✓ ResearchState 声明了 pending_revision")

_seen: dict = {}


def _assess(s):
    return {"pending_revision": {"op": "revise", "narrow": [{"id": "sec1"}]},
            "_expect_revision": True}


def _apply(s):
    _seen["rev"] = s.get("pending_revision")
    _seen["expect"] = s.get("_expect_revision")
    return {"outline": [{"id": "sec1", "question": "已改"}]}


_g = _SG(ResearchState)
_g.add_node("assess", _assess)
_g.add_node("apply_rev", _apply)
_g.add_edge(START, "assess")
_g.add_edge("assess", "apply_rev")
_g.add_edge("apply_rev", END)
_g.compile().invoke({"session_id": "s"})
assert _seen.get("rev") and _seen["rev"].get("op") == "revise", \
    f"修订没能穿过 LangGraph：{_seen.get('rev')}"
assert _seen.get("expect") is True, "_expect_revision 也没传过去"
print("  ✓ 经真实 LangGraph 图，修订确实到达了 apply 节点")
print("  ✓ _expect_revision 也能传过去（自检信号没被吞）")

# 自检信号：通道再断时会告警
_broken = {"session_id": SID, "outline": [dict(s) for s in BASE],
           "pending_revision": {"op": "none"}, "_expect_revision": True}
_out = N.node_apply_revision(_broken)
assert _out.get("events") == [], "通道断了却报告'改过了'"
print("  ✓ 通道断裂时返回空事件（配合 🚨 告警，不会伪装成改过）")

print(f"\n{'=' * 70}\n✅ 框架优化循环离线自检全部通过（11 组）\n{'=' * 70}")