"""checkpoint 清理（批 8，TECH_DEBT #1 的机制层）。

checkpoints.db 每个 thread（=一个会话）累积几十个 checkpoint，
一场研究约 20–27MB。本模块**不治根因**（state 里的全文照旧），
只提供裁剪：每个 thread 只保留顶层（checkpoint_ns=''）最新的
那个 checkpoint 及其 writes；子图命名空间（ns≠''）的整层删掉。

「最新」判定用 `max(checkpoint_id)`——LangGraph 的 checkpoint_id
是 uuid6（时间单调），已在真实库上与 rowid/parent 链尖核对一致
（批 8 验证：3 个 thread 三种口径结果相同）。

CLI：`python -m research_agent.maintenance prune-checkpoints`
默认 dry-run，`--apply` 才动库，`--vacuum` 须配 `--apply`。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _ckpt_path() -> Path:
    # 与 runner.py 同口径：模块常量，不另造解析
    from .graph.runner import CKPT_DB
    return CKPT_DB


def _research_paths() -> list[Path]:
    # 与 repo.py 同口径：RA_DB_PATH 覆盖，否则默认 data/research.db
    from .db.repo import DEFAULT_DB
    return [Path(os.environ.get("RA_DB_PATH", str(DEFAULT_DB)))]


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _threads(conn) -> list[str]:
    return [r["thread_id"] for r in conn.execute(
        "SELECT DISTINCT thread_id FROM checkpoints")]


def prune_thread(ckpt_db_path: str | Path, thread_id: str) -> dict:
    """删除该 thread 除「ns='' 且 checkpoint_id=最新」外的全部
    checkpoints/writes 行。不存在返回全 0。单事务；不 VACUUM。"""
    conn = _connect(Path(ckpt_db_path))
    try:
        latest = conn.execute(
            "SELECT max(checkpoint_id) c FROM checkpoints "
            "WHERE thread_id=? AND checkpoint_ns=''", (thread_id,)).fetchone()["c"]
        if latest is None:
            return {"deleted_checkpoints": 0, "deleted_writes": 0,
                    "kept_checkpoint_id": None}
        with conn:
            dc = conn.execute(
                "DELETE FROM checkpoints WHERE thread_id=? AND NOT ("
                "checkpoint_ns='' AND checkpoint_id=?)",
                (thread_id, latest)).rowcount
            # writes 的存活口径与 checkpoints 同：只留 ns='' + 最新 id 的
            dw = conn.execute(
                "DELETE FROM writes WHERE thread_id=? AND NOT ("
                "checkpoint_ns='' AND checkpoint_id=?)",
                (thread_id, latest)).rowcount
        return {"deleted_checkpoints": dc, "deleted_writes": dw,
                "kept_checkpoint_id": latest}
    finally:
        conn.close()


def _session_index(paths: list[Path]) -> dict[str, dict]:
    """合并多个研究库的 session 状态（同 id 后者不覆盖前者——
    先出现的库优先；ref 库与主库不会撞 id）。"""
    idx: dict[str, dict] = {}
    for p in paths:
        if not Path(p).exists():
            continue
        c = _connect(Path(p))
        try:
            has_mode = any(r["name"] == "mode" for r in
                           c.execute("PRAGMA table_info(session)"))
            cols = "id,status,updated_at,deleted_at" + \
                (",mode" if has_mode else ",'standard' mode")
            for r in c.execute(f"SELECT {cols} FROM session"):
                idx.setdefault(r["id"], dict(r))
        finally:
            c.close()
    return idx


def plan_prune(ckpt_db_path: str | Path,
               research_db_paths: list[str | Path],
               stale_days: float = 7) -> list[dict]:
    """对每个 thread 分类：孤儿→delete_all；done/failed→keep_latest；
    running/paused→keep_all（僵尸=stale_days 天未更新→keep_latest）；
    其它→keep_all。"""
    ckpt = _connect(Path(ckpt_db_path))
    sess = _session_index([Path(p) for p in research_db_paths])
    now = time.time()
    plan = []
    try:
        for tid in _threads(ckpt):
            n = ckpt.execute(
                "SELECT count(*) c FROM checkpoints WHERE thread_id=?",
                (tid,)).fetchone()["c"]
            approx = ckpt.execute(
                "SELECT sum(length(checkpoint)+length(metadata)) s "
                "FROM checkpoints WHERE thread_id=?", (tid,)).fetchone()["s"] or 0
            approx += ckpt.execute(
                "SELECT sum(length(value)) s FROM writes WHERE thread_id=?",
                (tid,)).fetchone()["s"] or 0
            row = sess.get(tid)
            if row is None:
                action, status = "delete_all", "孤儿"
            else:
                status = row["status"]
                if row.get("deleted_at"):
                    status += "(回收站)"
                if status.startswith(("done", "failed")) \
                        or status in ("done", "failed"):
                    action = "keep_latest"
                elif status.startswith(("running", "paused")) \
                        or status in ("running", "paused"):
                    stale = False
                    try:
                        stale = (now - _iso_ts(row["updated_at"])) \
                            > stale_days * 86400
                    except Exception:
                        pass
                    action = "keep_latest" if stale else "keep_all"
                else:
                    action = "keep_all"
            plan.append({"thread_id": tid, "status": status,
                         "n_checkpoints": n, "approx_bytes": approx,
                         "action": action})
    finally:
        ckpt.close()
    return plan


def _iso_ts(s: str | None) -> float:
    """会话 updated_at（ISO 或 unix 秒）→ epoch。"""
    if not s:
        return 0.0
    try:
        return float(s)
    except (TypeError, ValueError):
        pass
    from datetime import datetime
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def apply_plan(ckpt_db_path: str | Path, plan: list[dict],
               vacuum: bool = False, delete_orphans: bool = False,
               session_total: int | None = None,
               yes_really: bool = False) -> dict:
    """执行计划。返回执行统计。

    护栏（批 8 复审）：
    - 孤儿（delete_all）默认跳过——`session` 行在哪个研究库取决于
      `RA_DB_PATH`/调用者传入的 `--db`，没带对库会把真会话误判成孤儿
      后整体删除，不可逆。
    - `delete_orphans=True` 时还要过一道闸：传入研究库合并后
      session 数为 0、或孤儿占全部 thread >50% → 拒绝执行
      （`refused=True`），除非 `yes_really=True`。
    - `session_total=None`（调用方没传）视为 0——宁可误拒。
    """
    stats = {"deleted_threads": 0, "pruned_threads": 0,
             "kept_threads": 0, "skipped_orphans": 0,
             "refused": False, "refuse_reason": None,
             "vacuumed": False, "vacuum_err": None}
    n_threads = len(plan)
    n_orph = sum(1 for i in plan if i["action"] == "delete_all")
    if delete_orphans and not yes_really:
        reason = None
        if not session_total:
            reason = ("传入研究库合并后 session 总数为 0——"
                      "--db 很可能没指向真实研究库，孤儿判定不可信")
        elif n_orph > n_threads * 0.5:
            reason = (f"孤儿占比 {n_orph}/{n_threads} 超过 50%——"
                      "孤儿判定大概率错了，请确认 --db 指向真实研究库")
        if reason:
            stats.update(refused=True, refuse_reason=reason)
            return stats
    conn = _connect(Path(ckpt_db_path))
    try:
        for item in plan:
            tid, act = item["thread_id"], item["action"]
            if act == "delete_all":
                if not delete_orphans:
                    stats["skipped_orphans"] += 1
                    continue
                with conn:
                    conn.execute("DELETE FROM checkpoints WHERE thread_id=?", (tid,))
                    conn.execute("DELETE FROM writes WHERE thread_id=?", (tid,))
                stats["deleted_threads"] += 1
            elif act == "keep_latest":
                conn.close()  # prune_thread 用自己的连接
                res = prune_thread(ckpt_db_path, tid)
                conn = _connect(Path(ckpt_db_path))
                stats["pruned_threads"] += 1
                if res["deleted_checkpoints"] == 0:
                    stats["kept_threads"] += 1
            else:
                stats["kept_threads"] += 1
        if vacuum:
            # VACUUM 要独占且**不能在事务里**。Python sqlite3 的隐式
            # 事务管理与显式 BEGIN 冲突（'cannot VACUUM from within a
            # transaction'）——只能用 isolation_level=None 的专用连接。
            try:
                vc = sqlite3.connect(str(ckpt_db_path),
                                     isolation_level=None)
                try:
                    vc.execute("PRAGMA busy_timeout=2000")
                    vc.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    # ⚠️ VACUUM 不能在任何事务里（SQLite 硬限制，
                    # 'cannot VACUUM from within a transaction'）。
                    # 锁探测的正确姿势：BEGIN EXCLUSIVE 试探 →
                    # 拿得到说明无人持有 → ROLLBACK 放掉 →
                    # autocommit 下 VACUUM。拿不到（2s 超时）→ 拒绝。
                    vc.execute("BEGIN EXCLUSIVE")
                    vc.execute("ROLLBACK")
                    vc.execute("VACUUM")
                    stats["vacuumed"] = True
                finally:
                    vc.close()
            except Exception as e:  # noqa: BLE001 — 锁冲突要报告不炸
                stats["vacuum_err"] = f"拒绝 VACUUM：{e}"
    finally:
        conn.close()
    return stats


def _fmt_bytes(n: int | float) -> str:
    return f"{n / 1048576:.1f}MB" if n >= 1048576 else f"{n / 1024:.1f}KB"


def cli(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="maintenance")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prune-checkpoints")
    p.add_argument("--ckpt-db", default=None)
    p.add_argument("--db", action="append", default=None,
                   help="研究库路径，可重复或逗号分隔（合并判定孤儿）")
    p.add_argument("--stale-days", type=float, default=7)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--vacuum", action="store_true")
    p.add_argument("--delete-orphans", action="store_true",
                   help="连孤儿 thread 一起删（默认只裁剪已完成会话）")
    p.add_argument("--yes-really", action="store_true",
                   help="研究库为空的场景下仍确认删除孤儿")
    a = p.parse_args(argv[1:] if argv and argv[0] == "prune-checkpoints" else argv)

    ckpt = Path(a.ckpt_db) if a.ckpt_db else _ckpt_path()
    if a.db:
        dbs = [Path(x) for item in a.db for x in item.split(",")]
    else:
        dbs = _research_paths()
    if a.vacuum and not a.apply:
        print("❌ --vacuum 需要 --apply 才生效"); return 2

    plan = plan_prune(ckpt, dbs, a.stale_days)
    # _session_index 内部已按 id 去重——合并库后的 session 总数就是它的长度
    sess_total = len(_session_index(dbs))
    free_prune = sum(i["approx_bytes"] for i in plan
                     if i["action"] == "keep_latest")
    free_orph = sum(i["approx_bytes"] for i in plan
                    if i["action"] == "delete_all")
    n_orph = sum(1 for i in plan if i["action"] == "delete_all")
    n_prune = sum(1 for i in plan if i["action"] == "keep_latest")
    n_keep = sum(1 for i in plan if i["action"] == "keep_all")
    print(f"{'执行' if a.apply else '预览（dry-run，未改库）'}  "
          f"ckpt={ckpt}  研究库={[str(d) for d in dbs]}"
          f"  stale>{a.stale_days}d")
    print(f"{'thread_id':18s} {'状态':12s} {'ckpt':>5s} {'估算':>9s} 动作")
    for i in sorted(plan, key=lambda x: -x["approx_bytes"]):
        act = i["action"]
        disp = "delete_all(需 --delete-orphans)" \
            if act == "delete_all" and not a.delete_orphans else act
        print(f"{i['thread_id']:18s} {i['status']:12s} "
              f"{i['n_checkpoints']:5d} {_fmt_bytes(i['approx_bytes']):>9s} "
              f"{disp}")
    print(f"\n{'将' if not a.apply else '已'}：裁剪 {n_prune} 个 thread"
          f"（释放 {_fmt_bytes(free_prune)}），保留 {n_keep} 个；"
          f"另有 {n_orph} 个孤儿 thread 可释放 {_fmt_bytes(free_orph)}"
          f"（{'已授权' if a.delete_orphans else '需 --delete-orphans'}）")
    if not a.apply:
        return 0
    # --apply 附加保险：研究库空或孤儿占比过半又没授权删孤儿时给醒目警告
    if (sess_total == 0 or n_orph > len(plan) * 0.5) and not a.delete_orphans:
        print("\n⚠️  注意：孤儿 thread 占比过半或研究库为空——"
              "请确认 --db 指向真实研究库（可重复传入）。"
              "本次只裁剪 keep_latest，孤儿未被删除。")
    st = apply_plan(ckpt, plan, vacuum=a.vacuum,
                    delete_orphans=a.delete_orphans,
                    session_total=sess_total, yes_really=a.yes_really)
    if st["refused"]:
        print(f"❌ 已拒绝执行：{st['refuse_reason']}"
              f"（确需执行请加 --yes-really）")
        return 1
    print(f"已执行：{st}")
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
