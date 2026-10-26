"""write_plan_spec 强制工具提交 + 瘦身变体 A/B 实验（真实 API）。

三组各 n=3、串行（40/min 限流不叠加）：
  C = 旧路径 `complete_json`（手写 JSON，复刻旧调用）
  F = 新路径 `complete_json_forced` + submit_spec，完整提示词
  S = 新路径 + RA_SPEC_SLIM（提示词末尾拼 SLIM_NOTE）

输入：v22（data/refs/ra_v22_final.db → /tmp 副本）session s_f29d6edbcbe9，
按 exp_write_preload 的口径重建 WriteState，node_plan_spec 的输入
就是 `_clip(_dumps(_brief(state)), 40000)`。

产物：/tmp/exp_plan_spec/{tag}.json + summary.{json,txt}
用法：set -a && source .env && set +a && .venv/bin/python tests/exp_plan_spec.py
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REAL_SID = "s_f29d6edbcbe9"
SRC_DB = ROOT / "data/refs/ra_v22_final.db"
DB = "/tmp/ra_v22_spec_ab.db"
OUT = Path("/tmp/exp_plan_spec")
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
    """与 node_plan_spec 输入同口径的 WriteState（照抄 exp_write_preload）。"""
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


def run_one(tag: str, mode: str, state: dict, repo) -> dict:
    if not repo.q1("SELECT id FROM session WHERE id=?", (tag,)):
        repo.x("INSERT INTO session (id, title, question, status, "
               "created_at, updated_at) VALUES (?,?,?,?,"
               "datetime('now'),datetime('now'))",
               (tag, f"exp {tag}", f"exp {tag}", "done"))
    prompt = W._clip(W._dumps(W._brief(state)), 40000)
    row0 = repo.q1("SELECT COALESCE(MAX(rowid),0) m FROM llm_call")["m"]
    t0 = time.time()
    spec, err = None, ""
    try:
        with session_scope(tag):
            if mode == "C":
                spec = LLM.complete_json(
                    "write_plan_spec", prompt,
                    system=prompts.WRITE_PLAN_SPEC)
            else:
                system = prompts.WRITE_PLAN_SPEC + (
                    prompts.WRITE_PLAN_SPEC_SLIM_NOTE
                    if mode == "S" else "")
                raw = LLM.complete_json_forced(
                    "write_plan_spec", prompt, "submit_spec",
                    W.SPEC_SCHEMA, system=system)
                if W._spec_valid(raw):
                    spec = {k: v for k, v in raw.items()
                            if k in W._SPEC_KEYS}
                else:
                    err = f"缺必填键 {raw if not isinstance(raw,dict) else [k for k in W._SPEC_REQUIRED if k not in raw]}"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    wall = round(time.time() - t0, 1)
    rows = repo.q("SELECT SUM(input_tokens) i, SUM(output_tokens) o, "
                  "COUNT(*) n FROM llm_call WHERE session_id=? AND rowid>?",
                  (tag, row0))
    u = dict(rows[0]) if rows else {}
    spec = spec if isinstance(spec, dict) else None
    secs = [s.get("id") for s in (spec or {}).get("sections") or []]
    outline_ids = [s.get("id") for s in state.get("outline") or []]
    res = {
        "tag": tag, "mode": mode, "wall_s": wall,
        "input_tokens": u.get("i"), "output_tokens": u.get("o"),
        "ok": spec is not None, "error": err[:300],
        "counts": {k: (len(spec.get(k)) if isinstance(spec.get(k), list)
                       else len(str(spec.get(k) or "")))
                   for k in W._SPEC_KEYS} if spec else {},
        "sections_cover": (set(outline_ids) <= set(secs)
                           if spec else False),
        "sections_ids": secs,
        "chars": len(json.dumps(spec, ensure_ascii=False)) if spec else 0,
        "prompt_chars": len(prompt),
        "spec": spec,
    }
    (OUT / f"{tag}.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def main() -> None:
    repo = get_repo()
    state = build_state(repo)
    ids = [s.get("id") for s in state.get("outline") or []]
    print(f"session={REAL_SID} outline={len(ids)}节 "
          f"on_topic={len(state['on_topic'])}", flush=True)
    results = []
    for mode in ("C", "F", "S"):
        for i in range(3):
            tag = f"{mode}{i}"
            print(f"[{time.strftime('%H:%M:%S')}] {tag} mode={mode}",
                  flush=True)
            r = run_one(tag, mode, state, repo)
            results.append(r)
            print(f"  wall={r['wall_s']}s ok={r['ok']} "
                  f"in={r['input_tokens']} out={r['output_tokens']} "
                  f"cover={r['sections_cover']} counts={r['counts']} "
                  f"{r['error'][:80]}", flush=True)
    summary = {"runs": [{k: v for k, v in r.items() if k != "spec"}
                        for r in results]}
    for mode in ("C", "F", "S"):
        rs = [r for r in results if r["mode"] == mode]
        summary[mode] = {
            "wall_med": statistics.median(r["wall_s"] for r in rs),
            "out_med": statistics.median(
                r["output_tokens"] or 0 for r in rs),
            "in_med": statistics.median(r["input_tokens"] or 0 for r in rs),
            "ok_n": sum(1 for r in rs if r["ok"]),
            "cover_all": all(r["sections_cover"] for r in rs),
            "counts_med": {k: statistics.median(
                r["counts"].get(k, 0) for r in rs)
                for k in ("glossary", "units", "cautions",
                          "spec_uncertainty")},
        }
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({m: summary[m] for m in ("C", "F", "S")},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
