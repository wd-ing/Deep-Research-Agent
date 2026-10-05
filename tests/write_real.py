"""用 2026-10-04 那场研究的真实数据单跑 write 子图。

按约定 1，write 板块做完之前测试只打写作环节。数据来自
/tmp/ra_e2e7.db：12 节大纲 + 202 条断言 + 22 个来源，一个字不用编。

跑完输出四轴评估：
  术语一致性 / 跨任务重复率 / 引用规范率 / 任务分布
外加缓存命中率与成本。

用法：python tests/write_real.py [轮次上限]
"""

from __future__ import annotations

import json
import os
import re
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

SRC_DB = "/tmp/ra_e2e7.db"
OUT_DB = os.environ.setdefault("RA_DB_PATH", "/tmp/ra_write_real.db")

from research_agent.db.repo import get_repo  # noqa: E402
from research_agent.llm.provider import session_scope  # noqa: E402
from research_agent.graph.subgraphs import write as W  # noqa: E402


def load_real() -> tuple:
    if not Path(SRC_DB).exists():
        sys.exit(f"缺真实数据 {SRC_DB}")
    import sqlite3
    c = sqlite3.connect(SRC_DB)
    c.row_factory = sqlite3.Row
    plan = json.loads(c.execute("SELECT outline_json FROM session LIMIT 1")
                      .fetchone()[0] or "{}")
    outline = plan.get("sections") or []
    ev = [r[0] for r in c.execute(
        "SELECT all_evidence FROM session LIMIT 1")] if False else None

    # 断言只在上一次运行的 state 里，报告与 chunk 在库里；
    # 这里从上一轮 e2e 日志重建断言：source 表 + 报告里的引用反推太脆弱，
    # 所以直接复用报告正文与大纲，断言用上一轮抽取的真实文本。
    rows = c.execute(
        "SELECT s.url, s.domain, s.title, s.source_type, s.source_date, "
        "       s.char_count, c.content, c.heading_path "
        "FROM chunk c LEFT JOIN source s ON s.id=c.source_id "
        "WHERE c.session_id IS NOT NULL LIMIT 400").fetchall()
    sources, seen = [], set()
    for r in rows:
        if not r["url"] or r["url"] in seen:
            continue
        seen.add(r["url"])
        sources.append(dict(r))
    outline_md = (Path(sorted(Path("/tmp").glob("e2e_report_s_*.md"),
                             key=lambda p: p.stat().st_mtime)[-1])
                  .read_text(encoding="utf-8"))
    return outline, sources, outline_md, c


def build_claims(c, sources) -> list[dict]:
    """从真实 chunk 正文重建断言。

    上一轮的断言存在 LangGraph checkpoint 里，结构不稳定；这里用
    「每段 chunk 抽前两句」当作断言原文——内容是真的，只是粒度比
    抽取环节粗。够验证 write 子图的行为。
    """
    url2n = {s["url"]: i + 1 for i, s in enumerate(sources)}
    out: list[dict] = []
    for r in c.execute(
            "SELECT c.content, s.url, s.source_type FROM chunk c "
            "LEFT JOIN source s ON s.id=c.source_id "
            "WHERE c.session_id IS NOT NULL ORDER BY c.ingested_at LIMIT 260"):
        text = (r["content"] or "").strip()
        if len(text) < 40:
            continue
        sentences = [s for s in re.split(r"(?<=[。！？])", text) if len(s) > 12]
        for sent in sentences[:2]:
            n = url2n.get(r["url"])
            if not n:
                continue
            out.append({"i": len(out), "text": sent.strip()[:300], "entities": [],
                        "source": n, "nature": "unknown",
                        "confidence": "medium", "date": None})
    return out


def main() -> None:
    repo = get_repo()
    outline, sources, old_md, c = load_real()
    evidence = build_claims(c, sources)
    src_lines = [f"[S{i+1}] {s['title'] or s['domain']} | {s['domain']} "
                 f"| {s['source_type']} | {s['source_date'] or '日期未知'} "
                 f"| 正文 {s['char_count'] or 0} 字"
                 for i, s in enumerate(sources)]
    # 必须按出现顺序编号，evidence 里的 source 用的是同一个下标
    print(f"真实数据：大纲 {len(outline)} 节、来源 {len(sources)} 个、"
          f"断言 {len(evidence)} 条")

    sid = repo.create_session("write 子图真实数据测试",
                              "固态电池的产业化进展，以及各家技术路线的分歧点")
    t0 = time.time()
    # 必须进会话作用域，否则 llm_call 的 session_id 全是 NULL，
    # 成本与缓存命中率都算不出来（真实链路里 node_write 上的 @scoped 负责这件事，
    # 单跑子图时得自己进）。
    with session_scope(sid):
        out = W.run_write(
            session_id=sid,
            question=repo.q1("SELECT question FROM session WHERE id=?",
                             (sid,))["question"],
            outline=outline, signals={}, evidence=evidence, src_lines=src_lines)
    dt = round(time.time() - t0, 1)
    md = out.get("report_markdown") or ""
    Path(f"/tmp/write_real_report.md").write_text(md, encoding="utf-8")

    print(f"\n{'='*72}\n耗时 {dt} 秒，报告 {len(md)} 字 → /tmp/write_real_report.md")
    print(f"{'='*72}")

    for e in out.get("events") or []:
        print("  ·", json.dumps(e, ensure_ascii=False)[:260])

    # ---------------- 四轴评估 ----------------
    print(f"\n{'='*72}\n四轴评估\n{'='*72}")
    tr = repo.write_trace(sid)
    spec = tr.get("spec") or {}
    tasks = tr.get("tasks") or []
    audit = tr.get("audit") or {}

    print(f"\n【轴 0】主 agent 的规划")
    print(f"  论点: {str(spec.get('thesis'))[:150]}")
    print(f"  术语表 {len(spec.get('glossary') or [])} 条 | "
          f"口径 {len(spec.get('units') or [])} 条 | "
          f"注意事项 {len(spec.get('cautions') or [])} 条")
    print(f"  规约自检: {str(spec.get('spec_uncertainty'))[:200] or '（空）'}")
    print(f"  任务 {len(tasks)} 个，上限 {W.MAX_TASKS}")
    for t in tasks:
        print(f"    {t['id']:<5} 覆盖 {t['covers']}  证据 {len(t['evidence']):>3} 条"
              f"  依赖 {t.get('depends_on') or '无'}")

    # 轴 1：术语一致性
    print(f"\n【轴 1】术语一致性")
    gl = spec.get("glossary") or []
    bad_terms = []
    for g in gl[:8]:
        canon = g.get("canonical")
        if not canon:
            continue
        n = len(re.findall(re.escape(canon), md))
        variants = {a: len(re.findall(re.escape(str(a)), md))
                    for a in (g.get("aliases") or [])}
        print(f"  规范名「{canon}」出现 {n} 次；别名 {variants}")
        if n == 0:
            bad_terms.append(canon)

    # 轴 2：跨任务重复
    print(f"\n【轴 2】跨任务重复率")
    per = {}
    for t in tasks:
        srcs = {str(evidence[i]["source"]) for i in t["evidence"]
                if 0 <= i < len(evidence)}
        per[t["id"]] = srcs
    multi = {s: [tid for tid, ss in per.items() if s in ss]
             for s in {x for ss in per.values() for x in ss}}
    shared = {k: v for k, v in multi.items() if len(v) > 1}
    print(f"  跨多个任务引用的来源: {len(shared)} / {len(multi)}")
    for s, ts in list(shared.items())[:6]:
        print(f"    [S{s}] 出现在 {ts}")
    sizes = sorted((len(v) for v in per.values()), reverse=True)
    print(f"  每任务来源数 最大 {sizes[0] if sizes else 0} / 最小 {sizes[-1] if sizes else 0}"
          f"（上一轮分节版是 22 / 3）")

    # 轴 3：引用规范
    print(f"\n【轴 3】引用规范")
    loose = W.CITATION_LOOSE_RE.findall(md)
    odd = [x for x in loose if not re.fullmatch(r"\[S\d+\]", x)]
    all_cites = {int(x) for x in re.findall(r"\[S(\d+)\]", md)}
    listed = {int(x) for x in re.findall(r"^\[S(\d+)\]", "\n".join(src_lines), re.M)}
    print(f"  引用总数 {len(re.findall(r'\[S\\d+\\]', md))}，"
          f"涉及 {len(all_cites)} 个来源")
    print(f"  非标准写法: {len(odd)} 处 {odd[:5]}")
    print(f"  引用了但清单里没有: {sorted(all_cites - listed) or '无'}")
    print(f"  程序校验出的任务级问题: "
          f"{json.dumps(tr.get('stats') or {}, ensure_ascii=False)[:200]}")

    # 轴 4：审查
    print(f"\n【轴 4】审查与返修")
    print(f"  需要返修: {audit.get('need_rework')}  "
          f"返修轮数: {out.get('rework_rounds', 0)}  "
          f"判为有效: {out.get('rework_effective')}")
    print(f"  理由: {str(audit.get('reason'))[:200]}")
    print(f"  规约存疑: {str(audit.get('spec_issue'))[:200]}")
    for f in (audit.get("findings") or [])[:6]:
        print(f"    [{f.get('severity')}] {f.get('task')}/{f.get('section')}: "
              f"{str(f.get('problem'))[:120]}")
    if audit.get("remaining_issues"):
        print(f"  改不动的问题: {audit['remaining_issues']}")

    # 缓存与成本
    print(f"\n{'='*72}\n缓存与成本\n{'='*72}")
    cs = repo.cache_stats(sid)
    print(f"  调用 {cs['calls']} 次 | 缓存读 {cs['cache_read_tokens']} token"
          f" | 建缓存 {cs['cache_creation_tokens']} | "
          f"未命中输入 {cs['uncached_input_tokens']}")
    print(f"  缓存命中率: {cs['cache_hit_ratio']:.1%}")
    per_stage = repo.q(
        "SELECT stage, COUNT(*) n, SUM(input_tokens) it, "
        "  SUM(cache_read_tokens) cr, ROUND(AVG(latency_ms)/1000.0,1) sec "
        "FROM llm_call WHERE session_id=? GROUP BY stage ORDER BY it DESC", (sid,))
    print(f"\n  {'阶段':<16}{'次数':>4}{'未命中入':>9}{'缓存读':>9}{'均秒':>7}")
    for r in per_stage:
        print(f"  {r['stage']:<16}{r['n']:>4}{r['it'] or 0:>9}"
              f"{r['cr'] or 0:>9}{r['sec'] or 0:>7}")
    est = repo.q1("SELECT SUM(est_input_tokens) e, SUM(input_tokens) a "
                  "FROM llm_call WHERE session_id=?", (sid,))
    print(f"\n  本地估算输入 {est['e'] or 0} vs API 报的真实输入 {est['a'] or 0}"
          f"（估算偏差 {((est['e'] or 0)/(est['a'] or 1)-1):+.0%}）")
    print(f"\n  session_id = {sid}")


if __name__ == "__main__":
    main()
