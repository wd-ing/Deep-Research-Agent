"""`_minimax_send` 统一过载重试的守门测试（2026-10-20，平台拥堵期）。

背景：`_minimax_tools` 以前拿到非 200 直接 raise，529 零重试——
v17 实测 summarize 5 个批次整批报废、44/162 个相关段落没概括。
现在 `_minimax` 与 `_minimax_tools` 都走 `_minimax_send`：
过载类（5xx/流式 api_error）按 `overload_retry` 独立额度重试，
每次记一行 `llm_call status='retry'`；429/敏感词/额度耗尽不重试。

桩 `LLMClient._minimax_once`（实例属性，序列化返回/异常）与
`provider._sleep`（记录等待、不真睡）。不联网。

用法：
    .venv/bin/python tests/test_overload_retry.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("MINIMAX_API_KEY", "stub-not-used")
os.environ.setdefault("ARK_API_KEY", "stub-not-used")
os.environ["RA_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "t.db")

import research_agent.llm.provider as P               # noqa: E402
from research_agent.db.repo import get_repo           # noqa: E402
from research_agent.graph.subgraphs import _retry as R  # noqa: E402
from research_agent.llm.provider import (LLMClient,   # noqa: E402
                                        ModelRegistry, session_scope)

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}"
          + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


BODY_OVERLOAD = ('{"type":"error","error":{"type":"overloaded_error",'
                 '"message":"当前服务集群负载较高，请稍后重试"}}')
BODY_SEAT = ('{"type":"error","error":{"type":"rate_limit_error",'
             '"message":"MODEL_SEAT_QUOTA_EXHAUSTED"}}')
BODY_FILTER = ('{"type":"error","error":{"type":"api_error",'
               '"message":"content_filter new_sensitive hit"}}')
ERR_STREAM = RuntimeError(
    "MiniMax 流式错误: {'type': 'api_error', "
    "'message': 'unknown error, 722 (1000)'}")

OK_TEXT = {"content": [{"type": "text", "text": "答"}],
           "stop_reason": "end_turn",
           "usage": {"input_tokens": 10, "output_tokens": 5}}
OK_TOOL = {"content": [{"type": "tool_use", "id": "u1",
                        "name": "emit_summaries",
                        "input": {"summaries": [{"segment": "S1·1",
                                                 "summary": "概括"}]}}],
           "stop_reason": "tool_use",
           "usage": {"input_tokens": 10, "output_tokens": 5}}


class Rig:
    """脚本化的 _minimax_once + 记录型 _sleep。"""

    def __init__(self, script):
        self.script = list(script)   # (status,body) 或 Exception
        self.calls = 0
        self.sleeps: list[float] = []

    def once(self, cfg, url, headers, payload):
        self.calls += 1
        item = self.script.pop(0) if self.script else (200, "", OK_TEXT)
        if isinstance(item, Exception):
            raise item
        status, body = item
        return status, body, (OK_TEXT if status == 200 else None)

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)


def _client(script, stage="relevance") -> tuple[Rig, LLMClient, object]:
    rig = Rig(script)
    client = LLMClient()
    client._minimax_once = rig.once
    cfg = client.registry.stage(stage)
    # 同上：桩只覆盖 _minimax_once，迁 Ark 后把 stage 换成 minimax 配置不联网。
    import copy as _copy
    cfg = _copy.copy(cfg)
    cfg.provider = "minimax"
    cfg.base_url = "https://api.minimaxi.com/anthropic"
    cfg.api_key_env = "MINIMAX_API_KEY"
    client.registry._cache[stage] = cfg
    cfg.overload_retry.max_retries = 5
    cfg.overload_retry.base_s = 10.0
    cfg.overload_retry.cap_s = 60.0
    cfg.overload_retry.jitter = 0.25
    old_sleep = P._sleep
    P._sleep = rig.sleep
    return rig, client, (cfg, old_sleep)


def _unrig(old_sleep) -> None:
    P._sleep = old_sleep


def _retry_rows(sid: str) -> list:
    return get_repo().q(
        "SELECT stage,status,error,input_tokens,output_tokens "
        "FROM llm_call WHERE session_id=? AND status='retry'", (sid,))


_SID = [0]


def _new_session() -> str:
    _SID[0] += 1
    return get_repo().create_session(f"t{_SID[0]}", "q")


def t1_529_529_200():
    rig, client, (cfg, os_) = _client(
        [(529, BODY_OVERLOAD), (529, BODY_OVERLOAD), (200, "")])
    try:
        sid = _new_session()
        with session_scope(sid):
            text, usage = client._minimax(cfg, "prompt", None, False)
        rows = _retry_rows(sid)
        check("① 529,529,200 → 3 次请求、成功返回",
              rig.calls == 3 and text == "答", f"calls={rig.calls}")
        check("① 2 条 retry 行", len(rows) == 2, str(len(rows)))
        check("① 等待单调不减且在 [0.75,1.25]×base 内",
              len(rig.sleeps) == 2
              and all(0.75 * b <= s <= 1.25 * b
                      for s, b in zip(rig.sleeps, (10, 20)))
              and rig.sleeps[0] <= rig.sleeps[1],
              str(rig.sleeps))
        check("① retry 行 error 格式固定（可被 run_health 解析）",
              all("attempt" in (r["error"] or "")
                  and "| wait " in (r["error"] or "") for r in rows),
              rows[0]["error"] if rows else "无")
    finally:
        _unrig(os_)


def t2_exhausted():
    rig, client, (cfg, os_) = _client([(529, BODY_OVERLOAD)] * 6)
    try:
        sid = _new_session()
        err = None
        with session_scope(sid):
            try:
                client._minimax(cfg, "p", None, False)
            except RuntimeError as e:
                err = e
        check("② 连撞 6 次 529 → 恰好 1+5 次请求后抛出",
              rig.calls == 6, str(rig.calls))
        check("② 错误文本以 `MiniMax 529` 开头",
              err is not None and str(err).startswith("MiniMax 529"),
              str(err)[:60])
        check("② `_transient_http` 判真（任务级重试判据不破）",
              err is not None and R._transient_http(err))
        check("② 5 条 retry 行", len(_retry_rows(sid)) == 5)
    finally:
        _unrig(os_)


def t3_no_retry():
    for label, item in (
            ("429 seat_quota", (429, BODY_SEAT)),
            ("400 参数错", (400, '{"error":"bad"}')),
            ("500 内容过滤", (500, BODY_FILTER))):
        rig, client, (cfg, os_) = _client([item, (200, "")])
        try:
            try:
                client._minimax(cfg, "p", None, False)
            except RuntimeError:
                pass
            check(f"③ {label} 不重试（1 次请求）", rig.calls == 1,
                  str(rig.calls))
        finally:
            _unrig(os_)


def t4_stream_api_error():
    rig, client, (cfg, os_) = _client([ERR_STREAM, (200, "")])
    try:
        sid = _new_session()
        with session_scope(sid):
            text, _ = client._minimax(cfg, "p", None, False)
        rows = _retry_rows(sid)
        check("④ 流式 api_error(722) 重试成功",
              rig.calls == 2 and text == "答")
        check("④ retry 行 cause 带 api_error 字样",
              len(rows) == 1 and "api_error" in (rows[0]["error"] or ""),
              rows[0]["error"] if rows else "无")
    finally:
        _unrig(os_)


def t5_both_entries():
    # complete_json（_minimax 路）：529 → 200（200 返回体必须是 JSON 文本）
    rig = Rig([(529, BODY_OVERLOAD), (200, "")])
    client = LLMClient()
    parsed_json = {"content": [{"type": "text", "text": '{"a": 1}'}],
                   "stop_reason": "end_turn",
                   "usage": {"input_tokens": 1, "output_tokens": 1}}

    def once_json(cfg, url, headers, payload):
        rig.calls += 1
        item = rig.script.pop(0) if rig.script else (200, "")
        return item[0], item[1], (parsed_json if item[0] == 200 else None)
    client._minimax_once = once_json
    cfg = client.registry.stage("relevance")
    # 2026-11-03 迁 Ark 后该环节走 _opencode 真 HTTP——本测的是重试/护栏机制，
    # 不是平台，把 stage 换成走 _minimax_once 桩的 minimax 配置（不联网）。
    import copy as _copy
    cfg = _copy.copy(cfg)
    cfg.provider = "minimax"
    cfg.base_url = "https://api.minimaxi.com/anthropic"
    cfg.api_key_env = "MINIMAX_API_KEY"
    client.registry._cache["relevance"] = cfg
    old_sleep = P._sleep
    P._sleep = rig.sleep
    try:
        sid = _new_session()
        with session_scope(sid):
            out = client.complete_json("relevance", "p")
        check("⑤ complete_json（_minimax 路）529 后成功",
              rig.calls == 2 and out == {"a": 1}, f"{rig.calls} {out}")
        check("⑤ relevance 记到 1 条 retry 行",
              len(_retry_rows(sid)) == 1)
    finally:
        P._sleep = old_sleep

    # complete_json_forced（_minimax_tools 路）：529 → 工具调用
    rig = Rig([(529, BODY_OVERLOAD), (200, "")])
    client = LLMClient()

    def once_tool(cfg, url, headers, payload):
        rig.calls += 1
        item = rig.script.pop(0) if rig.script else (200, "")
        return item[0], item[1], (OK_TOOL if item[0] == 200 else None)
    client._minimax_once = once_tool
    # 同上：summarize 也要走桩住的 _minimax_tools，不联网。
    import copy as _copy
    cfg2 = _copy.copy(client.registry.stage("summarize"))
    cfg2.provider = "minimax"
    cfg2.base_url = "https://api.minimaxi.com/anthropic"
    cfg2.api_key_env = "MINIMAX_API_KEY"
    client.registry._cache["summarize"] = cfg2
    P._sleep = rig.sleep
    try:
        sid = _new_session()
        with session_scope(sid):
            out = client.complete_json_forced(
                "summarize", "p", "emit_summaries",
                {"type": "object",
                 "properties": {"summaries": {"type": "array"}},
                 "required": ["summaries"]})
        check("⑤ complete_json_forced（_minimax_tools 路）529 后成功",
              rig.calls == 2
              and out == {"summaries": [{"segment": "S1·1",
                                         "summary": "概括"}]},
              f"{rig.calls} {out}")
        check("⑤ summarize 记到 1 条 retry 行",
              len(_retry_rows(sid)) == 1)
    finally:
        P._sleep = old_sleep


def t6_no_nested_retry():
    """send 额度用尽的 529 到 `_minimax` 后不得再吃 net_left 二次重试。"""
    rig, client, (cfg, os_) = _client([(529, BODY_OVERLOAD)] * 10)
    cfg.overload_retry.max_retries = 1     # send 只做 1+1=2 次
    cfg.guard.retry_on_transport = 3       # net_left 充足，旧逻辑会再加 3 次
    try:
        try:
            client._minimax(cfg, "p", None, False)
        except RuntimeError:
            pass
        check("⑥ 5xx 不会被 _minimax 旧逻辑二次重试（总请求=2）",
              rig.calls == 2, str(rig.calls))
    finally:
        _unrig(os_)


def t7_retry_row_shape():
    rig, client, (cfg, os_) = _client([(529, BODY_OVERLOAD), (200, "")])
    try:
        sid = _new_session()
        with session_scope(sid):
            # 走 complete()：ok 行由它记（_minimax 本身不记账）
            client.complete("relevance", "p")
        rows = get_repo().q(
            "SELECT status,input_tokens,output_tokens FROM llm_call "
            "WHERE session_id=? ORDER BY rowid", (sid,))
        retry = [r for r in rows if r["status"] == "retry"]
        check("⑦ retry 行 tokens=0",
              len(retry) == 1 and retry[0]["input_tokens"] == 0
              and retry[0]["output_tokens"] == 0, str(dict(retry[0]) if retry else {}))
        st = get_repo().cache_stats(sid)
        check("⑦ cache_stats.calls 不计 retry 行（单列 retries）",
              st["calls"] == 1 and st["retries"] == 1, str(st))
        check("⑦ ok/error/retry 合计与落库行数一致",
              len(rows) == 2, str(len(rows)))
    finally:
        _unrig(os_)


def t8_env_override():
    os.environ["RA_OVERLOAD_RETRIES"] = "0"
    os.environ["RA_OVERLOAD_BASE_S"] = "0"
    try:
        reg = ModelRegistry()
        cfg = reg.stage("relevance")
        check("⑧ env 覆盖：max_retries=0、base_s=0",
              cfg.overload_retry.max_retries == 0
              and cfg.overload_retry.base_s == 0.0,
              f"{cfg.overload_retry}")
    finally:
        os.environ.pop("RA_OVERLOAD_RETRIES", None)
        os.environ.pop("RA_OVERLOAD_BASE_S", None)
        ModelRegistry().stage("relevance")  # 让缓存重建，别影响其它测试


def main() -> int:
    print("=== _minimax_send 统一过载重试 ===")
    for fn in (t1_529_529_200, t2_exhausted, t3_no_retry,
               t4_stream_api_error, t5_both_entries, t6_no_nested_retry,
               t7_retry_row_shape, t8_env_override):
        fn()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败：")
        for f in FAILED:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
