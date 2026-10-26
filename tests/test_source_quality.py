"""批 7 任务 2 离线守门：source_quality 规则 + node_retrieve 丢弃点。

不发真实调用。用法：.venv/bin/python tests/test_source_quality.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []


def check(label, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {label}"
          + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def t_defect():
    from research_agent.search.quality import page_defect, load_rules
    r = load_rules()
    check("规则已载入（yaml 在位）", len(r["domains"]) >= 5, str(r["domains"]))

    # 域名命中：精确、子域都丢；notliepin 不丢
    check("liepin.com 命中",
          page_defect("https://www.liepin.com/job/1", "JD", "x")
          == "drop_domain:liepin.com")
    check("a.liepin.com 子域命中",
          page_defect("https://a.liepin.com/x", "JD", "x") is not None)
    check("notliepin.com 不命中",
          page_defect("https://notliepin.com/x", "正常标题", "正常正文") is None)
    check("jobxzc.com 命中",
          page_defect("https://jobxzc.com/a", "HR看简历", "x") is not None)

    # 未渲染模板标题
    check("{{custom_sec.title}} 命中",
          page_defect("https://ccj.pku.edu.cn/a", "{{custom_sec.title}}", "x")
          == "template_title")
    check("${var} 命中",
          page_defect("https://x.com", "${title}", "x") == "template_title")
    check("<% %> 命中",
          page_defect("https://x.com", "<% title %>", "x") == "template_title")
    # 正常含单花括号的技术文章不误判
    check("单 { 不误判",
          page_defect("https://x.com", "JSON 形如 {a:1} 的结构", "x") is None)
    # 正文前 300 字内 {{}} 丢；301 字之后不判
    check("正文头模板命中",
          page_defect("https://x.com", "正常标题", "{{body.x}} 正文") is not None)
    late = "正文前 300 字内没有模板。" * 30 + "{{late.var}}"
    check("{{}} 在 300 字后不判",
          page_defect("https://x.com", "正常标题", late) is None,
          f"len>300 part starts at {late.find('{{')}")


def t_rules_fallback(tmp_path: Path):
    import importlib
    import research_agent.search.quality as Q
    # YAML 缺失 → 空规则不抛、page_defect 恒 None
    orig_path, orig_rules = Q.RULES_PATH, Q._rules
    try:
        Q.RULES_PATH = Path(tmp_path) / "nope.yaml"
        Q._rules = None
        r = Q.load_rules()
        check("YAML 缺失回落空规则",
          r["domains"] == set() and r["title"] == [] and r["head"] == [])
        check("空规则时 page_defect 恒 None",
              Q.page_defect("https://liepin.com", "{{x}}", "{{y}}") is None)
        # YAML 损坏同样回落
        bad = Path(tmp_path) / "bad.yaml"
        bad.write_text("drop_domains: [unclosed", encoding="utf-8")
        Q.RULES_PATH = bad
        Q._rules = None
        check("YAML 损坏回落空规则",
              Q.load_rules()["domains"] == set())
    finally:
        Q.RULES_PATH, Q._rules = orig_path, orig_rules


def t_retrieve_skip():
    """node_retrieve：命中页进 skipped(reason=low_quality:*)且不入库。"""
    from research_agent.graph import nodes as N
    from research_agent.search.base import SearchHit
    from research_agent.db.repo import Repo

    class P:
        def __init__(self, url, title, text):
            self.ok, self.url, self.title, self.text = True, url, title, text
            self.published, self.status, self.reason = None, 200, ""

    class FakeFetcher:
        def __init__(self, repo): pass
        async def fetch_many(self, urls, bypass_cache=False):
            out = []
            for u in urls:
                if "liepin.com" in u:
                    out.append(P(u, "招商团队招聘要求", "招聘职责若干"))
                else:
                    out.append(P(u, "Rust 与 Go 云原生后端对比", "正文讲 Rust Go 取舍"))
            return out

    class FakeProv:
        def __init__(self, hits): self.hits = hits
        def search(self, q, language=None, limit=8): return list(self.hits)

    repo = Repo(Path(tempfile.mkdtemp()) / "r.db")
    repo.init_schema()
    sid = repo.create_session("t", "Rust 与 Go 取舍")
    hits = [SearchHit(url="https://www.liepin.com/job/1", engine="t"),
            SearchHit(url="https://tonybai.com/a", engine="t")]
    fp, ff, fg = (N.SX, N.TV), N.Fetcher, N.get_repo
    N.SX = FakeProv(hits); N.TV = FakeProv([])
    N.Fetcher = FakeFetcher
    N.get_repo = lambda path=None: repo
    try:
        out = N.node_retrieve({"session_id": sid, "round": 0,
                               "visited_urls": [], "conflict_hashes": {},
                               "events": [], "probe": {},
                               "question": "Rust 与 Go 取舍",
                               "queries": {"zh": ["Rust Go"], "en": []},
                               "plan_queries": {"zh": ["Rust Go"], "en": []}})
    finally:
        N.SX, N.TV = fp; N.Fetcher = ff; N.get_repo = fg
    ev = (out or {}).get("events") or []
    sk = []
    for e in ev:
        sk += (e.get("skipped") or []) +               [x for x in (e.get("skipped") or [])]
    # skipped 在 retrieve 事件里
    all_sk = [x for e in ev for x in (e.get("skipped") or [])]
    lq = [x for x in all_sk if "low_quality:" in str(x.get("reason", ""))]
    check("retrieve: liepin 页进 skipped(low_quality)",
          len(lq) >= 1, str(all_sk)[:200])
    check("retrieve: liepin 页未入库",
          repo.q1("SELECT COUNT(*) n FROM source WHERE session_id=? "
                  "AND url LIKE '%liepin%'", (sid,))["n"] == 0)
    check("retrieve: 正常页入库",
          repo.q1("SELECT COUNT(*) n FROM source WHERE session_id=? "
                  "AND url LIKE '%tonybai%'", (sid,))["n"] == 1,
          str(repo.q("SELECT url FROM source WHERE session_id=?", (sid,))))


def main():
    t_defect()
    t_rules_fallback(Path(tempfile.mkdtemp()))
    t_retrieve_skip()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项未过：{FAILED}")
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    main()
