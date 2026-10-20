#!/usr/bin/env python3
"""给回放会话注入「修订痕迹」演示数据（**合成演示数据，非真实运行**）。

前端「朱批修订视图」要从 `GET /api/sessions/{sid}/review` 读
`patch.applied_items` 明细；回放库里的会话克隆自真实报告但没有
补丁事件。本脚本从该会话的 `report.markdown` 里挑 3 句真实正文，
伪造对应的 old/new 明细写成一条 `write_revise` progress 事件进
sse_event，并补一行 run_health（字段形状同 `nodes._run_health`）。

⚠️ 构造逻辑：new = 报告里真实存在的句子；old = new 里插了一个
   无出处的年份短语（模拟「返修把凭空数字删掉」）。第 3 条是
   delete 型（old=真实片段、new=""）。这些补丁并没有真的写回
   报告正文——只供前端视图联调。

`--flow` 时改写流程页演示数据（也是合成）：1 条带
`section_segments` 的 `write_plan_tasks` + 1 条带 `section_material`
的 `assess` progress 事件。小节 id 从该会话真实事件（write_task
的 covers / assess 的 thin_sections）里取。

⚠️ 两条合成事件的自增 id 排在历史事件**之后**——位置不代表
   真实时序（真实运行时 plan_tasks 在检索之后、首个 write_task
   之前），只供前端字段联调。

幂等：演示事件带 `"demo": true` 标记；重跑先删掉自己同类（按
event.kind 区分 review/flow 两组，互不误删）的演示行，不动真实
事件。run_health 行只在会话还没有时补，绝不覆盖真数据。

用法：
    .venv/bin/python tests/seed_review_demo.py <db路径> <session_id>
    .venv/bin/python tests/seed_review_demo.py <db路径> <session_id> --flow
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _sentences(md: str) -> list[str]:
    """正文句子（跳过标题/引言/表格/列表行），按中文句号切。"""
    out = []
    for line in md.split("\n"):
        line = line.strip()
        if not line or line[0] in "#>|-" or line.startswith("来源"):
            continue
        for s in re.split(r"(?<=。)", line):
            s = s.strip()
            if len(s) >= 25:
                out.append(s)
    return out


def _pick(md: str) -> tuple[list[str], str, str]:
    """挑 2 句普通正文 + 1 句带 [S?·?] 引用的；再挑一段 delete 用片段。"""
    sents = _sentences(md)
    cited = [s for s in sents if re.search(r"\[S\d+·\d+\]", s)]
    plain = [s for s in sents if not re.search(r"\[S\d+·\d+\]", s)]
    if len(plain) < 2 or not cited:
        raise SystemExit("报告里凑不出 2 句无引用正文 + 1 句带引用正文")
    picked = [cited[0], plain[0], plain[min(2, len(plain) - 1)]]

    # delete 片段：从 picked[1] 里取中间一小段，before=它前面的字
    frag_src = picked[1]
    frag = frag_src[len(frag_src) // 2:][:18] or frag_src[:15]
    return picked, frag, frag_src


def _clean_demo(db, sid: str, kinds: set[str]) -> int:
    """删掉这组（kind 属于 kinds）的演示行——只认 "demo": true，
    不碰真实事件，也不误删另一组演示数据。"""
    n = 0
    for r in db.execute(
            "SELECT id, event_json FROM sse_event WHERE session_id=?",
            (sid,)).fetchall():
        try:
            inner = (json.loads(r["event_json"]) or {}).get("event") or {}
        except (ValueError, TypeError):
            continue
        if inner.get("demo") and inner.get("kind") in kinds:
            db.execute("DELETE FROM sse_event WHERE id=?", (r["id"],))
            n += 1
    return n


def _insert(db, sid: str, node: str, event: dict) -> None:
    db.execute(
        "INSERT INTO sse_event(session_id, at, kind, node, event_json) "
        "VALUES (?,?,?,?,?)",
        (sid, _now(), "progress", node,
         json.dumps({"kind": "progress", "node": node, "event": event},
                    ensure_ascii=False)))


def _flow_seed(db, sid: str) -> int:
    """流程页演示事件（合成，非真实运行）：小节材料厚度。"""
    n = _clean_demo(db, sid, {"write_plan_tasks", "assess"})
    if n:
        print(f"清掉上轮 flow 演示事件 {n} 行")

    # 真实小节 id：write_task 的 covers 顺序就是大纲顺序；
    # 老会话没有 write_task 时退到 thin_sections/narrowed 里的 secN
    sec_ids: list[str] = []
    for r in db.execute(
            "SELECT event_json FROM sse_event WHERE session_id=? "
            "AND kind='progress' ORDER BY id", (sid,)).fetchall():
        try:
            inner = (json.loads(r["event_json"]) or {}).get("event") or {}
        except (ValueError, TypeError):
            continue
        cands: list[str] = []
        if inner.get("kind") == "write_task":
            cands = [c for c in (inner.get("covers") or [])
                     if isinstance(c, str)]
        elif inner.get("kind") == "assess":
            cands = [c for c in (inner.get("thin_sections") or [])
                     if isinstance(c, str)]
        for c in cands:
            if re.fullmatch(r"sec\d+", c) and c not in sec_ids:
                sec_ids.append(c)
    sec_ids.sort(key=lambda s: int(s[3:]))
    if not sec_ids:
        raise SystemExit(f"{sid} 的事件流里找不到任何小节 id")

    # 造有厚有薄、一节为 0 的分布——刻度条的区分度全指着这个
    hits = {s: n for s, n in zip(
        sec_ids, (12, 8, 0, 9, 2, 11, 5, 7, 4, 6)[:len(sec_ids)])}
    plan_ev = {"kind": "write_plan_tasks", "demo": True,   # ⚠️ 合成演示数据
               "tasks": len(sec_ids), "raw_tasks": len(sec_ids),
               "segments_available": 113, "tool_reads": 0,
               "plan_retried": False, "fallback_summary_segments": 0,
               "section_segments": hits, "uncovered_sections": []}
    # best<=1 对应 thin_sections 的判薄口径（最高吻合度<=1）
    assess_ev = {"kind": "assess", "demo": True,           # ⚠️ 合成演示数据
                 "round": 99, "segments_total": 113, "new_segments": 0,
                 "round_gain": 0.0, "no_new_streak": 1,
                 "has_enough": False,
                 "thin_sections": [s for s in sec_ids
                                  if hits[s] <= 2],
                 "section_material": [
                     {"id": s, "hits": hits[s],
                      "best": 1 if hits[s] <= 2 else min(4, hits[s] // 3)}
                     for s in sec_ids],
                 "gaps": 0, "gaps_by_urgency": {},
                 "queries_fresh": 0, "queries_dropped_repeat": 0,
                 "queries_fallback": False,
                 "stop_would_be": "（演示）", "stop_layer": "—"}
    _insert(db, sid, "write", plan_ev)
    _insert(db, sid, "assess", assess_ev)
    db.commit()
    print(f"已写入 {sid}：write_plan_tasks(section_segments) + "
          f"assess(section_material) 演示事件，覆盖 {len(sec_ids)} 节："
          f"{hits}")
    print(f"验证: curl -s http://127.0.0.1:8000/api/sessions/{sid}/events"
          " | python3 -m json.tool | tail -40")
    return 0


def main() -> int:
    if len(sys.argv) not in (3, 4):
        print(__doc__)
        return 2
    db_path, sid = sys.argv[1], sys.argv[2]
    flow = len(sys.argv) == 4 and sys.argv[3] == "--flow"
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    if flow:
        return _flow_seed(db, sid)

    row = db.execute("SELECT markdown FROM report WHERE session_id=?",
                     (sid,)).fetchone()
    if not row or not row["markdown"]:
        raise SystemExit(f"{sid} 没有 report.markdown")
    md = row["markdown"]

    # 幂等：清掉上一轮 review 演示行（按 kind 限定，不碰 flow 组的）
    n_del = _clean_demo(db, sid, {"write_revise"})
    db.execute("DELETE FROM run_trace WHERE session_id=? AND kind='run_health' "
               "AND detail_json LIKE '%\"demo\": true%'", (sid,))
    if n_del:
        print(f"清掉上轮演示事件 {n_del} 行")

    sents, frag, frag_src = _pick(md)

    def _edit_item(s: str, i: int) -> dict:
        # old = 真实句里插一个无出处年份短语——模拟返修删掉了凭空数字
        k = s.find("，")
        at = k if 0 < k < 30 else min(12, len(s) // 2)
        old = s[:at] + "（2024 年）" + s[at:]
        idx = md.find(s)          # new 是真实句，before 按它在正文里的位置取
        return {"task": "t1", "section": f"sec{i + 1}",
                "section_title": f"演示小节 {i + 1}",
                "action": "edit",
                "problem": "凭空出现的年份，材料里没有出处",
                "old": old[:400], "new": s[:400],
                "before": md[max(0, idx - 40):idx] if idx >= 0 else ""}

    # delete 型：old=真实片段，new=""，before=片段前 40 字（在整篇 md 里定位）
    idx = md.find(frag)
    before = md[max(0, idx - 40):idx] if idx >= 0 else ""
    del_item = {"task": "t1", "section": "sec3",
                "section_title": "演示小节 3",
                "action": "delete",
                "problem": "该句与上下文重复且无独立出处，删除",
                "old": frag, "new": "", "before": before}

    items = [_edit_item(sents[0], 0), _edit_item(sents[1], 1), del_item]
    envelope = {
        "kind": "progress", "node": "write",
        "event": {
            "kind": "write_revise", "round": 1,
            "demo": True,               # ⚠️ 合成演示数据，非真实运行
            "tasks": ["t1"],
            "targets": ["t1→sec1:edit", "t1→sec2:edit", "t1→sec3:delete"],
            "patch": {"items": 3, "patch_calls": 1, "applied": 3,
                      "applied_items": items, "applied_truncated": 0,
                      "rejected": [], "unresolved": 0,
                      "fallback_sections": []},
        },
    }
    db.execute(
        "INSERT INTO sse_event(session_id, at, kind, node, event_json) "
        "VALUES (?,?,?,?,?)",
        (sid, _now(), "progress", "write",
         json.dumps(envelope, ensure_ascii=False)))

    has_health = db.execute(
        "SELECT 1 FROM run_trace WHERE session_id=? AND kind='run_health'",
        (sid,)).fetchone()
    if not has_health:
        health = {"kind": "run_health", "demo": True,   # ⚠️ 合成演示数据
                  "llm_by_stage": {},
                  "unsummarized_on_topic_chunks":
                      {"missing": 0, "total": 0, "sources": 0},
                  "gate_degraded_chunks": 0, "unjudged_sources": 0,
                  "failed_tasks": 0}
        db.execute(
            "INSERT INTO run_trace(session_id, round, at, kind, detail_json) "
            "VALUES (?,?,?,?,?)",
            (sid, 0, _now(), "run_health",
             json.dumps(health, ensure_ascii=False)))
    else:
        print("会话已有 run_health，跳过演示行（不覆盖真数据）")

    db.commit()
    print(f"已写入 {sid}：1 条 write_revise 演示事件"
          f"（{len(items)} 条 applied_items）"
          f"{'' if not has_health else '；health 已存在未动'}")
    print(f"验证: curl -s http://127.0.0.1:8000/api/sessions/{sid}/review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
