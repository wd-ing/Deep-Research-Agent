"""合并闸门提示词回放实验（真实 API，V1 现行 / V2 变体，各 n=2 串行）。

复用生产路径 `extract_batch.summarize_segments`（批 12 段/24k 字符、
topic_label=question[:80]、emit_summaries 强制工具、合并 relevant 判定），
monkeypatch prompts.SUMMARIZE_GATE_NOTE 切 V1/V2，其余一字不动。

样本：JUNK（v25 检出的无关来源段）/ CONTROL（rule 层命中段）/
BORDER（merged 放行、规则零命中段，只报告不设判据）。

产物：/tmp/exp_gate_note/{set}_{V}x{i}.json + summary.json
用法：set -a && source .env && set +a && .venv/bin/python tests/experiments/exp_gate_note.py
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OUT = Path("/tmp/exp_gate_note")
OUT.mkdir(parents=True, exist_ok=True)

DBS = {
    "rustgo": ROOT / "data/refs/ra_v25_rustgo.db",
    "glp1": ROOT / "data/refs/ra_v25_glp1.db",
    "cbam": ROOT / "data/refs/ra_v25_cbam.db",
}

JUNK_RULES = {
    "rustgo": [("renrendoc.com", "招聘"), ("liepin.com", None),
               ("jobxzc.com", None),
               ("renrendoc.com", "微服务容器化迁移方案")],
    "glp1": [("ccj.pku.edu.cn", "{{custom_sec"),
             ("baike.so.com", "金德走班")],
}


def q(db, sql, args=()):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def chunks_of(dbname, pred):
    db = DBS[dbname]
    sid = q(db, "SELECT id FROM session LIMIT 1")[0]["id"]
    rows = q(db, "SELECT c.id cid, c.heading_path, c.content, c.summary, "
                 "c.on_topic, c.gate_method, c.topic_reason, "
                 "s.domain, s.title "
                 "FROM chunk c JOIN source s ON s.id=c.source_id "
                 "WHERE c.session_id=?", (sid,))
    return [r for r in rows if pred(r)]


def sample_sets():
    junk, control, border = [], [], []
    for name in DBS:
        q_ = q(DBS[name], "SELECT question FROM session LIMIT 1")[0]["question"]

        def is_junk(r):
            for rule in JUNK_RULES.get(name, []):
                if isinstance(rule, tuple) and len(rule) == 2:
                    d, t = rule
                    if r["domain"] == d and (t is None or t in (r["title"] or "")):
                        return True
            return False

        junk += [(name, q_, r) for r in chunks_of(name, is_junk)]
        rng = random.Random(0)
        ctrl_pool = [r for r in chunks_of(
            name, lambda r: r["on_topic"] == 1
            and r["gate_method"] == "rule" and not is_junk(r))]
        rng.shuffle(ctrl_pool)
        control += [(name, q_, r) for r in ctrl_pool[:30]]
        # 「规则命中为 0」= gate_method='merged'（规则层没过、
        # 由合并判定放行）——topic_reason 存的是方法说明不是命中数。
        b_pool = [r for r in chunks_of(
            name, lambda r: r["on_topic"] == 1
            and r["gate_method"] == "merged"
            and not is_junk(r))]
        rng.shuffle(b_pool)
        border += [(name, q_, r) for r in b_pool[:20]]
    return {"junk": junk, "control": control, "border": border}


def run_set(tag, entries, note, llm_probe=None):
    """entries: [(dbname, question, row)]，按 question 分组提交生产调用。"""
    import research_agent.graph.subgraphs.extract_batch as EB
    from research_agent.graph import prompts
    from research_agent.llm.provider import session_scope

    orig = prompts.SUMMARIZE_GATE_NOTE
    prompts.SUMMARIZE_GATE_NOTE = note
    verdicts: dict[str, list[bool | None]] = {}
    deviants = 0
    t0 = time.time()
    try:
        # 按研究对象分组（同库同题），与生产同口径逐组提交
        groups: dict[str, list] = {}
        for name, q2, r in entries:
            groups.setdefault(q2, []).append(r)
        for q2, rows in groups.items():
            items = [{"chunk_id": r["cid"], "text": r["content"] or "",
                      "heading": r["heading_path"] or ""}
                     for r in rows]
            if not items:
                continue
            with session_scope("exp_gate"):
                out = EB.summarize_segments(items, topic_label=q2[:80])
            rel = out.get("relevant") or {}
            for it in items:
                cid = it["chunk_id"]
                v = rel.get(cid)
                verdicts.setdefault(str(cid), []).append(v)
                if v is None:
                    deviants += 1
    finally:
        prompts.SUMMARIZE_GATE_NOTE = orig
    return {"tag": tag, "wall_s": round(time.time() - t0, 1),
            "verdicts": verdicts, "deviants": deviants}


def majority(vs):
    t = sum(1 for v in vs if v is True)
    f = sum(1 for v in vs if v is False)
    return True if t >= f else False     # 平票按 true


def main():
    sets = sample_sets()
    print({k: len(v) for k, v in sets.items()}, flush=True)
    from research_agent.graph import prompts
    results: dict[str, dict] = {}
    for vname, note in (("V1", prompts.SUMMARIZE_GATE_NOTE),
                        ("V2", prompts.SUMMARIZE_GATE_NOTE_V2)):
        for sname, entries in sets.items():
            for i in range(2):
                tag = f"{vname}_{sname}_{i}"
                print(f"[{time.strftime('%H:%M:%S')}] {tag} "
                      f"n={len(entries)}", flush=True)
                r = run_set(tag, entries, note)
                (OUT / f"{tag}.json").write_text(
                    json.dumps(r, ensure_ascii=False), encoding="utf-8")
                results.setdefault(f"{vname}_{sname}", []).append(r)
    # 汇总：每段跨两次跑取多数（平票 true）
    summary = {}
    for sname in sets:
        row = {}
        for vname in ("V1", "V2"):
            merged: dict[str, list] = {}
            devs = 0
            for r in results[f"{vname}_{sname}"]:
                devs += r["deviants"]
                for cid, vs in r["verdicts"].items():
                    merged.setdefault(cid, []).extend(vs)
            verdict = {cid: majority(vs) for cid, vs in merged.items()}
            row[vname] = {"false_n": sum(1 for v in verdict.values()
                                         if v is False),
                          "total": len(verdict), "deviants": devs,
                          "verdicts": verdict}
        summary[sname] = row
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    for sname in sets:
        for vname in ("V1", "V2"):
            r = summary[sname][vname]
            print(f"{sname:8s} {vname}: false={r['false_n']}/{r['total']} "
                  f"dev={r['deviants']}", flush=True)


if __name__ == "__main__":
    main()
