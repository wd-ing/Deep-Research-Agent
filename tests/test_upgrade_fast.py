"""快速档一键补深度 守门（离线，2026-11-05）。

update_state 到 as_node="gate" 后 next=('assess',) 是
lead 在 v34 真库 checkpoint 副本上验证过的事实；本测试
用 update_state(as_node="summary") 伪造快速档终态线程。

用法：.venv/bin/python tests/test_upgrade_fast.py
"""
import sys, sqlite3, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _isolate  # noqa: F401, E402

from unittest.mock import MagicMock
from fastapi.testclient import TestClient

import research_agent.server as S
from research_agent.db.repo import get_repo, Repo

FAILED = []
def check(l, c, d=""):
    print(("✅" if c else "❌"), l, (f"—— {d}" if d else ""))
    if not c: FAILED.append(l)

BRIEF = "# 快速简报\n\n固态电池三段论。[S1·1]"

def fast_values(sid):
    """伪造「已完成快速档线程」的终态 state（照 v34 键集）。"""
    return {"session_id": sid, "question": "q", "title": "t",
            "mode": "fast", "time_sensitive": False,
            "probe": {"quality": "enough"}, "probe_degraded": False,
            "probe_headline": "h", "outline": [{"id": "sec1", "title": "t"}],
            "outline_revision_log": [], "plan_queries": [],
            "round": 1, "visited_urls": {"u1"}, "url_count": 1,
            "consecutive_no_new": 0, "merged_gate": 1,
            "used_queries": ["q1"], "last_round_segments": [],
            "pending_extract": [], "all_evidence": [],
            "sources": [{"id": "s1", "domain": "d.com", "title": "t"}],
            "conflict_hashes": set(), "report_markdown": BRIEF,
            "cost_tokens": 1000, "elapsed_s": 60.0,
            "events": [{"kind": "retrieve", "round": 1},
                       {"kind": "outline"}]}

def make_fast_session(repo, status="done"):
    sid = repo.create_session("升级测试", "固态电池？", mode="fast")
    repo.save_report(sid, BRIEF)
    repo.x("UPDATE session SET status=? WHERE id=?", (status, sid))
    return sid

def fake_thread(sid, mode="fast"):
    cfg = {"configurable": {"thread_id": sid}}
    v = fast_values(sid); v["mode"] = mode
    S.graph().update_state(cfg, v, as_node="summary")
    return cfg

def main():
    repo = get_repo()
    c = TestClient(S.app)

    # 桩续跑：记录调用、立即结束（不发 LLM、不留挂起任务）
    calls = []
    orig_resume = S._resume_after_upgrade
    async def fake_resume(sid, cfg, seen, streaming):
        calls.append((sid, streaming))
    S._resume_after_upgrade = fake_resume
    try:
        # a) 成功路径
        sid = make_fast_session(repo)
        cfg = fake_thread(sid)
        st0 = S.graph().get_state(cfg)
        check("夹具有效：伪造线程 next==()",
              st0.next == (), f"next={st0.next}——as_node 可能要换")
        r = c.post(f"/api/sessions/{sid}/upgrade")
        check("a) 200 ok", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        row = repo.q1("SELECT * FROM session WHERE id=?", (sid,))
        check("a) session mode/状态/标记",
              row["mode"] == "standard" and row["status"] == "running"
              and row["upgraded_from"] == "fast",
              f'{row["mode"]}/{row["status"]}/{row["upgraded_from"]}')
        rv = repo.get_report_version(sid, "fast_brief")
        check("a) 简报快照逐字相等", rv and rv["markdown"] == BRIEF)
        rep = repo.q1("SELECT markdown FROM report WHERE session_id=?", (sid,))
        check("a) 原报告未被覆盖", rep["markdown"] == BRIEF)
        st = S.graph().get_state(cfg)
        check("a) next==('assess',)",
              st.next == ("assess",), f"next={st.next}")
        check("a) values.mode==standard",
              st.values.get("mode") == "standard")
        check("a) round/sources/used_queries 不变",
              st.values.get("round") == 1
              and len(st.values.get("sources") or []) == 1
              and st.values.get("used_queries") == ["q1"])
        evs = {S._event_key({"kind": "retrieve", "round": 1}),
               S._event_key({"kind": "outline"})}
        check("a) _last_seen 预置含伪造事件指纹",
              evs <= (S._last_seen.get(sid) or set()),
              str(S._last_seen.get(sid)))
        sse = repo.q("SELECT kind,event_json FROM sse_event WHERE session_id=?", (sid,))
        upg = [x for x in sse if x["kind"] == "progress"
               and '"upgrade"' in (x["event_json"] or "")]
        check("a) sse_event 有 upgrade 进度事件", len(upg) >= 1, str(len(upg)))
        check("a) 续跑桩调用恰好 1 次", len(calls) == 1, str(calls))

        # b) 404
        r = c.post("/api/sessions/不存在/upgrade")
        check("b) 404", r.status_code == 404, str(r.status_code))

        # c) not_fast / not_done / already_upgraded
        sid_std = repo.create_session("标准", "q", mode="standard")
        repo.x("UPDATE session SET status='done' WHERE id=?", (sid_std,))
        r = c.post(f"/api/sessions/{sid_std}/upgrade")
        check("c) not_fast 409",
              r.status_code == 409
              and r.json()["detail"]["reason"] == "not_fast",
              f"{r.status_code} {r.text[:120]}")
        sid_run = repo.create_session("跑着", "q", mode="fast")
        r = c.post(f"/api/sessions/{sid_run}/upgrade")
        check("c) not_done 409",
              r.status_code == 409
              and r.json()["detail"]["reason"] == "not_done")
        r = c.post(f"/api/sessions/{sid}/upgrade")
        check("c) already_upgraded 409 + 续跑仍 1 次",
              r.status_code == 409
              and r.json()["detail"]["reason"] == "already_upgraded"
              and len(calls) == 1, f"{r.status_code} calls={len(calls)}")

        # d) no_checkpoint：没线程 / 线程 mode!=fast
        sid_nc = make_fast_session(repo)
        r = c.post(f"/api/sessions/{sid_nc}/upgrade")
        check("d) 无线程 → no_checkpoint 409",
              r.status_code == 409
              and r.json()["detail"]["reason"] == "no_checkpoint",
              f"{r.status_code} {r.text[:150]}")
        sid_st = make_fast_session(repo)
        fake_thread(sid_st, mode="standard")
        r = c.post(f"/api/sessions/{sid_st}/upgrade")
        check("d) 线程 mode!=fast → no_checkpoint 409",
              r.status_code == 409
              and r.json()["detail"]["reason"] == "no_checkpoint")

        # e) busy：桩 claim_upgrade 返回 False
        orig_claim = repo.claim_upgrade
        repo.claim_upgrade = lambda sid2: False
        sid_b = make_fast_session(repo); fake_thread(sid_b)
        r = c.post(f"/api/sessions/{sid_b}/upgrade")
        repo.claim_upgrade = orig_claim
        row_b = repo.q1("SELECT * FROM session WHERE id=?", (sid_b,))
        check("e) busy 409 + session 行未变",
              r.status_code == 409
              and r.json()["detail"]["reason"] == "busy"
              and row_b["mode"] == "fast" and row_b["status"] == "done"
              and row_b["upgraded_from"] is None,
              f"{r.status_code} {r.text[:120]}")

        # f) 回滚：先伪造好线程，再桩 update_state 抛异常 → 500 + 行恢复
        sid_r = make_fast_session(repo); cfg_r = fake_thread(sid_r)
        orig_us = S.graph().update_state
        S.graph().update_state = MagicMock(side_effect=RuntimeError("boom"))
        r = c.post(f"/api/sessions/{sid_r}/upgrade")
        S.graph().update_state = orig_us
        row_r = repo.q1("SELECT * FROM session WHERE id=?", (sid_r,))
        check("f) 500 update_state_failed + 回滚",
              r.status_code == 500
              and r.json()["detail"]["reason"] == "update_state_failed"
              and row_r["mode"] == "fast" and row_r["status"] == "done"
              and row_r["upgraded_from"] is None,
              f"{r.status_code} {r.text[:150]} mode={row_r['mode']}")
        rep_r = repo.q1("SELECT markdown FROM report WHERE session_id=?", (sid_r,))
        check("f) 报告未变", rep_r["markdown"] == BRIEF)

        # g) GET：升级前 upgradable/fast_brief；升级后翻转；列表含字段
        sid_g = make_fast_session(repo); fake_thread(sid_g)
        d = c.get(f"/api/sessions/{sid_g}").json()
        check("g) 升级前 upgradable=True fast_brief=None",
              d["upgradable"] is True and d["fast_brief"] is None,
              f"{d.get('upgradable')} {type(d.get('fast_brief'))}")
        r = c.post(f"/api/sessions/{sid_g}/upgrade")
        d = c.get(f"/api/sessions/{sid_g}").json()
        check("g) 升级后 upgradable=False fast_brief=简报 upgraded_from=fast",
              d["upgradable"] is False and d["fast_brief"] == BRIEF
              and d["upgraded_from"] == "fast",
              f"{d.get('upgradable')} {d.get('upgraded_from')}")
        d2 = c.get(f"/api/sessions/{sid_nc}").json()   # fast done 但无线程
        check("g) 无线程的 fast done → upgradable=False 不 500",
              d2["upgradable"] is False)
        d3 = c.get(f"/api/sessions/{sid_std}").json()  # standard
        check("g) standard → upgradable=False", d3["upgradable"] is False)
        lst = c.get("/api/sessions").json()
        check("g) 列表项含 upgraded_from",
              all("upgraded_from" in x for x in lst) and
              any(x.get("upgraded_from") == "fast" for x in lst))

        # h) 旧库迁移 + purge 级联
        import subprocess
        subprocess.run(["sqlite3", "data/refs/ra_v30_fast.db",
                        ".backup /tmp/x_old_upg.db"], check=True)
        old = Repo("/tmp/x_old_upg.db")
        old.init_schema()   # Repo() 不自动迁移——get_repo() 才调 init_schema
        cols = {r["name"] for r in old.q("PRAGMA table_info(session)")}
        tbls = {r["name"] for r in old.q(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        migs = {r["name"] for r in old.q("SELECT name FROM schema_migrations")}
        check("h) 旧库迁移：upgraded_from 列 + report_version 表 + 0013",
              "upgraded_from" in cols and "report_version" in tbls
              and any("0013" in m for m in migs),
              f"cols差={('upgraded_from' in cols)} tbls差={('report_version' in tbls)}")
        old2 = Repo("/tmp/x_old_upg.db")
        old2.init_schema()   # 再开不报错=幂等
        check("h) 迁移幂等", True)
        osid = old.q1("SELECT id FROM session LIMIT 1")["id"]
        old.snapshot_report(osid, "fast_brief")
        old.purge_session(osid)
        left = old.q1("SELECT 1 FROM report_version WHERE session_id=?", (osid,))
        check("h) purge 会话后 report_version 无残留", left is None)
    finally:
        S._resume_after_upgrade = orig_resume

    print("\n" + ("全部通过" if not FAILED else f"失败 {FAILED}"))
    return 1 if FAILED else 0

if __name__ == "__main__":
    raise SystemExit(main())
