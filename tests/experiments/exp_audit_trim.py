"""write_audit 输出瘦身变体 A/B（真实 API，A0/A1 各 n=3 串行）。

输入：v22 库返修前草稿 = 终稿 markdown 去掉「存疑与未完成」及来源附录、
把 write_revise 事件里 applied 的补丁 new→old 还原。
走生产调用：W.node_audit(state)（complete_json_forced + 归一化），
A1 时翻 W._AUDIT_TRIM。

产物：/tmp/exp_audit_trim/{tag}.json + summary.json
用法：set -a && source .env && set +a && .venv/bin/python tests/experiments/exp_audit_trim.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

REAL_SID = "s_f29d6edbcbe9"
SRC_DB = ROOT / "data/refs/ra_v22_final.db"
DB = "/tmp/ra_v22_audit_ab.db"
OUT = Path("/tmp/exp_audit_trim")
OUT.mkdir(parents=True, exist_ok=True)

if not Path(DB).exists():
    shutil.copy(SRC_DB, DB)
os.environ["RA_DB_PATH"] = DB

from research_agent.db.repo import get_repo              # noqa: E402
from research_agent.graph.subgraphs import write as W    # noqa: E402
from research_agent.llm.provider import session_scope    # noqa: E402


def build_state(repo) -> dict:
    rep = repo.q1("SELECT markdown FROM report WHERE session_id=?",
                  (REAL_SID,))["markdown"]
    # 还原返修前：把已应用补丁的 new 换回 old
    n_patches = 0
    for r in repo.q(
            "SELECT event_json FROM sse_event WHERE session_id=? "
            "AND event_json LIKE '%write_revise%'", (REAL_SID,)):
        e = json.loads(r[0]); e = e.get("event", e)
        if e.get("kind") != "write_revise":
            continue
        for it in (e.get("patch") or {}).get("applied_items") or []:
            if it.get("new") and it.get("new") in rep:
                rep = rep.replace(it["new"], it["old"], 1)
                n_patches += 1
    # assembled ≈ 终稿正文（存疑节与来源附录是 final 阶段加的）
    i = rep.find("## 存疑与未完成")
    assembled = rep[:i] if i > 0 else rep
    tr = repo.write_trace(REAL_SID)
    q = repo.q1("SELECT question, outline_json FROM session WHERE id=?",
                (REAL_SID,))
    return {
        "session_id": REAL_SID, "question": q["question"],
        "outline": (json.loads(q["outline_json"] or "{}")
                    .get("sections") or []),
        "assembled": assembled, "spec": tr.get("spec") or {},
        "tasks": tr.get("tasks") or [],
        "violations": {},   # v22 审查时 violations={}（auto_fix 已清；无法逐字重建，记此口径）
        "rework_rounds": 0, "rework_effective": True,
        "n_patches_reverted": n_patches,
    }


def run_one(tag: str, trim: bool, state: dict, repo) -> dict:
    if not repo.q1("SELECT id FROM session WHERE id=?", (tag,)):
        repo.x("INSERT INTO session (id, title, question, status, "
               "created_at, updated_at) VALUES (?,?,?,?,"
               "datetime('now'),datetime('now'))",
               (tag, f"exp {tag}", f"exp {tag}", "done"))
    row0 = repo.q1("SELECT COALESCE(MAX(rowid),0) m FROM llm_call")["m"]
    orig = W._AUDIT_TRIM
    W._AUDIT_TRIM = trim
    t0 = time.time()
    try:
        with session_scope(tag):
            out = W.node_audit(state)
    except Exception as e:
        out = {"audit": None, "events": [],
               "_err": f"{type(e).__name__}: {e}"}
    finally:
        W._AUDIT_TRIM = orig
    wall = round(time.time() - t0, 1)
    u = dict(repo.q1("SELECT SUM(input_tokens) i, SUM(output_tokens) o, "
                "COUNT(*) n FROM llm_call WHERE session_id=? AND rowid>?",
                (tag, row0)) or {})
    a = out.get("audit") or {}
    ev = (out.get("events") or [{}])[0]
    findings = [f for f in (a.get("findings") or []) if isinstance(f, dict)]
    sev: dict[str, int] = {}
    for f in findings:
        s = str(f.get("severity") or "?")
        sev[s] = sev.get(s, 0) + 1
    # v22 已知 high：t8→sec8 鹏辉 Secu 商业化落地 vs sec6 矛盾
    caught = any(
        str(f.get("severity")) == "high"
        and (str(f.get("task")) == "t8"
             or str(f.get("section")) in ("sec6", "sec8"))
        and ("鹏辉" in str(f.get("problem")) + str(f.get("quote"))
             or "Secu" in str(f.get("problem")) + str(f.get("quote")))
        for f in findings)
    res = {
        "tag": tag, "trim": trim, "wall_s": wall,
        "input_tokens": u.get("i"), "output_tokens": u.get("o"),
        "err": out.get("_err", ""),
        "need_rework": a.get("need_rework"),
        "normalize_fail": ev.get("normalize_fail"),
        "sev": sev, "findings_n": len(findings),
        "blocked_n": len(a.get("blocked") or []),
        "caught_known": caught,
        "targets": a.get("targets"),
        "findings": findings, "blocked": a.get("blocked"),
        "reason": a.get("reason"),
    }
    (OUT / f"{tag}.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def main() -> None:
    repo = get_repo()
    state = build_state(repo)
    print(f"assembled={len(state['assembled'])} chars "
          f"patches_reverted={state['n_patches_reverted']}", flush=True)
    results = []
    for grp, trim in (("A0", False), ("A1", True)):
        for i in range(3):
            tag = f"{grp}{i}"
            print(f"[{time.strftime('%H:%M:%S')}] {tag} trim={trim}",
                  flush=True)
            r = run_one(tag, trim, state, repo)
            results.append(r)
            print(f"  wall={r['wall_s']}s out={r['output_tokens']} "
                  f"sev={r['sev']} blocked={r['blocked_n']} "
                  f"caught={r['caught_known']} nr={r['need_rework']} "
                  f"nf={len(r['normalize_fail'] or [])} "
                  f"err={r['err'][:60]}", flush=True)
    summary = {"runs": results}
    for grp in ("A0", "A1"):
        rs = [r for r in results if r["tag"].startswith(grp)]
        summary[grp] = {
            "wall_med": statistics.median(r["wall_s"] for r in rs),
            "out_med": statistics.median(
                r["output_tokens"] or 0 for r in rs),
            "caught_n": sum(1 for r in rs if r["caught_known"]),
            "high_each": [r["sev"].get("high", 0) for r in rs],
            "blocked_med": statistics.median(r["blocked_n"] for r in rs),
            "nf_total": sum(len(r["normalize_fail"] or []) for r in rs),
        }
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({g: summary[g] for g in ("A0", "A1")},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
