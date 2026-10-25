"""「写作任务一次带全证据」（RA_WRITE_PRELOAD）A/B 实验驱动（真实 API）。

2026-10-25 批 3。输入取自 v20 基线 session s_b5b4c1d4469a：
write_trace 里的 spec/tasks + session.outline_json + chunk/source 表
（编号逻辑照抄 node_write），重建 WriteState 后**只跑 node_dispatch**
（含 auto_fix / check_citations 收尾），不跑审查返修。

两组各 n=3，串行：A = 现状多轮读证据（_WRITE_PRELOAD=False），
B = 预加载（=True）。每次运行：dispatch 墙钟、每任务墙钟/via、
write_section 的调用数与 token、wait_ms、失败任务、violations、
ground_scan 同口径的「证据外数字」去重数、各任务正文字数。

用法：
    .venv/bin/python tests/exp_write_preload.py

输出：/tmp/exp_write_preload/{tag}.json（drafts+meta）+ summary.txt。
⚠️ 库是 data/refs/ra_v20_batch2.db 的 /tmp 副本；llm 记账按 tag 会话
   归属（session_scope + 桩 session 行），业务读写都落在副本上。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        k, v = _line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

REAL_SID = "s_b5b4c1d4469a"
SRC_DB = ROOT / "data/refs/ra_v20_batch2.db"
DB = "/tmp/ra_v20_preload_ab.db"
OUT = Path("/tmp/exp_write_preload")
OUT.mkdir(parents=True, exist_ok=True)

if not Path(DB).exists():
    shutil.copy(SRC_DB, DB)
os.environ["RA_DB_PATH"] = DB
# 脚本内直接翻转模块开关，env 保持默认关（防子进程误开）。
os.environ["RA_WRITE_PRELOAD"] = "0"

from research_agent.db.repo import get_repo             # noqa: E402
from research_agent.graph.subgraphs import write as W    # noqa: E402
from research_agent.llm.provider import session_scope    # noqa: E402

# ---- ground_scan 同口径（tests/ground_scan.py 的宽松比对）----
UNITS = (r"(?:wh/kg|w·h·kg-1|wh·kg-1|gwh|mwh|kwh|wh|ah|ms/cm|s/cm|usd(?:/kwh)?|"
         r"美元|元|%|倍|℃|°c|mm|μm|nm|mpa|gpa|v|圈|项|万辆|辆|gw|万|亿|km|"
         r"分钟|小时|kg|g|l|次|年)")
CIT = re.compile(r"\[S\d+(?:·\d+)?\]")
NUM_UNIT = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:[–—~～-]\s*(\d+(?:\.\d+)?))?\s*" + UNITS,
    re.I)


def build_state(repo) -> dict:
    """照抄 node_write 的来源/段号逻辑重建 WriteState。"""
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
    n_no_summary = sum(1 for c in segs
                       if not (c.get("summary") or "").strip())
    keep = [c for c in segs if c.get("on_topic")]
    src_lines = [
        f"[S{src_no[s['id']]}] {s.get('title') or s['domain']} | {s['domain']} "
        f"| {s.get('source_type')} | {s.get('source_date') or '日期未知'}"
        f" | {s.get('source_nature') or '性质未判'}"
        f" | 正文 {s.get('char_count') or 0} 字"
        for s in all_sources]
    tr = repo.write_trace(REAL_SID)
    q = repo.q1("SELECT question, outline_json FROM session WHERE id=?",
                (REAL_SID,))
    return {
        "session_id": REAL_SID, "question": q["question"],
        "outline": (json.loads(q["outline_json"] or "{}")
                    .get("sections") or []),
        "signals": {},
        "segments": segs, "on_topic": keep, "src_lines": src_lines,
        "n_no_summary": n_no_summary,
        "spec": tr.get("spec") or {}, "tasks": tr.get("tasks") or [],
        "drafts": {}, "violations": {}, "failed": [],
        "rework_rounds": 0, "rework_effective": True,
        "revise_unresolved": [],
    }


def ground_count(drafts: dict, tasks: list, state: dict) -> dict:
    """ground_scan 口径：每个任务分配段全文当证据，数正文里
    「带单位数字/年份」在证据里找不到的去重数。"""
    seg_text: dict[str, str] = {}
    for g in state.get("on_topic") or []:
        seg_text[g.get("ref")] = re.sub(r"(?<=\d),(?=\d{3})", "",
                                        g.get("content") or "")
    plan = re.sub(r"(?<=\d),(?=\d{3})", "", json.dumps(
        {"spec": state.get("spec"), "tasks": tasks}, ensure_ascii=False))
    tmap = {t["id"]: t for t in tasks}
    tot, bad = 0, set()
    in_plan = 0
    for tid, secs in (drafts or {}).items():
        t = tmap.get(tid)
        if not t:
            continue
        ev = "\n".join(seg_text.get(r, "")
                       for r in (t.get("segments") or []))
        for sec, body in secs.items():
            for line in CIT.sub("", body).splitlines():
                if line.lstrip().startswith("#"):
                    continue
                line = re.sub(r"(?<=\d),(?=\d{3})", "", line)
                for m in NUM_UNIT.finditer(line):
                    for g in (m.group(1), m.group(2)):
                        if not g:
                            continue
                        tot += 1
                        pat = r"(?<![\d.])" + re.escape(g) + r"(?![\d])"
                        if re.search(pat, ev) is None:
                            if (tid, g) not in bad and re.search(pat, plan):
                                in_plan += 1
                            bad.add((tid, g))
    return {"total": tot, "bad_dedup": len(bad), "in_plan": in_plan}


def run_one(tag: str, preload: bool, state: dict, repo) -> dict:
    """跑一次 node_dispatch，返回该次运行的全部指标。"""
    # 桩 session 行：llm_call/span 按 tag 归属，外键/联查不缺父行。
    if not repo.q1("SELECT id FROM session WHERE id=?", (tag,)):
        repo.x("INSERT INTO session (id, title, question, status, created_at, "
               "updated_at) VALUES (?,?,?,?,datetime('now'),datetime('now'))",
               (tag, f"exp {tag}", f"exp {tag}", "done"))

    # write_trace stats 是累积的——diff 出本次的 via/fallback 计数。
    before = repo.write_trace(REAL_SID).get("stats") or {}
    llm_before = repo.q1("SELECT COALESCE(MAX(rowid),0) m FROM llm_call")["m"]

    # 每任务墙钟 + via：包 _run_task 计时、包 _record_submit 拿 via
    # （不改生产代码）。
    task_wall: dict[str, float] = {}
    task_via: dict[str, str] = {}
    orig_run = W._run_task
    orig_rec = W._record_submit

    def timed(st, task, revise):
        t0 = time.time()
        r = orig_run(st, task, revise)
        task_wall[task["id"]] = round(time.time() - t0, 1)
        return r

    def rec(st, task_id, via, notes):
        task_via[task_id] = via
        return orig_rec(st, task_id, via, notes)

    orig_flag = W._WRITE_PRELOAD
    W._WRITE_PRELOAD = preload
    W._run_task = timed
    W._record_submit = rec
    t0 = time.time()
    try:
        with session_scope(tag):
            out = W.node_dispatch(state)
    finally:
        W._run_task = orig_run
        W._record_submit = orig_rec
        W._WRITE_PRELOAD = orig_flag
    wall = round(time.time() - t0, 1)

    after = repo.write_trace(REAL_SID).get("stats") or {}

    def _diff_counter(key):
        a, b = before.get(key), after.get(key)
        if isinstance(a, dict) or isinstance(b, dict):
            return {k: (b or {}).get(k, 0) - (a or {}).get(k, 0)
                    for k in set(a or {}) | set(b or {})}
        return (b or 0) - (a or 0)

    rows = repo.q(
        "SELECT stage, COUNT(*) n, SUM(input_tokens) tin, "
        "       SUM(output_tokens) tout, SUM(wait_ms) w "
        "FROM llm_call WHERE session_id=? AND rowid>? GROUP BY stage",
        (tag, llm_before))
    calls = {r["stage"]: dict(r) for r in rows}
    ws = calls.get("write_section", {})
    drafts = out.get("drafts") or {}
    failed = out.get("failed") or []
    violations = out.get("violations") or {}
    viol_by_kind: dict[str, int] = {}
    for tid, bads in violations.items():
        for b in bads or []:
            k = b.get("kind") or "?"
            viol_by_kind[k] = viol_by_kind.get(k, 0) + 1
    ground = ground_count(drafts, state.get("tasks") or [], state)
    chars = {tid: sum(len(str(v)) for v in secs.values())
             for tid, secs in drafts.items()}
    via_diff = _diff_counter("submit_via")
    # via 按任务：drafts 里每任务对应计数不可分，但 preload_fallback
    # 与 preload 计数能从 stats diff 拿。
    res = {
        "tag": tag, "preload": preload, "wall_s": wall,
        "task_wall": task_wall, "task_via": task_via,
        "submit_via": via_diff,
        "preload_fallback": _diff_counter("preload_fallback"),
        "calls": calls,
        "write_section_n": ws.get("n", 0),
        "write_section_in": ws.get("tin", 0) or 0,
        "write_section_out": ws.get("tout", 0) or 0,
        "wait_ms_total": (ws.get("w") or 0),
        "failed": [f.get("task") for f in failed],
        "violations_total": sum(len(v or []) for v in violations.values()),
        "viol_by_kind": viol_by_kind,
        "ground": ground,
        "chars_total": sum(chars.values()), "chars_by_task": chars,
        "empty_sections": [tid for tid, c in chars.items() if c == 0],
        "drafts": drafts,
    }
    (OUT / f"{tag}.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def line(r: dict) -> str:
    return (f"{r['tag']}: wall={r['wall_s']}s n={r['write_section_n']} "
            f"in={r['write_section_in']} out={r['write_section_out']} "
            f"wait={r['wait_ms_total']}ms failed={len(r['failed'])} "
            f"viol={r['violations_total']}{r['viol_by_kind']} "
            f"ground_bad={r['ground']['bad_dedup']}/{r['ground']['total']} "
            f"chars={r['chars_total']} via={r['submit_via']} "
            f"pf={r['preload_fallback']}")


def main() -> None:
    repo = get_repo()
    state = build_state(repo)
    print(f"session={REAL_SID} tasks={len(state['tasks'])} "
          f"on_topic={len(state['on_topic'])}", flush=True)
    results = []
    plan = [("A", False)] * 3 + [("B", True)] * 3
    for i, (grp, preload) in enumerate(plan):
        tag = f"exp_{grp}{i}"
        print(f"[{time.strftime('%H:%M:%S')}] {tag} preload={preload} 起跑",
              flush=True)
        try:
            r = run_one(tag, preload, build_state(repo), repo)
        except Exception as e:
            print(f"⚠️  {tag} 崩了：{type(e).__name__}: {e}", flush=True)
            r = {"tag": tag, "preload": preload, "error": repr(e)}
            (OUT / f"{tag}.json").write_text(json.dumps(r), encoding="utf-8")
        results.append(r)
        print(line(r) if "error" not in r else f"{tag}: ERROR {r['error']}",
              flush=True)

    # 汇总：判据所需的组均值
    def runs_of(grp):
        return [r for r in results
                if r.get("tag", "").startswith("exp_" + grp)
                and "error" not in r]

    def avg(grp, key):
        xs = [r[key] for r in runs_of(grp) if key in r]
        return sum(xs) / len(xs) if xs else 0

    summary = {"runs": [{k: v for k, v in r.items() if k != "drafts"}
                        for r in results]}
    for grp in ("A", "B"):
        summary[grp] = {k: round(avg(grp, k), 1) for k in
                        ("wall_s", "write_section_n", "write_section_in",
                         "write_section_out", "wait_ms_total",
                         "violations_total", "chars_total")}
        summary[grp]["failed_total"] = sum(
            len(r.get("failed", [])) for r in runs_of(grp))
        gb = [r["ground"]["bad_dedup"] for r in runs_of(grp)
              if "ground" in r]
        summary[grp]["ground_bad_avg"] = round(sum(gb) / len(gb), 2) if gb else 0
        summary[grp]["preload_fallback_runs"] = [
            r.get("preload_fallback") for r in runs_of(grp)]
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(OUT / "summary.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(
            line(r) if "error" not in r else f"{r['tag']}: ERROR {r['error']}"
            for r in results))
        f.write("\n\n" + json.dumps(
            {g: summary[g] for g in ("A", "B")}, ensure_ascii=False, indent=1))
    print("\n=== 组均值 ===")
    print(json.dumps({g: summary[g] for g in ("A", "B")},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
