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


# 订阅者：thread_id -> 队列
_subs: dict[str: set[queue.Queue]] = {}


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
def list_sessions(archived: str | None = None) -> list[dict]:
    repo = get_repo()
    if archived == "1":
        rows = repo.q(
            "SELECT id,title,question,status,cost_tokens,created_at,archived_at "
            "FROM session WHERE archived_at IS NOT NULL ORDER BY archived_at DESC"
        )
    else:
        rows = repo.q(
            "SELECT id,title,question,status,cost_tokens,created_at,archived_at "
            "FROM session ORDER BY created_at DESC"
        )
    return [dict(r) for r in rows]


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
def run_session(sid: str) -> dict:
    """启动研究。plan 节点跑完会停在 await_approval 等你审批。"""
    from langgraph.types import Command

    repo = get_repo()
    s = repo.q1("SELECT * FROM session WHERE id=?", (sid,))
    if not s:
        raise HTTPException(404, "会话不存在")

    _subs.setdefault(sid, set())
    q: queue.Queue = queue.Queue(maxsize=1000)
    _subs[sid].add(q)

    def _task() -> None:
        try:
            out = graph().invoke(
                {"session_id": sid, "question": s["question"],
                 "title": s["title"], "elapsed_s": 0.0, "cost_tokens": 0},
                config={"configurable": {"thread_id": sid}},
            )
            _emit(sid, {"kind": "done", "state": {
                "report": out.get("report_markdown", "")[:2000],
                "round": out.get("round"), "url_count": out.get("url_count"),
            }})
        except Exception as e:
            _emit(sid, {"kind": "error", "error": f"{type(e).__name__}: {e}"})
        finally:
            _subs.get(sid, set()).discard(q)

    asyncio.create_task(asyncio.to_thread(_task))
    return {"ok": True, "note": "已启动，事件流见 /api/stream/{sid}"}


@app.post("/api/sessions/{sid}/approve")
def approve(sid: str, body: ApproveIn) -> dict:
    from langgraph.types import Command

    async def _go() -> None:
        try:
            graph().invoke(
                Command(resume={"approved": body.approved,
                                "approved_recalled_ids": body.approved_recalled_ids}),
                config={"configurable": {"thread_id": sid}},
            )
        except Exception as e:
            _emit(sid, {"kind": "error", "error": f"{type(e).__name__}: {e}"})

    asyncio.create_task(asyncio.to_thread(_go))
    return {"ok": True}


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
