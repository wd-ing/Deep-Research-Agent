"""快速档（C1）离线守门：mode 字段/API/路由/取数/证据拼装/node_answer。

不发真实调用。用法：.venv/bin/python tests/test_fast_mode.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _isolate  # noqa: F401, E402
os.environ.setdefault("MINIMAX_API_KEY", "stub-not-used")

FAILED: list[str] = []


def check(label, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {label}"
          + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


# ---------------- 迁移 ----------------

def t_migration():
    from research_agent.db.repo import Repo
    tmp = Path(tempfile.mkdtemp()) / "m.db"
    r = Repo(tmp)
    r.init_schema()
    sid = r.create_session("t", "q")
    row = r.q1("SELECT mode FROM session WHERE id=?", (sid,))
    check("迁移：新库 mode 列 + 默认 standard",
          row["mode"] == "standard", str(row["mode"]))
    sid2 = r.create_session("t2", "q2", mode="fast")
    check("create_session(mode=fast) 落库",
          r.q1("SELECT mode FROM session WHERE id=?", (sid2,))["mode"]
          == "fast")
    # 旧库升级：v22 库没有 mode 列
    src = ROOT / "data/refs/ra_v22_final.db"
    if src.exists():
        old = Path(tempfile.mkdtemp()) / "old.db"
        shutil.copy(src, old)
        r2 = Repo(old)
        r2.init_schema()
        v = r2.q1("SELECT mode FROM session LIMIT 1")
        check("旧库升级：session.mode='standard'", v["mode"] == "standard")
    else:
        check("旧库升级（v22 库缺失，跳过）", True)


# ---------------- CreateIn / 路由 / caps ----------------

def t_api_route():
    from research_agent import server as S
    from fastapi import HTTPException
    from pydantic import ValidationError
    check("CreateIn 缺省 standard", S.CreateIn(question="q").mode
          == "standard")
    check("CreateIn fast 通过", S.CreateIn(question="q", mode="fast")
          .mode == "fast")
    try:
        S.CreateIn(question="q", mode="xxx")
        check("CreateIn 非法值 422", False, "未抛")
    except ValidationError:
        check("CreateIn 非法值 422", True)
    from research_agent.graph.nodes import route_after_gate, _mode_caps
    check("route_after_gate fast→answer",
          route_after_gate({"mode": "fast"}) == "answer")
    check("route_after_gate standard/缺省/未知→assess",
          route_after_gate({"mode": "standard"}) == "assess"
          and route_after_gate({}) == "assess"
          and route_after_gate({"mode": "zzz"}) == "assess")
    check("_mode_caps 标准档与原常量一致",
          _mode_caps({}) == (50, 5) and _mode_caps({"mode": "x"}) == (50, 5))
    check("_mode_caps 快速档 (24,3)", _mode_caps({"mode": "fast"}) == (24, 3))


# ---------------- _load_numbered_evidence ----------------

def t_evidence():
    from research_agent.graph import nodes as N
    from research_agent.db.repo import Repo
    tmp = Path(tempfile.mkdtemp()) / "e.db"
    r = Repo(tmp)
    r.init_schema()
    sid = r.create_session("t", "q")
    a = r.add_source(session_id=sid, url="http://a", url_normalized="a",
                     domain="a.com", title="A", source_type="s",
                     engine="t", source_date=None, content_hash="h1",
                     char_count=1, http_status=200)
    b = r.add_source(session_id=sid, url="http://b", url_normalized="b",
                     domain="b.com", title="B", source_type="s",
                     engine="t", source_date=None, content_hash="h2",
                     char_count=1, http_status=200)
    for i, (so, txt) in enumerate(((a, "aa"), (a, "ab"), (b, "bb"))):
        r.add_chunk(session_id=sid, source_id=so, heading_path=f"h{i}",
                    content=txt, summary=f"sum{i}", token_count=10,
                    origin="search", on_topic=1, ingested_at="t",
                    status="done")
    r.x("UPDATE chunk SET on_topic=0 WHERE heading_path='h2'")
    ev = N._load_numbered_evidence(r, sid)
    check("编号：S1·1 S1·2 S2·1",
          [c["ref"] for c in ev["segs"]] == ["S1·1", "S1·2", "S2·1"],
          str([c["ref"] for c in ev["segs"]]))
    check("keep 只留 on_topic",
          [c["ref"] for c in ev["keep"]] == ["S1·1", "S1·2"],
          str([c["ref"] for c in ev["keep"]]))
    check("src_lines 编号与来源一致",
          ev["src_lines"][0].startswith("[S1] A") and
          ev["src_lines"][1].startswith("[S2] B"))
    check("n_no_summary 计数", ev["n_no_summary"] == 0)


# ---------------- 证据拼装 ----------------

def t_lines():
    from research_agent.graph.nodes import _answer_evidence_lines
    keep = [{"source_id": "s1", "ref": "S1·1", "heading_path": "h",
             "summary": "概" * 600},
            {"source_id": "s1", "ref": "S1·2", "heading_path": "h",
             "summary": "s2"},
            {"source_id": "s2", "ref": "S2·1", "heading_path": "h",
             "summary": "s3"},
            {"source_id": "s2", "ref": "S2·2", "heading_path": "h",
             "summary": ""}]
    lines, used, dropped = _answer_evidence_lines(keep)
    check("无概括段不进", used == 3 and len(lines) == 3)
    check("summary 截 450", all("概" * 451 not in l for l in lines))
    check("行格式", lines[0].startswith("[S1·1] h | "))
    # 超上限：段数最多来源从末尾丢
    big = [{"source_id": "s1", "ref": f"S1·{i}", "heading_path": "h",
            "summary": "x" * 900} for i in range(1, 200)]
    big += [{"source_id": "s2", "ref": "S2·1", "heading_path": "h",
             "summary": "y" * 100}]
    lines2, used2, dropped2 = _answer_evidence_lines(big)
    tot = sum(len(x) for x in lines2)
    check("超上限去段后 ≤80000", tot <= 80000, str(tot))
    check("去段只砍段数最多来源",
          dropped2 > 0
          and sum(1 for l in lines2 if l.startswith("[S1·")) < 199)
    check("S2·1 保留（小来源）", any(l.startswith("[S2·1]")
                                    for l in lines2))


# ---------------- node_answer 桩 ----------------

def _state(tmpdb, mode="fast", with_src=True):
    from research_agent.db.repo import Repo
    r = Repo(tmpdb)
    r.init_schema()
    sid = r.create_session("t", "问什么", mode=mode)
    if with_src:
        a = r.add_source(session_id=sid, url="http://a",
                         url_normalized="a", domain="a.com",
                         title="来源A", source_type="news", engine="t",
                         source_date=None, content_hash="h1",
                         char_count=1, http_status=200)
        for i, su in enumerate(("概括甲句", "概括乙句")):
            r.add_chunk(session_id=sid, source_id=a, heading_path=f"h{i}",
                        content="x", summary=su, token_count=10,
                        origin="search", on_topic=1, ingested_at="t",
                        status="done")
    return r, sid, {"session_id": sid, "mode": mode, "question": "问什么",
                    "outline": [{"id": "sec1", "title": "小节一",
                                 "question": "q1"}],
                    "events": []}


def t_node_answer():
    from research_agent.graph import nodes as N
    tmp = Path(tempfile.mkdtemp()) / "n.db"

    class Stub:
        def __init__(self, ret): self.ret = ret
        def complete(self, stage, prompt, system=None):
            self.last = (stage, system)
            if isinstance(self.ret, Exception):
                raise self.ret
            return self.ret

    def run(ret, **kw):
        r, sid, st = _state(tmp, **kw)
        stub = Stub(ret)
        o = N.LLM.complete
        o_repo = N.get_repo
        N.LLM.complete = stub.complete
        N.get_repo = lambda path=None: r
        try:
            out = N.node_answer(st)
        finally:
            N.LLM.complete = o
            N.get_repo = o_repo
        return r, sid, out, stub

    # (a) 合规文本
    good = ("## 直接回答\n\n结论先行 [S1·1][S1·2]。\n\n## 分歧与不确定\n\n"
            "只有单一来源 [S1·1]。")
    r, sid, out, stub = run(good)
    md = out["report_markdown"]
    ev = out["events"][0]
    check("a 走 fast_answer 档 + 系统提示词",
          stub.last[0] == "fast_answer"
          and stub.last[1] is __import__(
              "research_agent.graph.prompts", fromlist=["x"])
          .FAST_ANSWER_SYSTEM)
    check("a 正文+来源清单落库",
          "## 直接回答" in md and "### 来源" in md
          and "[S1] 来源A" in md, md[:80])
    check("a 事件字段", ev["kind"] == "answer"
          and ev["citations_removed"] == 0
          and ev["segments_used"] == 2
          and ev["degraded"] is False, str(ev)[:200])

    # (b) 越界+自创+t1 泄漏 → 修复/删除+存疑节
    bad = "## 答\n\n某句 [S99·1] 与 [S1·1-2 类同] 还有 t1 泄漏 [S1·1]。"
    r, sid, out, stub = run(bad)
    md = out["report_markdown"]
    ev = out["events"][0]
    check("b 越界/自创引用被删",
          "[S99·1]" not in md and "类同" not in md
          and "[S1·1]" in md, md[:140])
    check("b 存疑节出现且 citations_removed>0",
          "## 存疑与未完成" in md and ev["citations_removed"] >= 1,
          str(ev)[:160])

    # (c) 空输出/抛错 → 降级产出，会话不失败
    r, sid, out, stub = run("")
    check("c 空输出 → 降级检索清单",
          "# 检索结果" in out["report_markdown"]
          and out["events"][0]["degraded"] is True)
    d = r.q1("SELECT degraded FROM session WHERE id=?", (sid,))["degraded"]
    check("c session.degraded 记原因", bool(d))
    r, sid, out, stub = run(RuntimeError("MiniMax 429: boom"))
    check("c 抛错同样降级不抛", "# 检索结果" in out["report_markdown"])

    # (d) 无来源 → 「本轮没有可用来源」同款
    r, sid, st = _state(tmp, with_src=False)
    o_repo = N.get_repo
    N.get_repo = lambda path=None: r
    try:
        out = N.node_answer(st)
    finally:
        N.get_repo = o_repo
    check("d 无来源降级",
          "本轮没有可用来源" in out["report_markdown"])


def main():
    t_migration()
    t_api_route()
    t_evidence()
    t_lines()
    t_node_answer()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项未过：{FAILED}")
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    main()
