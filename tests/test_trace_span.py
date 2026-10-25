"""trace_span 埋点与 llm_call 细分列的守门测试（离线，不调真模型）。

2026-10-21 加。验五件事：
  1. trace.span 正确记起止、吞异常（坏 DB 路径下绝不抛）；
  2. 迁移 0011：trace_span 表/索引 + llm_call 三个新列，老库能升级；
  3. _throttle 的限流等待能落进 llm_call.wait_ms（用桩，不打网络）；
  4. checkpoint 包装器记录 span 且不改变 put 的行为/返回值；
  5. timeline_report 在老库（v13）与新库上都不崩。

用法：.venv/bin/python tests/test_trace_span.py
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="ra_trace_test_"))
DB = TMP / "test_trace.db"


def fresh_env(db_path: str):
    """换 RA_DB_PATH 时要重置 get_repo 的单例（它只读一次环境变量）。"""
    import research_agent.db.repo as repo_mod
    import os
    os.environ["RA_DB_PATH"] = db_path
    repo_mod._repo = None
    return repo_mod.get_repo()


FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}" + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def t1_span_records_and_swallows():
    repo = fresh_env(str(DB))
    sid = repo.create_session("t", "q")

    from research_agent.llm.provider import session_scope
    from research_agent.trace import span, record_span

    with session_scope(sid):
        with span("node", "test.node", foo=1) as d:
            time.sleep(0.01)
            d["extra"] = "filled later"
        # 裸 record_span
        record_span("llm_wait", "x", time.time() - 0.02, time.time(),
                    {"wait_s": 0.02})

    rows = repo.q("SELECT * FROM trace_span WHERE session_id=? ORDER BY id",
                  (sid,))
    check("① span 落库", len(rows) == 2, f"{len(rows)} 行")
    r0 = next((r for r in rows if r["name"] == "test.node"), None)
    check("① 起止正确", r0 is not None
          and 0 < r0["ended_at"] - r0["started_at"],
          f"{r0['ended_at'] - r0['started_at']:.3f}s" if r0 else "无行")
    check("① 后补 detail 进去了", r0 is not None
          and '"filled later"' in (r0["detail_json"] or ""),
          f"detail={r0['detail_json'] if r0 else None}")
    check("① thread 有值", r0 is not None and bool(r0["thread"]))

    # 坏路径下绝不抛：把单例指到写不进去的路径
    import research_agent.db.repo as repo_mod
    import os
    old_db = os.environ["RA_DB_PATH"]
    try:
        os.environ["RA_DB_PATH"] = "/nonexistent_dir_xyz/deep/bad.db"
        repo_mod._repo = None
        with span("node", "should.not.throw"):
            pass
        record_span("node", "x", time.time(), time.time())
        check("① 写不进去也吞掉", True)
    finally:
        fresh_env(old_db)


def t2_migration():
    # 新库已建（t1）；确认列与索引
    repo = fresh_env(str(DB))
    cols = {r["name"] for r in repo.q("PRAGMA table_info(llm_call)")}
    check("② llm_call 新列",
          {"started_at", "wait_ms", "ttfb_ms"} <= cols,
          f"cols={sorted(cols)[-6:]}")
    check("② trace_span 表存在",
          repo.q1("SELECT COUNT(*) n FROM trace_span") is not None)
    idx = repo.q("SELECT name FROM sqlite_master "
                 "WHERE type='index' AND tbl_name='trace_span'")
    check("② 索引存在", any("session" in (r["name"] or "") for r in idx))

    # 老库升级：v13 参考库拷到 /tmp 再跑迁移（别碰 data/refs 本体）
    src = ROOT / "data" / "refs" / "ra_v13_gate.db"
    if not src.exists():
        check("② 老库升级（v13 副本）", False, "参考库不在")
        return
    old_db = TMP / "v13.db"
    shutil.copy(src, old_db)
    for ext in ("-wal", "-shm"):
        p = Path(str(src) + ext)
        if p.exists():
            shutil.copy(p, Path(str(old_db) + ext))
    # fresh_env 里 Repo.__init__ → init_schema() 已经跑过迁移，
    # 所以这里直接验证结果，不再调 run_migrations。
    repo2 = fresh_env(str(old_db))
    cols = {r["name"] for r in repo2.q("PRAGMA table_info(llm_call)")}
    has_span = repo2.q1(
        "SELECT COUNT(*) n FROM sqlite_master WHERE name='trace_span'")["n"]
    check("② 老库升级不崩且有新列+新表",
          {"started_at", "wait_ms", "ttfb_ms"} <= cols and has_span == 1)


def t3_throttle_wait_ms():
    repo = fresh_env(str(DB))
    sid = repo.create_session("t", "q")
    import research_agent.llm.provider as P
    from research_agent.llm.provider import (
        LLMClient, SendRateLimiter, session_scope)
    import httpx

    P.reset_limiters()
    # 造一个装得很满的小窗口：第 2 次 acquire 必等
    lim = SendRateLimiter(1, window_s=0.2)
    lim.acquire()                       # 占掉唯一额度
    key = "test-throttle.example.com"
    P._LIMITERS[key] = lim

    client = LLMClient()
    req = httpx.Request("POST", f"https://{key}/v1/chat", content=b"{}")
    cfg = P.StageConfig(stage="t", provider="minimax", model="m",
                        max_output_tokens=10, temperature=0,
                        reasoning_effort=None, thinking_effort=None,
                        timeout_s=1)
    with session_scope(sid):
        # ⚠️ _record 里 session_id 从 contextvar 取——必须在 scope 里记，
        # 否则写进 session_id=NULL 的行，外键上查不到。
        client._throttle(req)           # 这次 acquire 会被压住
        client._record(cfg, "p", "out",
                       {"input_tokens": 1, "output_tokens": 1,
                        "started_at": time.time()})
    row = repo.q1("SELECT wait_ms, started_at, ttfb_ms FROM llm_call "
                  "WHERE session_id=? ORDER BY created_at DESC", (sid,))
    check("③ wait_ms 落库", row is not None and (row["wait_ms"] or 0) > 0,
          f"wait_ms={row['wait_ms'] if row else None}")
    check("③ started_at 落库", row is not None and row["started_at"] is not None)
    check("③ ttfb_ms 允许为空（没真发请求）", row is not None)
    P.reset_limiters()


def t4_checkpoint_wrapper():
    from research_agent.graph.runner import wrap_sync_saver, CKPT_DB
    import sqlite3 as sq

    # 临时 checkpoint 库，别碰 data/checkpoints.db
    ck = TMP / "ck.db"
    conn = sq.connect(str(ck), check_same_thread=False)
    from langgraph.checkpoint.sqlite import SqliteSaver
    saver = wrap_sync_saver(SqliteSaver(conn))
    saver.setup()

    repo = fresh_env(str(DB))
    sid = repo.create_session("t", "q")
    from research_agent.llm.provider import session_scope
    with session_scope(sid):
        cfg = {"configurable": {"thread_id": sid, "checkpoint_ns": "",
                                "checkpoint_id": "c1"}}
        cp = {"v": 4, "id": "c1", "ts": "t", "channel_values": {},
              "channel_versions": {}, "versions_seen": {},
              "updated_channels": []}
        out = saver.put(cfg, cp, {"source": "test", "step": 0}, {})
        saver.put_writes(cfg, [("ch", "v")], "task1")
    check("④ put 返回 config", out.get("configurable", {}).get("checkpoint_id")
          == "c1", f"got={out.get('configurable', {}).get('checkpoint_id')}")
    rows = repo.q("SELECT * FROM trace_span WHERE session_id=? AND kind='checkpoint'",
                  (sid,))
    check("④ checkpoint span 落库", len(rows) >= 2, f"{len(rows)} 行")
    if rows:
        import json as _j
        d = _j.loads(rows[0]["detail_json"] or "{}")
        check("④ 记了 thread_id/ns", d.get("thread_id") == sid
              and "checkpoint_ns" in d)


def t5_timeline_report():
    tr = ROOT / "tests" / "timeline_report.py"
    # 新库（有 trace_span）
    r1 = subprocess.run([sys.executable, str(tr), str(DB)],
                        capture_output=True, text=True, timeout=60)
    check("⑤ 新库不崩", r1.returncode == 0,
          (r1.stderr or "")[-200:])
    # 老库（v13 副本在 t2 已迁移——**不能复用**，拿原始 refs 库跑降级路径）
    old_src = TMP / "v13_raw.db"
    shutil.copy(ROOT / "data" / "refs" / "ra_v13_gate.db", old_src)
    r2 = subprocess.run([sys.executable, str(tr), str(old_src)],
                        capture_output=True, text=True, timeout=60)
    check("⑤ 老库（未迁移）不崩", r2.returncode == 0,
          (r2.stderr or "")[-200:])
    check("⑤ 老库打印无埋点提示",
          "无埋点" in r2.stdout or "没有可分析" in r2.stdout)


if __name__ == "__main__":
    print(f"测试库：{DB}")
    t1_span_records_and_swallows()
    t2_migration()
    t3_throttle_wait_ms()
    t4_checkpoint_wrapper()
    t5_timeline_report()
    print()
    if FAILED:
        print("失败：" + "、".join(FAILED))
        sys.exit(1)
    print("全过 ✅")
