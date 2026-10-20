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

幂等：演示事件带 `"demo": true` 标记；重跑先删掉自己上次写的行，
不动真实事件。run_health 行只在会话还没有时补，绝不覆盖真数据。

用法：
    .venv/bin/python tests/seed_review_demo.py <db路径> <session_id>
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


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    db_path, sid = sys.argv[1], sys.argv[2]
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    row = db.execute("SELECT markdown FROM report WHERE session_id=?",
                     (sid,)).fetchone()
    if not row or not row["markdown"]:
        raise SystemExit(f"{sid} 没有 report.markdown")
    md = row["markdown"]

    # 幂等：清掉上一轮演示行（只认 "demo": true 标记的行）
    n_del = 0
    for r in db.execute(
            "SELECT id, event_json FROM sse_event WHERE session_id=?",
            (sid,)).fetchall():
        try:
            inner = (json.loads(r["event_json"]) or {}).get("event") or {}
        except (ValueError, TypeError):
            continue
        if inner.get("demo"):
            db.execute("DELETE FROM sse_event WHERE id=?", (r["id"],))
            n_del += 1
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
