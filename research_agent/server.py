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
    # 落库（0009）：每次真跑自动沉淀可回放的事件流，
    # tests/dump_events.py 直接导出成 fixture。SSE 本来就是尽力送达，
    # 所以这里也绝不能反过来弄挂一次运行。
    try:
        get_repo().save_sse_event(thread_id, event)
    except Exception:
        pass


# ---------------------------------------------------------------- API

class CreateIn(BaseModel):
    question: str = Field(min_length=1)
    title: str | None = None


class ApproveIn(BaseModel):
    approved: bool = True
    # 原来还有 `approved_recalled_ids`（历史召回的段落勾选）。召回 2026-10-12
    # 砍掉后它没有任何消费方——前端从没提交过，图里也从没读过非空值。
    # 2026-10-13 删掉，别让它看起来像「审批页能勾选召回段落」。


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
        "       deleted_at,folder_id,summary_json,degraded FROM session "
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


@app.get("/api/sessions/search")
def search_sessions(q: str = "", limit: int = 20) -> dict:
    """跨会话搜索（D2）：命中 标题 / 问题 / 报告正文 三处。

    正文命中时带定位片段，前端可以直接展示「在哪句话里命中的」。
    和 `/api/sessions?q=`（只过滤标题+问题）不冲突：那是列表筛选用，
    这个是有结果片段的全文搜索。软删除的会话一律不搜。

    LIKE 对 CJK 是精确子串匹配、对 ASCII 不区分大小写——够用；
    上 FTS5 的边际收益撑不起再养一份倒排索引。
    """
    repo = get_repo()
    q = q.strip()
    if not q:
        return {"q": q, "hits": []}
    like = f"%{q}%"
    # 正文匹配先取回再裁片段，SQL 里 substr 定位对多字节字符不可靠
    rows = repo.q(
        "SELECT s.id, s.title, s.question, s.status, s.created_at,"
        "       r.markdown AS body "
        "FROM session s LEFT JOIN report r ON r.session_id = s.id "
        "WHERE s.deleted_at IS NULL AND ("
        "  s.title LIKE ? OR s.question LIKE ? OR r.markdown LIKE ?) "
        "ORDER BY s.created_at DESC LIMIT ?", (like, like, like, limit * 2))
    hits = []
    qlen = len(q)
    for r in rows:
        d = dict(r)
        body = d.pop("body") or ""
        where_in = ("title" if q.lower() in d["title"].lower() else
                    "question" if q.lower() in d["question"].lower() else "body")
        snippet = ""
        if where_in == "body":
            # lower() 对中文无影响，定位 ASCII 大小写不敏感
            pos = body.lower().find(q.lower())
            if pos >= 0:
                lo, hi = max(0, pos - 40), pos + qlen + 60
                snippet = ("…" if lo else "") + body[lo:hi].replace("\n", " ") + \
                          ("…" if hi < len(body) else "")
        hits.append({"id": d["id"], "title": d["title"], "status": d["status"],
                     "created_at": d["created_at"],
                     "matched_in": where_in, "snippet": snippet})
        if len(hits) >= limit:
            break
    return {"q": q, "hits": hits}


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


class FolderOrderIn(BaseModel):
    parent_id: str | None = None
    ids: list[str] = Field(min_length=1)


# ⚠️ **必须定义在 `/api/folders/{fid}` 之前**。两者路径段数相同，
# FastAPI 按定义顺序匹配——`{fid}` 在前的话，`order` 会被当成 folder id，
# 结果是 404「文件夹不存在」，而不是走到这个端点。
@app.patch("/api/folders/order")
def reorder_folders(body: FolderOrderIn) -> dict:
    """同级重排。`ids` 是该父节点下的完整顺序（顶层传 parent_id=null）。

    跨层级移动不走这里，走 `PATCH /api/folders/{fid}` 的 `parent_id`——
    两件事分开，拖拽的行为才可预期。
    """
    repo = get_repo()
    if body.parent_id and not repo.q1(
            "SELECT 1 FROM folder WHERE id=?", (body.parent_id,)):
        raise HTTPException(404, "父文件夹不存在")
    dup = {x for x in body.ids if body.ids.count(x) > 1}
    if dup:
        raise HTTPException(400, f"顺序里有重复：{'、'.join(sorted(dup))}")
    missing = [i for i in body.ids
               if not repo.q1("SELECT 1 FROM folder WHERE id=?", (i,))]
    if missing:
        raise HTTPException(400, f"不存在的文件夹：{'、'.join(missing)}")
    n = repo.reorder_folders(body.parent_id, body.ids)
    return {"ok": True, "reordered": n}


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
    out["ignored_citations"] = _ignored_citations(repo, sid)
    return out


@app.post("/api/sessions/{sid}/archive")
def archive(sid: str) -> dict:
    """显式归档：只是从默认列表里收起来，报告和数据一律不动。"""
    repo = get_repo()
    repo.archive_session(sid)
    return {"ok": True, "note": "已归档，不再出现在默认会话列表中"}


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


@app.get("/api/sessions/{sid}/trace")
def session_trace(sid: str) -> dict:
    """按**轮次**汇总的检索轨迹——给「检索轮次」图表用。

    ## 为什么不用前端已经拿到的 progress 事件

    跑完的会话 `useSession` 直接 return，**根本不开 EventSource**，
    `progress` 恒为空数组。只从前端事件推的话，这张图只在跑的过程中可见，
    刷新一下就没了——而「这次为什么只查到 41 个来源」恰恰是事后才想看的东西。
    所以走 DB：`run_trace` 是落盘的，任何时候都能读。

    ## round 用哪个口径（这条很容易搞错）

    `run_trace.round` 有两套含义并存：

    - `kind='retrieve'` / `kind='assess'`：**本次是第几轮**（1,2,3…）
    - `kind='node:*'`：进入该节点时**已完成几轮**（`node:retrieve` 是 0,1,2…）

    本端点**只取前两种事件**，「第 N 轮」的含义因此唯一。
    详细约定见 `repo.save_node_timing` 的 docstring。
    """
    repo = get_repo()
    if not repo.q1("SELECT 1 FROM session WHERE id=?", (sid,)):
        raise HTTPException(404, "会话不存在")

    rows = repo.q(
        "SELECT round, kind, detail_json FROM run_trace "
        "WHERE session_id=? AND kind IN ('retrieve','assess') ORDER BY round",
        (sid,),
    )

    by_round: dict[int, dict] = {}
    for r in rows:
        rnd = r["round"]
        # round 理论上从 1 开始。0 是历史脏数据（早期版本没给 retrieve 记轮次），
        # 别把它画成「第 0 轮」，直接跳过。
        if rnd is None or rnd < 1:
            continue
        try:
            d = json.loads(r["detail_json"] or "{}")
        except (ValueError, TypeError):
            d = {}
        if not isinstance(d, dict):
            d = {}

        slot = by_round.setdefault(rnd, {"round": rnd})
        if r["kind"] == "retrieve":
            for k in ("hits", "accepted", "new_sources", "fetched_total"):
                if isinstance(d.get(k), (int, float)):
                    slot[k] = d[k]
        else:
            for k in ("segments_total", "new_segments", "gaps"):
                if isinstance(d.get(k), (int, float)):
                    slot[k] = d[k]
            for k in ("round_gain", "has_enough"):
                if k in d:
                    slot[k] = d[k]
            urg = d.get("gaps_by_urgency")
            if isinstance(urg, dict):
                slot["gaps_by_urgency"] = urg
            # 停止判据**只在真的触发了的那一轮**有值。
            # 保留它是为了回答「为什么停在第 N 轮」，不是为了展示判据本身。
            if d.get("stop_would_be"):
                slot["stop_would_be"] = d["stop_would_be"]
                slot["stop_layer"] = d.get("stop_layer") or ""

    return {"rounds": [by_round[k] for k in sorted(by_round)]}


@app.get("/api/sessions/{sid}/review")
def session_review(sid: str) -> dict:
    """修订痕迹视图供数：已应用补丁的逐条明细 + 运行健康度。

    数据全从落库读——`/trace` 端点注释里那条教训同样适用：
    已完成的会话前端不开 EventSource，内存态拿不到。

    `patches` 按 sse_event.id 升序聚合所有 write_revise 事件的
    `patch.applied_items`；`old` 是应用前文本、`before` 是其前 40 字
    （delete 型靠它定位）。同一补丁在多轮返修里可能被重复打上，
    按 (task, section, old, new) 去重。老会话/没走补丁的会话
    没有 applied_items——patches 就是空数组，这不是错误。
    """
    repo = get_repo()
    if not repo.q1("SELECT 1 FROM session WHERE id=?", (sid,)):
        raise HTTPException(404, "会话不存在")

    patches: list[dict] = []
    seen: set = set()
    for r in repo.q(
            "SELECT event_json FROM sse_event "
            "WHERE session_id=? AND kind='progress' ORDER BY id", (sid,)):
        try:
            ev = json.loads(r["event_json"] or "{}")
        except (ValueError, TypeError):
            continue                    # 损坏行跳过：日志表不该让整个端点 500
        inner = ev.get("event") if isinstance(ev.get("event"), dict) else ev
        if not isinstance(inner, dict) or inner.get("kind") != "write_revise":
            continue
        rnd = inner.get("round")
        for it in ((inner.get("patch") or {}).get("applied_items") or []):
            if not isinstance(it, dict):
                continue
            key = (it.get("task"), it.get("section"),
                   it.get("old"), it.get("new"))
            if key in seen:
                continue
            seen.add(key)
            patches.append({**it, "round": rnd})

    # run_trace 的 run_health 按 (session_id, round, kind) upsert，
    # 多轮 finalize 取 round 最大的那行；老会话没有这条，health 给 null。
    hr = repo.q1(
        "SELECT detail_json FROM run_trace "
        "WHERE session_id=? AND kind='run_health' "
        "ORDER BY round DESC LIMIT 1", (sid,))
    health = None
    if hr:
        try:
            d = json.loads(hr["detail_json"] or "{}")
            if isinstance(d, dict):
                health = d
        except (ValueError, TypeError):
            pass

    return {"patches": patches, "health": health}


@app.get("/api/sessions/{sid}/approval")
def approval(sid: str) -> dict:
    """当前待审批的框架（没有就返回 waiting=False）。

    审批 payload 是 `node_await_approval` 组好后交给 `interrupt()` 的，
    checkpointer 存着，**只能从 graph state 里读**——DB 里没有这张表。

    里面有什么：最终框架、每节的材料支撑说明、被修订过没有、
    这份框架可不可信（材料够了 vs 撞了上限只是尽力而为）、当前材料统计。
    前端必须把这些都显示出来——否则用户批的是一份未经材料检验的框架。
    """
    # ⚠️ 带 status：前端 SSE 断线后的兜底轮询全靠这个端点
    # （拉 `/api/sessions/{id}` 是 110 KB，每 4 秒一次太重）。
    # 不知道状态就没法判断「这场已经跑完了，可以停了」——
    # 于是那个轮询会一直转下去。跑完了前端会把这个字段读成终态然后自停。
    row = get_repo().q1("SELECT status, cost_tokens FROM session WHERE id=?", (sid,))
    status = row["status"] if row else None
    # 累计 token 一并回：前端轮询这个端点当「活性信号」用，
    # 同一行多读一列，零额外开销。
    cost = row["cost_tokens"] if row else None
    st = graph().get_state({"configurable": {"thread_id": sid}})
    ints = getattr(st, "interrupts", None) or getattr(st, "__interrupt__", None) or []
    if not ints:
        return {"waiting": False, "status": status, "cost_tokens": cost,
                "note": "当前没有待审批的节点（可能还没跑到审批点，或已经批过了）"}
    first = ints[0]
    payload = getattr(first, "value", None)
    if not isinstance(payload, dict):
        payload = {"raw": str(payload)}
    return {"waiting": True, "status": status, "cost_tokens": cost,
            "approval": payload}


# ---------------------------------------------------------------- 设置

MODELS_YAML = REPO_ROOT / "config" / "models.yaml"


@app.get("/api/config")
def get_config() -> dict:
    """models.yaml 原文 + 当前生效的「环节 → 模型」解析表（设置页用）。

    运行时进程读的是启动时那一次——改了文件**要重启才生效**，
    前端必须照实说，别做成「保存即生效」的假象。
    """
    import yaml
    text = MODELS_YAML.read_text(encoding="utf-8")
    # 「当前生效」那份——ModelRegistry 启动时读进的版本。
    # 解析失败不该挂掉整个端点：文件本身就坏了时更要让人能在页面上看到。
    stages: dict[str, str] = {}
    try:
        from .llm.provider import ModelRegistry
        reg = ModelRegistry()
        for name in (reg.raw.get("stages") or {}):
            cfg = reg.stage(name)
            stages[name] = f"{cfg.provider} / {cfg.model}"
    except Exception as e:
        stages = {"_error": f"配置解析失败：{e}"}
    return {"path": str(MODELS_YAML), "content": text,
            "stages": stages, "restart_required": True}


class ConfigIn(BaseModel):
    content: str


@app.put("/api/config")
def put_config(body: ConfigIn) -> dict:
    """写回 models.yaml。只收 YAML 能解析且带 stages: 的内容——
    坏配置写进去会让下一次启动直接炸，校验必须在这里拦。"""
    import yaml
    content = body.content
    if not content.strip():
        raise HTTPException(400, "内容为空")
    try:
        parsed = yaml.safe_load(content)
    except Exception as e:
        raise HTTPException(400, f"YAML 解析失败，没有写入：{e}")
    if not isinstance(parsed, dict) or not parsed.get("stages"):
        raise HTTPException(400, "不像 models.yaml：顶层缺 stages: 映射，没有写入")
    MODELS_YAML.write_text(content, encoding="utf-8")
    return {"ok": True, "restart_required": True,
            "note": "已写回 models.yaml——重启服务后生效"}


@app.get("/api/sessions/{sid}/seg_preview")
def seg_preview(sid: str) -> dict:
    """报告里所有引用段号的**轻量预览**（前端 hover 气泡用）。

    ## 为什么不复用 `/seg/{ref}` 或 `/segments`

    v5 报告 162 处引用，去重后 **93 个不同段号**：

    - 逐个点开 = 93 次请求，鼠标扫一遍报告就几十个请求
    - 一次全取 `/segments` = 把 143 段**全文**都拉下来（几十 KB，
      而 hover 时看不到全文，只会用前两三行）

    所以这里只给「够判断这段是什么」的那几样：标题、概括、前 180 字、
    来源域名与性质、以及**是否已被用户排除**。

    最后一项是刻意加的：被排除的来源不会从报告里消失（阶段 5 的前提），
    hover 时如果看不到这个标记，用户会以为点开抽屉才知道——
    而抽屉要点一下才看得见。气泡是第一次提醒的机会。

    ## 段号必须复用 `EvidenceTools`

    这里的 `tools.chunk_of` 就是 `node_write` 写引用时用的同一套编号。
    **自己按 rowid 重算一遍会和报告里的段号错位**——
    `/seg/{ref}` 已经踩过这个坑，注释还留在那里。
    """
    from .tools.evidence import EvidenceTools

    repo = get_repo()
    if not repo.q1("SELECT 1 FROM session WHERE id=?", (sid,)):
        raise HTTPException(404, "会话不存在")
    try:
        tools = EvidenceTools(repo, sid)
    except Exception as e:
        raise HTTPException(500, f"段号索引构建失败：{e}")
    if not tools.chunk_of:
        return {}

    rows = repo.q(
        "SELECT c.id, c.source_id, c.heading_path, c.summary, c.content, "
        "       c.token_count, s.domain, s.title, s.source_nature "
        "FROM chunk c LEFT JOIN source s ON s.id=c.source_id "
        "WHERE c.session_id=?", (sid,))
    by_id = {r["id"]: r for r in rows}
    bad = {r["source_id"] for r in repo.q(
        "SELECT source_id FROM ignored_source WHERE source_id IN "
        "(SELECT id FROM source WHERE session_id=?)", (sid,))}

    out: dict[str, dict] = {}
    for ref, cid in tools.chunk_of.items():
        r = by_id.get(cid)
        if not r:
            continue
        # 摘录取正文前 180 字。**折叠空白**——抓来的网页正文里
        # 到处是换行和连续空格，不折的话一行放不下三个字。
        text = " ".join((r["content"] or "").split())[:180]
        out[ref] = {
            "标题": r["heading_path"] or "",
            "概括": r["summary"] or "",
            "摘录": text,
            "来源号": tools._src_ref(r["source_id"]) if r["source_id"] else "",
            "域名": r["domain"] or "",
            "名称": r["title"] or "",
            "性质": r["source_nature"] or "",
            "已排除": bool(r["source_id"] and r["source_id"] in bad),
            "token": r["token_count"],
        }
    return out


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

    def _on_progress(session_id, node, ev):
        # 环节内实时进度（fire_progress）：原样套上 progress 信封推出去。
        _emit(session_id or sid, {"kind": "progress", "node": node, "event": ev})

    def _task_plain() -> None:
        """原来的 invoke 路径，保留作回退。"""
        from .llm.provider import progress_scope
        try:
            # 非流式没有订阅者，但 _emit 会把事件落进 sse_event（0009），
            # 让这条路径也能被 dump_events 完整回放。
            with progress_scope(_on_progress):
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
        from .llm.provider import delta_scope, progress_scope

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
            with delta_scope(_on_delta), progress_scope(_on_progress):
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
    cmd = Command(resume={"approved": body.approved})
    # 上一次 run 推到哪了——续跑要接着往后推，否则会把旧事件重发一遍
    seen_keys = _last_seen.get(sid) or set()

    async def _go_stream() -> None:
        from .llm.provider import progress_scope
        nonlocal seen_keys
        try:
            # 写作阶段在审批之后跑——write_task 这类环节内进度全靠这条链路推
            with progress_scope(lambda session_id, node, ev: _emit(
                    session_id or sid,
                    {"kind": "progress", "node": node, "event": ev})):
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
        from .llm.provider import progress_scope
        try:
            with progress_scope(lambda session_id, node, ev: _emit(
                    session_id or sid,
                    {"kind": "progress", "node": node, "event": ev})):
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
