"""E2: 写手规则提示词 A/B（运行时 monkeypatch WRITE_RULES，不改仓库文件）。

A = 当前 WRITE_RULES。
B = 在 `**交卷方式（硬性）：**` 之前插入 /tmp/exp_prompts/write_rules_add.txt
    （第 8 条，与第 7 条之间一个空行，其余逐字不动）。

每次运行 = t2 任务 state → 生产入口 W._run_task(state, t2, None)。
t2 的 mandate/must_cover 原样保留（含 711 Wh/kg、1978/1987/1988/2014/2022
等证据段里不存在的数字）。state.session_id=真实 sid（EvidenceTools 走它），
llm_call 用 session_scope(唯一假 sid) 标记归属。

用法: .venv/bin/python tests/exp_ab_write.py
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
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

from research_agent.db.repo import get_repo  # noqa: E402
from research_agent.graph import prompts  # noqa: E402
from research_agent.graph.subgraphs import write as W  # noqa: E402
from research_agent.llm.provider import session_scope  # noqa: E402
from research_agent.tools.evidence import EvidenceTools  # noqa: E402

SID = "s_2571e8f58f50"
NS = "write:fe076c41-284c-5a99-e570-08e90ec7ccd5"
TS_AFTER = "2026-10-19T12:50"
OUT = Path("/tmp/exp_ab")
RAW = OUT / "e2_raw"
RAW.mkdir(parents=True, exist_ok=True)
N_PER = 4
WORKERS = 2

# ---------- 还原最新快照（含 tasks） ----------
ser = JsonPlusSerializer()
c = sqlite3.connect(ROOT / "data/checkpoints.db")
SNAP = None
for typ, blob in c.execute(
        "select type,checkpoint from checkpoints where thread_id=? "
        "and checkpoint_ns=? order by checkpoint_id desc", (SID, NS)):
    cp = ser.loads_typed((typ, blob))
    cv = dict(cp["channel_values"])
    if cp.get("ts", "") > TS_AFTER and len(cv) > 1 and cv.get("tasks"):
        SNAP = cv
        print("task state @", cp["ts"], flush=True)
        break
assert SNAP
T2 = next(t for t in SNAP["tasks"] if t.get("id") == "t2")
print("t2 covers:", T2.get("covers"), "| mandate:", str(T2.get("mandate"))[:80],
      "| must_cover:", json.dumps(W._as_str_list(T2.get("must_cover")),
                                  ensure_ascii=False)[:200], flush=True)

# ---------- 提示词变体 ----------
ANCHOR = "**交卷方式（硬性）：**"
ADD = Path("/tmp/exp_prompts/write_rules_add.txt").read_text(encoding="utf-8")
if ANCHOR not in prompts.WRITE_RULES:
    sys.exit("交卷方式锚点不存在，停")
ORIG_RULES = prompts.WRITE_RULES
B_RULES = ORIG_RULES.replace(ANCHOR, ADD.rstrip() + "\n\n" + ANCHOR, 1)

# ---------- tok()（与 E1 同） ----------
YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
NUM_RE = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:Wh/kg|GWh|MWh|kWh|Wh|Ah|mS/cm|S/cm|USD|美元|元|%|％"
    r"|倍|℃|项|万辆|辆|圈|GW|万|亿)")


def tok(text: str) -> set:
    t = re.sub(r"\s+", "", text or "")
    return set(YEAR_RE.findall(t)) | set(NUM_RE.findall(t))


# t2 的 ungrounded 固定集合
res = EvidenceTools(get_repo(), SID).dispatch(
    [{"name": "read_segments", "input": {"segments": T2.get("segments") or []}}])
SEG_FULL = "\n\n".join(str(g.get("全文") or "")
                       for g in (json.loads(res[0]["content"]).get("段落") or []))
CLAIM_T2 = tok(str(T2.get("mandate") or "") + " "
               + " ".join(W._as_str_list(T2.get("must_cover"))))
UNGROUNDED_T2 = sorted(
    x for x in CLAIM_T2 if x not in re.sub(r"\s+", "", SEG_FULL))
print("claim_t2:", sorted(CLAIM_T2), flush=True)
print("ungrounded_t2:", UNGROUNDED_T2, flush=True)

PROBE_RE = re.compile(r"未列入|未纳入|证据清单|来自.{0,6}文章")
GAP_RE = re.compile(r"本次材料未覆盖|未找到独立信源")


def all_paras(body: str) -> list[str]:
    return [p for p in re.split(r"\n\s*\n", body or "") if p.strip()]


def body_paras(body: str) -> list[str]:
    """空行分段；排除整段都是标题/表格行的段。"""
    out = []
    for p in all_paras(body):
        lines = [l for l in p.strip().splitlines() if l.strip()]
        if all(l.strip().startswith("#") or l.strip().startswith("|")
               for l in lines):
            continue
        out.append(p)
    return out


def metrics(body: str) -> dict:
    paras = body_paras(body)
    every = all_paras(body)
    ungr_detail = []
    n_ungr_in = n_ungr_nocite = 0
    for tk in UNGROUNDED_T2:
        if tk not in body:
            continue
        n_ungr_in += 1
        hit = [p for p in every if tk in p]
        nocite = [p for p in hit if "[S" not in p]
        if nocite:
            n_ungr_nocite += 1
        ungr_detail.append({"token": tk, "n_paras": len(hit),
                            "paras": hit, "paras_nocite": nocite})
    para_nocite_tok = [p for p in paras
                       if tok(p) and "[S" not in p]
    return {"n_ungr_in_body": n_ungr_in,
            "n_ungr_nocite": n_ungr_nocite,
            "ungr_detail": ungr_detail,
            "n_para_tok_nocite": len(para_nocite_tok),
            "para_tok_nocite": para_nocite_tok,
            "n_probe_refs": len(PROBE_RE.findall(body)),
            "n_gap_marks": len(GAP_RE.findall(body)),
            "body_chars": len(body)}


def run_one(variant: str, i: int) -> dict:
    tag = f"exp_ab_e2_{variant}_{i}"
    st = dict(SNAP)
    st["session_id"] = SID
    rec = {"variant": variant, "i": i, "tag": tag, "secs": None,
           "err": None, "error": None, "sec_s": None, "usage": {},
           "notes": None, "notes_unavailable": None}
    try:
        t0 = time.time()
        with session_scope(tag):
            secs, err = W._run_task(st, dict(T2), None)
        rec["sec_s"] = round(time.time() - t0, 1)
        rec["secs"], rec["err"] = secs, err
        tr = get_repo().write_trace(SID) or {}
        notes = (tr.get("stats") or {}).get("section_notes") or {}
        rec["notes"] = notes.get(T2["id"]) or notes.get("t2")
        if not rec["notes"]:
            rec["notes_unavailable"] = "write_trace.section_notes 无 t2 条目"
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


results: list[dict] = []
for variant, rules in (("A", ORIG_RULES), ("B", B_RULES)):
    prompts.WRITE_RULES = rules
    print(f"== phase {variant} ==", flush=True)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        rs = list(ex.map(lambda i: run_one(variant, i), range(N_PER)))
    results += rs
    for r in rs:
        print(variant, r["i"], "err:", r.get("err"), "exc:", r.get("error"),
              "sec:", r["sec_s"], "usage:", r["usage"], flush=True)
prompts.WRITE_RULES = ORIG_RULES

agg, lines = [], []
for v in ("A", "B"):
    rs = [r for r in results if r["variant"] == v]
    agg.append(f"{v}: runs={len(rs)}")

for r in results:
    body = "\n\n".join(str(v) for v in (r.get("secs") or {}).values())
    r["body"] = body
    r["metrics"] = metrics(body)
    (RAW / f"{r['variant']}_{r['i']}.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    m = r["metrics"]
    lines.append(
        f"--- {r['variant']}_{r['i']} err={r.get('err')!r} exc={r.get('error')!r} "
        f"sec={r['sec_s']} usage={r['usage']} notes={r.get('notes')!r} "
        f"notes_unavailable={r.get('notes_unavailable')!r}\n"
        f"    ungr_in_body={m['n_ungr_in_body']} ungr_nocite={m['n_ungr_nocite']} "
        f"para_tok_nocite={m['n_para_tok_nocite']} probe_refs={m['n_probe_refs']} "
        f"gap_marks={m['n_gap_marks']} body_chars={m['body_chars']}\n"
        f"    ungr_paras={json.dumps(m['ungr_detail'], ensure_ascii=False)}\n"
        f"    para_tok_nocite={json.dumps(m['para_tok_nocite'], ensure_ascii=False)}")

(OUT / "e2_summary.txt").write_text(
    "\n".join(agg) + "\n\n" + "\n".join(lines), encoding="utf-8")
print("WROTE", OUT / "e2_summary.txt", flush=True)
