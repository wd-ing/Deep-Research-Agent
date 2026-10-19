"""`node_retrieve` 探针种子注入的守门测试（离线，不联网）。

2026-10-20 加。背景：`node_probe_deep` 抓来喂轮廓模型的页面用完即丢
（`bypass_cache=True` 也不写缓存），9 个历史会话 24 个深读页面没有
一个进过 `source` 表——页面里的事实进了轮廓/规划，下游把它当成
「无出处的知识」。现在第 1 轮把 `probe.deep_urls_read` 作为
`SearchHit(engine="probe")` 种子放在命中列表最前面，走同一套
抓取/哈希去重/分段/extract 流程。

桩：`SX.search`/`TV.search`（模块级 provider 实例的方法）与
`nodes.Fetcher`（节点里 `Fetcher(repo).fetch_many(...)` 的类引用）。
DB 用 RA_DB_PATH 指的临时库；`save_run_trace`/timing 内部静默吞错，
但 `add_source`/`add_chunk` 有 session 外键，所以要真建 session 行。

用法：
    .venv/bin/python tests/test_probe_seed.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="probe_seed_"))
os.environ["RA_DB_PATH"] = str(_TMP / "t.db")
os.environ.setdefault("MINIMAX_API_KEY", "stub-not-used")

from research_agent.db.repo import get_repo  # noqa: E402
from research_agent.graph import nodes as N  # noqa: E402
from research_agent.ingest.extract import Page  # noqa: E402
from research_agent.search.base import SearchHit  # noqa: E402
from research_agent.search.normalize import normalize_url  # noqa: E402

SID = get_repo().create_session("probe_seed", "q")
TEXT = "这是关于固态电池路线取舍的测试页面正文，内容各不相同。"

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}"
          + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


class Ctx:
    """一次性桩集合：搜索返回 + 抓取页面表 + 调用记录。"""

    def __init__(self, search_hits: dict, pages: dict):
        self.search_hits = search_hits   # {"sx": [...], "tv": [...]}
        self.pages = pages               # url -> Page
        self.fetched: list[list[str]] = []

    def search_sx(self, q, language="zh-CN", limit=10):
        return list(self.search_hits.get("sx") or [])

    def search_tv(self, q, language="en", limit=10):
        return list(self.search_hits.get("tv") or [])

    def fetcher_cls(self):
        outer = self

        class FakeFetcher:
            def __init__(self, repo):
                pass

            async def fetch_many(self, urls, bypass_cache=False):
                outer.fetched.append(list(urls))
                return [outer.pages.get(u) or
                        Page(url=u, ok=False, reason="no_stub") for u in urls]

        return FakeFetcher

    def __enter__(self):
        self._orig = (N.SX.search, N.TV.search, N.Fetcher)
        N.SX.search = self.search_sx
        N.TV.search = self.search_tv
        N.Fetcher = self.fetcher_cls()
        return self

    def __exit__(self, *a):
        N.SX.search, N.TV.search, N.Fetcher = self._orig


def _state(**over) -> dict:
    st = {"session_id": SID, "question": "q",
          "plan_queries": {"zh": ["测试查询"], "en": []},
          "round": 0, "visited_urls": [], "sources": [],
          "used_queries": [], "probe": None}
    st.update(over)
    return st


def _page(url: str, i: int = 0) -> Page:
    return Page(url=url, ok=True, text=TEXT + str(i),
                status=200, title=f"t{i}")


def _hit(url: str) -> SearchHit:
    return SearchHit(url=url, title="h", snippet="s", engine="searxng")


def t1_seeds_before_search_hits():
    """rnd=1：种子 [A,B] + 搜索 [B,C] → 抓 A,B,C（B 不重复）。"""
    ctx = Ctx({"sx": [_hit("u://B"), _hit("u://C")]},
              {"u://A": _page("u://A", 1), "u://B": _page("u://B", 2),
               "u://C": _page("u://C", 3)})
    with ctx:
        out = N.node_retrieve(
            _state(probe={"deep_urls_read": ["u://A", "u://B"]}))
    ev = [e for e in out.get("events", []) if e.get("kind") == "retrieve"][0]
    check("① 抓取顺序 A,B,C（B 去重）",
          ctx.fetched == [["u://A", "u://B", "u://C"]], str(ctx.fetched))
    check("① accepted=3", ev.get("accepted") == 3, str(ev))
    check("① seeded_from_probe=2", ev.get("seeded_from_probe") == 2)
    check("① 种子进了 visited（归一化后，下轮不再抓）",
          all(normalize_url(u) in out["visited_urls"]
              for u in ("u://A", "u://B")), str(out["visited_urls"]))
    rows = get_repo().q(
        "select url,engine from source where session_id=? and url like 'u://%' "
        "order by rowid", (SID,))
    check("① 种子落了 source 表且 engine=probe",
          rows[0]["url"] == "u://A" and rows[0]["engine"] == "probe",
          str(rows))


def t2_budget_seed_first():
    """额度只剩 1：抓的是种子 A，不是搜索命中。"""
    ctx = Ctx({"sx": [_hit("u://C2")]},
              {"u://A2": _page("u://A2", 5), "u://C2": _page("u://C2", 6)})
    st = _state(probe={"deep_urls_read": ["u://A2", "u://B2"]},
                sources=[{}] * (N.HARD["max_urls"] - 1))
    with ctx:
        out = N.node_retrieve(st)
    ev = [e for e in out.get("events", []) if e.get("kind") == "retrieve"][0]
    check("② 额度 1 时只抓种子 A",
          ctx.fetched == [["u://A2"]], str(ctx.fetched))
    check("② seeded_from_probe=2（B 被预算截掉但已计入种子）",
          ev.get("seeded_from_probe") == 2)
    check("② accepted=1", ev.get("accepted") == 1)


def t3_round2_no_seed():
    """rnd>=2：probe 里有 deep_urls_read 也不加种子。"""
    ctx = Ctx({"sx": [_hit("u://C3")]},
              {"u://C3": _page("u://C3", 7)})
    st = _state(probe={"deep_urls_read": ["u://A3"]}, round=1,
                visited_urls=["u://old"], sources=[{"id": "x"}])
    with ctx:
        out = N.node_retrieve(st)
    ev = [e for e in out.get("events", []) if e.get("kind") == "retrieve"][0]
    check("③ 第 2 轮不抓种子", ctx.fetched == [["u://C3"]], str(ctx.fetched))
    check("③ seeded_from_probe=0", ev.get("seeded_from_probe") == 0)


def t4_no_probe_no_seed():
    """probe=None / 缺 deep_urls_read：不报错、无种子。"""
    ctx = Ctx({"sx": [_hit("u://C4")]}, {"u://C4": _page("u://C4", 8)})
    with ctx:
        out = N.node_retrieve(_state(probe=None))
        out2 = N.node_retrieve(_state(probe={"quality": "ok"},
                                    visited_urls=["u://C4"]))
    ev = [e for e in out.get("events", []) if e.get("kind") == "retrieve"][0]
    ev2 = [e for e in out2.get("events", []) if e.get("kind") == "retrieve"][0]
    check("④ probe=None 不报错、无种子",
          ev.get("seeded_from_probe") == 0
          and ctx.fetched[0] == ["u://C4"])
    check("④ 缺 deep_urls_read 也无种子",
          ev2.get("seeded_from_probe") == 0)


def t5_seed_fetch_fail_skipped():
    """种子抓取失败进 skipped，不影响其余命中。"""
    ctx = Ctx({"sx": [_hit("u://C5")]},
              {"u://A5": Page(url="u://A5", ok=False, reason="403"),
               "u://C5": _page("u://C5", 9)})
    with ctx:
        out = N.node_retrieve(_state(probe={"deep_urls_read": ["u://A5"]}))
    ev = [e for e in out.get("events", []) if e.get("kind") == "retrieve"][0]
    check("⑤ 失败种子进 skipped",
          any(s.get("url") == "u://A5" and s.get("reason") == "403"
              for s in ev.get("skipped") or []), str(ev.get("skipped")))
    check("⑤ 其余命中照常 accepted=1", ev.get("accepted") == 1)
    check("⑤ seeded_from_probe=1", ev.get("seeded_from_probe") == 1)


def main() -> int:
    print("=== node_retrieve 探针种子 ===")
    for fn in (t1_seeds_before_search_hits, t2_budget_seed_first,
               t3_round2_no_seed, t4_no_probe_no_seed,
               t5_seed_fetch_fail_skipped):
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
