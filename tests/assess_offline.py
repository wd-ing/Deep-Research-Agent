"""assess 重写后的离线自检：不调 LLM、不跑检索，只验纯逻辑。

验的东西
--------
1. `_section_terms` 能从题面抽出实体（中文实体不该被切碎）
2. `_match_for_section` 能判断「这节在材料里有没有对应内容」
3. `node_assess` 不再早退：它真的会读段落、真的会调 LLM、真的会算新增段落数
4. **重复查询词被丢弃**（第二批的核心防御）
5. `_predict_stop` 能说清是哪个条件拦下的
6. `Repo.x` 的回滚修复有效（外键失败后连接不被锁死）

用法：.venv/bin/python tests/assess_offline.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
os.environ["RA_DB_PATH"] = "/tmp/ra_assess_offline.db"

from research_agent.db.repo import get_repo  # noqa: E402
from research_agent.graph import nodes as N  # noqa: E402

DB = Path("/tmp/ra_assess_offline.db")
for suf in ("", "-wal", "-shm"):
    p = Path(str(DB) + suf)
    if p.exists():
        p.unlink()
repo = get_repo()
SID = "s_assess_offline"
repo.x("INSERT INTO session (id,title,question,status,created_at,updated_at) "
       "VALUES (?,?,?,?,?,?)", (SID, "t", "固态电池产业化", "running",
                                "2026-01-01T00:00:00+00:00",
                                "2026-01-01T00:00:00+00:00"))

OUTLINE = [
    {"id": "sec1", "title": "半固态与全固态的技术边界",
     "question": "半固态、准固态与全固态电池分别如何定义？判断依据是电解质组成还是液体含量？"},
    {"id": "sec7", "title": "代表企业的产品边界与路线选择",
     "question": "丰田、宁德时代、比亚迪、国轩高科、清陶新能源分别押注何种固态电解质？"},
    {"id": "sec13", "title": "政策与协同平台",
     "question": "2025年八部门行动方案将如何影响标准制定与产业协作？"},
]
for i, (u, dom, ttl) in enumerate([("https://a.com/x", "a.com", "A"),
                                   ("https://b.com/y", "b.com", "B"),
                                   ("https://c.com/z", "c.com", "C")], 1):
    repo.x("INSERT INTO source (session_id,url,url_normalized,domain,title,"
           "source_type,char_count,fetched_at,on_topic) "
           "VALUES (?,?,?,?,?,?,?,?,?)",
           (SID, u, u, dom, ttl, "media", 1000,
            f"2026-01-01T00:00:0{i}+00:00", 1))
SEGS = [
    ("s1", "宁德时代在凝聚态与全固态两条路线并行推进，预计 2027 年有小批量装车。"),
    ("s2", "半固态与准固态的界限在行业里并无统一定义，液态含量口径各家不同。"),
    ("s3", "某券商研报梳理了国内 20 余家企业建设全固态中试线的进展。"),
]
SOURCES = [repo.add_source(session_id=SID, url=u, url_normalized=u,
                           domain=d, title=ttl, source_type="media",
                           char_count=1000)
           for u, d, ttl in [("https://a.com/x", "a.com", "A"),
                             ("https://b.com/y", "b.com", "B"),
                             ("https://c.com/z", "c.com", "C")]]
for i, (summ, src_id) in enumerate(zip([s[1] for s in SEGS], SOURCES), 1):
    repo.x("INSERT INTO chunk (session_id,source_id,origin,content,summary,"
           "token_count,status,on_topic,ingested_at) "
           "VALUES (?,?,?,?,?,?,?,?,?)",
           (SID, src_id, "web", summ * 5, summ, 700, "done", 1,
            f"2026-01-01 00:00:{i:02d}"))

print("=" * 70)
print("1. _section_terms：题面抽实体")
print("=" * 70)
for s in OUTLINE:
    print(f"  {s['id']}: {N._section_terms(s)}")
t7 = N._section_terms(OUTLINE[1])
assert any("国轩高科" in x for x in t7), f"中文实体被切碎了：{t7}"
print("  ✓ 中文实体（国轩高科）完整抽出，没被切碎")

print(f"\n{'='*70}\n2. _match_for_section：题面在材料里有没有对应")
print("=" * 70)
segs = N._segments_for_assess(repo, SID)
print(f"  读到的段落 {len(segs)} 条")
for s in OUTLINE:
    hits = N._match_for_section(s, segs)
    sc = N._score_sections(s, segs)
    n, top = len(sc), (sc[0][0] if sc else 0)
    print(f"  {s['id']}: 沾边 {n} 段，最高吻合度 {top}，样例 {hits[:1]}")
assert N._score_sections(OUTLINE[1], segs), "sec7 应该命中宁德时代/比亚迪"
assert not N._score_sections(OUTLINE[2], segs), "sec13 问八部门文件，材料里没有"
print("  ✓ 有材料的节能命中，没材料的节能识别为 0")

print(f"\n{'='*70}\n3. node_assess 真的干活了（mock LLM）")
print("=" * 70)
mock = {
    "has_enough": False,
    "gaps": [
        {"section_id": "sec13", "what_missing":
         "缺八部门行动方案正式文本、协同平台成立文件与地方专项基金文件",
         "urgency": "high"},
        {"section_id": "sec7", "what_missing":
         "缺国轩高科与清陶新能源的电解质路线披露", "urgency": "medium"},
    ],
    "next_queries": {"zh": ["固态电池 八部门 行动方案 2025 原文",
                             "国轩高科 固态电池 电解质 路线"],
                    "en": ["all-solid-state battery policy China 2025"]},
}
calls = []
_orig = N.LLM.complete_json


def spy(stage, prompt, system=None):
    calls.append((stage, prompt))
    if stage == "assess":
        return mock
    return {}


N.LLM.complete_json = spy
Q1 = {"zh": ["固态电池 八部门 行动方案 2025 原文"], "en": []}
st = {"session_id": SID, "question": "固态电池产业化", "outline": OUTLINE,
      "round": 0, "plan_queries": Q1, "used_queries": ["固态电池 八部门 行动方案 2025 原文"],
      "consecutive_no_new": 0, "last_round_segments": None}
out = N.node_assess(st)
N.LLM.complete_json = _orig

print(f"  assess 被调用 {sum(1 for c in calls if c[0]=='assess')} 次"
      f"（原先是 0 次，永远早退）")
assert any(c[0] == "assess" for c in calls), "assess 还是没被调用"
brief = [c[1] for c in calls if c[0] == "assess"][0]
print(f"  输入规模 {len(brief)} 字（原先上限 40000 字符平铺 150 条 claim）")
print(f"  有按小节分组: {'沾边段数' in brief and '最高吻合度' in brief}")
assert "沾边段数" in brief and "最高吻合度" in brief, "输入没有按小节重组"
print("  ✓ assess 真的被调用了，输入按小节重组")

ev = out["events"][0]
print(f"\n  事件: segments={ev['segments_total']} new={ev['new_segments']} "
      f"streak={ev['no_new_streak']} gaps={ev['gaps']} "
      f"thin={ev['thin_sections']}")
print(f"  丢弃的重复词: {ev['queries_dropped_repeat']} 条")
print(f"  停止原因预测: {ev['stop_would_be'] or '（会继续检索）'}")
assert ev["segments_total"] == 3, ev["segments_total"]
assert ev["new_segments"] == 3, ev["new_segments"]
assert ev["thin_sections"] == ["sec7", "sec13"], \
    f"薄弱小节判错：{ev['thin_sections']}"
# 判据是「最高吻合度 ≤ 1」而不是「沾边段数为 0」——
# 沾边但都对不上题，材料照样是不够用（实测 sec7 沾边 68 段最高分只有 2）。
print("  ✓ 薄弱小节按「最高吻合度 ≤ 1」判定，沾边多但不对题的也算薄弱")
assert out["consecutive_no_new"] == 0, "本轮有 3 段新增，streak 不该 +1"
print("  ✓ 有新增段落时 consecutive_no_new 归零（原先恒 +1）")

print(f"\n{'='*70}\n4. 重复查询词被丢弃（第二批的核心防御）")
print("=" * 70)
print(f"  输入的 next_queries 里有 1 条是已搜过的：{Q1['zh'][0]}")
print(f"  实际采纳的查询：{out['plan_queries']}")
assert Q1["zh"][0] not in out["plan_queries"]["zh"], "重复词没有被丢弃！"
assert "国轩高科 固态电池 电解质 路线" in out["plan_queries"]["zh"]
print("  ✓ 已搜过的词被丢掉，新的词保留")
print("  ✓ 这一点正是 R7 那轮「第 2 轮搜了一模一样的词」的修复点")

print(f"\n{'='*70}\n5. 第 2 轮不新增 → streak 真的会涨")
print("=" * 70)
st2 = dict(st, round=1, last_round_segments=3)
out2 = N.node_assess(st2)
print(f"  new_segments={out2['events'][0]['new_segments']} "
      f"streak={out2['consecutive_no_new']}")
assert out2["events"][0]["new_segments"] == 0
assert out2["consecutive_no_new"] == 1
print("  ✓ 本轮没有新段落时 streak 涨 1——现在它是真的计数器了")

print(f"\n{'='*70}\n6. _predict_stop 能说清是哪个条件拦下的")
print("=" * 70)
cases = [
    # stop_reason 现在存的是一句描述而不是 "satisfied" 哨兵值——
    # 哨兵说不清「是谁停的」，而我们要能归因到 prompt 层还是机制层。
    ({"stop_reason": "assess 说够了"}, "assess 说够了"),
    ({"consecutive_no_new": 2, "plan_queries": Q1}, "无新增"),
    ({"round": 9, "plan_queries": Q1}, "轮数"),
    ({"url_count": 60, "plan_queries": Q1}, "URL"),
    ({"elapsed_s": 40 * 60, "plan_queries": Q1}, "超时"),
    ({"cost_tokens": 900_000, "plan_queries": Q1}, "token"),
    ({"plan_queries": {}}, "查询"),
]
for over, tag in cases:
    base = {"round": 0, "url_count": 0, "elapsed_s": 0, "cost_tokens": 0,
            "consecutive_no_new": 0, "plan_queries": Q1}
    base.update(over)
    why = N._predict_stop(base)
    assert tag in why, f"预期含「{tag}」，实际「{why}」"
    print(f"  {tag:<8} → {why}")
print("  ✓ 每个硬条件都能说清楚，不再是「停了但不知道为什么」")

print(f"\n{'='*70}\n7. Repo.x 回滚修复（外键失败后连接不被锁死）")
print("=" * 70)
# 用一个**不存在**的 session id → 外键失败 → 旧代码不 rollback，
# 连接会一直持着写锁，同库其它线程各等满 busy_timeout（30 秒）。
# R3 踩过一次：write 阶段 4 个并发子 agent 各卡 30 秒。
try:
    repo.x("INSERT INTO run_trace (session_id,round,at,kind,detail_json) "
           "VALUES (?,?,?,?,?)", ("s_not_exist", 0, "2026-01-01T00:00:00+00:00", "probe", "{}"))
    print("  ⚠️ 外键竟然没拦住，跳过这项断言")
except Exception as e:
    print(f"  外键失败如期抛出：{type(e).__name__}")
n = repo.x("INSERT INTO run_trace (session_id,round,at,kind,detail_json) "
           "VALUES (?,?,?,?,?)", (SID, 99, "2026-01-01T00:00:00+00:00", "probe", "{}"))
print(f"  外键失败后同连接仍能写入 → 连接没被锁死（外键失败那条确实没进去）")
_rows = repo.run_trace(SID)
print(f"  run_trace 读回 {len(_rows)} 条："
      f"{[(r['round'], r['kind']) for r in _rows]}")
assert not any(r["kind"] == "probe" and r["round"] == 0 for r in _rows), \
    "外键失败的行不该落库"
assert any(r["kind"] == "probe" and r["round"] == 99 for r in _rows), \
    "后续写入应该成功"
# 这一条是踩过的坑：INSERT 的列顺序是 (…, at, kind, …)，
# 参数却按 (…, kind, at, …) 传，于是 kind 列里存的是时间戳。
# 断言漏了它，只看输出才发现——所以现在显式钉住。
assert all(r["kind"] in ("assess", "retrieve", "probe") for r in _rows), \
    f"kind 列存的不是 kind：{[(r['round'], r['kind']) for r in _rows]}"
assert all(re.match(r"^\d{4}-\d{2}-\d{2}T", str(r["at"])) for r in _rows), \
    f"at 列存的不是时间戳：{[(r['round'], r['at']) for r in _rows]}"
print("  ✓ kind / at 两列没有串位")
print("  ✓ 回滚生效（没有这一条，外键失败会让并发线程各等 30 秒）")

print(f"\n{'='*70}\n✅ assess 离线自检全部通过\n{'='*70}")