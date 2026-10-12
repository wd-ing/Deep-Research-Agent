"""FastAPI 服务：会话 API + SSE 事件流 + 人工审批。

数据库与检索同进程（P2 约束），绑 0.0.0.0 为将来的手机访问留口子。
"""

from __future__ import annotations

import asyncio
import json
import queue
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .db.repo import get_repo
from .graph.runner import get_graph

REPO_ROOT = Path(__file__).resolve().parents[1]
app = FastAPI(title="Research Agent", version="0.1.0")

_graph = None


def graph():
    global _graph
    if _graph is None:
        _graph = get_graph()
    return _graph


async def agraph():
    """异步主图：astream 进度推送用。

    ⚠️ 必须和 `graph()` 分开：SqliteSaver 只支持同步方法，
    `astream()` 会直接抛 NotImplementedError（2026-10-11 实测）。
    两条路径写同一个 checkpoints.db，所以能接力。
    """
    from .graph.runner import get_agraph
    return await get_agraph()


# 订阅者：thread_id -> 队列
_subs: dict[str: set[queue.Queue]] = {}

# 每个 session 已经推过的事件指纹集合。
# `events` 在 state 里是累加器，run 与 approve 是两段独立推流，
# 不记就会在审批后重发旧事件；用**内容指纹**而不是索引游标——
# 游标会被跨节点的累计长度带偏，把后续增量整段吞掉（2026-10-12 修）。
_last_seen: dict[str, set] = {}


def _event_key(ev: dict) -> str:
    """事件指纹：同一个事件重复推时能被认出来。"""
    try:
        return json.dumps(ev, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return repr(ev)


def _emit(thread_id: str, event: dict) -> None:
    for q in _subs.get(thread_id, ()):
        try:
            q.put_nowait(event)
        except queue.Full:
            pass


# ---------------------------------------------------------------- API

class CreateIn(BaseModel):
    question: str = Field(min_length=1)
    title: str | None = None


class ApproveIn(BaseModel):
    approved: bool = True
    approved_recalled_ids: list[str] = Field(default_factory=list)


@app.post("/api/sessions")
def create_session(body: CreateIn) -> dict:
    repo = get_repo()
    title = body.title or body.question[:40]
    sid = repo.create_session(title, body.question)
    return {"session_id": sid, "thread_id": sid}


@app.get("/api/sessions")
def list_sessions(
    archived: str | None = None,
    deleted: str | None = None,
    folder_id: str | None = None,
    q: str | None = None,
) -> list[dict]:
    """列会话。

    ⚠️ **`deleted_at IS NULL` 是硬过滤**（2026-10-12 加）——
    软删除的会话默认不出现，回收站视图才看得见。
    漏了这一句会出现「删了还在列表里」。

    `folder_id` 支持传空串表示「未分类」（folder_id IS NULL）。
    """
    import json as _json
    repo = get_repo()
    where, params = ["1=1"], []
    if deleted == "1":
        where.append("deleted_at IS NOT NULL")
    else:
        where.append("deleted_at IS NULL")
    if archived == "1":
        where.append("archived_at IS NOT NULL")
    elif archived == "0":
        where.append("archived_at IS NULL")
    if folder_id == "":
        where.append("folder_id IS NULL")
    elif folder_id:
        where.append("folder_id=?")
        params.append(folder_id)
    if q:
        where.append("(title LIKE ? OR question LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    order = "archived_at DESC, created_at DESC" if archived == "1" else "created_at DESC"
    rows = repo.q(
        "SELECT id,title,question,status,cost_tokens,created_at,archived_at,"
        "       deleted_at,folder_id,summary_json FROM session "
        f"WHERE {' AND '.join(where)} ORDER BY {order}", tuple(params))
    out = []
    for r in rows:
        d = dict(r)
        raw = d.pop("summary_json", None)
        try:
            d["summary"] = _json.loads(raw) if raw else None
        except Exception:
            d["summary"] = None
        out.append(d)
    return out


@app.patch("/api/sessions/{sid}")
def patch_session(sid: str, body: dict) -> dict:
    """改名 / 移文件夹 / 归档或取消归档。字段任选，都可省略。"""
    repo = get_repo()
    if not repo.q1("SELECT 1 FROM session WHERE id=?", (sid,)):
        raise HTTPException(404, "会话不存在")
    fields = {k: body[k] for k in ("title", "folder_id", "archived") if k in body}
    if not fields:
        raise HTTPException(400, "没有可改的字段")
    repo.update_session(sid, **fields)
    return {"ok": True, "changed": sorted(fields)}


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str) -> dict:
    """移到回收站（软删除）。报告 / 来源 / 段**一律留着**。"""
    repo = get_repo()
    s = repo.q1("SELECT deleted_at FROM session WHERE id=?", (sid,))
    if not s:
        raise HTTPException(404, "会话不存在")
    if s["deleted_at"]:
        return {"ok": True, "already": "in_trash"}
    n_src = repo.q1("SELECT COUNT(*) AS c FROM source WHERE session_id=?", (sid,))["c"]
    n_seg = repo.q1("SELECT COUNT(*) AS c FROM chunk WHERE session_id=?", (sid,))["c"]
    has_rep = bool(repo.q1("SELECT 1 FROM report WHERE session_id=?", (sid,)))
    repo.soft_delete_session(sid)
    return {"ok": True, "moved_to": "trash",
            "will_remove": {"report": has_rep, "sources": n_src, "segments": n_seg},
            "note": "可在回收站恢复"}


@app.post("/api/sessions/{sid}/restore")
def restore_session(sid: str) -> dict:
    repo = get_repo()
    if not repo.q1("SELECT 1 FROM session WHERE id=?", (sid,)):
        raise HTTPException(404, "会话不存在")
    repo.restore_session(sid)
    return {"ok": True}


@app.delete("/api/sessions/{sid}/purge")
def purge_session(sid: str) -> dict:
    """真删，不可恢复。回收站「清空」用。"""
    repo = get_repo()
    if not repo.q1("SELECT 1 FROM session WHERE id=?", (sid,)):
        raise HTTPException(404, "会话不存在")
    repo.purge_session(sid)
    return {"ok": True, "purged": True,
            "note": "报告、来源、段落已随外键级联删除，不可恢复"}


@app.get("/api/trash")
def list_trash() -> list[dict]:
    return list_sessions(deleted="1")


@app.get("/api/folders")
def list_folders() -> list[dict]:
    """文件夹树 + 每层会话数，末尾附「未分类」。"""
    return get_repo().folders_with_counts()


def _cited_counts(repo, sid: str) -> dict[str, int]:
    """某场报告里，每个来源被引了多少处 → {source_id: 处数}。

    **引用关系只存在于报告正文的 `[S3·5]` 标记里，库里没存**
    （全库 14 张表没有 citation / ref 之类的关系表），只能数 markdown。

    段号 → source_id 的映射**必须问 `EvidenceTools`**：报告里的 `S3` 是
    按 rowid 排的显示编号，不是 `source.id`。自己另算一套必然对不上
    （HANDOVER 坑 8）。来源库引用数和报告提示条都走这里，两边口径一致。
    """
    import re
    from collections import Counter

    from .tools.evidence import EvidenceTools

    rep = repo.q1("SELECT markdown FROM report WHERE session_id=?", (sid,))
    if not rep or not (rep["markdown"] or ""):
        return {}
    try:
        tools = EvidenceTools(repo, sid)
    except Exception:
        return {}          # 索引建不起来就当没引用，不要整页 500
    out: dict[str, int] = {}
    for ref, n in Counter(re.findall(r"\[(S\d+)·", rep["markdown"])).items():
        real = tools._source_id(ref)
        if real:
            out[real] = out.get(real, 0) + n
    return out


@app.get("/api/sources/{src_id}")
def get_source(src_id: str) -> dict:
    """单个来源 + 它的段落。点来源库里的某一条看它到底说了什么。

    段号（`S3·5`）**必须由 `EvidenceTools` 生成**，不能自己按 rowid 数——
    与报告正文里的编号不一致的话，点开看到的段就是另一段。
    """
    from .tools.evidence import EvidenceTools

    repo = get_repo()
    row = repo.q1(
        "SELECT s.*, COALESCE(NULLIF(se.title,''), se.question) AS session_title, "
        "       (SELECT COUNT(*) FROM ignored_source ig WHERE ig.source_id=s.id) AS ignored, "
        "       (SELECT ig.reason FROM ignored_source ig WHERE ig.source_id=s.id) "
        "         AS ignore_reason, "
        "       (SELECT ig.ignored_at FROM ignored_source ig WHERE ig.source_id=s.id) "
        "         AS ignored_at "
        "FROM source s JOIN session se ON se.id = s.session_id WHERE s.id=?",
        (src_id,),
    )
    if not row:
        raise HTTPException(404, "来源不存在")
    out = dict(row)
    out["nature"] = out.get("source_nature") or "unknown"
    out["nature_label"] = NATURE_LABELS.get(out["nature"], out["nature"])
    out["ignored"] = bool(out.pop("ignored"))

    try:
        tools = EvidenceTools(repo, out["session_id"])
    except Exception as e:
        raise HTTPException(500, f"段号索引构建失败：{e}")

    chunks = repo.q(
        "SELECT id, heading_path, summary, content, token_count, on_topic "
        "FROM chunk WHERE source_id=? ORDER BY rowid", (src_id,),
    )
    segs = []
    for c in chunks:
        d = dict(c)
        d["ref"] = tools._ref(d["id"])      # chunk_id → 报告里用的段号
        # 正文可能很长，详情页只给前 1200 字，完整原文用 /seg 端点按段取
        d["content"] = (d["content"] or "")[:1200]
        segs.append(d)
    out["segments"] = segs
    return out


# 来源性质的规范取值。**写在服务端**而不是让前端各写一份中英文对照——
# 词表是提示词（prompts.SOURCE_NATURE）定的，只有这一处该知道它。
NATURE_LABELS = {
    "first_party": "一手",
    "research": "研究",
    "media": "媒体",
    "community": "社区",
    "aggregator": "聚合",
    "unknown": "未判",
}
NATURE_ORDER = ["first_party", "research", "media", "community", "aggregator", "unknown"]


@app.get("/api/sources")
def list_sources(q: str = "", session_id: str = "") -> dict:
    """来源库：全部场次收集到的来源 + 每场的引用数。

    **引用数必须复用 `EvidenceTools` 的段号方案**——
    报告里的 `[S2·1]` 里那个 `S2` 是**显示编号**（按 session 内 rowid 排），
    不是 `source.id`。自己另算一套必然与点开抽屉看到的对不上
    （这个错 HANDOVER 的坑 8 记过一次）。所以这里直接问 `EvidenceTools`。

    没有 `source.cited_count` 这类字段可读——引用关系只存在于**报告正文**
    的标记里，库里没存，所以只能从 markdown 数（见 `_cited_counts`）。

    过滤条件与别处保持一致：`superseded_by IS NULL`（同源被采纳的那条）、
    所属会话不在回收站。
    """
    repo = get_repo()
    params: list = []
    where = ["s.superseded_by IS NULL", "se.deleted_at IS NULL"]
    if session_id:
        where.append("s.session_id=?")
        params.append(session_id)
    if q:
        where.append("(s.title LIKE ? OR s.domain LIKE ? OR s.url LIKE ?)")
        params += [f"%{q}%"] * 3

    rows = repo.q(
        "SELECT s.id, s.session_id, s.title, s.url, s.domain, s.source_date,"
        "       s.fetched_at, s.source_type, s.source_nature, s.nature_reason,"
        "       s.char_count, s.http_status, s.on_topic,"
        "       (SELECT COUNT(*) FROM chunk c WHERE c.source_id=s.id) AS segments,"
        "       (SELECT COALESCE(SUM(c.token_count),0) FROM chunk c"
        "         WHERE c.source_id=s.id) AS tokens,"
        "       (SELECT COUNT(*) FROM ignored_source ig WHERE ig.source_id=s.id)"
        "         AS ignored,"
        "       (SELECT ig.reason FROM ignored_source ig WHERE ig.source_id=s.id)"
        "         AS ignore_reason,"
        "       (SELECT ig.ignored_at FROM ignored_source ig WHERE ig.source_id=s.id)"
        "         AS ignored_at,"
        "       COALESCE(NULLIF(se.title,''), se.question) AS session_title,"
        "       se.created_at AS session_created_at "
        "FROM source s JOIN session se ON se.id = s.session_id "
        f"WHERE {' AND '.join(where)} ORDER BY se.created_at DESC, s.rowid",
        tuple(params),
    )
    out = [dict(r) for r in rows]

    # ---- 引用数：从报告正文数 ----
    cites: dict[str, int] = {}
    for sid in {r["session_id"] for r in out}:
        for real, n in _cited_counts(repo, sid).items():
            cites[real] = cites.get(real, 0) + n

    for r in out:
        # 未判 = NULL 与 unknown 归一组：前者是没判，后者是判不出来，
        # 对用户是同一件事——「这条的出处性质还不知道」
        nature = r["source_nature"] or "unknown"
        r["nature"] = nature
        r["nature_label"] = NATURE_LABELS.get(nature, nature)
        r["citations"] = cites.get(r["id"], 0)
        r["ignored"] = bool(r.pop("ignored"))

    total_cites = sum(r["citations"] for r in out)
    return {
        "sources": out,
        "total": len(out),
        "segments": sum(r["segments"] for r in out),
        "citations": total_cites,
        "by_nature": {k: sum(1 for r in out if r["nature"] == k) for k in NATURE_ORDER},
        "nature_order": NATURE_ORDER,
        "nature_labels": NATURE_LABELS,
    }


class IgnoreIn(BaseModel):
    reason: str = ""


@app.post("/api/sources/{src_id}/ignore")
def ignore_source(src_id: str, body: IgnoreIn) -> dict:
    """标记来源为无用。**幂等**——`source_id` 是主键，重复标记不报错也不覆盖原因。

    为什么不改数据：报告已经写完了，排除一个来源**不会回头改写报告**。
    这条接口只负责记录「用户认为它不可信」，真正要紧的是让这个判断
    出现在用户还会再看的地方（来源库 / 引用抽屉 / 报告顶部）。
    """
    repo = get_repo()
    if not repo.q1("SELECT 1 FROM source WHERE id=?", (src_id,)):
        raise HTTPException(404, "来源不存在")
    repo.ignore_source(src_id, body.reason)
    cited = _cited_counts(repo, repo.q1("SELECT session_id FROM source WHERE id=?",
                                        (src_id,))["session_id"]).get(src_id, 0)
    return {
        "ok": True,
        "ignored": True,
        "reason": body.reason,
        "citations": cited,
        # 这条来源已经进了写完的报告——前端据此告诉用户「报告会挂提示条」，
        # 别让他以为标记完就等于报告里没有它了。
        "note": ("这场研究的报告已经引用了它，报告顶部会出现提示条。"
                 if cited else "已标记，不影响任何已完成的报告。"),
    }


@app.post("/api/sources/{src_id}/unignore")
def unignore_source(src_id: str) -> dict:
    """取消排除。**恢复是非破坏性的**，不加确认框。"""
    repo = get_repo()
    repo.unignore_source(src_id)
    return {"ok": True, "ignored": False}


class FolderIn(BaseModel):
    name: str = Field(min_length=1)
    parent_id: str | None = None


@app.post("/api/folders")
def create_folder(body: FolderIn) -> dict:
    return {"folder_id": get_repo().create_folder(body.name, body.parent_id)}


class FolderPatch(BaseModel):
    name: str | None = None
    parent_id: str | None = None


@app.patch("/api/folders/{fid}")
def patch_folder(fid: str, body: FolderPatch) -> dict:
    repo = get_repo()
    if not repo.q1("SELECT 1 FROM folder WHERE id=?", (fid,)):
        raise HTTPException(404, "文件夹不存在")
    try:
        if body.name is not None:
            repo.rename_folder(fid, body.name)
        if body.parent_id is not None or "parent_id" in body.model_fields_set:
            repo.move_folder(fid, body.parent_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.delete("/api/folders/{fid}")
def delete_folder(fid: str) -> dict:
    """删文件夹（连同子文件夹）。里面的会话移到「未分类」，不删。"""
    repo = get_repo()
    if not repo.q1("SELECT 1 FROM folder WHERE id=?", (fid,)):
        raise HTTPException(404, "文件夹不存在")
    moved = repo.delete_folder(fid)
    return {"ok": True, "sessions_moved": moved,
            "note": "文件夹内的会话已移到「未分类」" if moved else ""}


def _ignored_citations(repo, sid: str) -> list[dict]:
    """本场报告里**被引用过、但已被用户标记排除**的来源。

    报告顶部那条提示条的唯一数据源，也是整个阶段 5 存在的理由：
    排除发生在报告写完之后，**不会回头改写它**，所以用户必须知道
    报告里有一部分材料的来源后来被他标成了不可信。这是给不可逆信息兜底。

    引用数仍走 `_cited_counts`（数 markdown + EvidenceTools 映射），
    与来源库的引用数同源，两边不会打架。
    """
    cites = _cited_counts(repo, sid)
    if not cites:
        return []
    hit = [cid for cid in cites if repo.q1(
        "SELECT 1 FROM ignored_source WHERE source_id=?", (cid,))]
    if not hit:
        return []
    rows = repo.q(
        f"SELECT id, domain, title FROM source WHERE id IN ({','.join('?' * len(hit))})",
        hit,
    )
    # 按被引次数从多到少——提示条只显示前几个时，先显示最该看的
    return sorted(
        [{"id": r["id"], "domain": r["domain"], "title": r["title"],
          "citations": cites[r["id"]],
          "reason": repo.q1("SELECT reason FROM ignored_source WHERE source_id=?",
                            (r["id"],))["reason"] or ""}
         for r in rows],
        key=lambda x: -x["citations"],
    )


@app.get("/api/sessions/{sid}")
def get_session(sid: str) -> dict:
    repo = get_repo()
    s = repo.q1("SELECT * FROM session WHERE id=?", (sid,))
    if not s:
        raise HTTPException(404, "会话不存在")
    rep = repo.q1("SELECT markdown,created_at FROM report WHERE session_id=?", (sid,))
    srcs = repo.q("SELECT * FROM source WHERE session_id=? AND superseded_by IS NULL", (sid,))
    out = dict(s)
    out["report"] = rep["markdown"] if rep else None
    out["sources"] = [dict(r) for r in srcs]
    out["topics"] = repo.topics_of(sid)
    out["open_contradictions"] = [dict(r) for r in repo.open_contradictions(sid)]
    out["ignored_citations"] = _ignored_citations(repo, sid)
    return out


@app.post("/api/sessions/{sid}/archive")
def archive(sid: str) -> dict:
    """显式归档。只有归档后的会话才进入历史检索。"""
    repo = get_repo()
    repo.archive_session(sid)
    return {"ok": True, "note": "已归档，将参与后续会话的历史召回"}


@app.post("/api/sessions/{sid}/topics")
def set_topics(sid: str, body: dict) -> dict:
    repo = get_repo()
    repo.set_topics(sid, body.get("topics") or [])
    return {"topics": repo.topics_of(sid)}


@app.get("/api/sessions/{sid}/chunks")
def list_chunks(sid: str, limit: int = 200) -> list[dict]:
    repo = get_repo()
    rows = repo.q(
        "SELECT c.id,c.summary,c.heading_path,c.token_count,c.status,"
        "       s.url,s.domain,s.source_type,s.engine,s.source_date,s.title "
        "FROM chunk c LEFT JOIN source s ON s.id=c.source_id "
        "WHERE c.session_id=? ORDER BY c.ingested_at LIMIT ?",
        (sid, limit),
    )
    return [dict(r) for r in rows]


@app.get("/api/sessions/{sid}/approval")
def approval(sid: str) -> dict:
    """当前待审批的框架（没有就返回 waiting=False）。

    审批 payload 是 `node_await_approval` 组好后交给 `interrupt()` 的，
    checkpointer 存着，**只能从 graph state 里读**——DB 里没有这张表。

    里面有什么：最终框架、每节的材料支撑说明、被修订过没有、
    这份框架可不可信（材料够了 vs 撞了上限只是尽力而为）、当前材料统计。
    前端必须把这些都显示出来——否则用户批的是一份未经材料检验的框架。
    """
    st = graph().get_state({"configurable": {"thread_id": sid}})
    ints = getattr(st, "interrupts", None) or getattr(st, "__interrupt__", None) or []
    if not ints:
        return {"waiting": False,
                "note": "当前没有待审批的节点（可能还没跑到审批点，或已经批过了）"}
    first = ints[0]
    payload = getattr(first, "value", None)
    if not isinstance(payload, dict):
        payload = {"raw": str(payload)}
    return {"waiting": True, "approval": payload}


@app.get("/api/sessions/{sid}/seg/{ref}")
def read_segment(sid: str, ref: str) -> dict:
    """按段号取原文（`S1·5` 形式），报告里的 `[S1·5]` 点击就走这里。

    **段号方案必须复用 `EvidenceTools` 那一套**——来源按 rowid 编 S1 S2、
    段按来源内 rowid 编 `S1·5`。`node_write` 和它是同一套编号，
    这里自己另算一套就会出现「点开是另一个段」的错位。
    """
    from .tools.evidence import EvidenceTools

    repo = get_repo()
    if not repo.q1("SELECT 1 FROM session WHERE id=?", (sid,)):
        raise HTTPException(404, "会话不存在")
    try:
        tools = EvidenceTools(repo, sid)
    except Exception as e:
        raise HTTPException(500, f"段号索引构建失败：{e}")
    data = tools._read_segments([ref])
    # 返回形状是 `{"段落": [...], "没找到": [...]}`，
    # 查无此段时字典仍然非空——所以要判「段落」空不空，不能判 data 空不空。
    if not data.get("段落"):
        raise HTTPException(404, f"段号 {ref} 不存在")
    # ⚠️ 段落里的 `来源号` 是**显示编号**（S2），不是 `source.id`（src_xxx）。
    # 让前端拿它去比 `source.id` 必然匹配不上——编号方案只有一处是对的
    # （见 `EvidenceTools`）。所以来源元数据在这里拼好再给前端。
    sid_ref = data["段落"][0].get("来源号") or ""
    if sid_ref:
        real = tools._source_id(sid_ref)
        if real:
            row = repo.q1(
                "SELECT id,title,domain,url,source_date,source_nature "
                "FROM source WHERE id=?", (real,))
            if row:
                meta = dict(row)
                ig = repo.q1("SELECT reason FROM ignored_source WHERE source_id=?",
                             (real,))
                # 引用抽屉要标出「这条来自已被排除的来源」——
                # 用户点开某段引用时，是最后一次能提醒他这段材料不可信的时机
                meta["ignored"] = bool(ig)
                meta["ignore_reason"] = (ig["reason"] or "") if ig else ""
                data["段落"][0]["来源"] = meta
    return data


@app.get("/api/sessions/{sid}/segments")
def read_segments(sid: str, refs: str) -> dict:
    """一次取多段（`refs=S1·5,S2·1`），给前端做批量展开用。"""
    from .tools.evidence import EvidenceTools

    repo = get_repo()
    if not repo.q1("SELECT 1 FROM session WHERE id=?", (sid,)):
        raise HTTPException(404, "会话不存在")
    wanted = [r.strip() for r in (refs or "").split(",") if r.strip()][:50]
    if not wanted:
        raise HTTPException(400, "refs 为空")
    try:
        return EvidenceTools(repo, sid)._read_segments(wanted)
    except Exception as e:
        raise HTTPException(500, f"读取失败：{e}")


@app.get("/api/topics")
def topics() -> list[dict]:
    repo = get_repo()
    rows = repo.q(
        "SELECT t.name, COUNT(st.session_id) AS sessions FROM topic t "
        "JOIN session_topic st ON st.topic_id=t.id GROUP BY t.id ORDER BY sessions DESC"
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 运行

@app.post("/api/sessions/{sid}/run")
async def run_session(sid: str, streaming: bool = True) -> dict:
    # ⚠️ 必须是 `async def`（2026-10-11 修）：同步端点会被 FastAPI 丢进线程池，
    # 那里**没有运行中的事件循环**，`asyncio.create_task` 直接
    # `RuntimeError: no running event loop`。改成 async 端点后
    # FastAPI 给它真正的 loop，create_task 才成立。
    # `graph().invoke()` 那条回退路径仍然要 to_thread（它是同步阻塞的）。
    """启动研究。assess 跑完会停在 await_approval 等你审批。

    `streaming=true` 走 `astream`：节点跑完就把进度推到 SSE。
    **默认开启**——不然前端面对的就是 25 分钟黑箱。

    出问题可以 `?streaming=false` 退回原来那条 `invoke` 路径：
    那条路径行为完全没变，只是收不到中间事件，仍然会在结束时推 `done`。
    """
    repo = get_repo()
    s = repo.q1("SELECT * FROM session WHERE id=?", (sid,))
    if not s:
        raise HTTPException(404, "会话不存在")

    _subs.setdefault(sid, set())
    q: queue.Queue = queue.Queue(maxsize=2000)
    _subs[sid].add(q)

    inp = {"session_id": sid, "question": s["question"],
           "title": s["title"], "elapsed_s": 0.0, "cost_tokens": 0}
    cfg = {"configurable": {"thread_id": sid}}

    def _finish(out: dict) -> None:
        _emit(sid, {"kind": "done", "state": {
            "report": (out.get("report_markdown") or "")[:2000],
            "report_chars": len(out.get("report_markdown") or ""),
            "round": out.get("round"), "url_count": out.get("url_count"),
            "cost_tokens": out.get("cost_tokens"),
        }})

    async def _task_stream() -> None:
        """astream 路径：每个节点更新到达就推事件。

        **`events` 是累加器**（`state.py` 里 `Annotated[list, operator.add]`），
        所以每次拿到的都是**迄今全部**事件——要去重，
        否则同一个事件会被反复推，前端看到一堆重复。
        """
        seen_keys: set[str] = set()
        try:
            async for update in (await agraph()).astream(inp, config=cfg, stream_mode="updates"):
                for node, delta in (update or {}).items():
                    if not isinstance(delta, dict):
                        continue
                    _emit(sid, {"kind": "node", "node": node})
                    # ⚠️ 用**内容指纹**去重，不用索引游标（2026-10-12 修）。
                    # `events` 是累加器，每个 delta 都是「迄今全部」；
                    # 多节点轮流推时游标会被推到比某节点实际长度更靠后，
                    # 那之后的增量被整段吞掉——症状是「node 有 retrieve，
                    # 但它的事件一条都没推」。
                    for e in delta.get("events") or []:
                        k = _event_key(e)
                        if k in seen_keys:
                            continue
                        seen_keys.add(k)
                        _emit(sid, {"kind": "progress", "node": node, "event": e})
            # 收尾：astream 迭代完拿不到最终 state，用 checkpointer 读
            st = graph().get_state(cfg)
            _finish(dict(st.values or {}))
        except Exception as e:
            _emit(sid, {"kind": "error", "error": f"{type(e).__name__}: {e}"})
        finally:
            _subs.get(sid, set()).discard(q)
            _last_seen[sid] = seen_keys

    def _task_plain() -> None:
        """原来的 invoke 路径，保留作回退。"""
        try:
            _finish(graph().invoke(inp, config=cfg))
        except Exception as e:
            _emit(sid, {"kind": "error", "error": f"{type(e).__name__}: {e}"})
        finally:
            _subs.get(sid, set()).discard(q)

    if streaming:
        # 思考增量：平台会流式吐 reasoning_content（实测 174 块），
        # 推给前端就能实时显示「AI 正在想什么」。
        # 用 contextvar 注册，所以并发子 agent 要靠 submit_run_in_context 继承
        # （provider.py 里写了同一个坑：C1-j）。
        from .llm.provider import delta_scope

        def _on_delta(session_id, stage, kind, chunk):
            # 增量很碎：**实测平均 7.2 字符/块**（probe_outline 一口气 2,336 块
            # 才 16,892 字）。一块一帧会把 SSE 队列冲爆（maxsize=2000），
            # 前端也被刷屏。所以后端先攒，攒够再推。
            #
            # 思考与正文**分开攒、分开推**（2026-10-11）：
            # 思考是「AI 在想什么」，正文是「这节要交付的东西」——
            # 语义不同，混在一个流里前端就分不清哪块是给用户看的成品。
            # key 多带一段 kind，两条流各自独立计时。
            buf.setdefault((session_id or sid, stage, kind), []).append(chunk)

        async def _with_delta(coro):
            with delta_scope(_on_delta):
                await coro

        # 攒够 ~40 字或 0.25 秒就推一次。40 字是「够读一句」的下限，
        # 0.25 秒保证长思考时前端不会干等太久。
        buf: dict[tuple, list[str]] = {}
        last_push = time.time()

        # 正文攒够的字数比思考多：思考要「跟得上」，正文要「读得顺」。
        THRESH = {"reasoning": 40, "text": 120}

        async def _flush_loop() -> None:
            nonlocal last_push
            while True:
                await asyncio.sleep(0.25)
                now = time.time()
                for (s_id, stage, kind), parts in list(buf.items()):
                    text = "".join(parts)
                    # 攒够了或超时了就推；推完清空
                    if len(text) >= THRESH.get(kind, 40) or now - last_push >= 0.25:
                        if text:
                            _emit(s_id, {"kind": "thinking" if kind == "reasoning"
                                          else "draft",
                                          "stage": stage, "delta": text})
                        parts.clear()
                last_push = now

        flusher = asyncio.create_task(_flush_loop())

        async def _task_stream_delta() -> None:
            try:
                await _with_delta(_task_stream())
            finally:
                # 收尾前把没推出去的残余推掉，别让最后几句话丢了
                for (s_id, stage, kind), parts in buf.items():
                    text = "".join(parts)
                    if text:
                        _emit(s_id, {"kind": "thinking" if kind == "reasoning"
                                      else "draft", "stage": stage,
                                      "delta": text, "final": True})
                buf.clear()

        asyncio.create_task(_task_stream_delta())
    else:
        asyncio.create_task(asyncio.to_thread(_task_plain))
    return {"ok": True, "streaming": streaming,
            "note": "已启动，事件流见 /api/stream/{sid}"}


@app.post("/api/sessions/{sid}/approve")
async def approve(sid: str, body: ApproveIn, streaming: bool = True) -> dict:
    # ⚠️ 同样必须是 async（2026-10-11 修）：我第一版只把 run_session 改成 async，
    # approve 漏了，结果点了批准直接 500 —— **同一个错犯两次**。
    # 凡是端点里要 `asyncio.create_task`，就必须是 async def。
    """审批后续跑。同样走 astream 推事件。

    批准之后到写完报告是**最长的一段**（write_plan + 并发写作 + 审查 + 返修），
    不推事件的话前端从点批准到出报告又是全程黑箱。
    """
    from langgraph.types import Command

    cfg = {"configurable": {"thread_id": sid}}
    cmd = Command(resume={"approved": body.approved,
                          "approved_recalled_ids": body.approved_recalled_ids})
    # 上一次 run 推到哪了——续跑要接着往后推，否则会把旧事件重发一遍
    seen_keys = _last_seen.get(sid) or set()

    async def _go_stream() -> None:
        nonlocal seen_keys
        try:
            async for update in (await agraph()).astream(cmd, config=cfg, stream_mode="updates"):
                for node, delta in (update or {}).items():
                    if not isinstance(delta, dict):
                        continue
                    _emit(sid, {"kind": "node", "node": node})
                    for e in delta.get("events") or []:
                        k = _event_key(e)
                        if k in seen_keys:
                            continue
                        seen_keys.add(k)
                        _emit(sid, {"kind": "progress", "node": node, "event": e})
            st = graph().get_state(cfg)
            out = dict(st.values or {})
            _emit(sid, {"kind": "done", "state": {
                "report": (out.get("report_markdown") or "")[:2000],
                "report_chars": len(out.get("report_markdown") or ""),
                "round": out.get("round"), "url_count": out.get("url_count"),
                "cost_tokens": out.get("cost_tokens"),
            }})
        except Exception as e:
            _emit(sid, {"kind": "error", "error": f"{type(e).__name__}: {e}"})
        finally:
            _last_seen[sid] = seen_keys

    def _go_plain() -> None:
        try:
            graph().invoke(cmd, config=cfg)
            st = graph().get_state(cfg)
            out = dict(st.values or {})
            _emit(sid, {"kind": "done", "state": {
                "report": (out.get("report_markdown") or "")[:2000],
                "report_chars": len(out.get("report_markdown") or ""),
                "round": out.get("round"),
            }})
        except Exception as e:
            _emit(sid, {"kind": "error", "error": f"{type(e).__name__}: {e}"})

    if streaming:
        asyncio.create_task(_go_stream())
    else:
        asyncio.create_task(asyncio.to_thread(_go_plain))
    return {"ok": True, "streaming": streaming}


@app.get("/api/stream/{sid}")
def stream(sid: str) -> StreamingResponse:
    q: queue.Queue = queue.Queue(maxsize=1000)
    _subs.setdefault(sid, set()).add(q)

    def gen():
        yield f"data: {json.dumps({'kind': 'connected', 'session_id': sid})}\n\n"
        try:
            while True:
                try:
                    # 短轮询：既能让 keepalive 及时发出，
                    # 也能让客户端断开后及时收尾，不会占着线程干等
                    ev = q.get(timeout=2.0)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
        finally:
            _subs.get(sid, set()).discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/health")
def health() -> dict:
    from .graph.nodes import SX, TV
    return {
        "ok": True,
        "searxng": SX.health(),
        "tavily": TV.health(),
    }


@app.get("/api/search")
def search(q: str, lang: str = "zh-CN", limit: int = 8) -> dict:
    """手动检索调试口。也用于验证两个后端。"""
    from .graph.nodes import SX, TV

    if lang.startswith("zh"):
        hits = SX.search(q, language="zh-CN", limit=limit)
    else:
        hits = TV.search(q, language="en", limit=limit)
    return {"backend": "searxng" if lang.startswith("zh") else "tavily",
            "count": len(hits),
            "hits": [{"url": h.url, "title": h.title, "engine": h.engine} for h in hits]}
