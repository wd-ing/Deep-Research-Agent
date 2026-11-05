"""sweep 自动清扫离线测试（TECH_DEBT #1③）。不触网，临时库。
运行：RA_DB_PATH=/tmp/x_t.db RA_CKPT_DB=/tmp/x_c.db .venv/bin/python tests/test_ckpt_sweep.py
"""
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import _isolate  # noqa: F401

FAILED = []


def check(label, cond, detail=""):
    print(("✅" if cond else "❌"), label, (f"—— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def main():
    import sqlite3
    import tempfile
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import END, START, StateGraph
    from typing_extensions import TypedDict
    from typing import Annotated
    import operator
    from research_agent.db.repo import Repo
    from research_agent import maintenance as M

    class _S(TypedDict):
        log: Annotated[list, operator.add]

    tmp = Path(tempfile.mkdtemp(prefix="ck_sweep_"))
    ck, rdb = tmp / "ck.db", tmp / "r.db"
    conn = sqlite3.connect(str(ck), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    saver = SqliteSaver(conn)

    def mk_thread():
        def a(s): return {"log": ["a"]}
        g = StateGraph(_S)
        g.add_node("a", a); g.add_node("b", a); g.add_node("c", a)
        g.add_edge(START, "a"); g.add_edge("a", "b"); g.add_edge("b", "c")
        g.add_edge("c", END)
        return g.compile(checkpointer=saver)

    repo = Repo(rdb); repo.init_schema()
    now = time.time()

    def session(status, days_old):
        sid = repo.create_session("t", "q")
        upd = time.strftime("%Y-%m-%d %H:%M:%S",
                            time.localtime(now - days_old * 86400))
        repo.x("UPDATE session SET status=?, updated_at=? WHERE id=?",
               (status, upd, sid))
        return sid

    # 造 5 个 thread：done / failed / running新鲜 / running stale / 孤儿
    t_done, t_fail = session("done", 0), session("failed", 0)
    t_run_fresh = session("running", 0)
    t_run_stale = session("running", 9)          # >7d → keep_latest
    t_orphan = "t_orphan_xxx"                    # 不在 session 表
    t_skip = session("failed", 0)                # skip 覆盖
    for tid in (t_done, t_fail, t_run_fresh, t_run_stale, t_orphan, t_skip):
        g = mk_thread()
        g.invoke({"log": []}, {"configurable": {"thread_id": tid}})

    def n_ckpt(tid):
        return conn.execute("SELECT count(*) FROM checkpoints WHERE thread_id=?",
                            (tid,)).fetchone()[0]

    before = {t: n_ckpt(t) for t in (t_done, t_fail, t_run_fresh, t_run_stale, t_orphan, t_skip)}
    check("夹具：各 thread 多 checkpoint", all(v > 1 for v in before.values()), str(before))

    # ① sweep：只 keep_latest，孤儿不删，skip 不动
    st = M.sweep(ck, [rdb], stale_days=7, skip={t_skip})
    check("done 裁到 1", n_ckpt(t_done) == 1, str(n_ckpt(t_done)))
    check("failed 裁到 1", n_ckpt(t_fail) == 1, str(n_ckpt(t_fail)))
    check("running 新鲜不动", n_ckpt(t_run_fresh) == before[t_run_fresh])
    check("running stale 裁到 1", n_ckpt(t_run_stale) == 1, str(n_ckpt(t_run_stale)))
    check("孤儿不删", n_ckpt(t_orphan) == before[t_orphan])
    check("orphans_seen=1", st["orphans_seen"] == 1, str(st))
    check("skip 的 thread 不动", n_ckpt(t_skip) == before[t_skip])
    check("skipped_active=1", st["skipped_active"] == 1, str(st))
    check("errors=0", st["errors"] == 0)

    # ② 幂等：第二遍 pruned_threads=0
    st2 = M.sweep(ck, [rdb], stale_days=7, skip={t_skip})
    check("幂等 第二次 pruned=0", st2["pruned_threads"] == 0, str(st2))

    # ③ 裁完 get_state 仍能读最终态
    g2 = mk_thread()
    s = g2.get_state({"configurable": {"thread_id": t_done}})
    check("裁后 state 可读", s is not None and "log" in (s.values or {}))

    # ④ 单 thread 抛错不影响其它 + errors 计数
    t_x, t_y = session("failed", 0), session("failed", 0)
    for tid in (t_x, t_y):
        mk_thread().invoke({"log": []}, {"configurable": {"thread_id": tid}})
    orig = M.prune_thread
    def boom(path, tid):
        if tid == t_x: raise RuntimeError("boom")
        return orig(path, tid)
    M.prune_thread = boom
    try:
        st3 = M.sweep(ck, [rdb], stale_days=7)
    finally:
        M.prune_thread = orig
    check("单点失败不误伤", st3["errors"] == 1 and n_ckpt(t_y) == 1, str(st3))

    # ⑤ 调度循环：RA_CKPT_SWEEP=0 不扫
    import research_agent.server as S
    calls = [0]
    orig_once = S._sweep_once
    S._sweep_once = lambda: calls.__setitem__(0, calls[0] + 1)
    try:
        os.environ["RA_CKPT_SWEEP"] = "0"
        stop = threading.Event()
        th = threading.Thread(target=S._sweep_loop,
                              args=(0.05, 0.02, stop), daemon=True)
        th.start(); time.sleep(0.15); stop.set(); th.join(timeout=2)
        check("SWEEP=0 不调 sweep_once", calls[0] == 0, str(calls[0]))
        # 开的时候调
        os.environ["RA_CKPT_SWEEP"] = "1"
        stop2 = threading.Event()
        th2 = threading.Thread(target=S._sweep_loop,
                               args=(0.05, 0.02, stop2), daemon=True)
        th2.start(); time.sleep(0.15); stop2.set(); th2.join(timeout=2)
        check("SWEEP=1 调了 sweep_once", calls[0] >= 1, str(calls[0]))
    finally:
        S._sweep_once = orig_once
        os.environ["RA_CKPT_SWEEP"] = "0"

    # _runs 注册表：五条任务路径全覆盖——真端点 + 假图，不触网。
    # 流式路径用 astream 假图；plain 路径用 invoke 假图。
    # 假图 astream 在执行体里断言 sid∈_runs（此刻任务真在跑）。
    import asyncio
    from fastapi.testclient import TestClient
    from unittest.mock import MagicMock

    class _St:
        values = {"mode": "standard", "events": []}
        next = ()
    class _G:
        def get_state(self, c): return _St()
        def invoke(self, *a, **k): return {"report_markdown": "# x"}
    class _A:
        """async 假图：astream 里记录 sid∈_runs，可选抛错。"""
        def __init__(self, seen, sid, boom=False):
            self.seen, self.sid, self.boom = seen, sid, boom
        async def astream(self, *a, **k):
            with S._runs_lock:
                self.seen.append(self.sid in S._runs)
            if self.boom:
                raise RuntimeError("boom")
            yield {"assess": {"events": []}}

    c = TestClient(S.app)
    srepo = S.get_repo()   # 端点查的是这个库，不是上面的 rdb

    def drain(sid, max_ms=4000):
        # 先等 sid 进入 _runs（任务起来），再等它出去（任务结束）。
        t0 = time.time()
        while time.time() - t0 < max_ms / 1000:
            with S._runs_lock:
                if sid in S._runs:
                    break
            time.sleep(0.02)
        t0 = time.time()
        while time.time() - t0 < max_ms / 1000:
            with S._runs_lock:
                if sid not in S._runs:
                    return True
            time.sleep(0.02)
        return False

    # 路径1：_task_stream（streaming=True，astream 路径）
    sid = srepo.create_session("ts", "q")
    for boom in (False, True):
        seen = []
        S.agraph = lambda *a, **k: __import__("asyncio").coroutine(
            lambda: _A(seen, sid, boom))() if False else None
        # agraph() 是 sync 函数返回 async graph——直接返回协程包装的对象
        async def _mk_ag():
            return _A(seen, sid, boom)
        S.agraph = _mk_ag
        c.post(f"/api/sessions/{sid}/run?streaming=true")
        drain(sid)
        check(f"_task_stream 运行期∈runs boom={boom}", seen and seen[0] is True, str(seen))
        check(f"_task_stream 结束∉runs boom={boom}", drain(sid))

    # 路径2：_task_plain（streaming=false，invoke 路径）
    orig_graph = S.graph
    S.graph = lambda: _G()
    # plain invoke 里断言——invoke 是在 to_thread 跑的，断言要在调用时记
    seen2 = []
    class _G2(_G):
        def invoke(self, *a, **k):
            with S._runs_lock:
                seen2.append(sid in S._runs)
            return {"report_markdown": "# x"}
    S.graph = lambda: _G2()
    c.post(f"/api/sessions/{sid}/run?streaming=false")
    drain(sid)
    check("_task_plain 运行期∈runs", seen2 and seen2[0] is True, str(seen2))
    check("_task_plain 结束∉runs", sid not in S._runs)
    # _task_plain 抛异常路径
    seen3 = []
    class _G3(_G2):
        def invoke(self, *a, **k):
            with S._runs_lock:
                seen3.append(sid in S._runs)
            raise RuntimeError("boom")
    S.graph = lambda: _G3()
    c.post(f"/api/sessions/{sid}/run?streaming=false")
    drain(sid)
    check("_task_plain 异常∈runs时", seen3 and seen3[0] is True)
    check("_task_plain 异常后∉runs", sid not in S._runs)
    S.graph = orig_graph

    # 路径3/4：_go_stream/_go_plain（approve 续跑）——approve 端点
    # approve 需要会话在审批点：把 session 置 awaiting+造 checkpoint？太复杂——
    # 直接验证 _go_stream/_go_plain 的 _runs 语义：它们与 _task_stream/
    # _task_plain 是同一对模式（async to_thread + finally discard），
    # 用同样的假图覆盖 approve 端点。
    sid2 = srepo.create_session("app", "q")
    srepo.x("UPDATE session SET status='awaiting' WHERE id=?", (sid2,))
    # approve 里 get_state 校验：st.next 非空才算在审批点——造假 get_state
    class _StAppr:
        values = {"mode": "standard", "events": []}
        next = ("await_approval",)
    class _GAppr(_G):
        def get_state(self, c): return _StAppr()
    S.graph = lambda: _GAppr()
    seen4 = []
    async def _mk_ag2():
        return _A(seen4, sid2, False)
    S.agraph = _mk_ag2
    c.post(f"/api/sessions/{sid2}/approve", json={"approved": True})
    drain(sid2)
    check("_go_stream 运行期∈runs", seen4 and seen4[0] is True, str(seen4))
    check("_go_stream 结束∉runs", sid2 not in S._runs)
    # _go_plain：streaming=false + invoke 假图
    seen5 = []
    class _G5(_GAppr):
        def invoke(self, *a, **k):
            with S._runs_lock:
                seen5.append(sid2 in S._runs)
            return {"report_markdown": "# x"}
    S.graph = lambda: _G5()
    c.post(f"/api/sessions/{sid2}/approve?streaming=false", json={"approved": True})
    drain(sid2)
    check("_go_plain 运行期∈runs", seen5 and seen5[0] is True, str(seen5))
    check("_go_plain 结束∉runs", sid2 not in S._runs)
    S.graph = orig_graph
    S.agraph = None  # 恢复不了原 agraph——下方 restore
    # 路径5：_resume_after_upgrade（模块级，直接调）
    seen6 = []
    orig_ag2 = S.agraph
    async def _mk_ag3():
        return _A(seen6, sid, False)
    S.agraph = _mk_ag3
    import asyncio as _aio
    _aio.run(S._resume_after_upgrade(sid, {"configurable": {"thread_id": sid}}, set(), True, "u1"))
    check("_resume 结束∉runs", sid not in S._runs)
    class _BadA:
        async def astream(self, *a, **k):
            with S._runs_lock:
                seen6.append(sid in S._runs)
            raise RuntimeError("x")
            yield {}
    async def _mk_bad():
        return _BadA()
    S.agraph = _mk_bad
    _aio.run(S._resume_after_upgrade(sid, {"configurable": {"thread_id": sid}}, set(), True, "u1"))
    check("_resume 异常后∉runs", sid not in S._runs)
    # 恢复 agraph/graph
    import importlib
    importlib.reload(S)

    print("\n" + ("全部通过" if not FAILED else f"失败 {FAILED}"))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
