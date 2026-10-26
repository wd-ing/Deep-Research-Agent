"""fast_answer 篇幅变体 A/B（真实 API，P0 现行提示词 / P1=V2，各 n=3 串行）。

输入：data/refs/ra_fast_smoke.db → /tmp 副本（session s_a07f29d024f6），
直接调生产 node_answer（证据拼装 + LLM + 后处理全走真路），
llm_call 按 rowid 窗口收账。P1 用 monkeypatch 换 prompts.FAST_ANSWER_SYSTEM。

产物：/tmp/exp_fast_answer/{tag}.json + summary.json
用法：set -a && source .env && set +a && .venv/bin/python tests/exp_fast_answer.py
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REAL_SID = "s_a07f29d024f6"
SRC_DB = ROOT / "data/refs/ra_fast_smoke.db"
DB = "/tmp/ra_fast_answer_ab.db"
OUT = Path("/tmp/exp_fast_answer")
OUT.mkdir(parents=True, exist_ok=True)

if not Path(DB).exists():
    shutil.copy(SRC_DB, DB)
os.environ["RA_DB_PATH"] = DB

from research_agent.db.repo import get_repo              # noqa: E402
from research_agent.graph import nodes as N              # noqa: E402
from research_agent.graph import prompts                  # noqa: E402


def run_one(tag: str, state: dict, repo, v2: bool) -> dict:
    orig_sys = prompts.FAST_ANSWER_SYSTEM
    if v2:
        prompts.FAST_ANSWER_SYSTEM = prompts.FAST_ANSWER_SYSTEM_V2
    row0 = repo.q1("SELECT COALESCE(MAX(rowid),0) m FROM llm_call")["m"]
    t0 = time.time()
    try:
        out = N.node_answer(dict(state))
        err = ""
    except Exception as e:
        out, err = {}, f"{type(e).__name__}: {e}"
    finally:
        prompts.FAST_ANSWER_SYSTEM = orig_sys
    wall = round(time.time() - t0, 1)
    rows = [dict(r) for r in repo.q(
        "SELECT stage,status,latency_ms,input_tokens,output_tokens,error "
        "FROM llm_call WHERE rowid>? AND stage='fast_answer'", (row0,))]
    md = out.get("report_markdown") or ""
    ev = (out.get("events") or [{}])[0]
    toks = set(re.findall(r"\[S(\d+)·(\d+)\]", md))
    ns = repo.q1("SELECT COUNT(*) n FROM source WHERE session_id=?",
                 (REAL_SID,))["n"]
    cnt = {i: repo.q1("SELECT COUNT(*) n FROM chunk WHERE source_id=?",
                      (srcid,))["n"]
           for i, srcid in enumerate(
               [r["id"] for r in repo.q("SELECT id FROM source "
                                        "WHERE session_id=? "
                                        "ORDER BY rowid", (REAL_SID,))],
               1)}
    oob = [f"S{a}·{b}" for a, b in toks
           if int(a) > ns or int(b) > cnt.get(int(a), 0)]
    res = {
        "tag": tag, "v2": v2, "wall_s": wall, "err": err[:200],
        "calls": [{"lat_ms": r["latency_ms"], "in": r["input_tokens"],
                   "out": r["output_tokens"], "status": r["status"],
                   "err": (r["error"] or "")[:120]} for r in rows],
        "n_calls": len(rows),
        "retry_rows": sum(1 for r in rows if r["status"] == "retry"),
        "chars": len(md),
        "h2": len(re.findall(r"^## ", md, re.M)),
        "has_divergence": "分歧与不确定" in md,
        "citations_total": ev.get("citations_total"),
        "citations_removed": ev.get("citations_removed"),
        "oob": oob, "answer_ev": {k: v for k, v in ev.items()
                                 if k != "kind"},
    }
    (OUT / f"{tag}.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def main() -> None:
    repo = get_repo()
    q = repo.q1("SELECT question, outline_json FROM session WHERE id=?",
                (REAL_SID,))
    state = {"session_id": REAL_SID, "question": q["question"],
             "mode": "fast",
             "outline": (json.loads(q["outline_json"] or "{}")
                         .get("sections") or []),
             "events": []}
    results = []
    for grp, v2 in (("P0", False), ("P1", True)):
        for i in range(3):
            tag = f"{grp}{i}"
            print(f"[{time.strftime('%H:%M:%S')}] {tag} v2={v2}",
                  flush=True)
            r = run_one(tag, state, repo, v2)
            results.append(r)
            print(f"  wall={r['wall_s']}s calls={r['n_calls']} "
                  f"retry={r['retry_rows']} chars={r['chars']} "
                  f"h2={r['h2']} cite_rm={r['citations_removed']} "
                  f"outs={[c['out'] for c in r['calls']]} "
                  f"{r['err'][:60]}", flush=True)
    summary = {"runs": results}
    for grp in ("P0", "P1"):
        rs = [r for r in results if r["tag"].startswith(grp)]
        outs = [c["out"] for r in rs for c in r["calls"]
                if r["status"] != "error"] + [0]
        # 只看 ok 调用（retry 行 token 也记账）
        okouts = [c["out"] for r in rs for c in r["calls"]
                  if c["status"] == "ok"]
        summary[grp] = {
            "wall_med": statistics.median(r["wall_s"] for r in rs),
            "out_med": statistics.median(okouts or [0]),
            "retry_n": sum(r["retry_rows"] for r in rs),
            "chars_med": statistics.median(r["chars"] for r in rs),
            "div_all": all(r["has_divergence"] for r in rs),
            "rm_med": statistics.median(
                r["citations_removed"] or 0 for r in rs),
        }
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({g: summary[g] for g in ("P0", "P1")},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
