"""用 R5 那场研究的真实数据单跑 write 子图，验证 R6 的修复。

按约定 1，write 板块做完之前测试只打写作环节。

数据来自 /tmp/ra_r5_e2e.db（session s_a9f5bc761016）：32 个来源、177 个真实段落、
10 节大纲、探针轮廓，全部是上一轮真跑出来的，一个字不用编。

跑法：把整库复制一份再跑，原库留着做对比；**跳过段级闸门**
（R5 已经把 on_topic 落库了，重跑一次要多花 577 次调用）。
EvidenceTools 靠 session_id 找段落，所以复制整库而不是新建会话。

看三件事：
  1. 多节任务能不能各自归位（装配自检有没有报警）
  2. 返修调用数与收敛情况（对照 R5 的 27 次不收敛）
  3. 审查留痕是不是留全了每一轮（R5 只剩最后一轮）

用法：.venv/bin/python tests/write_real.py [源库]
"""

from __future__ import annotations

import json
import os
import re
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
OUT_DB = "/tmp/ra_r6_real.db"
SID = "s_a9f5bc761016"
os.environ["RA_DB_PATH"] = OUT_DB          # 必须在 get_repo 之前设好

from research_agent.db.repo import get_repo  # noqa: E402
from research_agent.llm.provider import session_scope  # noqa: E402
from research_agent.graph.subgraphs import write as W  # noqa: E402


def prepare() -> None:
    if not Path(SRC_DB).exists():
        sys.exit(f"缺真实数据 {SRC_DB}")
    for suffix in ("", "-wal", "-shm"):
        p = Path(OUT_DB + suffix)
        if p.exists():
            p.unlink()
    shutil.copy(SRC_DB, OUT_DB)          # 整库复制：EvidenceTools 靠 sid 找段落
    print(f"已复制 {SRC_DB} → {OUT_DB}（原库保留用于对比）")


def load(repo) -> tuple:
    sess = repo.q1("SELECT question, outline_json, probe_json FROM session WHERE id=?",
                   (SID,))
    if not sess:
        sys.exit(f"库里没有 session {SID}")
    outline = json.loads(sess["outline_json"] or "{}").get("sections") or []
    signals = (json.loads(sess["probe_json"] or "{}") or {}).get("signals") or {}

    sources = [dict(r) for r in repo.q(
        "SELECT id, title, domain, url, source_type, source_date, char_count, "
        "source_nature FROM source WHERE session_id=? ORDER BY rowid", (SID,))]
    src_no = {s["id"]: i for i, s in enumerate(sources, 1)}

    segs = [dict(r) for r in repo.q(
        "SELECT id, source_id, heading_path, content, summary, token_count, on_topic "
        "FROM chunk WHERE session_id=? ORDER BY rowid", (SID,))]
    seg_no: dict[str, int] = {}
    for c in segs:
        if not src_no.get(c["source_id"]):
            continue
        n = seg_no.get(c["source_id"], 0) + 1
        seg_no[c["source_id"]] = n
        c["ref"] = f"S{src_no[c['source_id']]}·{n}"

    # 来源清单和段号必须用**同一个**编号。原先这里写成 enumerate(sources) 从 0 起，
    # 而 src_no 是 enumerate(sources, 1) 从 1 起——两者差一格，于是报告里
    # 每个 [Sn·m] 都指向了清单里下一行的那个来源。543 处引用全指错，没人发现。
    # 现在从 src_no 反过来生成清单，编号不可能再分叉。
    src_lines = [f"[S{src_no[s['id']]}] {s.get('title') or s['domain']} | {s['domain']} "
                 f"| {s.get('source_type')} | {s.get('source_date') or '日期未知'}"
                 f" | {s.get('source_nature') or '性质未判'}"
                 f" | 正文 {s.get('char_count') or 0} 字"
                 for s in sources]
    on_topic = [c for c in segs if c.get("on_topic")]

    # 编号一致性自检。段号里的来源部分和来源清单必须同源——
    # 这两者一旦差一格，报告里每个引用都会指向下一个来源，而且**不会报任何错**。
    listed = {int(x) for x in re.findall(r"^\[S(\d+)\]", "\n".join(src_lines), re.M)}
    used = {int(c["ref"].split("·")[0][1:]) for c in segs}
    assert listed == used, (
        f"来源编号与段号不是同一套：清单 {sorted(listed)[:4]}… "
        f"段号用到 {sorted(used)[:4]}…")
    return sess["question"], outline, signals, src_lines, segs, on_topic


def main() -> None:
    prepare()
    repo = get_repo()
    question, outline, signals, src_lines, segs, on_topic = load(repo)
    n_no_summary = sum(1 for c in segs if not (c.get("summary") or "").strip())
    multi = [t for t in (outline or []) if t.get("id")]
    print(f"真实数据：大纲 {len(outline)} 节、来源 {len(src_lines)} 个、"
          f"段落 {len(segs)} 条（闸门通过 {len(on_topic)}）、"
          f"无概括 {n_no_summary} 条")
    print("（闸门沿用 R5 已落库的结果，不重跑）")

    t0 = time.time()
    with session_scope(SID):
        out = W.run_write(
            session_id=SID, question=question, outline=outline,
            signals=signals, segments=segs, on_topic=on_topic,
            src_lines=src_lines, n_no_summary=n_no_summary)
    dt = round(time.time() - t0, 1)
    md = out.get("report_markdown") or ""
    Path("/tmp/write_real_report.md").write_text(md, encoding="utf-8")

    print(f"\n{'='*72}\n耗时 {dt} 秒，报告 {len(md)} 字 → /tmp/write_real_report.md")
    print("=" * 72)
    for e in out.get("events") or []:
        print("  ·", json.dumps(e, ensure_ascii=False)[:240])

    tr = repo.write_trace(SID)
    spec = tr.get("spec") or {}
    tasks = tr.get("tasks") or []
    audit = tr.get("audit") or {}

    # ---------------- R6 重点 1：多节任务与装配自检 ----------------
    print(f"\n{'='*72}\n【一】多节任务与装配自检（R6 的核心修复）\n{'='*72}")
    multi_sec = [t for t in tasks if len(t.get("covers") or []) > 1]
    print(f"  任务 {len(tasks)} 个，其中负责多节的有 {len(multi_sec)} 个：")
    for t in multi_sec:
        print(f"    {t['id']} → {t['covers']}")
    drafts = out.get("drafts") or {}
    for t in multi_sec:
        secs = drafts.get(t["id"]) or {}
        print(f"    {t['id']} 实际产出的���节: {sorted(secs)}")
        bodies = [v for v in secs.values() if v]
        if len(bodies) > 1:
            print(f"      正文两两不同: {len(set(bodies)) == len(bodies)}")
    asm = [e for e in (out.get("events") or []) if e.get("kind") == "write_assemble"]
    print(f"  装配自检报警: {asm[0].get('warnings') if asm else '?'} 条")
    print(f"  存疑一节里有装配自检条目: "
          f"{'装配自检' in md}")

    # ---------------- R6 重点 2：返修 ----------------
    print(f"\n{'='*72}\n【二】返修（R5 是 27 次调用且不收敛）\n{'='*72}")
    n_rev = repo.q1("SELECT COUNT(*) n FROM llm_call "
                    "WHERE session_id=? AND stage='write_revise'", (SID,))["n"]
    rev_ev = [e for e in (out.get("events") or []) if e.get("kind") == "write_revise"]
    print(f"  write_revise 调用 {n_rev} 次，返修轮数 {len(rev_ev)}")
    for e in rev_ev:
        print(f"    第 {e.get('round')} 轮 → 任务 {e.get('tasks')}")
        print(f"      目标 {e.get('targets')}")
        if e.get('first_serial_s') is not None:
            print(f"      首个串行 {e.get('first_serial_s')} 秒")
    print(f"  返修轮数 {out.get('rework_rounds', 0)}，判为有效 {out.get('rework_effective')}")
    print(f"  理由: {str(audit.get('reason'))[:220]}")

    # ---------------- R6 重点 3：审查留痕 ----------------
    print(f"\n{'='*72}\n【三】审查留痕（R5 只剩最后一轮）\n{'='*72}")
    hist = tr.get("audit_history") or []
    for h in hist:
        print(f"  第 {h.get('round')} 轮：findings {h.get('findings')} 条、"
              f"high {h.get('high')} 个、need_rework {h.get('need_rework')}、"
              f"rework_effective {h.get('rework_effective')}")
        if h.get("targets"):
            print(f"    目标 {h['targets']}")
    print(f"  共留 {len(hist)} 轮（R5 那次只查得到 1 轮）")

    print(f"\n  最后一轮 findings {len(audit.get('findings') or [])} 条：")
    for f in (audit.get("findings") or [])[:10]:
        print(f"    [{f.get('severity')}] {f.get('task')}/{f.get('section')}: "
              f"{str(f.get('problem'))[:110]}")
    if audit.get("spec_issue"):
        print(f"  规约存疑: {str(audit['spec_issue'])[:220]}")
    for r in (audit.get("remaining_issues") or [])[:5]:
        print(f"  改不动: {str(r)[:160]}")

    # ---------------- 引用质量 ----------------
    print(f"\n{'='*72}\n【四】引用与工具使用\n{'='*72}")
    all_refs = re.findall(r"\[S(\d+)·(\d+)\]", md)
    print(f"  段级引用 {len(all_refs)} 处，覆盖 {len(set(all_refs))} 个不同段落")
    print(f"  报告字数 {len(md)}")
    print(f"  程序校验出的引用问题: {out.get('violations')}")
    stats = tr.get("stats") or {}
    print(f"  工具使用: {json.dumps(stats.get('tool_reads') or {}, ensure_ascii=False)}")
    n_tool = repo.q1("SELECT COUNT(*) n FROM llm_call WHERE session_id=? "
                     "AND stage IN ('write_section','write_revise')", (SID,))["n"]
    print(f"  写作阶段调用（含工具轮）{n_tool} 次")

    cs = repo.cache_stats(SID)
    print(f"\n{'='*72}\n缓存与成本\n{'='*72}")
    print(f"  调用 {cs['calls']} 次 | 缓存读 {cs['cache_read_tokens']} token"
          f" | 建缓存 {cs['cache_creation_tokens']} | 命中率 {cs['cache_hit_ratio']:.1%}")
    for r in repo.q(
            "SELECT stage, COUNT(*) n, SUM(input_tokens) it, SUM(cache_read_tokens) cr,"
            " ROUND(AVG(latency_ms)/1000.0,1) sec FROM llm_call "
            "WHERE session_id=? GROUP BY stage ORDER BY it DESC", (SID,)):
        print(f"  {r['stage']:<16}{r['n']:>4}{r['it'] or 0:>10}"
              f"{r['cr'] or 0:>10}{r['sec'] or 0:>8}")
    print(f"\n  session_id = {SID}（库 {OUT_DB}）")


if __name__ == "__main__":
    main()
