"""拿任何一个已有 session 的真实数据，单跑 write 子图。

**这既是「续跑」也是「定向测试」。** 改了 write 之后想验证，
不必把探针和检索循环重跑一遍——直接用最近一轮留下的数据接着跑就行，
省时间也省搜索额度。

流程：
    1. .venv/bin/python tests/probe_assess_run.py "问题" /tmp/x.db     # 只到 assess
    2. .venv/bin/python tests/write_resume.py /tmp/x.db [session_id]   # ← 这里接着跑
    3. .venv/bin/python tests/cost_report.py  /tmp/x.db [session_id]   # 看账

默认取库里**最新**的那个 session。不指定 session_id 就自动挑，
所以「跑完检索循环 → 立刻续跑 write」是一条命令接一条命令的事。

用法：
    .venv/bin/python tests/write_resume.py <库路径> [session_id]
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

SRC_DB = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ra_r5_e2e.db"
SID = sys.argv[2] if len(sys.argv) > 2 else None
OUT_DB = os.environ.get("RA_WRITE_RESUME_OUT") or (str(SRC_DB) + ".write")
os.environ["RA_DB_PATH"] = OUT_DB          # 必须在 get_repo 之前设好

from research_agent.db.repo import get_repo  # noqa: E402
from research_agent.llm.provider import session_scope  # noqa: E402
from research_agent.graph.subgraphs import write as W  # noqa: E402


def load(session_id: str) -> dict:
    """把这个 session 已经攒下的东西读出来，组装成 run_write 要的形状。

    **闸门不重跑**——on_topic 已经落库了，重跑一次要多花几百次调用。
    """
    import sqlite3
    repo = get_repo()

    sess = repo.q1("SELECT question, outline_json, probe_json FROM session WHERE id=?",
                   (session_id,))
    if not sess:
        sys.exit(f"库里没有 session {session_id}")
    outline = json.loads(sess["outline_json"] or "{}").get("sections") or []
    signals = (json.loads(sess["probe_json"] or "{}") or {}).get("signals") or {}

    srcs = [dict(r) for r in repo.q(
        "SELECT id, title, domain, url, source_type, source_date, char_count, "
        "       source_nature FROM source WHERE session_id=? ORDER BY rowid",
        (session_id,))]
    src_no = {s["id"]: i for i, s in enumerate(srcs, 1)}

    segs, seg_no = [], {}
    for r in repo.q(
            "SELECT id, source_id, heading_path, content, summary, token_count, "
            "       on_topic, source_date FROM chunk WHERE session_id=? ORDER BY rowid",
            (session_id,)):
        if not src_no.get(r["source_id"]):
            continue
        n = seg_no.get(r["source_id"], 0) + 1
        seg_no[r["source_id"]] = n
        segs.append({**dict(r), "ref": f"S{src_no[r['source_id']]}·{n}"})

    # 与 node_write 同源：清单编号和段号必须出自同一个映射，否则引用会错位
    src_lines = [f"[S{src_no[s['id']]}] {s.get('title') or s['domain']} | "
                 f"{s['domain']} | {s.get('source_type')} | "
                 f"{s.get('source_date') or '日期未知'} | "
                 f"{s.get('source_nature') or '性质未判'} | "
                 f"正文 {s.get('char_count') or 0} 字" for s in srcs]
    listed = {int(x) for x in __import__("re").findall(
        r"^\[S(\d+)\]", "\n".join(src_lines), __import__("re").M)}
    used = {int(c["ref"].split("·")[0][1:]) for c in segs}
    assert listed == used, (
        f"来源编号与段号不是同一套：清单 {sorted(listed)[:4]}… "
        f"段号用到 {sorted(used)[:4]}…（会导致每个引用都指错来源）")

    on_topic = [c for c in segs if c.get("on_topic")]
    n_no_summary = sum(1 for c in segs if not (c.get("summary") or "").strip())
    return {"question": sess["question"], "outline": outline, "signals": signals,
            "src_lines": src_lines, "segs": segs, "on_topic": on_topic,
            "n_no_summary": n_no_summary, "n_src": len(srcs)}


def main() -> None:
    if not Path(SRC_DB).exists():
        sys.exit(f"缺库 {SRC_DB}")
    sid = SID
    if not sid:
        import sqlite3
        c = sqlite3.connect(SRC_DB)
        rows = c.execute("SELECT id FROM session ORDER BY created_at DESC").fetchall()
        if not rows:
            sys.exit(f"{SRC_DB} 里没有 session")
        sid = rows[0][0]
    print(f"续跑：库 {SRC_DB} 的 session {sid} → 写到 {OUT_DB}", flush=True)

    for suf in ("", "-wal", "-shm"):
        p = Path(OUT_DB + suf)
        if p.exists():
            p.unlink()
    shutil.copy(SRC_DB, OUT_DB)      # EvidenceTools 靠 session_id 找段落，所以整库复制
    os.environ["RA_DB_PATH"] = OUT_DB

    d = load(sid)
    print(f"  复用材料：{d['n_src']} 个来源、{len(d['segs'])} 段"
          f"（过闸门 {len(d['on_topic'])}，无概括 {d['n_no_summary']}）、"
          f"{len(d['outline'])} 节大纲", flush=True)

    t0 = time.time()
    with session_scope(sid):
        out = W.run_write(
            session_id=sid, question=d["question"], outline=d["outline"],
            signals=d["signals"], segments=d["segs"], on_topic=d["on_topic"],
            src_lines=d["src_lines"], n_no_summary=d["n_no_summary"])
    dt = round(time.time() - t0, 1)
    md = out.get("report_markdown") or ""
    Path("/tmp/write_resume_report.md").write_text(md, encoding="utf-8")

    print(f"\n{'='*72}\nwrite 段耗时 {dt} 秒，报告 {len(md)} 字 "
          f"→ /tmp/write_resume_report.md\n{'='*72}")
    for e in out.get("events") or []:
        print("  ·", json.dumps(e, ensure_ascii=False)[:230])

    print("\n---- 看这一段的账 ----")
    print(f"  .venv/bin/python tests/cost_report.py {OUT_DB} {sid}")

    tr = get_repo().write_trace(sid)
    hist = tr.get("audit_history") or []
    print(f"\n  审查留痕 {len(hist)} 轮")
    for h in hist:
        print(f"    第 {h.get('round')} 轮：findings {h.get('findings')}、"
              f"high {h.get('high')}、need_rework {h.get('need_rework')}")
    a = tr.get("audit") or {}
    if a.get("spec_issue"):
        print(f"  规约存疑：{str(a['spec_issue'])[:200]}")
    for r in (a.get("remaining_issues") or [])[:4]:
        print(f"  改不动：{str(r)[:140]}")


if __name__ == "__main__":
    main()