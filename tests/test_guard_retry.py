"""护栏丢弃尝试也落 llm_call 的守门测试（2026-10-25 加）。

背景：基线 v18 里 outline 有 52s「静默」——第一次尝试思考吃光预算、
正文为空，护栏翻倍重发成功，**被丢弃的第一次没有任何 llm_call 行**。
现在每次被丢弃的尝试都要记 status='retry'、error 前缀 `guard:*`。

桩 `_minimax_once`（与 test_overload_retry 同一套 Rig），不联网。
用法：.venv/bin/python tests/test_guard_retry.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("MINIMAX_API_KEY", "stub-not-used")
os.environ["RA_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "t.db")

import research_agent.llm.provider as P               # noqa: E402
from research_agent.db.repo import get_repo           # noqa: E402
from research_agent.llm.provider import (LLMClient, session_scope)  # noqa: E402

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}"
          + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


EMPTY = {"content": [{"type": "thinking", "thinking": "x" * 9000}],
         "stop_reason": "max_tokens",
         "usage": {"input_tokens": 10, "output_tokens": 8192}}
OK_TEXT = {"content": [{"type": "text", "text": "答"}],
           "stop_reason": "end_turn",
           "usage": {"input_tokens": 10, "output_tokens": 5}}


class Rig:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.sleeps: list[float] = []

    def once(self, cfg, url, headers, payload):
        self.calls += 1
        item = self.script.pop(0) if self.script else (200, "", OK_TEXT)
        if isinstance(item, Exception):
            raise item
        status, body = item[0], item[1]
        return status, body, (item[2] if len(item) > 2 else OK_TEXT)

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)


def _client(script):
    rig = Rig(script)
    client = LLMClient()
    client._minimax_once = rig.once
    cfg = client.registry.stage("relevance")
    cfg.overload_retry.max_retries = 0   # 不挡我们脚本化的过载路径
    cfg.guard.retry_on_transport = 1
    old_sleep = P._sleep
    P._sleep = rig.sleep
    return rig, client, cfg, old_sleep


def rows(sid):
    return get_repo().q(
        "SELECT stage,status,error,input_tokens,output_tokens,latency_ms,"
        "started_at,ttfb_ms FROM llm_call WHERE session_id=?"
        " ORDER BY created_at", (sid,))


def t1_empty_then_ok():
    """空返回 → 翻倍重试 → 成功：应有两行（retry+ok）。"""
    rig, client, cfg, os_ = _client([(200, "", EMPTY), (200, "", OK_TEXT)])
    try:
        sid = get_repo().create_session("t1", "q")
        with session_scope(sid):
            # 走 complete() 而非裸 _minimax：ok 行由 complete 层的 _record 记。
            text = client.complete("relevance", "prompt")
        rs = rows(sid)
        check("① 空→成功：2 次请求、最终返回", rig.calls == 2 and text == "答")
        check("① 两行 llm_call", len(rs) == 2, str(len(rs)))
        check("① 第一行 retry+guard:empty",
              rs[0]["status"] == "retry"
              and (rs[0]["error"] or "").startswith("guard:empty"),
              rs[0]["error"])
        check("① retry 行带真实 token 与 budget 翻倍",
              rs[0]["output_tokens"] == 8192 and "4096→8192" in (rs[0]["error"] or ""),
              rs[0]["error"])
        check("① retry 行带 started_at/latency_ms",
              rs[0]["started_at"] is not None and rs[0]["latency_ms"] is not None)
        check("① 第二行 ok", rs[1]["status"] == "ok" and rs[1]["output_tokens"] == 5)
        # cache_stats 口径：retry 不计 calls
        cs = get_repo().cache_stats(sid)
        check("① cache_stats calls=1 retries=1",
              cs["calls"] == 1 and cs["retries"] == 1, str(cs))
    finally:
        P._sleep = os_


def t2_transport_then_ok():
    """传输断连 → 重试 → 成功：retry 行 cause=transport。"""
    import httpx
    rig, client, cfg, os_ = _client(
        [httpx.ConnectError("boom"), (200, "", OK_TEXT)])
    try:
        sid = get_repo().create_session("t2", "q")
        with session_scope(sid):
            text = client.complete("relevance", "prompt")
        rs = rows(sid)
        check("② transport→成功：2 次请求", rig.calls == 2 and text == "答")
        check("② retry 行 cause=transport",
              len(rs) == 2 and rs[0]["status"] == "retry"
              and (rs[0]["error"] or "").startswith("guard:transport"),
              rs[0]["error"] if rs else "无行")
        check("② transport 行仍带 latency/started_at",
              rs[0]["latency_ms"] is not None and rs[0]["started_at"] is not None)
    finally:
        P._sleep = os_


def t3_quota_exhausted():
    """额度用尽：每次丢弃的尝试各一行 retry（含最后一次），
    外层 complete() 再补一行 error（EmptyOutput）。"""
    rig, client, cfg, os_ = _client([(200, "", EMPTY)] * 5)
    # relevance 默认 retry_on_empty=1、retry_on_transport=1 → 至多 3 次尝试。
    try:
        sid = get_repo().create_session("t3", "q")
        with session_scope(sid):
            try:
                client.complete("relevance", "prompt")
                ok = "没抛"
            except P.EmptyOutput:
                ok = "EmptyOutput"
        rs = rows(sid)
        n_retry = sum(1 for r in rs if r["status"] == "retry")
        check("③ 额度用尽 → EmptyOutput", ok == "EmptyOutput")
        check("③ 每次被丢的尝试都有 retry 行（2 次尝试=2 retry 行）",
              n_retry == 2, f"retry={n_retry} rows={len(rs)}")
        check("③ 末尾一行是 error",
              rs[-1]["status"] == "error", rs[-1]["status"])
    finally:
        P._sleep = os_


if __name__ == "__main__":
    t1_empty_then_ok()
    t2_transport_then_ok()
    t3_quota_exhausted()
    print()
    if FAILED:
        print("失败：" + "、".join(FAILED))
        sys.exit(1)
    print("全过 ✅")
