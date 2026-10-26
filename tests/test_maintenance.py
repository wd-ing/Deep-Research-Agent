"""批 8 离线守门：checkpoint 清理（maintenance.py + server 自动钩子）。

真实 SqliteSaver + 小 StateGraph 在 /tmp 临时库造数据，零 API。
用法：.venv/bin/python tests/test_maintenance.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []


def check(label, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {label}"
          + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


from typing import Annotated
import operator
from typing_extensions import TypedDict


class _S(TypedDict):
    log: Annotated[list, operator.add]


def _mk_graph(ckpt_path):
    """3 节点小图 + 真 SqliteSaver，每个 invoke 产生多个 checkpoint。"""
    import sqlite3
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import END, START, StateGraph

    def a(s): return {"log": ["a"]}
    def b_(s): return {"log": ["b"]}
    def c(s): return {"log": ["c"]}
    g = StateGraph(_S)
    g.add_node("a", a); g.add_node("b", b_); g.add_node("c", c)
    g.add_edge(START, "a"); g.add_edge("a", "b"); g.add_edge("b", "c")
    g.add_edge("c", END)
    conn = sqlite3.connect(str(ckpt_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    saver = SqliteSaver(conn)
    return g.compile(checkpointer=saver), saver


def _mk_research_db(path: Path, sessions: dict[str, tuple[str, str]]):
    """sessions: {label: (status, updated_at_epoch_str)}。
    create_session 自己生成 id——返回 {label: 真实 sid}，thread_id 用它。"""
    from research_agent.db.repo import Repo
    r = Repo(path)
    r.init_schema()
    now = time.time()
    ids = {}
    for label, (status, upd) in sessions.items():
        sid = r.create_session(label, "q")
        r.x("UPDATE session SET status=?, updated_at=? WHERE id=?",
            (status, upd, sid))
        if label == "trash":
            r.x("UPDATE session SET deleted_at=? WHERE id=?", (now, sid))
        ids[label] = sid
    return ids


def main():
    from research_agent.maintenance import (apply_plan, plan_prune,
                                            prune_thread)
    tmp = Path(tempfile.mkdtemp())
    ck = tmp / "ck.db"
    rdb = tmp / "research.db"

    g, saver = _mk_graph(ck)
    cfg = lambda sid: {"configurable": {"thread_id": sid}}  # noqa: E731

    # 先用研究库生成真实 session id，再拿它们当 thread_id 跑图
    old_days = time.time() - 10 * 86400
    ids = _mk_research_db(rdb, {
        "done": ("done", str(time.time())),
        "running": ("running", str(time.time())),
        "stale": ("running", str(old_days)),
        "trash": ("done", str(time.time())),
    })
    s_done, s_running, s_stale, s_trash, s_orphan = (
        ids["done"], ids["running"], ids["stale"], ids["trash"], "s_orphan")

    st_done = g.invoke({"log": []}, config=cfg(s_done))
    g.invoke({"log": []}, config=cfg(s_running))
    g.invoke({"log": []}, config=cfg(s_orphan))
    g.invoke({"log": []}, config=cfg(s_stale))
    g.invoke({"log": []}, config=cfg(s_trash))

    # 手工补一个 ns≠'' 的行模拟子图 checkpoint（s_done 名下）
    import sqlite3
    c = sqlite3.connect(str(ck))
    c.execute(
        "INSERT INTO checkpoints (thread_id,checkpoint_ns,checkpoint_id,"
        "type,checkpoint,metadata) SELECT thread_id,'sub:fake',"
        "checkpoint_id||'-sub',type,checkpoint,metadata "
        "FROM checkpoints WHERE thread_id=? AND checkpoint_ns='' LIMIT 1",
        (s_done,))
    c.commit(); c.close()

    # --- (a) plan_prune 分类 ---
    plan = plan_prune(ck, [rdb])
    by_tid = {p["thread_id"]: p for p in plan}
    check("a 孤儿→delete_all", by_tid[s_orphan]["action"] == "delete_all")
    check("a done→keep_latest", by_tid[s_done]["action"] == "keep_latest")
    check("a running→keep_all", by_tid[s_running]["action"] == "keep_all")
    check("a 僵尸running→keep_latest",
          by_tid[s_stale]["action"] == "keep_latest")
    check("a 回收站done→keep_latest",
          by_tid[s_trash]["action"] == "keep_latest",
          by_tid[s_trash]["status"])

    # --- (b) dry-run 不改库 ---
    n_before = sqlite3.connect(str(ck)).execute(
        "SELECT count(*) FROM checkpoints").fetchone()[0]
    rc = os.system(
        f".venv/bin/python -m research_agent.maintenance prune-checkpoints "
        f"--ckpt-db {ck} --db {rdb}")
    n_after = sqlite3.connect(str(ck)).execute(
        "SELECT count(*) FROM checkpoints").fetchone()[0]
    check("b dry-run 不改库", n_before == n_after and rc == 0,
          f"{n_before}→{n_after}")

    # --- (h) 不存在 thread 安全 ---
    r0 = prune_thread(ck, "s_nonexistent")
    check("h 不存在 thread 返回 0",
          r0["deleted_checkpoints"] == 0 and r0["kept_checkpoint_id"] is None)

    # --- (c) keep_latest：只剩 ns='' 最新行 + get_state 仍可读 ---
    st_before = g.get_state(cfg(s_done))
    res = prune_thread(ck, s_done)
    c = sqlite3.connect(str(ck)); c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT checkpoint_ns, checkpoint_id FROM checkpoints "
        "WHERE thread_id=?", (s_done,)).fetchall()
    check("c 只剩 1 行且 ns='' 且 id=kept",
          len(rows) == 1 and rows[0]["checkpoint_ns"] == ''
          and rows[0]["checkpoint_id"] == res["kept_checkpoint_id"],
          str([dict(r) for r in rows]))
    st_after = g.get_state(cfg(s_done))
    check("c get_state 清理后仍可读且一致",
          (st_after.values or {}).get("log") == (st_before.values or {}).get("log"),
          f"{st_after.values}")

    # --- (d) running 原样不动 ---
    n_run_before = sqlite3.connect(str(ck)).execute(
        "SELECT count(*) FROM checkpoints WHERE thread_id=?", (s_running,)
    ).fetchone()[0]
    stats = apply_plan(ck, [p for p in plan if p["thread_id"] == s_running])
    n_run_after = sqlite3.connect(str(ck)).execute(
        "SELECT count(*) FROM checkpoints WHERE thread_id=?", (s_running,)
    ).fetchone()[0]
    check("d running thread 不动", n_run_before == n_run_after,
          f"{n_run_before}→{n_run_after}")

    # --- (e) delete_all 清干净 + 孤儿护栏 ---
    # 孤儿默认不删：不带 delete_orphans 时 skipped
    st_no = apply_plan(ck, [p for p in plan if p["thread_id"] == s_orphan])
    n_orph_keep = sqlite3.connect(str(ck)).execute(
        "SELECT count(*) FROM checkpoints WHERE thread_id=?", (s_orphan,)
    ).fetchone()[0]
    check("e2 孤儿默认不删", st_no["skipped_orphans"] == 1 and n_orph_keep > 0)
    # 护栏②：只传孤儿项时占比 100% >50% → 即便 session_total>0 也拒
    st_ratio = apply_plan(ck, [p for p in plan if p["thread_id"] == s_orphan],
                          delete_orphans=True, session_total=5)
    check("e3 孤儿占比>50% 拒绝", st_ratio["refused"],
          st_ratio["refuse_reason"])
    # 护栏①：session_total=0 直接拒（哪怕计划里没有孤儿项）
    st_ref = apply_plan(ck, [p for p in plan if p["thread_id"] == s_running],
                        delete_orphans=True, session_total=0)
    check("e4 session_total=0 拒绝", st_ref["refused"])
    # 真删：全量计划 + delete_orphans（1/5 孤儿 <50%）→ 放行
    st_yes = apply_plan(ck, plan, delete_orphans=True, session_total=5)
    n_orph = sqlite3.connect(str(ck)).execute(
        "SELECT count(*) FROM checkpoints WHERE thread_id=?", (s_orphan,)
    ).fetchone()[0]
    check("e delete_all 清空", n_orph == 0 and st_yes["deleted_threads"] == 1,
          f"deleted={st_yes['deleted_threads']}")
    # CLI 级拒绝：session 总数 0 的研究库 + --delete-orphans → 退出码 1
    empty_db = tmp / "empty.db"
    _mk_research_db(empty_db, {})
    rc_ref = os.system(
        f".venv/bin/python -m research_agent.maintenance prune-checkpoints "
        f"--ckpt-db {ck} --db {empty_db} --delete-orphans --apply "
        f">/dev/null 2>&1")
    check("CLI 空库+delete-orphans 拒绝(非零退出)", rc_ref != 0, f"rc={rc_ref}")
    rc_yr = os.system(
        f".venv/bin/python -m research_agent.maintenance prune-checkpoints "
        f"--ckpt-db {ck} --db {empty_db} --delete-orphans --yes-really "
        f">/dev/null 2>&1")
    check("CLI --yes-really dry-run 放行", rc_yr == 0, f"rc={rc_yr}")

    # --- (f) VACUUM：有人持写事务→拒绝；无人→成功且文件变小 ---
    # 造点可 vacuum 的垃圾：把 s_stale/s_trash 也裁剪掉，再量基线
    apply_plan(ck, [p for p in plan
                    if p["thread_id"] in (s_stale, s_trash)])
    sz0 = ck.stat().st_size
    holder = sqlite3.connect(str(ck))
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("CREATE TABLE IF NOT EXISTS _lock_probe(i)")
    st_lock = apply_plan(ck, [], vacuum=True)
    holder.rollback(); holder.close()
    check("f 有人持写事务时拒绝 vacuum",
          not st_lock["vacuumed"] and st_lock["vacuum_err"],
          str(st_lock["vacuum_err"]))
    # WAL 模式下「文件大小」要算 主文件+WAL——数据可能还在 -wal。
    def _db_bytes():
        return ck.stat().st_size + sum(
            (ck.parent / f"{ck.name}{sfx}").stat().st_size
            for sfx in ("-wal",) if (ck.parent / f"{ck.name}{sfx}").exists())
    sz_before_vac = _db_bytes()
    st_ok = apply_plan(ck, [], vacuum=True)
    free_n = sqlite3.connect(str(ck)).execute(
        "PRAGMA freelist_count").fetchone()[0]
    check("f 无锁时 vacuum 成功且总占用不增",
          st_ok["vacuumed"] and _db_bytes() <= sz_before_vac,
          f"{sz_before_vac}→{_db_bytes()} freelist={free_n}")

    # --- (g) 自动清理开关 ---
    import importlib
    import research_agent.server as SV
    # RA_CKPT_AUTOPRUNE=0 → prune_thread 不被调用
    called = []
    import research_agent.maintenance as M
    orig = M.prune_thread
    M.prune_thread = lambda *a, **k: called.append(1) or {
        "deleted_checkpoints": 0, "deleted_writes": 0,
        "kept_checkpoint_id": None}
    try:
        os.environ["RA_CKPT_AUTOPRUNE"] = "0"
        SV._autoprune("s_x")
        check("g AUTOPRUNE=0 不清理", not called)
        del os.environ["RA_CKPT_AUTOPRUNE"]
        SV._autoprune("s_x")
        check("g AUTOPRUNE 默认调用", len(called) == 1)
        # 异常被吞
        M.prune_thread = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("boom"))
        SV._autoprune("s_x")
        check("g prune 异常被吞", True)
    finally:
        M.prune_thread = orig
        os.environ.pop("RA_CKPT_AUTOPRUNE", None)

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项未过：{FAILED}")
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    main()
