"""流程页供数：/events 端点 + assess/write_plan_tasks 新字段守门（离线）。

2026-10-20 加，第 3 轮前端流程页重构的前置：

- `GET /api/sessions/{sid}/events` 把 sse_event 的历史事件
  原样信封吐回去——跑完/刷新的会话前端没有 EventSource 可接，
  「回看流程」只能靠这张落库表。
- `assess` 事件加 `section_material`（每节 沾边段数/最高吻合度），
  `write_plan_tasks` 事件加 `section_segments`（每节分到的证据段数，
  任务级近似）——供小节「材料厚度」刻度用。

用法：
    .venv/bin/python tests/test_flow_data.py
"""

from __future__ import annotations

import json
import os
import _isolate  # noqa: F401
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("MINIMAX_API_KEY", "stub-not-used")
os.environ["RA_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "t.db")

from research_agent.db.repo import get_repo                        # noqa: E402
from research_agent.graph import nodes as N                        # noqa: E402
from research_agent.graph.subgraphs import write as W              # noqa: E402

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}"
          + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


# ------------------------------------------------------------- ① /events 端点

def t_events_endpoint():
    repo = get_repo()
    sid = repo.create_session("t-events", "q")
    # 每种信封各塞一条 + 一条损坏 JSON；thinking/draft 必须被滤掉
    repo.save_sse_event(sid, {"kind": "node", "node": "retrieve",
                              "event": {"k": 1}})
    repo.save_sse_event(sid, {"kind": "thinking", "node": "assess",
                              "delta": "…"})
    repo.save_sse_event(sid, {"kind": "progress", "node": "assess",
                              "event": {"kind": "assess", "round": 1}})
    repo.save_sse_event(sid, {"kind": "draft", "node": "write",
                              "delta": "xx"})
    repo.save_sse_event(sid, {"kind": "done"})
    repo.x("INSERT INTO sse_event(session_id, at, kind, node, event_json) "
           "VALUES (?,?,?,?,?)",
           (sid, "2026-10-20T00:00:00.000+00:00", "progress", "write",
            "{不是合法json"))
    sid_empty = repo.create_session("t-events-empty", "q")

    # 截断：塞 5003 条 progress（含上面已有的几条，单独开会话数干净）
    sid_big = repo.create_session("t-events-big", "q")
    for i in range(1, 5004):
        repo.save_sse_event(sid_big, {"kind": "progress", "node": "write",
                                      "event": {"kind": "w", "i": i}})

    try:
        from fastapi.testclient import TestClient
        from research_agent.server import app
        c = TestClient(app)

        d = c.get(f"/api/sessions/{sid}/events").json()
        check("① 只含 node/progress/done/error，按 id 升序",
              [e["kind"] for e in d["events"]] ==
              ["node", "progress", "done"],
              json.dumps([e["kind"] for e in d["events"]]))
        check("① 信封原样返回（内层 event 字段完整）",
              d["events"][1]["node"] == "assess"
              and d["events"][1]["event"]["round"] == 1,
              json.dumps(d["events"][1], ensure_ascii=False)[:120])
        check("① last_id 是最后一行 id", isinstance(d["last_id"], int)
              and d["last_id"] > 0, str(d["last_id"]))
        check("① truncated 未触发时为 false", d.get("truncated") is False)

        d2 = c.get(f"/api/sessions/{sid_empty}/events").json()
        check("① 无事件会话：events=[]、last_id=0",
              d2["events"] == [] and d2["last_id"] == 0, str(d2))

        r404 = c.get("/api/sessions/s_no_such/events")
        check("① 不存在的 sid → 404", r404.status_code == 404)

        d3 = c.get(f"/api/sessions/{sid_big}/events").json()
        check("① 5003 行 → truncated:true、只留最后 5000",
              d3["truncated"] is True and len(d3["events"]) == 5000
              and d3["events"][0]["event"]["i"] == 4
              and d3["events"][-1]["event"]["i"] == 5003,
              f"n={len(d3['events'])} first={d3['events'][0]['event']['i']}")
    except ImportError:
        from research_agent import server as SV
        try:
            d = SV.session_events(sid)
            check("① 直调 session_events：3 条过滤后事件",
                  len(d["events"]) == 3)
        except Exception as e:
            check("① 直调 session_events", False, repr(e))
        check("① TestClient 缺失，404/截断用例未跑", True)


# ------------------------------------------------------------- ② assess 事件

def t_assess_section_material():
    """section_material：每节 {id, hits, best}；sec1 有命中、sec2 没有。"""
    segs = [{"ref": "S1·1", "heading_path": "", "summary": "固态电池路线对比"},
            {"ref": "S1·2", "heading_path": "", "summary": "液态电池成本"}]
    state = {"session_id": "s_x", "round": 0, "question": "q",
             "outline": [{"id": "sec1", "title": "固态电池", "question": ""},
                         {"id": "sec2", "title": "界面阻抗", "question": ""}],
             "used_queries": [], "plan_queries": {},
             "consecutive_no_new": 0, "plan_approved": False}

    orig_segs, orig_cj = N._segments_for_assess, N.LLM.complete_json
    N._segments_for_assess = lambda repo, sid: segs
    N.LLM.complete_json = lambda stage, prompt, system=None: (
        {"has_enough": True, "next_queries": {}, "gaps": []})
    try:
        out = N.node_assess(state)
    finally:
        N._segments_for_assess, N.LLM.complete_json = orig_segs, orig_cj

    ev = (out.get("events") or [{}])[0]
    sm = ev.get("section_material")
    check("② assess 事件带 section_material",
          isinstance(sm, list) and len(sm) == 2, str(sm)[:160])
    if isinstance(sm, list) and len(sm) == 2:
        check("② 形状 {id,hits,best} 且命中数正确",
              sm[0] == {"id": "sec1", "hits": 1, "best": 1}
              and sm[1] == {"id": "sec2", "hits": 0, "best": 0},
              str(sm))
    # 老字段没被挤掉
    check("② thin_sections 等旧字段仍在",
          ev.get("thin_sections") == ["sec1", "sec2"]
          and ev.get("round") == 0, str(ev)[:200])


# ------------------------------------------- ③ write_plan_tasks 事件

class FakeCJ:
    def __init__(self, script):
        self.script = list(script)
        self.n = 0

    def __call__(self, stage, prompt, system=None):
        self.n += 1
        r = self.script[min(self.n - 1, len(self.script) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


def t_plan_tasks_section_segments():
    """sec1 只被 t1 盖→2；sec2 被两节盖、segments 重叠→并集 3；sec3 没人盖→0。"""
    tasks = {"tasks": [
        {"id": "t1", "covers": ["sec1", "sec2"], "mandate": "m",
         "segments": ["S1·1", "S1·2"]},
        {"id": "t2", "covers": ["sec2"], "mandate": "m",
         "segments": ["S1·2", "S1·3"]}]}
    state = {"session_id": "", "question": "q",
             "spec": {"thesis": "T"},
             "outline": [{"id": "sec1", "title": "一"},
                         {"id": "sec2", "title": "二"},
                         {"id": "sec3", "title": "三"}],
             "on_topic": [{"ref": "S1·1"}, {"ref": "S1·2"}, {"ref": "S1·3"}]}

    # sec3 没人盖 → 覆盖校验会重试一次；两次回同一份 → 取第二次，结果不变
    cj = FakeCJ([tasks, tasks])
    orig = W.LLM.complete_json
    W.LLM.complete_json = cj
    try:
        out = W.node_plan_tasks(state)
    finally:
        W.LLM.complete_json = orig

    ev = (out.get("events") or [{}])[0]
    ss = ev.get("section_segments")
    check("③ section_segments 逐节并集去重",
          ss == {"sec1": 2, "sec2": 3, "sec3": 0},
          str(ss))
    check("③ outline_titles 全节标题",
          ev.get("outline_titles") ==
          {"sec1": "一", "sec2": "二", "sec3": "三"},
          str(ev.get("outline_titles")))


def main() -> int:
    print("=== ① /events 端点 ===")
    t_events_endpoint()
    print("\n=== ② assess section_material ===")
    t_assess_section_material()
    print("\n=== ③ write_plan_tasks section_segments ===")
    t_plan_tasks_section_segments()
    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
