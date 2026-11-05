"""E4: 逐任务「无证据清单」注入 C/D 实验（不改仓库提示词/源码）。

A = 基线（不改 task；t2 的 A 复用 /tmp/exp_ab/e2_raw/A_*.json 正文）。
C = task["mandate"] 末尾追加程序算出的无证据清单块。
D = C + task["must_cover"] 中含 forbid token 的项替换为「……（具体数字/年份
    无证据支撑，如实写「本次材料未覆盖」）」。

forbid（按食谱）：cand = 两条正则对 `mandate + " ".join(must_cover)` 的
**原文匹配串**（不去空白，保留 "711 Wh/kg" 这种可读形态）；seg_text = 任务
segments 全文拼接后去所有空白；forbid = [c for c in cand
if c.replace(" ","") not in seg_text]，按首次出现顺序去重。

运行：t2×{C,D} 各 4 次；t3×{A,C,D} 各 3 次。生产入口 _run_task，
session_scope(唯一假 sid) 归属 llm_call（对应 session 行脚本自建）。
每任务内部串行跑，保证 write_trace.section_notes[tid] 归属不串。

用法: .venv/bin/python tests/experiments/exp_ab_inject.py
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
from research_agent.graph.subgraphs import write as W  # noqa: E402
from research_agent.llm.provider import session_scope  # noqa: E402
from research_agent.tools.evidence import EvidenceTools  # noqa: E402

SID = "s_2571e8f58f50"
NS = "write:fe076c41-284c-5a99-e570-08e90ec7ccd5"
TS_AFTER = "2026-10-19T12:50"
OUT = Path("/tmp/exp_ab")
RAW = OUT / "e4_raw"
RAW.mkdir(parents=True, exist_ok=True)

# ---------- 还原最新快照 ----------
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
TASKS = {t["id"]: t for t in SNAP["tasks"] if t.get("id") in ("t2", "t3")}
assert set(TASKS) == {"t2", "t3"}

# ---------- forbid 计算（按食谱） ----------
YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
NUM_RE = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:Wh/kg|GWh|MWh|kWh|Wh|Ah|mS/cm|S/cm|USD|美元|元|%|％"
    r"|倍|℃|项|万辆|辆|圈|GW|万|亿)")


def candidates(text: str) -> list[str]:
    """原文匹配串，按首次出现顺序去重（不去空白）。"""
    hits = YEAR_RE.findall(text) + NUM_RE.findall(text)
    # 合并后再按原文位置排序，保序去重。
    pos = []
    for m in YEAR_RE.finditer(text):
        pos.append((m.start(), m.group(0)))
    for m in NUM_RE.finditer(text):
        pos.append((m.start(), m.group(0)))
    pos.sort()
    seen, out = set(), []
    for _, s in pos:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def seg_fulltext(refs: list[str]) -> str:
    res = EvidenceTools(get_repo(), SID).dispatch(
        [{"name": "read_segments", "input": {"segments": refs}}])
    return "\n\n".join(str(g.get("全文") or "")
                       for g in (json.loads(res[0]["content"]).get("段落") or []))


FORBID: dict[str, list[str]] = {}
for tid, t in TASKS.items():
    text = str(t.get("mandate") or "") + " " + " ".join(
        W._as_str_list(t.get("must_cover")))
    cand = candidates(text)
    stext = re.sub(r"\s+", "", seg_fulltext(t.get("segments") or []))
    FORBID[tid] = [c for c in cand if c.replace(" ", "") not in stext]
    print(f"{tid} cand={len(cand)} forbid={FORBID[tid]}", flush=True)


def make_task(tid: str, variant: str) -> dict:
    t = dict(TASKS[tid])
    forbid = FORBID[tid]
    if variant in ("C", "D") and forbid:
        t["mandate"] = (str(t.get("mandate") or "") + "\n\n"
                        "【无证据清单——你的证据段落里找不到下列数字/年份，"
                        "**一律不得写成事实**，也不要写成「据某篇未列入清单的文章」"
                        "「来自某来源」：" + "、".join(forbid) + "。\n"
                        "这些内容对应的位置，直接写「本次材料未覆盖 ……」"
                        "并在 notes 里说明。上面「必须写到」里涉及这些数字/年份"
                        "的部分，以本条为准。】")
    if variant == "D" and forbid:
        mc = W._as_str_list(t.get("must_cover"))
        t["must_cover"] = [
            (f"{item[:14]}……（具体数字/年份无证据支撑，如实写"
             f"「本次材料未覆盖」）"
             if any(c in item or c.replace(" ", "") in item.replace(" ", "")
                    for c in forbid) else item)
            for item in mc]
    return t


# ---------- 指标 ----------
EXCUSE_RE = re.compile(r"未列入|未纳入|证据清单|来自.{0,6}文章")
GAP_RE = re.compile(r"本次材料未覆盖|未找到独立信源|未覆盖")


def tok(text: str) -> set:
    t = re.sub(r"\s+", "", text or "")
    return set(YEAR_RE.findall(t)) | set(NUM_RE.findall(t))


def body_paras(body: str) -> list[str]:
    out = []
    for p in re.split(r"\n\s*\n", body or ""):
        lines = [l for l in p.strip().splitlines() if l.strip()]
        if not lines:
            continue
        if all(l.strip().startswith("#") or l.strip().startswith("|")
               for l in lines):
            continue
        out.append(p)
    return out


def metrics(tid: str, body: str) -> dict:
    forbid = FORBID[tid]
    paras = body_paras(body)
    flat = re.sub(r"\s+", "", body or "")
    in_body = [c for c in forbid if c.replace(" ", "") in flat]
    detail = []
    n_nocite = 0
    for c in in_body:
        cs = c.replace(" ", "")
        hit = [p for p in paras
               if c in p or cs in re.sub(r"\s+", "", p)]
        nocite = [p for p in hit if "[S" not in p]
        if nocite:
            n_nocite += 1
        detail.append({"token": c, "paras": hit, "paras_nocite": nocite})
    return {"n_in_body": len(in_body), "n_in_body_nocite": n_nocite,
            "in_body_detail": detail,
            "numeric_nocite_paras": sum(
                1 for p in paras if tok(p) and "[S" not in p),
            "excuse_marks": len(EXCUSE_RE.findall(body or "")),
            "gap_marks": len(GAP_RE.findall(body or "")),
            "body_chars": len(body or "")}


# ---------- 运行 ----------
def run_one(tid: str, variant: str, i: int) -> dict:
    tag = f"exp_ab_e4_{tid}_{variant}_{i}"
    st = dict(SNAP)
    st["session_id"] = SID
    task = make_task(tid, variant)
    rec = {"task_id": tid, "variant": variant, "i": i, "tag": tag,
           "secs": None, "err": None, "error": None, "sec_s": None,
           "usage": {}, "notes": None, "notes_unavailable": None,
           "task_sent": {"mandate": task["mandate"],
                         "must_cover": task["must_cover"]}}
    try:
        t0 = time.time()
        with session_scope(tag):
            secs, err = W._run_task(st, task, None)
        rec["sec_s"] = round(time.time() - t0, 1)
        rec["secs"], rec["err"] = secs, err
        tr = get_repo().write_trace(SID) or {}
        notes = (tr.get("stats") or {}).get("section_notes") or {}
        rec["notes"] = notes.get(tid)
        if not rec["notes"]:
            rec["notes_unavailable"] = "write_trace.section_notes 无条目"
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


def run_group(tid: str, variant: str, n: int) -> list[dict]:
    return [run_one(tid, variant, i) for i in range(n)]


# FK 需要 session 行：先建齐
_now = "2026-10-20T00:00:00"
_d = sqlite3.connect(DB)
for tid, specs in (("t2", (("C", 4), ("D", 4))),
                   ("t3", (("A", 3), ("C", 3), ("D", 3)))):
    for v, n in specs:
        for i in range(n):
            _d.execute(
                "INSERT OR IGNORE INTO session "
                "(id,title,question,status,cost_tokens,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"exp_ab_e4_{tid}_{v}_{i}", "exp_ab", "e4", "running",
                 0, _now, _now))
_d.commit()
_d.close()

# 同任务的所有变体串行跑（notes 按 task_id 键控，并行会串）；
# t2/t3 两条任务链并行（2 路 ≤ 4 上限）。
def run_task_all(tid: str, specs) -> list[dict]:
    out = []
    for v, n in specs:
        out += run_group(tid, v, n)
    return out


results: list[dict] = []
with ThreadPoolExecutor(max_workers=2) as ex:
    futs = [ex.submit(run_task_all, "t2", (("C", 4), ("D", 4))),
            ex.submit(run_task_all, "t3", (("A", 3), ("C", 3), ("D", 3)))]
    for f in futs:
        rs = f.result()
        results += rs
        for r in rs:
            print(r["task_id"], r["variant"], r["i"],
                  "err:", r.get("err"), "exc:", r.get("error"),
                  "sec:", r["sec_s"], "usage:", r["usage"], flush=True)

# t2 的 A 基线：复用 e2_raw 正文 + forbid 重算指标
t2_base = []
for i in range(4):
    p = Path(f"/tmp/exp_ab/e2_raw/A_{i}.json")
    if p.exists():
        r = json.loads(p.read_text(encoding="utf-8"))
        body = r.get("body") or "\n\n".join(
            str(v) for v in (r.get("secs") or {}).values())
        t2_base.append({"task_id": "t2", "variant": "A", "i": i,
                        "sec_s": r.get("sec_s"), "usage": r.get("usage"),
                        "err": r.get("err"), "error": r.get("error"),
                        "notes": r.get("notes"), "body": body,
                        "metrics": metrics("t2", body),
                        "source": "e2_raw reuse"})

# ---------- 落盘与汇总 ----------
all_runs = results + t2_base
for r in results:
    body = "\n\n".join(str(v) for v in (r.get("secs") or {}).values())
    r["body"] = body
    r["metrics"] = metrics(r["task_id"], body)
    (RAW / f"{r['task_id']}_{r['variant']}_{r['i']}.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
for r in t2_base:
    (RAW / f"t2_A_{r['i']}.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")

lines = [f"forbid_t2={FORBID['t2']}", f"forbid_t3={FORBID['t3']}", ""]
for tid in ("t2", "t3"):
    for v in ("A", "C", "D"):
        rs = [r for r in all_runs
              if r["task_id"] == tid and r["variant"] == v]
        if not rs:
            continue
        ms = [r["metrics"] for r in rs if r.get("metrics")]
        n = len(ms)
        if n:
            lines.append(
                f"{tid}/{v} n={n}: in_body={sum(m['n_in_body'] for m in ms)/n:.2f} "
                f"in_body_nocite={sum(m['n_in_body_nocite'] for m in ms)/n:.2f} "
                f"num_nocite_paras={sum(m['numeric_nocite_paras'] for m in ms)/n:.2f} "
                f"excuse={sum(m['excuse_marks'] for m in ms)/n:.2f} "
                f"gap={sum(m['gap_marks'] for m in ms)/n:.2f} "
                f"chars={sum(m['body_chars'] for m in ms)/n:.0f}")
lines.append("")
for r in all_runs:
    m = r.get("metrics") or {}
    lines.append(
        f"--- {r['task_id']}_{r['variant']}_{r['i']} err={r.get('err')!r} "
        f"exc={r.get('error')!r} sec={r['sec_s']} usage={r.get('usage')} "
        f"secs_keys={list((r.get('secs') or {}).keys())} "
        f"notes={str(r.get('notes'))[:120]!r} "
        f"notes_unavailable={r.get('notes_unavailable')!r}\n"
        f"    in_body={m.get('n_in_body')} nocite={m.get('n_in_body_nocite')} "
        f"num_nocite_paras={m.get('numeric_nocite_paras')} "
        f"excuse={m.get('excuse_marks')} gap={m.get('gap_marks')} "
        f"chars={m.get('body_chars')}\n"
        f"    detail={json.dumps(m.get('in_body_detail'), ensure_ascii=False)}")

(OUT / "e4_summary.txt").write_text("\n".join(lines), encoding="utf-8")
print("WROTE", OUT / "e4_summary.txt", flush=True)
