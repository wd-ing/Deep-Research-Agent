"""坑99 段3：限额+并发。离线 TestClient + 假图，不触网。"""
import os, sys, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import _isolate  # noqa

FAILED = []
def check(label, cond, detail=""):
    print(("✅" if cond else "❌"), label, (f"—— {detail}" if detail and not cond else ""))
    if not cond: FAILED.append(label)

def main():
    tmp = Path(tempfile.mkdtemp(prefix="auth_l_"))
    os.environ["RA_AUTH"] = "1"
    os.environ["RA_USERS_PATH"] = str(tmp / "users.json")
    os.environ["RA_AUTH_SECRET"] = "lim-secret"
    os.environ["RA_DB_PATH"] = str(tmp / "r.db")
    os.environ["RA_CKPT_DB"] = str(tmp / "ck.db")

    import research_agent.auth as A
    A.add_user("u1", "U1", "t1", limits={"standard": 2, "fast": 6})
    A.add_user("u2", "U2", "t2", limits={"standard": 2, "fast": 6})
    A.add_user("boss", "B", "t3", admin=True)

    import importlib
    import research_agent.server as S
    importlib.reload(S)
    from fastapi.testclient import TestClient
    c = TestClient(S.app)
    ck = {u: A.sign_cookie(u) for u in ("u1", "u2", "boss")}
    repo = S.get_repo()

    def go(m, path, who, **kw):
        return getattr(c, m)(path,
            headers={"Cookie": f"{A.COOKIE_NAME}={ck[who]}"}, **kw)
    def mk(who="u1", mode="standard"):
        sid = repo.create_session("t", "q", owner=who)
        repo.x("UPDATE session SET mode=? WHERE id=?", (mode, sid))
        return sid

    # ---- 假图：空 astream 立即结束；hold=True 时阻塞等事件（占位测试用）
    hold_ev = threading.Event()
    class _G:
        class _St:
            values = {"mode": "standard", "events": []}
            next = ()
        def get_state(self, x): return _G._St()
        def invoke(self, *a, **k):
            hold_ev.wait(3)
            return {"report_markdown": "# x"}
    class _A:
        async def astream(self, *a, **k):
            hold_ev.wait(3)
            if False: yield {}
    async def _ag(): return _A()
    orig_graph, orig_agraph = S.graph, S.agraph
    S.graph = lambda: _G()
    S.agraph = _ag

    def run(who, sid):
        return go("post", f"/api/sessions/{sid}/run?streaming=true", who)

    # ============ 限额：标准 2 → 第 3 次 429 ============
    S._runs.clear()
    sids = [mk("u1") for _ in range(3)]
    r1, r2 = run("u1", sids[0]), run("u1", sids[1])
    check("前两次 200", r1.status_code == 200 and r2.status_code == 200,
          f"{r1.status_code},{r2.status_code}")
    # 任务立即结束释放占位；等 _runs 清空再跑第三次（限额与并发独立）
    for _ in range(50):
        if not S._runs: break
        time.sleep(0.05)
    r3 = run("u1", sids[2])
    check("第3次 standard→429 quota", r3.status_code == 429
          and r3.json()["detail"]["code"] == "quota_exceeded"
          and r3.json()["detail"]["kind"] == "standard"
          and r3.json()["detail"]["limit"] == 2
          and "resets_at" in r3.json()["detail"], str(r3.status_code))
    check("429 后无 usage 残留",
          repo.count_usage("u1", "standard", 0) == 2)

    # ============ POST 预检：429 且不建行 ============
    n0 = repo.q1("SELECT COUNT(*) c FROM session")["c"]
    r = go("post", "/api/sessions", "u1",
           json={"question": "x", "mode": "standard", "force_research": True})
    check("create 预检 429", r.status_code == 429, str(r.status_code))
    check("会话行数不变",
          repo.q1("SELECT COUNT(*) c FROM session")["c"] == n0)
    # 额度独立：fast 还有额度
    r = go("post", "/api/sessions", "u1",
           json={"question": "x", "mode": "fast", "force_research": True})
    check("fast 额度独立可建", r.status_code == 200, str(r.status_code))

    # ============ admin 不限但记账 ============
    hold_ev.set()                    # 任务即刻结束，不占位
    sidb = mk("boss")
    rs = []
    for _ in range(3):       # 每场间等占位释放——admin 也受每人 1 约束（设计如此）
        rs.append(go("post", f"/api/sessions/{sidb}/run?streaming=true", "boss"))
        for _ in range(120):
            if not S._runs: break
            time.sleep(0.05)
    check("admin 3 次全过", all(r.status_code == 200 for r in rs),
          str([r.status_code for r in rs]))
    for _ in range(50):
        if not S._runs: break
        time.sleep(0.05)
    check("admin 仍记账", repo.count_usage("boss", "standard", 0) == 3)

    # ============ 升级计 standard ============
    # u2 已用 standard=1；升级一场 fast→standard 后 used=2，第 3 场应 429
    repo.add_usage("u2", "standard", "seed", time.time())
    sidu = mk("u2", "fast")
    repo.x("UPDATE session SET status='done' WHERE id=?", (sidu,))
    # update_state 也会跑——get_state 桩已由 _G 给 values={mode:standard}
    S.graph = lambda: type("G", (), {
        "get_state": lambda self, c: type("S", (), {
            "values": {"mode": "fast", "events": []}, "next": ()})(),
        "update_state": lambda self, c, v, as_node: None,
        "invoke": lambda self, *a, **k: {"report_markdown": "x"}})()
    r = go("post", f"/api/sessions/{sidu}/upgrade", "u2")
    check("升级 200", r.status_code == 200, str(r.status_code))
    check("升级计 standard",
          repo.count_usage("u2", "standard", 0) == 2)
    for _ in range(50):
        if not S._runs: break
        time.sleep(0.05)

    # ============ 日界线：注入 _now 跨零点重置 ============
    day0 = S._shanghai_day_start(S._now())
    S._now = lambda: day0 - 10          # 昨天
    repo.add_usage("u2", "fast", "y1", S._now())
    repo.add_usage("u2", "fast", "y2", S._now())
    S._now = time.time                  # 回到今天
    check("昨天的不计今天",
          repo.count_usage("u2", "fast", S._shanghai_day_start(S._now())) == 0)

    # ============ 并发：每人 1（直接种子 _runs 测 _reserve_run 逻辑）====
    S.graph = lambda: _G(); S.agraph = _ag
    sA1, sA2, sB = mk("u1", "fast"), mk("u1", "fast"), mk("u2", "fast")
    with S._runs_lock:
        S._runs[sA1] = "u1"            # A 有一场在跑的等效态
    r_a2 = run("u1", sA2)
    check("A 占位时第二场→429 busy", r_a2.status_code == 429
          and r_a2.json()["detail"]["code"] == "busy", str(r_a2.status_code))
    r_b = go("post", f"/api/sessions/{sB}/run?streaming=true", "u2")
    check("B 不受影响", r_b.status_code == 200, str(r_b.status_code))
    # 同 sid 重复 → 409 不计
    r_dup = run("u1", sA1)
    check("同 sid→409 already", r_dup.status_code == 409
          and r_dup.json()["detail"]["code"] == "already_running")
    n_u1f = repo.count_usage("u1", "fast", 0)
    with S._runs_lock:
        S._runs.pop(sA1, None)
    check("429/409 不产生 usage",
          repo.count_usage("u1", "fast", 0) == n_u1f)
    check("429 后 sA2 会话状态不变", repo.q1(
        "SELECT status FROM session WHERE id=?", (sA2,))["status"] == "running")

    # ============ 全局 RA_MAX_RUNS ============
    os.environ["RA_MAX_RUNS"] = "2"
    with S._runs_lock:
        S._runs["x1"] = "u2"; S._runs["x2"] = "u3"   # 两个别人的占位
    sidg = mk("u1", "fast")
    r3rd = go("post", f"/api/sessions/{sidg}/run?streaming=true", "u1")
    check("全局 2 时→429 busy", r3rd.status_code == 429
          and r3rd.json()["detail"]["code"] == "busy", str(r3rd.status_code))
    with S._runs_lock:
        S._runs.clear()
    os.environ.pop("RA_MAX_RUNS")

    # ============ 真并发原子性（ThreadPoolExecutor+Barrier）============
    hold_ev.clear()
    pA, pB = mk("u1", "fast"), mk("u1", "fast")
    barrier = threading.Barrier(2)
    results = []
    def t_run(sid):
        c2 = TestClient(S.app)
        barrier.wait()
        r = c2.post(f"/api/sessions/{sid}/run?streaming=true",
                    headers={"Cookie": f"{A.COOKIE_NAME}={ck['u1']}"})
        results.append(r.status_code)
    with ThreadPoolExecutor(2) as ex:
        list(ex.map(t_run, [pA, pB]))
    check("并发同人只放1个", sorted(results) == [200, 429], str(results))
    hold_ev.set()
    for _ in range(100):
        if not S._runs: break
        time.sleep(0.05)

    # ============ 起任务失败：释放占位+撤销 usage ============
    # 走 streaming=false：只断 asyncio.to_thread，任务派发链炸在
    # create_task 里→走 except→释放占位+撤账（不碰 _flush_loop 那条）。
    u_before = repo.count_usage("u1", "fast", 0)
    orig_tt = S.asyncio.to_thread
    S.asyncio.to_thread = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("boom"))
    sidf = mk("u1", "fast")
    c_500 = TestClient(S.app, raise_server_exceptions=False)
    try:
        r = c_500.post(f"/api/sessions/{sidf}/run?streaming=false",
                       headers={"Cookie": f"{A.COOKIE_NAME}={ck['u1']}"})
    finally:
        S.asyncio.to_thread = orig_tt
    check("起任务失败→非200", r.status_code != 200, str(r.status_code))
    check("失败后占位释放", sidf not in S._runs)
    check("失败后 usage 撤销", repo.count_usage("u1", "fast", 0) == u_before)
    check("会话状态不变", repo.q1(
        "SELECT status FROM session WHERE id=?", (sidf,))["status"] == "running")

    # ============ 限额原子性：并发预检同事务拦截 ============
    # u2 standard 已用 2/2（seed+upgrade）——并发再记两条也应全拒；
    # 这条主要守「并发下不能突破 limit」
    ok_cnt = [0]
    def try_reserve():
        okk, _ = repo.reserve_usage("u2", "standard", 2,
                                    S._shanghai_day_start(S._now()),
                                    "x", S._now())
        if okk: ok_cnt[0] += 1
    ths = [threading.Thread(target=try_reserve) for _ in range(6)]
    [t.start() for t in ths]; [t.join() for t in ths]
    check("并发限额不超", ok_cnt[0] == 0, str(ok_cnt[0]))

    # ============ /api/auth/me ============
    me = go("get", "/api/auth/me", "u1").json()
    check("me.used 与记账一致",
          me["used"]["standard"] == repo.count_usage(
              "u1", "standard", S._shanghai_day_start(S._now())))
    check("me.limits", me["limits"] == {"standard": 2, "fast": 6})
    check("me.resets_at", me["resets_at"] > time.time())

    S.graph, S.agraph = orig_graph, orig_agraph
    print("\n" + ("全部通过" if not FAILED else f"失败 {FAILED}"))
    return 0 if not FAILED else 1

if __name__ == "__main__":
    sys.exit(main())
