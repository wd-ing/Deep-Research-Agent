"""E1: 规划阶段提示词 grounding A/B（不写仓库提示词，运行时 monkeypatch）。

A = 当前 WRITE_PLAN_SPEC / WRITE_PLAN_TASKS。
B = 当前提示词 + /tmp/exp_prompts/{plan_spec_add,plan_tasks_add}.txt 定点插入。

每次运行 = 还原的 plan 初始 state → node_plan_spec → merge → node_plan_tasks。
业务数据走 /tmp/ra_exp_ab.db（副本）；checkpoint 只读；llm_call 用
session_scope(唯一假 sid) 标记，EvidenceTools 读段仍走真实 sid。

用法: .venv/bin/python tests/experiments/exp_ab_plan.py
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
DB = "/tmp/ra_exp_ab.db"
os.environ["RA_DB_PATH"] = DB
for _line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        k, v = _line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer  # noqa: E402

from research_agent.db.repo import get_repo  # noqa: E402
from research_agent.graph import prompts  # noqa: E402
from research_agent.graph.subgraphs import write as W  # noqa: E402
from research_agent.llm.provider import session_scope  # noqa: E402
from research_agent.tools.evidence import EvidenceTools  # noqa: E402

SID = "s_2571e8f58f50"
NS = "write:fe076c41-284c-5a99-e570-08e90ec7ccd5"
TS_AFTER = "2026-10-19T12:50"
OUT = Path("/tmp/exp_ab")
RAW = OUT / "e1_raw"
RAW.mkdir(parents=True, exist_ok=True)
N_PER = 6
WORKERS = 3

# ---------- 还原 plan 初始 state ----------
ser = JsonPlusSerializer()
c = sqlite3.connect(ROOT / "data/checkpoints.db")
PLAN_STATE = None
for typ, blob in c.execute(
        "select type,checkpoint from checkpoints where thread_id=? "
        "and checkpoint_ns=? order by checkpoint_id", (SID, NS)):
    cp = ser.loads_typed((typ, blob))
    cv = dict(cp["channel_values"])
    if (cp.get("ts", "") > TS_AFTER and len(cv) > 1
            and cv.get("question") and not cv.get("spec")):
        PLAN_STATE = cv
        print("plan state @", cp["ts"], sorted(cv.keys()), flush=True)
        break
assert PLAN_STATE, "没有 plan 初始快照"

# ---------- 提示词变体 ----------
ANCHOR_SPEC = "概括不足以支撑判断时，用工具取原文再看。"
ANCHOR_TASKS = "分派时只管「哪一段该讲在哪一节」，不要考虑子 agent 怎么写"
ADD_SPEC = Path("/tmp/exp_prompts/plan_spec_add.txt").read_text(encoding="utf-8")
ADD_TASKS = Path("/tmp/exp_prompts/plan_tasks_add.txt").read_text(encoding="utf-8")
if ANCHOR_SPEC not in prompts.WRITE_PLAN_SPEC:
    sys.exit("spec 锚点不存在，停")
if ANCHOR_TASKS not in prompts.WRITE_PLAN_TASKS:
    sys.exit("tasks 锚点不存在，停")
B_SPEC = prompts.WRITE_PLAN_SPEC.replace(
    ANCHOR_SPEC, ANCHOR_SPEC + "\n\n" + ADD_SPEC.rstrip(), 1)
B_TASKS = prompts.WRITE_PLAN_TASKS.replace(
    ANCHOR_TASKS, ADD_TASKS.rstrip() + "\n\n" + ANCHOR_TASKS, 1)
ORIG_SPEC = prompts.WRITE_PLAN_SPEC
ORIG_TASKS = prompts.WRITE_PLAN_TASKS

# ---------- tok()（按食谱原样） ----------
YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
NUM_RE = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:Wh/kg|GWh|MWh|kWh|Wh|Ah|mS/cm|S/cm|USD|美元|元|%|％"
    r"|倍|℃|项|万辆|辆|圈|GW|万|亿)")


def tok(text: str) -> set:
    t = re.sub(r"\s+", "", text or "")
    return set(YEAR_RE.findall(t)) | set(NUM_RE.findall(t))


SEG_LOCK = threading.Lock()
SEG_CACHE: dict[str, str] = {}
_missing_warned: set[str] = set()


def seg_fulltext(refs: list[str]) -> str:
    """read_segments 取原文（非概括），按 ref 缓存。"""
    need = [r for r in refs if r not in SEG_CACHE]
    if need:
        try:
            res = EvidenceTools(get_repo(), SID).dispatch(
                [{"name": "read_segments", "input": {"segments": need}}])
            for g in (json.loads(res[0]["content"]).get("段落") or []):
                with SEG_LOCK:
                    SEG_CACHE[str(g.get("段号"))] = str(g.get("全文") or "")
        except Exception as e:
            print("  read_segments err", repr(e), flush=True)
    out = []
    for r in refs:
        s = SEG_CACHE.get(r)
        if s is None and r not in _missing_warned:
            with SEG_LOCK:
                _missing_warned.add(r)
            print("  seg not in DB:", r, flush=True)
        out.append(s or "")
    return "\n\n".join(out)


def run_one(variant: str, i: int) -> dict:
    tag = f"exp_ab_e1_{variant}_{i}"
    st = dict(PLAN_STATE)
    st["session_id"] = SID
    rec = {"variant": variant, "i": i, "tag": tag,
           "plan_failed": None, "error": None,
           "t_plan_spec_s": None, "t_plan_tasks_s": None,
           "usage": {}, "spec": None, "tasks": None}
    try:
        with session_scope(tag):
            t0 = time.time()
            r1 = W.node_plan_spec(st)
            rec["t_plan_spec_s"] = round(time.time() - t0, 1)
            st.update(r1 or {})
            if st.get("plan_failed"):
                rec["plan_failed"] = st.get("plan_failed")
            else:
                t1 = time.time()
                r2 = W.node_plan_tasks(st)
                rec["t_plan_tasks_s"] = round(time.time() - t1, 1)
                st.update(r2 or {})
                rec["plan_failed"] = st.get("plan_failed")
        rec["spec"] = st.get("spec")
        rec["tasks"] = st.get("tasks")
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
    tasks = rec.get("tasks") or []
    spec = rec.get("spec") or {}
    per_task = []
    n_claim = n_ungr = 0
    n_probe_mandate = 0
    seg_of_sec: dict[str, list[str]] = {}
    for t in tasks:
        claim = tok(str(t.get("mandate") or "") + " "
                    + " ".join(W._as_str_list(t.get("must_cover"))))
        stext = re.sub(r"\s+", "", seg_fulltext(t.get("segments") or []))
        ungr = sorted(x for x in claim if x not in stext)
        n_claim += len(claim)
        n_ungr += len(ungr)
        if re.search(r"未列入|未纳入|证据清单|来自.{0,6}文章",
                     str(t.get("mandate") or "")):
            n_probe_mandate += 1
        for sec in W._as_str_list(t.get("covers")):
            seg_of_sec.setdefault(sec, []).extend(t.get("segments") or [])
        per_task.append({"id": t.get("id"), "covers": t.get("covers"),
                         "n_claim": len(claim), "n_ungr": len(ungr),
                         "ungrounded": ungr})
    per_sec = []
    sec_unmatched = []
    for s in (spec.get("sections") or []):
        sid_ = s.get("id")
        claim = tok(str(s.get("role") or "") + " "
                    + " ".join(W._as_str_list(s.get("must_cover"))))
        refs = seg_of_sec.get(sid_)
        if not refs:
            sec_unmatched.append(sid_)
            stext = ""
        else:
            stext = re.sub(r"\s+", "", seg_fulltext(refs))
        per_sec.append({"id": sid_, "n_claim": len(claim),
                        "ungrounded": sorted(x for x in claim if x not in stext)})
    return {"n_tasks": len(tasks), "n_claim_tokens": n_claim,
            "n_ungrounded": n_ungr,
            "ungrounded_ratio": round(n_ungr / n_claim, 4) if n_claim else None,
            "n_mandate_probe_refs": n_probe_mandate,
            "per_task": per_task, "per_section": per_sec,
            "sections_without_task": sec_unmatched,
            "spec_uncertainty": spec.get("spec_uncertainty")}


results: list[dict] = []
for variant, sp, tp in (("A", ORIG_SPEC, ORIG_TASKS),
                        ("B", B_SPEC, B_TASKS)):
    prompts.WRITE_PLAN_SPEC = sp
    prompts.WRITE_PLAN_TASKS = tp
    print(f"== phase {variant} ==", flush=True)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        rs = list(ex.map(lambda i: run_one(variant, i), range(N_PER)))
    results += rs
    for r in rs:
        print(variant, r["i"], "failed:", bool(r.get("plan_failed")),
              "err:", r.get("error"),
              "spec_s:", r["t_plan_spec_s"], "tasks_s:", r["t_plan_tasks_s"],
              "usage:", r["usage"], flush=True)
prompts.WRITE_PLAN_SPEC = ORIG_SPEC
prompts.WRITE_PLAN_TASKS = ORIG_TASKS

agg, lines = [], []
for v in ("A", "B"):
    rs = [r for r in results if r["variant"] == v]
    ok_claim = ok_ungr = 0
    for r in rs:
        if r.get("error") or r.get("plan_failed"):
            continue
        m = metrics(r)
        ok_claim += m["n_claim_tokens"]
        ok_ungr += m["n_ungrounded"]
    agg.append(f"{v}: runs={len(rs)} claim={ok_claim} ungrounded={ok_ungr} "
               f"ratio={round(ok_ungr / ok_claim, 4) if ok_claim else None}")

for r in results:
    m = metrics(r) if not r.get("error") else {"per_task": [], "per_section": []}
    r["metrics"] = m
    (RAW / f"{r['variant']}_{r['i']}.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    if r.get("error"):
        lines.append(f"--- {r['variant']}_{r['i']} ERROR={r['error']!r}")
        continue
    lines.append(
        f"--- {r['variant']}_{r['i']} failed={r.get('plan_failed')!r} "
        f"t_spec={r['t_plan_spec_s']} t_tasks={r['t_plan_tasks_s']} "
        f"usage={r['usage']}\n"
        f"    tasks={m['n_tasks']} claim_tokens={m['n_claim_tokens']} "
        f"ungrounded={m['n_ungrounded']} ratio={m['ungrounded_ratio']} "
        f"mandate_probe_refs={m['n_mandate_probe_refs']}\n"
        f"    spec_uncertainty={json.dumps(m['spec_uncertainty'], ensure_ascii=False)}\n"
        f"    per_task={json.dumps(m['per_task'], ensure_ascii=False)}\n"
        f"    per_section={json.dumps(m['per_section'], ensure_ascii=False)}\n"
        f"    sections_without_task={m['sections_without_task']}")

(OUT / "e1_summary.txt").write_text(
    "\n".join(agg) + "\n\n" + "\n".join(lines), encoding="utf-8")
print("WROTE", OUT / "e1_summary.txt", flush=True)
