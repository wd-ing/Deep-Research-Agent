"""批 6-A 真实验证：v24 输入回放，G0(批5旧路径) vs G1(批6 node_plan_spec)。

G0 = 在脚本内复刻批 5 调用：complete_json_forced + SPEC_SCHEMA（无
minItems）+ WRITE_PLAN_SPEC+SLIM_NOTE，校验只看必填键（无覆盖检查）。
G1 = 生产 node_plan_spec（提示词动态 id 段 + minItems + 覆盖校验 +
缺项补占位），记录重试/stub。

输入：v24（data/refs/ra_v24_final.db → /tmp 副本）session
s_7cbfb208cc5b，_brief(state) 与生产同口径。两组各 n=3 串行。

产物：/tmp/exp_spec_coverage/{tag}.json + summary.json
用法：set -a && source .env && set +a && .venv/bin/python tests/experiments/exp_spec_coverage.py
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

REAL_SID = "s_7cbfb208cc5b"
SRC_DB = ROOT / "data/refs/ra_v24_final.db"
DB = "/tmp/ra_v24_spec_cover.db"
OUT = Path("/tmp/exp_spec_coverage")
OUT.mkdir(parents=True, exist_ok=True)

if not Path(DB).exists():
    shutil.copy(SRC_DB, DB)
os.environ["RA_DB_PATH"] = DB

from research_agent.db.repo import get_repo              # noqa: E402
from research_agent.graph.subgraphs import write as W    # noqa: E402
from research_agent.graph import prompts                  # noqa: E402
from research_agent.llm.provider import (LLMClient,      # noqa: E402
                                        session_scope)

LLM = LLMClient()


def build_state(repo) -> dict:
    all_sources = [dict(r) for r in repo.q(
        "SELECT id, title, domain, url, source_type, source_date, char_count, "
        "       source_nature FROM source WHERE session_id=? ORDER BY rowid",
        (REAL_SID,))]
    src_no = {s["id"]: i for i, s in enumerate(all_sources, 1)}
    repo_segments = [dict(r) for r in repo.q(
        "SELECT id, source_id, heading_path, content, summary, token_count, "
        "       on_topic FROM chunk WHERE session_id=? ORDER BY rowid",
        (REAL_SID,))]
    seg_no: dict[str, int] = {}
    for c in repo_segments:
        n = seg_no.get(c["source_id"], 0) + 1
        seg_no[c["source_id"]] = n
        c["ref"] = f"S{src_no.get(c['source_id'], 0)}·{n}"
    segs = [c for c in repo_segments if src_no.get(c["source_id"])]
    keep = [c for c in segs if c.get("on_topic")]
    src_lines = [
        f"[S{src_no[s['id']]}] {s.get('title') or s['domain']} | {s['domain']} "
        f"| {s.get('source_type')} | {s.get('source_date') or '日期未知'}"
        f" | {s.get('source_nature') or '性质未判'}"
        f" | 正文 {s.get('char_count') or 0} 字"
        for s in all_sources]
    q = repo.q1("SELECT question, outline_json, probe_json FROM session "
                "WHERE id=?", (REAL_SID,))
    return {
        "session_id": REAL_SID, "question": q["question"],
        "outline": (json.loads(q["outline_json"] or "{}")
                    .get("sections") or []),
        "signals": json.loads(q["probe_json"] or "{}"),
        "segments": segs, "on_topic": keep, "src_lines": src_lines,
    }


def want_ids(state):
    return [str(s.get("id")) for s in (state.get("outline") or [])
            if s.get("id")]


def missing_ids(spec, ids):
    got = {str(s.get("id")) for s in ((spec or {}).get("sections") or [])
           if isinstance(s, dict)}
    return [i for i in ids if i not in got]


def run_g0(tag: str, state: dict, repo) -> dict:
    """批 5 旧路径：SPEC_SCHEMA 原样 + 完整/SLIM 提示词，只看必填键。"""
    prompt = W._clip(W._dumps(W._brief(state)), 40000)
    system = prompts.WRITE_PLAN_SPEC + prompts.WRITE_PLAN_SPEC_SLIM_NOTE
    row0 = repo.q1("SELECT COALESCE(MAX(rowid),0) m FROM llm_call")["m"]
    t0, data, calls = time.time(), None, 0
    err = ""
    for _ in range(2):
        try:
            calls += 1
            with session_scope(tag):
                raw = LLM.complete_json_forced(
                    "write_plan_spec", prompt, "submit_spec",
                    W.SPEC_SCHEMA, system=system)
            if not W._spec_valid(raw):
                err = "缺必填键"
                continue
            data = raw
            err = ""
            break
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
    wall = round(time.time() - t0, 1)
    u = dict(repo.q1("SELECT SUM(output_tokens) o FROM llm_call "
                     "WHERE session_id=? AND rowid>?", (tag, row0)) or {})
    ids = want_ids(state)
    return {"tag": tag, "group": "G0", "wall_s": wall, "calls": calls,
            "out_tokens": u.get("o"), "err": err[:200],
            "sections_n": len((data or {}).get("sections") or []),
            "missing": missing_ids(data, ids), "stubbed": False,
            "prompt_chars": len(prompt)}


def run_g1(tag: str, state: dict, repo) -> dict:
    """生产 node_plan_spec（覆盖校验+补占位），计数真实调用次数。"""
    calls = {"n": 0}
    orig = LLM.complete_json_forced

    def counting(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)

    row0 = repo.q1("SELECT COALESCE(MAX(rowid),0) m FROM llm_call")["m"]
    t0 = time.time()
    err = ""
    try:
        W.LLM.complete_json_forced = counting
        with session_scope(tag):
            out = W.node_plan_spec(state)
    except Exception as e:
        out = {}
        err = f"{type(e).__name__}: {e}"
    finally:
        W.LLM.complete_json_forced = orig
    wall = round(time.time() - t0, 1)
    u = dict(repo.q1("SELECT SUM(output_tokens) o FROM llm_call "
                     "WHERE session_id=? AND rowid>?", (tag, row0)) or {})
    ids = want_ids(state)
    spec = out.get("spec") or {}
    ev = (out.get("events") or [{}])[0]
    return {"tag": tag, "group": "G1", "wall_s": wall,
            "calls": calls["n"], "out_tokens": u.get("o"),
            "err": err[:200] or out.get("plan_failed", ""),
            "sections_n": len(spec.get("sections") or []),
            "missing": [i for i in ids
                        if i not in {str(s.get("id")) for s in
                                     (spec.get("sections") or [])
                                     if isinstance(s, dict)}],
            "stubbed": bool(ev.get("sections_stubbed")),
            "ev_missing": ev.get("sections_missing"),
            "prompt_chars": len(W._clip(W._dumps(W._brief(state)), 40000))}


def main() -> None:
    repo = get_repo()
    state = build_state(repo)
    ids = want_ids(state)
    print(f"session={REAL_SID} outline={len(ids)}节 ids={ids} "
          f"on_topic={len(state['on_topic'])}", flush=True)
    results = []
    for grp, fn in (("G0", run_g0), ("G1", run_g1)):
        for i in range(3):
            tag = f"{grp}{i}"
            print(f"[{time.strftime('%H:%M:%S')}] {tag}", flush=True)
            if not repo.q1("SELECT id FROM session WHERE id=?", (tag,)):
                repo.x("INSERT INTO session (id, title, question, status, "
                       "created_at, updated_at) VALUES (?,?,?,?,"
                       "datetime('now'),datetime('now'))",
                       (tag, f"exp {tag}", f"exp {tag}", "done"))
            r = fn(tag, state, repo)
            results.append(r)
            print(f"  wall={r['wall_s']}s calls={r['calls']} "
                  f"out={r['out_tokens']} secs={r['sections_n']} "
                  f"missing={r['missing']} stub={r['stubbed']} "
                  f"{r['err'][:80]}", flush=True)
    summary = {"runs": results}
    for grp in ("G0", "G1"):
        rs = [r for r in results if r["group"] == grp]
        summary[grp] = {
            "wall_med": statistics.median(r["wall_s"] for r in rs),
            "out_med": statistics.median(r["out_tokens"] or 0 for r in rs),
            "full_cover": sum(1 for r in rs if not r["missing"]),
            "stubs": sum(1 for r in rs if r["stubbed"]),
            "calls_total": sum(r["calls"] for r in rs),
        }
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "runs.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({g: summary[g] for g in ("G0", "G1")},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
