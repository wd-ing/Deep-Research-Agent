"""SQLite 访问层：裸 SQL + 薄封装。

单文件数据库，向量以 float16 BLOB 存在 chunk 表里，检索时按需载入不常驻。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "data" / "research.db"
MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _jd(obj: Any) -> str | None:
    if obj is None:
        return None
    import json
    return json.dumps(obj, ensure_ascii=False)


def _jl(text: str | None) -> Any:
    if not text:
        return None
    import json
    try:
        return json.loads(text)
    except Exception:
        return None


class Repo:
    def __init__(self, path: str | Path = DEFAULT_DB) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()

    # ---------- 连接 ----------

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA busy_timeout = 30000")
            self._local.conn = c
        return c

    def init_schema(self) -> None:
        """按序执行未应用的迁移。

        结构的**唯一来源**是 migrations/ 目录。0001_init.sql 是基线，
        之后每次结构变更新增一个 00NN 文件，永远不改旧文件。

        早先这里同时存在 schema.sql（最新形态）与 migrations/（增量脚本）
        两份描述同一个结构，会静默漂移；为了让两者兼容，还加了「新库把迁移
        全部盖章为已应用」和「duplicate column 就跳过」两处容错——
        那是补丁，不是结构。现在只有一份真相，不需要它们了。
        """
        self.run_migrations()

    def run_migrations(self) -> list[str]:
        """执行未应用的迁移，返回本次实际执行的脚本名。"""
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        self.conn.commit()
        done = {r["name"] for r in self.q("SELECT name FROM schema_migrations")}

        applied: list[str] = []
        for f in sorted(MIGRATIONS.glob("*.sql")):
            if f.name in done:
                continue
            with self._write_lock:
                self.conn.executescript(f.read_text(encoding="utf-8"))
                self.conn.execute(
                    "INSERT INTO schema_migrations(name, applied_at) VALUES(?,?)",
                    (f.name, now()),
                )
                self.conn.commit()
            applied.append(f.name)
        return applied

    def q(self, sql: str, params: Sequence = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params))

    def q1(self, sql: str, params: Sequence = ()) -> sqlite3.Row | None:
        cur = self.conn.execute(sql, params)
        return cur.fetchone()

    def x(self, sql: str, params: Sequence = ()) -> sqlite3.Cursor:
        with self._write_lock:
            try:
                cur = self.conn.execute(sql, params)
                self.conn.commit()
                return cur
            except Exception:
                # 必须回滚。不回滚的话连接会一直持着写锁，
                # 同库其它线程（write 阶段是并发的）每个都要等满 busy_timeout。
                # 踩过一次：外键失败没回滚，4 个并发子 agent 各卡 30 秒。
                self.conn.rollback()
                raise

    # ---------- 会话 ----------

    def create_session(self, title: str, question: str) -> str:
        sid = new_id("s_")
        t = now()
        self.x(
            "INSERT INTO session (id,title,question,status,created_at,updated_at) "
            "VALUES (?,?,?, 'running', ?, ?)",
            (sid, title, question, t, t),
        )
        return sid

    def set_outline(self, session_id: str, outline: Any, plan: Any = None) -> None:
        import json

        self.x(
            "UPDATE session SET outline_json=?, plan_json=COALESCE(?, plan_json), updated_at=? "
            "WHERE id=?",
            (json.dumps(outline, ensure_ascii=False),
             json.dumps(plan, ensure_ascii=False) if plan is not None else None,
             now(), session_id),
        )

    def finish_session(self, session_id: str, status: str = "done") -> None:
        self.x("UPDATE session SET status=?, updated_at=? WHERE id=?",
               (status, now(), session_id))

    def archive_session(self, session_id: str) -> None:
        """归档：标记为「值得留」，从默认列表里收起来。

        ⚠️ 语义在 2026-10-12 变过。原来注释写的是「只有归档后的会话才
        进入历史检索」——但跨会话召回已经砍掉了（用户决定聚焦检索写作），
        归档现在**纯粹是整理动作**：不删除、不影响任何下游逻辑。
        别再给它加功能了。
        """
        t = now()
        self.x(
            "UPDATE session SET archived_at=?, status='archived', updated_at=? WHERE id=?",
            (t, t, session_id),
        )

    def add_cost(self, session_id: str, tokens: int) -> None:
        self.x("UPDATE session SET cost_tokens = cost_tokens + ?, updated_at=? WHERE id=?",
               (tokens, now(), session_id))

    # ---------- 会话管理（2026-10-12 产品化） ----------

    def update_session(self, session_id: str, **fields) -> None:
        """改会话的展示属性。**白名单字段**，不接受任意列名。

        只开放 title / folder_id / archived 三样：它们是纯展示属性，
        误传 status 或 cost_tokens 之类会直接写坏数据。
        """
        allowed = {"title": "title", "folder_id": "folder_id",
                   "archived": None}          # None = 走专用分支
        sets, params = [], []
        for k, v in fields.items():
            if k == "archived":
                # 归档是可逆的：给了 bool 就设/清 archived_at 与 status
                t = now()
                if v:
                    sets += ["archived_at=?", "status='archived'"]
                    params.append(t)
                else:
                    sets += ["archived_at=NULL",
                             "status=CASE WHEN status='archived' THEN 'done' "
                             "ELSE status END"]
                continue
            col = allowed.get(k)
            if not col:
                continue
            sets.append(f"{col}=?")
            params.append(v)
        if not sets:
            return
        sets.append("updated_at=?")
        params.append(now())
        params.append(session_id)
        self.x(f"UPDATE session SET {', '.join(sets)} WHERE id=?", tuple(params))

    def soft_delete_session(self, session_id: str) -> None:
        """移到回收站。**报告 / 来源 / 段一律不动**（用户 2026-10-12 定）。

        真删会级联掉报告、来源、段落，而误删是最容易发生的损失。
        软删除只是一列，恢复后完整回来。
        """
        self.x("UPDATE session SET deleted_at=?, updated_at=? WHERE id=?",
               (now(), now(), session_id))

    def restore_session(self, session_id: str) -> None:
        self.x("UPDATE session SET deleted_at=NULL, updated_at=? WHERE id=?",
               (now(), session_id))

    def purge_session(self, session_id: str) -> None:
        """真删（清空回收站用）。级联删掉报告/来源/段，靠的是外键 ON DELETE CASCADE。"""
        self.x("DELETE FROM session WHERE id=?", (session_id,))

    def set_summary(self, session_id: str, summary: dict) -> None:
        import json
        self.x("UPDATE session SET summary_json=?, updated_at=? WHERE id=?",
               (json.dumps(summary, ensure_ascii=False), now(), session_id))

    # ---------- 文件夹 ----------

    def create_folder(self, name: str, parent_id: str | None = None) -> str:
        fid = new_id("f_")
        t = now()
        mx = self.q1(
            "SELECT COALESCE(MAX(sort_order), 0) AS m FROM folder "
            "WHERE parent_id IS ?", (parent_id,))
        nxt = (mx["m"] if mx else 0) + 1
        self.x("INSERT INTO folder (id,name,parent_id,sort_order,created_at) "
               "VALUES (?,?,?,?,?)", (fid, name, parent_id, nxt, t))
        return fid

    def rename_folder(self, folder_id: str, name: str) -> None:
        self.x("UPDATE folder SET name=? WHERE id=?", (name, folder_id))

    def delete_folder(self, folder_id: str) -> int:
        """删文件夹，里面的会话**移到未分类**（folder_id=NULL），不连带删。

        ⚠️ 子文件夹也要一并删掉（`ON DELETE SET NULL` 只会把它们挂成孤儿，
        parent_id 指向已删除的行，**树就断了**）——2026-10-12 实测发现。
        所以这里先递归收集整棵子树再删。

        返回被移出（会话）的数量，给接口层报给用户听。
        ⚠️ **必须统计整棵子树**（2026-10-12 修）：`session.folder_id` 的外键是
        `ON DELETE SET NULL`，所以删子文件夹时**它的会话也会落回未分类**——
        但那些会话不由这一行 `UPDATE` 负责，只数直接子会话会**少报**。
        用户看到「移出 0 场」却发现列表里多了一堆未分类会话，会以为出 bug。
        """
        sub = self._subtree(folder_id)
        qs = ','.join('?' * len(sub))
        n = self.q1(f"SELECT COUNT(*) AS c FROM session WHERE folder_id IN ({qs})",
                    tuple(sub))
        for fid in sub:
            self.x("DELETE FROM folder WHERE id=?", (fid,))
        return int(n["c"] if n else 0)

    def _subtree(self, root: str) -> list[str]:
        """root 及其全部子孙的 id。逐层展开而不是递归 SQL（SQLite 不支持）。"""
        out, frontier = [root], [root]
        while frontier:
            rows = self.q(
                f"SELECT id FROM folder WHERE parent_id IN "
                f"({','.join('?' * len(frontier))})", tuple(frontier))
            frontier = [r["id"] for r in rows if r["id"] not in out]
            out.extend(frontier)
        return out

    def move_folder(self, folder_id: str, parent_id: str | None) -> None:
        """移动文件夹。**防自环**：把文件夹拖进自己的子孙里会让树断掉。"""
        if parent_id:
            seen = {folder_id}
            cur = parent_id
            while cur:
                if cur in seen:
                    raise ValueError("不能把文件夹移进它自己的子文件夹")
                seen.add(cur)
                row = self.q1("SELECT parent_id FROM folder WHERE id=?", (cur,))
                cur = (row["parent_id"] if row else None)
        self.x("UPDATE folder SET parent_id=? WHERE id=?", (parent_id, folder_id))

    def reorder_folders(self, parent_id: str | None, ids: Sequence[str]) -> int:
        """把同一父节点下的文件夹按 `ids` 的顺序重排 `sort_order`，返回改了几行。

        **只改顺序，不动父子关系**——跨层级移动走 `move_folder`（菜单里的
        「移到…」），拖拽只负责同级排序。两件事分开，行为才可预期。

        ⚠️ `parent_id IS ?` 而不是 `parent_id = ?`：SQLite 里 `=` 跟 NULL
        比较恒为假，顶层文件夹（parent_id IS NULL）会一条都改不到。
        这个坑在 `create_folder` 里已经踩过一次。

        不在 `ids` 里的同级文件夹**不动**——它们的 sort_order 原样保留。
        所以调用方应当传完整的同级列表（前端手里就有），传子集也能工作，
        只是可能出现并列号（并列时按 created_at 兜底，见 `folders_with_counts`）。
        """
        n = 0
        with self._write_lock:
            for i, fid in enumerate(ids, 1):
                cur = self.conn.execute(
                    "UPDATE folder SET sort_order=? WHERE id=? AND parent_id IS ?",
                    (i, fid, parent_id),
                )
                n += cur.rowcount or 0
            self.conn.commit()
        return n

    def folders_with_counts(self) -> list[dict]:
        """文件夹树 + 每个文件夹里有多少场会话（含未分类）。"""
        rows = self.q(
            "SELECT f.id, f.name, f.parent_id, f.sort_order, "
            "  (SELECT COUNT(*) FROM session s "
            "   WHERE s.folder_id=f.id AND s.deleted_at IS NULL) AS sessions "
            "FROM folder f ORDER BY f.sort_order, f.created_at"
        )
        out = [dict(r) for r in rows]
        loose = self.q1("SELECT COUNT(*) AS c FROM session "
                        "WHERE folder_id IS NULL AND deleted_at IS NULL")
        out.append({"id": None, "name": "未分类", "parent_id": None,
                    "sort_order": 9999, "sessions": int(loose["c"] if loose else 0)})
        return out

    # ---------- 报告 ----------

    def save_report(self, session_id: str, markdown: str) -> str:
        rid = new_id("r_")
        self.x("INSERT OR REPLACE INTO report (id,session_id,markdown,created_at) "
               "VALUES (?,?,?,?)", (rid, session_id, markdown, now()))
        return rid

    # ---------- 来源 ----------

    def add_source(self, **kw: Any) -> str:
        # 注意：生成的 id 必须写回 kw，否则 cols 里的 id 列会取到 None
        kw["id"] = kw.get("id") or new_id("src_")
        kw.setdefault("fetched_at", now())
        # 显式列出列名会让 schema 的 DEFAULT 失效，缺省值在这里补
        kw.setdefault("source_type", "unknown")
        cols = ["id","session_id","url","url_normalized","domain","title","source_type",
                "engine","source_date","fetched_at","content_hash","char_count",
                "http_status","superseded_by"]
        vals = [kw.get(c) for c in cols]
        self.x(
            f"INSERT INTO source ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            vals,
        )
        return kw["id"]

    def seen_urls(self, session_id: str) -> set[str]:
        rows = self.q("SELECT url_normalized FROM source WHERE session_id=?", (session_id,))
        return {r["url_normalized"] for r in rows}

    def known_content_hashes(self) -> dict[str, str]:
        """历史全库的正文哈希 -> source_id，用于跨会话同源检测。"""
        rows = self.q(
            "SELECT content_hash, id FROM source "
            "WHERE content_hash IS NOT NULL AND superseded_by IS NULL"
        )
        return {r["content_hash"]: r["id"] for r in rows}

    # ---------- 段落：两阶段入库 ----------

    def add_chunk(self, **kw: Any) -> str:
        # 同上：生成的 id 必须写回 kw
        kw["id"] = kw.get("id") or new_id("c_")
        kw.setdefault("status", "pending")
        kw.setdefault("ingested_at", now())
        # on_topic 是 NOT NULL DEFAULT 1，显式给默认值，避免 NOT NULL 违例
        kw.setdefault("on_topic", 1)
        cols = ["id","session_id","report_id","source_id","origin","summary","content",
                "heading_path","token_count","embedding","status","source_date",
                "ingested_at","superseded_by","on_topic","topic_reason","gate_method"]
        vals = [kw.get(c) for c in cols]
        self.x(
            f"INSERT INTO chunk ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            vals,
        )
        return kw["id"]

    def mark_chunk_topic(self, session_id: str, marks: Sequence[tuple]) -> None:
        """回写段级相关性。marks: [(on_topic, reason, method, chunk_id), ...]"""
        if not marks:
            return
        with self._write_lock:
            self.conn.executemany(
                "UPDATE chunk SET on_topic=?, topic_reason=?, gate_method=? WHERE id=?",
                list(marks),
            )
            self.conn.commit()

    def mark_summaries(self, summaries: dict[str, str]) -> None:
        """回写段落概括。{chunk_id: summary}"""
        if not summaries:
            return
        with self._write_lock:
            self.conn.executemany(
                "UPDATE chunk SET summary=? WHERE id=?",
                [(v, k) for k, v in summaries.items()],
            )
            self.conn.commit()

    def set_source_nature(self, source_id: str, nature: str, reason: str) -> None:
        self.x("UPDATE source SET source_nature=?, nature_reason=? WHERE id=?",
               (nature, reason, source_id))

    def complete_chunks(self, updates: Sequence[tuple[str, str]]) -> None:
        """批量补概括句并把段落标记为可写。updates: (chunk_id, summary)

        2026-10-13 改：原先是 `SET summary=?, embedding=?, status='complete'`，
        且签名收 `token_count`——但 SQL 里从来没写过它，列表推导用 `_` 直接
        丢掉了第 4 位，所以调用方那次 `count_tokens()` 是纯白算。向量化已停，
        `embedding` 列保留（历史数据还在）但不再写入。
        """
        with self._write_lock:
            self.conn.executemany(
                "UPDATE chunk SET summary=?, status='complete' WHERE id=?",
                [(s, cid) for cid, s in updates],
            )
            self.conn.commit()

    # ⚠️ 这里原来有 `ready_chunks(session_id=None)`（2026-10-13 删）。
    # docstring 写「只取 status='complete' 的段落参与检索」，但全库**没有任何
    # 一条按向量相似度检索的查询**——`chunk_fts` 是零 MATCH 查询，
    # `cosine_topk` 是零调用。所以这个方法服务的「检索集」从来不存在，
    # 唯一的调用方是 `tests/smoke.py` 在测它自己。
    # 它跟本轮删掉的 `cosine_topk` 是一套东西的上下游。

    def supersede_chunk(self, old_id: str, new_id: str) -> None:
        """事实被推翻时标记取代，不静默覆盖。"""
        self.x("UPDATE chunk SET superseded_by=? WHERE id=?", (new_id, old_id))

    # ---------- 无用来源排除 ----------

    def ignore_source(self, source_id: str, reason: str = "") -> None:
        """标记来源为无用。

        为什么需要它：实测库里混进了一条 VRPorn.com（与固态电池毫无关系）
        却通过了 relevance 闸门。闸门是 prompt 层判断，调它风险大；
        这里做**产品层兜底**——用户自己判断，报告里提示被引用的已排除来源。
        """
        self.x("INSERT OR IGNORE INTO ignored_source (source_id, reason, ignored_at) "
               "VALUES (?,?,?)", (source_id, reason or "", now()))

    def unignore_source(self, source_id: str) -> None:
        self.x("DELETE FROM ignored_source WHERE source_id=?", (source_id,))

    def ignored_source_ids(self) -> set[str]:
        return {r["source_id"] for r in self.q("SELECT source_id FROM ignored_source")}

    # ---------- 抓取缓存（7 天） ----------

    def cache_get(self, url_norm: str, ttl_days: int = 7) -> str | None:
        row = self.q1(
            "SELECT content, fetched_at FROM fetch_cache WHERE url_normalized=?", (url_norm,)
        )
        if not row:
            return None
        from datetime import timedelta

        try:
            ft = datetime.fromisoformat(row["fetched_at"].replace("Z", "+00:00"))
        except Exception:
            return None
        if datetime.now(timezone.utc) - ft > timedelta(days=ttl_days):
            return None
        return row["content"]

    def cache_put(self, url_norm: str, url: str, content: str,
                  content_hash: str, http_status: int) -> None:
        self.x(
            "INSERT OR REPLACE INTO fetch_cache "
            "(url_normalized,url,content,content_hash,fetched_at,http_status) "
            "VALUES (?,?,?,?,?,?)",
            (url_norm, url, content, content_hash, now(), http_status),
        )

    # ---------- 记账 ----------

    def log_llm_call(self, session_id, stage, model, input_tokens, output_tokens,
                     latency_ms=None, cache_read_tokens=0,
                     cache_creation_tokens=0, est_input_tokens=None,
                     status="ok", error=None) -> None:
        """记一次调用。

        input_tokens / output_tokens 必须是 API 报上来的真实值。
        原来这里记的是 _rough_tokens() 的本地估算——估算够用来看成本量级，
        但判断不了缓存命中，而那正是 write 环节最需要看的数。

        est_input_tokens 把估算值留一份，**仅供对比偏差，没有任何读取方**
        （`session_cost()` 与 max_tokens 硬条件都走真实 input_tokens）。
        2026-10-08 复核：这个字段**不能**拿 est/input_tokens 比——
        input_tokens 已经被缓存扣减过了，命中越多这个比值越大
        （write_section 实测 est/inp 均值 2.74，单条最高 8.98，
        而 est/(input+cache_read) 才是稳定的 1.54）。
        偏差来源是 `_rough_tokens` 用 tiktoken cl100k_base（OpenAI 的分词器）
        去估 MiniMax 的计费，中文系统性偏高约 1.5 倍。
        保留它只为留个偏差基线，不要拿它做判据。

        **status / error（2026-10-13 加）**：原来失败调用根本不会被记
        （`_record` 只在成功路径上调用），504/429 在表里查无实据。
        失败时照常记一行，`status='error'`、`error='类型: 消息'`，
        token 全 0、只有 `est_input_tokens` 留下这次尝试的规模。
        """
        self.x(
            "INSERT INTO llm_call (id,session_id,stage,model,input_tokens,"
            "output_tokens,latency_ms,cache_read_tokens,cache_creation_tokens,"
            "est_input_tokens,status,error,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id("l_"), session_id, stage, model, input_tokens,
             output_tokens, latency_ms, cache_read_tokens or 0,
             cache_creation_tokens or 0, est_input_tokens,
             status or "ok", (error or None), now()),
        )

    def cache_stats(self, session_id: str) -> dict:
        """一次研究的缓存命中情况。测试期最常查的一行。"""
        r = self.q1(
            "SELECT COUNT(*) n, "
            "  COALESCE(SUM(cache_read_tokens),0) read_tok, "
            "  COALESCE(SUM(cache_creation_tokens),0) create_tok, "
            "  COALESCE(SUM(input_tokens),0) uncached_in, "
            "  COALESCE(SUM(est_input_tokens),0) est_in "
            "FROM llm_call WHERE session_id=?", (session_id,))
        n = r["n"] or 0
        read, create = r["read_tok"] or 0, r["create_tok"] or 0
        total = read + create + r["uncached_in"] or 0
        return {"calls": n, "cache_read_tokens": read,
                "cache_creation_tokens": create, "uncached_input_tokens": r["uncached_in"],
                "cache_hit_ratio": round(read / total, 4) if total else 0.0}

    def save_write_trace(self, session_id: str, spec=None, tasks=None,
                         audit=None, stats=None, audit_history=None) -> None:
        """把 write 子图的中间产物落一份，供测试期检查。"""
        self.x(
            "INSERT INTO write_trace (session_id,spec_json,tasks_json,audit_json,"
            "stats_json,audit_history_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "  spec_json=COALESCE(excluded.spec_json,write_trace.spec_json), "
            "  tasks_json=COALESCE(excluded.tasks_json,write_trace.tasks_json), "
            "  audit_json=COALESCE(excluded.audit_json,write_trace.audit_json), "
            "  stats_json=COALESCE(excluded.stats_json,write_trace.stats_json), "
            "  audit_history_json=COALESCE(excluded.audit_history_json,"
            "                         write_trace.audit_history_json), "
            "  updated_at=excluded.updated_at",
            (session_id,
             _jd(spec) if spec is not None else None,
             _jd(tasks) if tasks is not None else None,
             _jd(audit) if audit is not None else None,
             _jd(stats) if stats is not None else None,
             _jd(audit_history) if audit_history is not None else None,
             now(), now()),
        )

    def write_trace(self, session_id: str) -> dict:
        r = self.q1("SELECT * FROM write_trace WHERE session_id=?", (session_id,))
        if not r:
            return {}
        keys = r.keys()
        return {"spec": _jl(r["spec_json"]), "tasks": _jl(r["tasks_json"]),
                "audit": _jl(r["audit_json"]), "stats": _jl(r["stats_json"]),
                "audit_history": _jl(r["audit_history_json"])
                if "audit_history_json" in keys else []}

    def save_run_trace(self, session_id: str, rnd: int, kind: str,
                       detail: dict) -> None:
        """落一条迭代决策。跑完能 SQL 查，不用翻 stdout。

        kind: assess（这一轮判断了什么、搜什么）/ retrieve（实际搜了什么）/
              stop（为什么停）。写失败不该影响研究，所以静默吞掉。
        """
        if not session_id:
            return
        try:
            self.x(
                "INSERT INTO run_trace (session_id, round, at, kind, detail_json) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(session_id, round, kind) DO UPDATE SET "
                "  at=excluded.at, detail_json=excluded.detail_json",
                (session_id, rnd, now(), kind, _jd(detail)),
            )
        except Exception:
            pass

    def run_trace(self, session_id: str) -> list[dict]:
        rows = self.q(
            "SELECT round, kind, at, detail_json FROM run_trace "
            "WHERE session_id=? ORDER BY round, kind", (session_id,))
        out = []
        for r in rows:
            d = _jl(r["detail_json"])
            out.append({"round": r["round"], "kind": r["kind"],
                        "at": r["at"], **(d if isinstance(d, dict) else {})})
        return out

    def save_node_timing(self, session_id: str, node: str, ms: float,
                         rnd: int = 0, **extra) -> None:
        """记一个节点的墙钟耗时。

        `llm_call` 只有 LLM 调用的耗时，缺了两块：检索/抓取的网络 IO，
        以及节点里不调模型的准备工作。要做成本优化就得知道**时间花在哪**，
        所以补这一层。

        节点名编进 kind 而不是塞进 detail：run_trace 的主键是
        (session_id, round, kind)，同一轮的多个节点用 detail 会互相覆盖。

        ⚠️ **`rnd` 的语义是「进入这个节点时，已经完成了几轮」**
        （`_timed_node` 直接取 `state["round"]`）。而 `retrieve` 事件的
        `round` 是「本次是第几轮」（`node_retrieve` 里 `state["round"] + 1`）。
        **两个都对，但口径不同**，别当成同一个东西：

            node:extract  round=1,2,3   ← 第 1/2/3 轮检索**之后**执行
            node:write    round=3       ← 完成 3 轮之后收尾
            node:retrieve round=0,1,2   ← 第 1 次检索**执行时**已完成 0 轮
            retrieve 事件 round=1,2,3   ← 同一批检索，标记为第 1/2/3 轮

        `retrieve` 看起来「差 1」，是因为它是唯一**执行中就推进 round** 的节点。
        **别去统一它们**：给 `node:*` 整体 +1 会把 `node:extract` 从 1,2,3
        改成 2,3,4——把本来对的改错了。

        画「第 N 轮」图表时**一律用事件口径**（`assess` / `retrieve`），
        节点耗时只用来对齐耗时，不单独当轮次用。
        """
        self.save_run_trace(session_id, rnd, f"node:{node}",
                            {"node": node, "ms": round(ms), **extra})

    def session_cost(self, session_id: str) -> int:
        row = self.q1(
            "SELECT COALESCE(SUM(input_tokens+output_tokens),0) AS t "
            "FROM llm_call WHERE session_id=?",
            (session_id,),
        )
        return int(row["t"]) if row else 0


_repo: Repo | None = None
_repo_lock = threading.Lock()


def get_repo(path: str | Path | None = None) -> Repo:
    global _repo
    if _repo is None:
        with _repo_lock:
            if _repo is None:
                _repo = Repo(path or os.environ.get("RA_DB_PATH", DEFAULT_DB))
                _repo.init_schema()
    return _repo
