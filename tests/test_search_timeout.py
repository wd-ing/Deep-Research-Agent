"""搜索超时（SEARCH_TIMEOUT_S）的守门测试（离线，桩 httpx）。

2026-10-25 加。v18 基线：Tavily p50 3.2s / p90 5.7s / max 49.3s，
SearXNG p50 3.2s / max 22.2s——长尾把 retrieve R1 拖到 82s。
现在默认单次 25s（RA_SEARCH_TIMEOUT_S 覆盖），超时**抛**给
retrieve 的失败路径（该查询记失败、事件可见、其余继续），
不再被吞成「0 命中」（那会把「慢」伪装成「空」）。

用法：.venv/bin/python tests/test_search_timeout.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx                                   # noqa: E402
from research_agent.search.searxng import SearXNGProvider  # noqa: E402
from research_agent.search.tavily import TavilyProvider    # noqa: E402

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}" + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def t1_env_default():
    """默认 25s，env 可覆盖。"""
    import importlib
    import os
    import research_agent.search.tavily as tav

    os.environ.pop("RA_SEARCH_TIMEOUT_S", None)
    importlib.reload(tav)
    p = tav.TavilyProvider(api_key="k")
    check("① 默认 25s", p.timeout_s == 25.0, str(p.timeout_s))
    os.environ["RA_SEARCH_TIMEOUT_S"] = "7"
    p = tav.TavilyProvider(api_key="k")
    check("① env 覆盖 7s", p.timeout_s == 7.0, str(p.timeout_s))
    p = tav.TavilyProvider(api_key="k", timeout_s=3.0)
    check("① 显式参数优先于 env", p.timeout_s == 3.0)
    os.environ.pop("RA_SEARCH_TIMEOUT_S", None)
    importlib.reload(tav)


def t2_timeout_raises():
    """TimeoutException 透出（retrieve 侧记 timeout=True）；其他异常仍吞。"""

    class Boom:
        def post(self, *a, **kw):
            raise httpx.ReadTimeout("stalled", request=None)

        def get(self, *a, **kw):
            raise httpx.ReadTimeout("stalled", request=None)

    import research_agent.search.tavily as tav
    old = tav.httpx.post
    tav.httpx.post = Boom().post
    try:
        try:
            tav.TavilyProvider(api_key="k").search("q")
            ok = "没抛"
        except httpx.TimeoutException:
            ok = "TimeoutException"
        check("② Tavily 超时透出（不透出就会被吞成 []）",
              ok == "TimeoutException", ok)
    finally:
        tav.httpx.post = old

    # 非超时异常仍吞 → []（现有失败语义不变）
    class Http5xx:
        def post(self, *a, **kw):
            raise httpx.ConnectError("refused", request=None)

    tav.httpx.post = Http5xx().post
    try:
        check("② 非超时异常仍返回 []",
              tav.TavilyProvider(api_key="k").search("q") == [])
    finally:
        tav.httpx.post = old


def t3_searxng_same():
    """SearXNG 同款：超时透出；env 生效。"""
    import os
    os.environ["RA_SEARCH_TIMEOUT_S"] = "9"
    p = SearXNGProvider()
    check("③ SearXNG env 9s", p.timeout_s == 9.0, str(p.timeout_s))
    os.environ.pop("RA_SEARCH_TIMEOUT_S", None)

    import research_agent.search.searxng as sx
    class To:
        def get(self, *a, **kw):
            raise httpx.ReadTimeout("stalled", request=None)
    p = SearXNGProvider()
    p.ensure_running = lambda: True   # 跳过本地服务拉起
    old = sx.httpx.get
    sx.httpx.get = To().get
    try:
        try:
            p.search("q")
            ok = "没抛"
        except httpx.TimeoutException:
            ok = "TimeoutException"
        check("③ SearXNG 超时透出", ok == "TimeoutException", ok)
    finally:
        sx.httpx.get = old


if __name__ == "__main__":
    t1_env_default()
    t2_timeout_raises()
    t3_searxng_same()
    print()
    if FAILED:
        print("失败：" + "、".join(FAILED))
        sys.exit(1)
    print("全过 ✅")
