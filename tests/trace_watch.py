"""边跑边看：轮询 run_trace，把每一轮的决策实时打出来。

为什么要它
----------
事件日志只在 stdout 里，跑完要翻文件。而这次要盯的是「每一轮 assess 到底
决定了什么」——那正好是 run_trace 表里的内容，SQL 直接查。

用法：
  .venv/bin/python tests/trace_watch.py <db路径> [间隔秒]
配合 tests/e2e_run.py 一起跑。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ra_e2e_run.db"
GAP = int(sys.argv[2]) if len(sys.argv) > 2 else 20


def q(sql: str, *a):
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, a)]
    except Exception:
        return []
    finally:
        c.close()


def show_assess(r: dict) -> None:
    d = json.loads(r["detail_json"] or "{}")
    print(f"\n── 第 {r['round']} 轮 assess ──", flush=True)
    print(f"  段落总数 {d.get('segments_total')}，本轮新增 {d.get('new_segments')}，"
          f"连续无新增 {d.get('streak')}")
    print(f"  has_enough = {d.get('has_enough')}")
    secs = d.get("sections") or []
    thin = [s["id"] for s in secs if not s.get("hits")]
    mid = [f"{s['id']}({s['hits']})" for s in secs if 0 < s.get("hits", 0) <= 2]
    print(f"  各节命中段落数：{[(s['id'], s.get('hits')) for s in secs]}")
    if thin:
        print(f"  ⚠ 命中 0 的薄弱小节：{thin}")
    if mid:
        print(f"  · 命中 ≤2 的小节：{mid}")
    print(f"  输入规模 {d.get('brief_chars')} 字")
    for g in (d.get("gaps") or [])[:8]:
        print(f"  缺口 [{g.get('urgency')}] {g.get('section_id')}: "
              f"{str(g.get('what_missing'))[:80]}")
    qs = d.get("queries") or {}
    print(f"  下一轮查询：zh={qs.get('zh')}")
    if qs.get("en"):
        print(f"              en={qs.get('en')}")
    if d.get("dropped_repeat"):
        print(f"  🚫 丢弃了 {len(d['dropped_repeat'])} 条已搜过的词："
              f"{d['dropped_repeat'][:3]}")
    if d.get("fallback_used"):
        print(f"  ↩ 走了兜底（自主扩展检索词）")
    sw = d.get("stop_would_be")
    print(f"  ⏹ 若现在收工：{sw or '（会继续检索）'}")


def show_retrieve(r: dict) -> None:
    d = json.loads(r["detail_json"] or "{}")
    print(f"\n── 第 {r['round']} 轮 retrieve ──", flush=True)
    qz = (d.get("queries") or {}).get("zh") or []
    print(f"  搜 {len(qz)} 条中文查询，命中 {d.get('hits')}，"
          f"接纳新来源 {d.get('accepted')}")
    for x in qz[:3]:
        print(f"    · {x}")
    if d.get("repeat_queries"):
        print(f"  🚨🚨 与上轮重复 {len(d['repeat_queries'])} 条："
              f"{d['repeat_queries'][:3]}  ← 这正是要修的 bug")


def main() -> None:
    print(f"盯 {DB}，每 {GAP} 秒查一次 run_trace（Ctrl-C 退出）", flush=True)
    seen: set[tuple] = set()
    idle = 0
    while True:
        rows = q("SELECT round, kind, at, detail_json FROM run_trace "
                 "ORDER BY rowid")
        for r in rows:
            key = (r["round"], r["kind"], r["at"])
            if key in seen:
                continue
            seen.add(key)
            if r["kind"] == "assess":
                show_assess(r)
            elif r["kind"] == "retrieve":
                show_retrieve(r)
        n_q = q("SELECT COUNT(*) c FROM session")
        status = n_q[0]["c"] if n_q else 0
        if not rows:
            idle += 1
            if idle % 6 == 0:
                print(f"  …还没有 trace（{idle*GAP} 秒）", flush=True)
        else:
            idle = 0
        # 结束了就退出
        if q("SELECT status FROM session LIMIT 1") and \
                q("SELECT status FROM session LIMIT 1")[0]["status"] == "done" \
                and rows:
            print("\n研究已结束（session status=done）", flush=True)
            break
        time.sleep(GAP)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass