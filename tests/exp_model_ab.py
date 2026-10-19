"""模型对照：M3（现行配置）vs M3.1-Flash low，用于 write_plan_spec / write_plan_tasks / assess。

变体：
  A = 现行配置（M3，思考不受控）
  B = 同一环节改走 `minimax31` 档案（MiniMax-M3.1-Flash），
      reasoning_effort=low + thinking_effort=low，其余键（stream / guard /
      max_output_tokens / timeout_s / temperature）原样沿用。
覆盖方式：运行时改 `LLM.registry.raw["stages"][stage]` 并清 `_cache`，不动 config/ 文件。
各变体**串行分阶段**跑（同一模块级对象，不能并发改），变体内 WORKERS 并发。

输入：
  plan   = write 子图最早的「有 question、还没 spec」快照 → node_plan_spec → node_plan_tasks
  assess = 主图（ns=''）里 round==1 的最新快照 → node_assess（它内部调 LLM.complete_json("assess")）

业务数据走传入的 DB 副本；checkpoint 只读；用量用 session_scope(唯一假 sid) 归属
（脚本先把假 session 行插进 DB 副本，llm_call 对 session 有外键）。

用法:
  .venv/bin/python tests/exp_model_ab.py <db副本> <session_id> <write_checkpoint_ns> [out_dir]
  例: ... /tmp/ra_v16_ab.db s_xxx write:xxxx /tmp/exp_model_ab
可选环境变量: AB_N=3（每格次数）AB_WORKERS=3  AB_ONLY=plan|assess（只跑一类）
"""

from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB, SID, NS = sys.argv[1], sys.argv[2], sys.argv[3]
OUT = Path(sys.argv[4] if len(sys.argv) > 4 else "/tmp/exp_model_ab")
OUT.mkdir(parents=True, exist_ok=True)
os.environ["RA_DB_PATH"] = DB
for _line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        k, v = _line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
N_PER = int(os.environ.get("AB_N", "3"))
WORKERS = int(os.environ.get("AB_WORKERS", "3"))
ONLY = os.environ.get("AB_ONLY", "")

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer  # noqa: E402

from research_agent.db.repo import get_repo  # noqa: E402
from research_agent.graph import nodes as N  # noqa: E402
from research_agent.graph.subgraphs import write as W  # noqa: E402
from research_agent.llm.provider import session_scope  # noqa: E402
from research_agent.tools.evidence import EvidenceTools  # noqa: E402

PLAN_STAGES = ("write_plan_spec", "write_plan_tasks")
ASSESS_STAGES = ("assess",)
ALL_STAGES = PLAN_STAGES + ASSESS_STAGES
ORIG = {s: copy.deepcopy(W.LLM.registry.raw["stages"][s]) for s in ALL_STAGES}


def set_variant(variant: str, stages: tuple[str, ...]) -> None:
    for llm in (W.LLM, N.LLM):
        reg = llm.registry
        for s in stages:
            base = copy.deepcopy(ORIG[s])
            if variant == "B":
                base.update({"provider": "minimax31", "reasoning_effort": "low",
                             "thinking_effort": "low"})
            reg.raw["stages"][s] = base
            reg._cache.pop(s, None)
        for s in stages:
            cfg = reg.stage(s)  # 立即校验，配置有问题当场炸
            print(f"  [{variant}] {s}: model={cfg.model} "
                  f"effort={cfg.reasoning_effort} think={cfg.thinking_effort} "
                  f"max_out={cfg.max_output_tokens}", flush=True)


# ---------- 还原快照 ----------
ser = JsonPlusSerializer()
cp_db = sqlite3.connect(ROOT / "data/checkpoints.db")


def snapshots(ns: str):
    for typ, blob in cp_db.execute(
            "select type,checkpoint from checkpoints where thread_id=? "
            "and checkpoint_ns=? order by checkpoint_id", (SID, ns)):
        cp = ser.loads_typed((typ, blob))
        yield cp.get("ts", ""), dict(cp["channel_values"])


PLAN_STATE = None
for ts, cv in snapshots(NS):
    if len(cv) > 1 and cv.get("question") and not cv.get("spec") \
            and cv.get("on_topic"):
        PLAN_STATE = cv
        print("plan state @", ts, flush=True)
        break
ASSESS_STATE = None
for ts, cv in snapshots(""):
    if cv.get("outline") and cv.get("round") == 1 and cv.get("session_id"):
        ASSESS_STATE = cv          # 取 round==1 里最新的一份
        a_ts = ts
if ASSESS_STATE:
    print("assess state @", a_ts, flush=True)
if not ONLY or ONLY == "plan":
    assert PLAN_STATE, "没有 plan 初始快照"
if not ONLY or ONLY == "assess":
    assert ASSESS_STATE, "没有 round==1 的主图快照"

# ---------- 度量（宽松：只比数字串，去千分位） ----------
UNITS = (r"(?:wh/kg|w·h·kg-1|wh·kg-1|gwh|mwh|kwh|wh|ah|ms/cm|s/cm|usd(?:/kwh)?|"
         r"美元|元|%|倍|℃|°c|mm|μm|nm|mpa|gpa|v|圈|项|万辆|辆|gw|万|亿|km|"
         r"分钟|小时|kg|g|l|次|年)")
NUM_UNIT = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:[–—~～-]\s*(\d+(?:\.\d+)?))?\s*" + UNITS,
    re.I)
YEAR = re.compile(r"(?<![\d.])((?:19|20)\d{2})(?![\d])")
SEG_LOCK = threading.Lock()
SEG_CACHE: dict[str, str] = {}


def seg_fulltext(refs: list[str]) -> str:
    need = [r for r in refs if r not in SEG_CACHE]
    if need:
        res = EvidenceTools(get_repo(), SID).dispatch(
            [{"name": "read_segments", "input": {"segments": need}}])
        for g in (json.loads(res[0]["content"]).get("段落") or []):
            with SEG_LOCK:
                SEG_CACHE[str(g.get("段号"))] = str(g.get("全文") or "")
    return "\n\n".join(SEG_CACHE.get(r, "") for r in refs)


def digits_of(text: str) -> set[str]:
    t = re.sub(r"(?<=\d),(?=\d{3})", "", text or "")
    out = set()
    for m in NUM_UNIT.finditer(t):
        out.update(g for g in (m.group(1), m.group(2)) if g)
    out.update(YEAR.findall(t))
    return out


def has_digits(g: str, ev: str) -> bool:
    return re.search(r"(?<![\d.])" + re.escape(g) + r"(?![\d])", ev) is not None


def plan_metrics(rec: dict) -> dict:
    tasks = rec.get("tasks") or []
    covered, n_claim, n_ungr, per = set(), 0, 0, []
    for t in tasks:
        claim = digits_of(str(t.get("mandate") or "") + " "
                          + " ".join(W._as_str_list(t.get("must_cover"))))
        ev = re.sub(r"(?<=\d),(?=\d{3})", "",
                    seg_fulltext(t.get("segments") or []))
        ungr = sorted(g for g in claim if not has_digits(g, ev))
        n_claim += len(claim)
        n_ungr += len(ungr)
        covered.update(W._as_str_list(t.get("covers")))
        per.append({"id": t.get("id"), "covers": t.get("covers"),
                    "n_seg": len(t.get("segments") or []),
                    "n_claim": len(claim), "ungrounded": ungr})
    outline_ids = [o.get("id") for o in (PLAN_STATE or {}).get("outline", [])
                   if o.get("id")]
    spec = rec.get("spec") or {}
    return {"n_tasks": len(tasks), "n_claim": n_claim, "n_ungrounded": n_ungr,
            "sections_uncovered": [s for s in outline_ids if s not in covered],
            "spec_sections": len(spec.get("sections") or []),
            "spec_cautions": len(spec.get("cautions") or []),
            "per_task": per}


# ---------- 运行 ----------
def ensure_session(tag: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    c = sqlite3.connect(DB)
    c.execute("insert or ignore into session(id,title,question,created_at,"
              "updated_at) values(?,?,?,?,?)", (tag, tag, tag, now, now))
    c.commit()
    c.close()


def usage_of(tag: str) -> dict:
    d = sqlite3.connect(DB)
    out = {}
    for st, n, it, ot, ms, bad in d.execute(
            "select stage,count(*),coalesce(sum(input_tokens),0),"
            "coalesce(sum(output_tokens),0),coalesce(sum(latency_ms),0),"
            "sum(status!='ok') from llm_call where session_id=? group by stage",
            (tag,)):
        out[st] = {"calls": n, "in_tokens": it, "out_tokens": ot,
                   "ms": ms, "failed_calls": bad}
    d.close()
    return out


def run_plan(variant: str, i: int) -> dict:
    tag = f"exp_mab_plan_{variant}_{i}"
    ensure_session(tag)
    st = dict(PLAN_STATE)
    st["session_id"] = SID
    rec = {"kind": "plan", "variant": variant, "i": i, "tag": tag,
           "plan_failed": None, "error": None, "t_spec_s": None,
           "t_tasks_s": None, "events": []}
    try:
        with session_scope(tag):
            t0 = time.time()
            r1 = W.node_plan_spec(st)
            rec["t_spec_s"] = round(time.time() - t0, 1)
            st.update(r1 or {})
            rec["events"] += (r1 or {}).get("events") or []
            if not st.get("plan_failed"):
                t1 = time.time()
                r2 = W.node_plan_tasks(st)
                rec["t_tasks_s"] = round(time.time() - t1, 1)
                st.update(r2 or {})
                rec["events"] += (r2 or {}).get("events") or []
            rec["plan_failed"] = st.get("plan_failed")
        rec["spec"], rec["tasks"] = st.get("spec"), st.get("tasks")
        rec["metrics"] = plan_metrics(rec)
    except Exception as e:
        rec["error"] = repr(e)
    rec["usage"] = usage_of(tag)
    return rec


_CAP = threading.local()
_orig_cj = N.LLM.complete_json


def _cj(stage, *a, **k):
    r = _orig_cj(stage, *a, **k)
    if stage == "assess":
        _CAP.data = r
    return r


N.LLM.complete_json = _cj


def run_assess(variant: str, i: int) -> dict:
    tag = f"exp_mab_assess_{variant}_{i}"
    ensure_session(tag)
    st = dict(ASSESS_STATE)
    st["session_id"] = SID
    rec = {"kind": "assess", "variant": variant, "i": i, "tag": tag,
           "error": None, "t_s": None, "data": None}
    _CAP.data = None
    try:
        with session_scope(tag):
            t0 = time.time()
            r = N.node_assess(st)
            rec["t_s"] = round(time.time() - t0, 1)
        rec["data"] = _CAP.data
        rec["events"] = (r or {}).get("events")
    except Exception as e:
        rec["error"] = repr(e)
    rec["usage"] = usage_of(tag)
    return rec


results: list[dict] = []
phases = []
if not ONLY or ONLY == "plan":
    phases += [("A", PLAN_STAGES, run_plan), ("B", PLAN_STAGES, run_plan)]
if not ONLY or ONLY == "assess":
    phases += [("A", ASSESS_STAGES, run_assess), ("B", ASSESS_STAGES, run_assess)]

for variant, stages, fn in phases:
    print(f"== {fn.__name__} variant {variant} ==", flush=True)
    set_variant(variant, stages)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        rs = list(ex.map(lambda i: fn(variant, i), range(N_PER)))
    results += rs
    for r in rs:
        brief = {k: r.get(k) for k in ("kind", "variant", "i", "error",
                                       "plan_failed", "t_spec_s", "t_tasks_s",
                                       "t_s")}
        print(json.dumps(brief, ensure_ascii=False), "usage:",
              json.dumps(r["usage"], ensure_ascii=False), flush=True)
    # 每阶段结束就落盘，中途挂了也留得住
    for r in rs:
        (OUT / f"{r['kind']}_{r['variant']}_{r['i']}.json").write_text(
            json.dumps(r, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8")

lines = []
for r in results:
    u = r["usage"]
    if r["kind"] == "plan":
        m = r.get("metrics") or {}
        lines.append(
            f"plan {r['variant']}_{r['i']} err={r['error']} failed={r['plan_failed']!r} "
            f"spec_s={r['t_spec_s']} tasks_s={r['t_tasks_s']} "
            f"tasks={m.get('n_tasks')} uncovered={m.get('sections_uncovered')} "
            f"claim={m.get('n_claim')} ungr={m.get('n_ungrounded')} "
            f"spec_secs={m.get('spec_sections')} usage={json.dumps(u, ensure_ascii=False)}")
    else:
        d = r.get("data") or {}
        gaps = [g for g in (d.get("gaps") or []) if isinstance(g, dict)]
        lines.append(
            f"assess {r['variant']}_{r['i']} err={r['error']} t={r['t_s']} "
            f"has_enough={d.get('has_enough')} gaps={len(gaps)} "
            f"urg={[g.get('urgency') for g in gaps]} "
            f"keys={sorted(d.keys())} usage={json.dumps(u, ensure_ascii=False)}")
(OUT / "summary.txt").write_text("\n".join(lines), encoding="utf-8")
print("\n".join(lines))
print("WROTE", OUT / "summary.txt", flush=True)
