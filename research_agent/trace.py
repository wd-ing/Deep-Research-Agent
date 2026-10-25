"""trace_span 通用埋点（2026-10-21，配合迁移 0011）。

为什么单独一个模块：要测的是「任意一段代码的绝对起止」——节点、子图节点、
checkpoint 写入、工具循环轮次、检索子步骤、限流等待——它们散落在
graph/ 与 llm/ 各处，共用的只有「拿 session、记一行」这一点。

三条纪律（都对应踩过的坑）：

  1. **绝不抛**。任何异常都吞掉——埋点把主流程炸挂是最糟糕的失败模式。
  2. **session 不进调用链**。沿用 llm.provider 的 `_CURRENT_SESSION`
     contextvar（`session_scope` / `run_in_context` 已有的传播链），
     调用方几十个埋点位置不用各传一遍。拿不到就记 NULL。
  3. **写库要便宜**。直接 `repo.x` 一行 INSERT：repo 本来就是
     每线程一个连接 + `_write_lock` 串行写，实测单条 ~0.1-0.3ms，
     加缓冲层只会引入「进程被打断丢一片」的新问题。

用法：

    with span("retrieve", "search", provider="searxng", query=q) as d:
        res = prov.search(q)
        d["hits"] = len(res)          # detail 可事后补充

    @traced("write.plan_spec")        # 子图节点（fn(state)->dict）
    def node_plan_spec(state): ...
"""

from __future__ import annotations

import functools
import json
import threading
import time
from contextlib import contextmanager
from typing import Any


def _session_id() -> str | None:
    """当前记账会话。contextvar 拿不到就 None，绝不报错。"""
    try:
        from .llm.provider import current_session
        return current_session()
    except Exception:
        return None


def record_span(kind: str, name: str, started_at: float, ended_at: float,
                detail: dict | None = None, session_id: str | None = None) -> None:
    """落一行 span。**任何失败都吞掉**——观测绝不许影响主流程。"""
    try:
        from .db.repo import get_repo

        get_repo().x(
            "INSERT INTO trace_span"
            "(session_id, kind, name, started_at, ended_at, thread, detail_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (session_id or _session_id(), kind, name,
             float(started_at), float(ended_at),
             threading.current_thread().name,
             json.dumps(detail, ensure_ascii=False, default=str)
             if detail is not None else None),
        )
    except Exception:
        pass


@contextmanager
def span(kind: str, name: str, session_id: str | None = None,
         **detail: Any):
    """记一段代码的起止。yield 出去的 dict 可以继续塞 detail（set_detail）。"""
    d = dict(detail)
    t0 = time.time()
    try:
        yield d
    finally:
        record_span(kind, name, t0, time.time(), d, session_id=session_id)


def traced(name: str, fn=None, kind: str = "subnode"):
    """子图节点埋点。`fn(state) -> dict` 签名不变（functools.wraps）。

    两种用法都行：`g.add_node("x", traced("w.x", node_x))` 或
    `@traced("w.x")`。加在 add_node 注册处而不是函数定义上——
    直接 import 节点函数的测试拿到的还是裸函数。
    """
    def deco(f):
        @functools.wraps(f)
        def wrapper(state):
            with span(kind, name):
                return f(state)
        return wrapper
    return deco(fn) if fn is not None else deco
