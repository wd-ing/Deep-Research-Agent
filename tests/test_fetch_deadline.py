"""抓取整体 deadline（FETCH_DEADLINE_S）的守门测试（离线，桩 httpx）。

2026-10-25 B2-2 加。v19 R1 fetch_all 49.8s 是单页长尾拖的
（p50 3.1s / p90 15.0s）——给每个 URL 加整体 deadline，
超时 Page(ok=False, reason="deadline") 走既有失败路径。

要点：① 永不返回的 URL 在 deadline 内收尾；② 其余 URL 不受影响；
③ 取消时信号量被释放，后续批次仍能抓。

用法：.venv/bin/python tests/test_fetch_deadline.py
"""

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import research_agent.ingest.extract as ext            # noqa: E402
from research_agent.ingest.extract import Fetcher, Page  # noqa: E402

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}" + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


class _Resp:
    status_code = 200
    encoding = "utf-8"
    content = b"<html><body>ok</body></html>"


class _FakeClient:
    """hang 路径永不返回；其余立即给 200。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url: str):
        if "hang" in url:
            await asyncio.sleep(3600)
        return _Resp()


class _Repo:
    def cache_get(self, norm, ttl_days):
        return None

    def cache_put(self, *a, **kw):
        pass


def _rig(deadline: float = 0.5) -> Fetcher:
    f = Fetcher(_Repo())
    # 抽正文那段走真 trafilatura 没必要——本测试只看抓取边界。
    f._extract = lambda html, url, status: Page(
        url=url, ok=True, text="x" * 500, status=status)
    old_deadline = ext.FETCH_DEADLINE_S
    old_client = ext.httpx.AsyncClient
    ext.FETCH_DEADLINE_S = deadline
    ext.httpx.AsyncClient = lambda *a, **kw: _FakeClient()
    f._restore = (old_deadline, old_client)
    return f


def _unrig(f: Fetcher) -> None:
    ext.FETCH_DEADLINE_S, ext.httpx.AsyncClient = f._restore


def t1_deadline_and_peers():
    """hang URL → ok=False/reason=deadline；好 URL 照常；整体不拖死。"""
    f = _rig(deadline=0.5)
    try:
        t0 = time.time()
        pages = asyncio.run(f.fetch_many(
            ["https://hang.example.com/a", "https://ok.example.com/b",
             "https://ok.example.com/c"]))
        wall = time.time() - t0
        by_url = {p.url: p for p in pages}
        hang = by_url["https://hang.example.com/a"]
        check("① hang → ok=False", not hang.ok, f"reason={hang.reason}")
        check("① hang reason=deadline", hang.reason == "deadline", hang.reason)
        check("① 好页照常 ok", by_url["https://ok.example.com/b"].ok
              and by_url["https://ok.example.com/c"].ok)
        check("① 批次 wall < 30s（没被拖满 3600s）", wall < 30, f"{wall:.2f}s")

        # 信号量释放：取消后马上再跑一批，必须照样完成。
        pages2 = asyncio.run(f.fetch_many(["https://ok.example.com/d"]))
        check("① 取消后信号量释放、下批能抓", pages2[0].ok,
              f"reason={pages2[0].reason}")
        check("① sem 计数复位", f.sem._value == 8, str(f.sem._value))
    finally:
        _unrig(f)


def t2_env_override():
    """RA_FETCH_DEADLINE_S 可覆盖默认 20。"""
    import importlib
    import os
    os.environ["RA_FETCH_DEADLINE_S"] = "7"
    importlib.reload(ext)
    try:
        check("② env 覆盖 7s", ext.FETCH_DEADLINE_S == 7.0,
              str(ext.FETCH_DEADLINE_S))
    finally:
        os.environ.pop("RA_FETCH_DEADLINE_S", None)
        importlib.reload(ext)
        check("② 无 env 回落 20s", ext.FETCH_DEADLINE_S == 20.0,
              str(ext.FETCH_DEADLINE_S))


if __name__ == "__main__":
    t1_deadline_and_peers()
    t2_env_override()
    print()
    if FAILED:
        print("失败：" + "、".join(FAILED))
        sys.exit(1)
    print("全过 ✅")
