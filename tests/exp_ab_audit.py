"""E3: 审查提示词补丁可用性 A/B（运行时 monkeypatch WRITE_AUDIT）。

V0 = 7f1eaac 的 WRITE_AUDIT（git show 取出 exec 拿字符串）。
V1 = 当前 HEAD 的 prompts.WRITE_AUDIT。
schema 两边都用 HEAD 的 W.AUDIT_SCHEMA（node_audit 内部引用）。

每次运行 = 还原的 rr==0 审查 state（assembled 已有、未审计）
→ W.node_audit(state)。state.session_id=None（不写 trace）；
llm_call 用 session_scope(唯一假 sid) 标记归属。

用法: .venv/bin/python tests/exp_ab_audit.py
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB = "/tmp/ra_exp_ab.db"
os.environ["RA_DB_PATH"] = DB
for _line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        k, v = _line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer  # noqa: E402

from research_agent.graph import prompts  # noqa: E402
from research_agent.graph.subgraphs import write as W  # noqa: E402
from research_agent.llm.provider import session_scope  # noqa: E402

SID = "s_2571e8f58f50"
NS = "write:fe076c41-284c-5a99-e570-08e90ec7ccd5"
TS_AFTER = "2026-10-19T12:50"
OUT = Path("/tmp/exp_ab")
RAW = OUT / "e3_raw"
RAW.mkdir(parents=True, exist_ok=True)
N_PER = 4
WORKERS = 4

# ---------- 还原审查 state：rr==0、assembled 已有、尚无 audit ----------
ser = JsonPlusSerializer()
c = sqlite3.connect(ROOT / "data/checkpoints.db")
STATE = None
for typ, blob in c.execute(
        "select type,checkpoint from checkpoints where thread_id=? "
        "and checkpoint_ns=? order by checkpoint_id", (SID, NS)):
    cp = ser.loads_typed((typ, blob))
    cv = dict(cp["channel_values"])
    if (cp.get("ts", "") > TS_AFTER and len(cv) > 1
            and cv.get("assembled") and (cv.get("rework_rounds") or 0) == 0
            and cv.get("drafts") and not cv.get("audit")):
        STATE = cv
        print("audit state @", cp["ts"], "| drafts:",
              {k: len(v) for k, v in (cv.get("drafts") or {}).items()}, flush=True)
        break
assert STATE, "没有 rr==0+assembled 快照"
STATE["session_id"] = None

# ---------- V0/V1 提示词 ----------
src = subprocess.run(
    ["git", "show", "7f1eaac:research_agent/graph/prompts.py"],
    cwd=ROOT, capture_output=True, text=True, check=True).stdout
ns_v0: dict = {}
exec(compile(src, "prompts@7f1eaac", "exec"), ns_v0)
V0 = ns_v0["WRITE_AUDIT"]
V1 = prompts.WRITE_AUDIT
print("V0 len:", len(V0), "| V1 len:", len(V1), flush=True)

NUMBERING_RE = re.compile(r"标题.{0,6}编号|8\.x|9\.x|编号体系")


def run_one(variant: str, i: int) -> dict:
    tag = f"exp_ab_e3_{variant}_{i}"
    st = dict(STATE)
    rec = {"variant": variant, "i": i, "tag": tag, "audit": None,
           "events": None, "error": None, "sec_s": None, "usage": {}}
    try:
        t0 = time.time()
        with session_scope(tag):
            out = W.node_audit(st)
        rec["sec_s"] = round(time.time() - t0, 1)
        rec["audit"] = out.get("audit")
        rec["events"] = out.get("events")
    except Exception as e:
        rec["error"] = repr(e)
        return rec
    d = sqlite3.connect(DB)
    for stage, n, ot, lat in d.execute(
            "select stage,count(*),coalesce(sum(output_tokens),0),"
            "coalesce(sum(latency_ms),0) from llm_call "
            "where session_id=? group by stage", (tag,)).fetchall():
        rec["usage"][stage] = {"calls": n, "out_tokens": ot, "ms": lat}
    return rec


def metrics(rec: dict) -> dict:
    a = rec.get("audit") or {}
    targets = a.get("targets") or []
    findings = a.get("findings") or []
    drafts = STATE.get("drafts") or {}
    action_dist: dict[str, int] = {}
    per_target = []
    n_heading = n_missing = n_multi = n_patchable = n_no_sec = 0
    n_numbering = 0
    for t in targets:
        act = str(t.get("action") or "rewrite_section")
        action_dist[act] = action_dist.get(act, 0) + 1
        q = str(t.get("quote") or "")
        body = (drafts.get(t.get("task")) or {}).get(t.get("section")) \
            if isinstance(drafts.get(t.get("task")), dict) else None
        if body is None:
            n_no_sec += 1
            patch = "no_section"
            miss = None
            multi = None
        else:
            patch = bool(W._patchable(t, body))
            n_patchable += patch
            miss = q not in body
            multi = body.count(q) > 1 if q else None
            n_missing += bool(miss)
            n_multi += bool(multi)
        if q.lstrip().startswith("#"):
            n_heading += 1
        per_target.append({
            "task": t.get("task"), "section": t.get("section"),
            "action": act, "quote50": q[:50], "patchable": patch,
            "quote_heading": q.lstrip().startswith("#"),
            "quote_absent": miss, "quote_multi": multi,
            "problem": t.get("problem"), "fix": t.get("fix")})
    for f in findings:
        txt = str(f.get("problem") or "") + " " + str(f.get("fix") or "")
        if NUMBERING_RE.search(txt):
            n_numbering += 1
    n_high = len(targets)
    return {"n_high": n_high,
            "n_patchable": n_patchable,
            "patchable_ratio": round(n_patchable / n_high, 4) if n_high else None,
            "action_dist": action_dist,
            "n_rewrite_section": action_dist.get("rewrite_section", 0),
            "n_quote_heading": n_heading, "n_quote_absent": n_missing,
            "n_quote_multi": n_multi, "n_no_section": n_no_sec,
            "n_numbering_findings": n_numbering,
            "need_rework": a.get("need_rework"),
            "n_findings": len(findings),
            "n_blocked": len(a.get("blocked") or []),
            "per_target": per_target}


results: list[dict] = []
for variant, p in (("V0", V0), ("V1", V1)):
    prompts.WRITE_AUDIT = p
    print(f"== phase {variant} ==", flush=True)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        rs = list(ex.map(lambda i: run_one(variant, i), range(N_PER)))
    results += rs
    for r in rs:
        a = r.get("audit") or {}
        print(variant, r["i"], "kind:", (r.get("events") or [{}])[0].get("kind")
              if r.get("events") else None,
              "need_rework:", a.get("need_rework"),
              "high:", len(a.get("targets") or []),
              "err:", r.get("error"), "sec:", r["sec_s"], flush=True)
prompts.WRITE_AUDIT = V1

agg, lines = [], []
for v in ("V0", "V1"):
    rs = [r for r in results if r["variant"] == v]
    agg.append(f"{v}: runs={len(rs)}")

for r in results:
    m = metrics(r)
    r["metrics"] = m
    (RAW / f"{r['variant']}_{r['i']}.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    tgt = json.dumps(m["per_target"], ensure_ascii=False)
    lines.append(
        f"--- {r['variant']}_{r['i']} exc={r.get('error')!r} sec={r['sec_s']} "
        f"usage={r['usage']}\n"
        f"    high={m['n_high']} patchable={m['n_patchable']} "
        f"ratio={m['patchable_ratio']} action_dist={m['action_dist']} "
        f"rewrite={m['n_rewrite_section']} need_rework={m['need_rework']}\n"
        f"    quote_heading={m['n_quote_heading']} absent={m['n_quote_absent']} "
        f"multi={m['n_quote_multi']} no_section={m['n_no_section']} "
        f"numbering_findings={m['n_numbering_findings']} "
        f"findings={m['n_findings']} blocked={m['n_blocked']}\n"
        f"    per_target={tgt}")

(OUT / "e3_summary.txt").write_text(
    "\n".join(agg) + "\n\n" + "\n".join(lines), encoding="utf-8")
print("WROTE", OUT / "e3_summary.txt", flush=True)
