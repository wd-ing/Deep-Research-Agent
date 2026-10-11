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

# 每个 session 的 events 已经推到第几条了。
# `events` 在 state 里是累加器，run 与 approve 是两段独立的推流，
# 不记这个数就会在审批后把第一轮的旧事件重发一遍。
_last_seen: dict[str, int] = {}


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
                data["段落"][0]["来源"] = dict(row)
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

    seen = 0

    async def _task_stream() -> None:
        """astream 路径：每个节点更新到达就推事件。

        **`events` 是累加器**（`state.py` 里 `Annotated[list, operator.add]`），
        所以每次拿到的都是**迄今全部**事件——必须自己算增量，
        否则同一个事件会被反复推，前端看到一堆重复。
        """
        nonlocal seen
        seen = 0
        try:
            async for update in (await agraph()).astream(inp, config=cfg, stream_mode="updates"):
                for node, delta in (update or {}).items():
                    if not isinstance(delta, dict):
                        continue
                    _emit(sid, {"kind": "node", "node": node})
                    evs = delta.get("events") or []
                    # 一个节点更新里可能有多个 events；按 seen 偏移切出新增部分
                    if len(evs) > seen:
                        for e in evs[seen:]:
                            _emit(sid, {"kind": "progress", "node": node, "event": e})
                    seen = len(evs)
            # 收尾：astream 迭代完拿不到最终 state，用 checkpointer 读
            st = graph().get_state(cfg)
            _finish(dict(st.values or {}))
        except Exception as e:
            _emit(sid, {"kind": "error", "error": f"{type(e).__name__}: {e}"})
        finally:
            _subs.get(sid, set()).discard(q)
            _last_seen[sid] = seen

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
    seen = _last_seen.get(sid, 0)

    async def _go_stream() -> None:
        nonlocal seen
        try:
            async for update in (await agraph()).astream(cmd, config=cfg, stream_mode="updates"):
                for node, delta in (update or {}).items():
                    if not isinstance(delta, dict):
                        continue
                    _emit(sid, {"kind": "node", "node": node})
                    evs = delta.get("events") or []
                    if len(evs) > seen:
                        for e in evs[seen:]:
                            _emit(sid, {"kind": "progress", "node": node, "event": e})
                    seen = len(evs)
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
            _last_seen[sid] = seen

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
