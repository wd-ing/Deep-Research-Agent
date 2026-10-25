"""B2-5 合并闸门 A/B：summarize 顺带判 relevant vs 分离 relevance 闸门。

2026-10-25 时间优化批 2。只读实验：不改生产代码。

样本：data/refs/ra_v19_batch1.db + ra_v18_baseline.db 里
`gate_method='llm'` 的段（当时由 LLM 判定而非规则放行的段；
chunk.on_topic 是当时闸门的 verdict，**不是金标准**），
各抽 ≤100 段（分层保留 on/off 比例），共 ≤200 段。

三组：
  ① merged  —— 强制工具 emit_summaries 扩展 {index,summary,relevant}，
               同一批输入顺带判相关（新方案）
  ② control —— 同样本把现有 RELEVANCE_BATCH 闸门原样再跑一遍
               （测 CONTEXT 坑 62 的方法噪声底：同方法自比一致率）
  ③ 比较量  —— merged/control verdict vs 库里 verdict 一致率；
               误丢率 = 库里 on_topic=1 且 merged=0 / 库里 on_topic=1；
               merged 的 summary 非空率与长度 vs 库存 summary

判据（预先定死）：merged-vs-库 ≥ control-vs-库 − 3pp；误丢 ≤8%；
summary 非空率不低于现状；合并调用 3/3 无结构偏离（偏离率要报）。

用法：.venv/bin/python tests/exp_merge_gate.py [n_runs]   # 默认 3
输出：/tmp/exp_merge_gate/raw.json + 终端表格
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_V19 = ROOT / "data/refs/ra_v19_batch1.db"
DB_V18 = ROOT / "data/refs/ra_v18_baseline.db"
OUT = Path("/tmp/exp_merge_gate")
OUT.mkdir(exist_ok=True)
EXP_DB = "/tmp/ra_merge_gate.db"

BATCH = 12            # 与 GATE_LLM_BATCH / SUMMARY_BATCH 同口径
PER_DB = 100


def _sample(db_path: Path, cap: int) -> list[dict]:
    """读 gate_method='llm' 的段，分层抽样保 on/off 比例。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT c.id, c.session_id, c.heading_path, c.summary, c.content,
                  c.on_topic, s.title AS src_title, sess.question AS q
             FROM chunk c JOIN source s ON c.source_id = s.id
             JOIN session sess ON c.session_id = sess.id
            WHERE c.gate_method='llm'""").fetchall()
    segs = [dict(r) for r in rows]
    random.Random(42).shuffle(segs)
    on = [s for s in segs if s["on_topic"]]
    off = [s for s in segs if not s["on_topic"]]
    # 先全装少数派，再按比例补多数派
    take_off = off[:cap]
    take_on = on[:cap - len(take_off)]
    picked = take_off + take_on
    random.Random(7).shuffle(picked)
    q = con.execute("SELECT question FROM session LIMIT 1").fetchone()
    label = (q[0] if q else "")[:80]
    for s in picked:
        s["label"] = s.pop("q") or label
        s["db"] = db_path.stem
    con.close()
    return picked


def _batches(segs: list[dict]) -> list[list[dict]]:
    return [segs[i:i + BATCH] for i in range(0, len(segs), BATCH)]


# -------------------------------------------------- ① merged 调用

MERGED_SYS = """为报告的每个段落写一句话概括，并同时判定这段是否在讲研究对象。

**调用 `emit_summaries` 工具交卷**，不要直接输出 JSON 文本。每段一条：
`{"index": 段落编号, "summary": "一句话概括（40字内）", "relevant": bool}`

`index` 必须与输入段落的 [n] 一一对应，顺序不变。

`relevant` 的判定标准：这段是不是在讲研究对象？逐段独立判断，
不要因为相邻段落相关就放过无关段。
- 讲的是研究对象本身 → true
- 讲的是别的东西，只是碰巧出现了相同的词 → false
- 讲的是研究对象的某个侧面（竞品、应用场景、相关技术）→ true
注意：宁可 true 也不要 false——判断不准的代价是多留一个无关段落，
反过来漏判会让报告缺内容。"""

MERGED_SCHEMA = {"type": "object", "properties": {"summaries": {
    "type": "array", "description": "与输入段落一一对应的概括+相关判定",
    "items": {"type": "object", "properties": {
        "index": {"type": "integer", "description": "段落下标，对应输入里的 [n]"},
        "summary": {"type": "string", "description": "一句话概括"},
        "relevant": {"type": "boolean",
                     "description": "这段是否在讲研究对象"}},
        "required": ["index", "summary", "relevant"]}}},
    "required": ["summaries"]}


def _norm_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    return None


def _merged_batch(LLM, batch: list[dict], label: str) -> dict:
    """返回 {index: {"summary","relevant"}}, dev: 结构偏离计数, err"""
    blob = "\n\n".join(
        f"[{j}] {s.get('heading_path') or '(无)'}\n{(s.get('content') or '')[:3000]}"
        for j, s in enumerate(batch))
    data = LLM.complete_json_forced(
        "summarize",
        f"研究对象：{label}\n\n以下是 {len(batch)} 个报告段落，"
        f"请为每段写一句话概括并判定 relevant：\n\n{blob}",
        name="emit_summaries", schema=MERGED_SCHEMA, system=MERGED_SYS)
    out, dev = {}, 0
    for it in ((data or {}).get("summaries") or []):
        idx = it.get("index")
        if not isinstance(idx, int) or not (0 <= idx < len(batch)):
            dev += 1                      # 越界下标：跳过不猜
            continue
        summ = (it.get("summary") or "").strip()
        rel = _norm_bool(it.get("relevant"))
        if not summ:
            dev += 1
        if rel is None:
            dev += 1
        out[idx] = {"summary": summ, "relevant": rel}
    return {"got": out, "deviations": dev, "of": len(batch)}


# -------------------------------------------------- ② control：现有闸门原样

def _gate_blob(seg: dict) -> str:
    return " ".join([
        seg.get("heading_path") or "", seg.get("src_title") or "",
        seg.get("summary") or "",
        "" if (seg.get("summary") or "").strip()
        else (seg.get("content") or "")[:600]])[:3000]


def _control_batch(LLM, batch: list[dict], label: str) -> dict:
    """逐字段复刻 gate_segments._judge_batch + straggler 逐段补判。"""
    from research_agent.graph import prompts
    parts = []
    for j, s in enumerate(batch):
        blob = _gate_blob(s)
        parts.append(
            f"[{j}] 段落标题：{s.get('heading_path') or '(无)'}\n"
            f"这段在讲：{s.get('summary') or '(无)'}\n"
            f"原文开头：\n{blob[:2000]}")
    out, missing, strag = {}, 0, 0
    try:
        d = LLM.complete_json(
            "relevance",
            f"研究对象：{label}\n\n以下是 {len(batch)} 个候选段落，"
            f"逐一判定是否相关：\n\n" + "\n\n".join(parts),
            system=prompts.RELEVANCE_BATCH)
        want = set(range(len(batch)))
        for v in ((d or {}).get("verdicts") or []):
            idx = v.get("index")
            if not isinstance(idx, int) or idx not in want or idx in out:
                continue
            out[idx] = bool(v.get("on_topic"))
    except Exception:
        pass                               # 整批失败 → 全部走补判
    for j, s in enumerate(batch):
        if j in out:
            continue
        strag += 1                         # straggler：逐段补判
        try:
            d = LLM.complete_json(
                "relevance",
                f"研究对象：{label}\n\n段落标题："
                f"{s.get('heading_path') or '(无)'}\n\n这段在讲："
                f"{s.get('summary') or '(无)'}\n\n原文开头：\n"
                f"{_gate_blob(s)[:2000]}",
                system=prompts.RELEVANCE)
            out[j] = bool((d or {}).get("on_topic"))
        except Exception:
            out[j] = True                  # 判不了就放行（生产口径）
    return {"verdicts": out, "stragglers": strag, "of": len(batch)}


# -------------------------------------------------- 主流程

def run_group(LLM, segs, fn, label_of) -> dict:
    per_seg = {}
    details = []
    for b in _batches(segs):
        label = label_of(b[0])
        try:
            r = fn(LLM, b, label)
            details.append(r)
            if "verdicts" in r:
                for j, s in enumerate(b):
                    per_seg[s["id"]] = r["verdicts"].get(j)
            else:
                for j, s in enumerate(b):
                    g = (r["got"] or {}).get(j) or {}
                    per_seg[s["id"]] = g
        except Exception as e:
            details.append({"error": f"{type(e).__name__}: {e}"[:160],
                            "of": len(b)})
            for s in b:
                per_seg[s["id"]] = None
    return {"per_seg": per_seg, "batches": details}


def main():
    n_runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    os.environ.setdefault("RA_DB_PATH", EXP_DB)
    for ln in open(ROOT / ".env"):
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

    from research_agent.db.repo import Repo
    from research_agent.llm.provider import LLMClient, _CURRENT_SESSION
    repo = Repo(EXP_DB)
    repo.init_schema(); repo.run_migrations()
    sid = repo.create_session("merge-gate-ab", "q")
    tok = _CURRENT_SESSION.set(sid)
    LLM = LLMClient()

    segs = _sample(DB_V19, PER_DB) + _sample(DB_V18, PER_DB)
    print(f"样本 {len(segs)} 段（v19 {sum(1 for s in segs if s['db'].endswith('v19_batch1'))}"
          f" / v18 {sum(1 for s in segs if s['db'].endswith('v18_baseline'))}），"
          f"批大小 {BATCH}，n={n_runs}")
    db_on = sum(1 for s in segs if s["on_topic"])
    print(f"库里 on_topic=1: {db_on}  /  off: {len(segs)-db_on}")

    raw = {"segs": [{k: s[k] for k in
                     ("id", "db", "on_topic", "heading_path")} for s in segs],
           "runs": []}
    try:
        for run in range(n_runs):
            t0 = time.time()
            m = run_group(LLM, segs, _merged_batch, lambda s: s["label"])
            c = run_group(LLM, segs, _control_batch, lambda s: s["label"])

            def agree(verdicts):
                both = dif = miss = 0
                for s in segs:
                    v = verdicts.get(s["id"])
                    if v is None or (isinstance(v, dict)
                                     and v.get("relevant") is None):
                        miss += 1
                        continue
                    vv = v["relevant"] if isinstance(v, dict) else v
                    both += 1
                    if bool(vv) == bool(s["on_topic"]):
                        dif += 1
                return both, dif, miss

            mb, md, mmiss = agree(m["per_seg"])
            cb, cd, cmiss = agree(c["per_seg"])
            false_drop = sum(
                1 for s in segs
                if s["on_topic"]
                and isinstance(m["per_seg"].get(s["id"]), dict)
                and m["per_seg"][s["id"]].get("relevant") is False)
            m_summ = [m["per_seg"].get(s["id"]) for s in segs]
            nonempty = sum(1 for v in m_summ
                           if isinstance(v, dict)
                           and (v.get("summary") or "").strip())
            lens = [len(v["summary"]) for v in m_summ
                    if isinstance(v, dict) and (v.get("summary") or "").strip()]
            devs = sum(b.get("deviations", 0) for b in m["batches"])
            strags = sum(b.get("stragglers", 0) for b in c["batches"])
            row = {
                "run": run, "wall_s": round(time.time() - t0, 1),
                "merged_agree": f"{md}/{mb}", "merged_agree_pct": round(100 * md / max(mb, 1), 1),
                "merged_missing": mmiss,
                "control_agree": f"{cd}/{cb}", "control_agree_pct": round(100 * cd / max(cb, 1), 1),
                "control_missing": cmiss,
                "false_drop": false_drop,
                "false_drop_pct": round(100 * false_drop / max(db_on, 1), 1),
                "summary_nonempty": f"{nonempty}/{len(segs)}",
                "summary_len_avg": round(sum(lens) / max(len(lens), 1), 1),
                "schema_devs": devs, "control_stragglers": strags,
            }
            raw["runs"].append(row)
            print(json.dumps(row, ensure_ascii=False))

            per_seg_dump = {}
            for s in segs:
                mv = m["per_seg"].get(s["id"])
                cv = c["per_seg"].get(s["id"])
                per_seg_dump[s["id"]] = {
                    "db": s["on_topic"],
                    "merged": (mv or {}).get("relevant") if isinstance(mv, dict) else mv,
                    "control": cv,
                    "m_summary": (mv or {}).get("summary") if isinstance(mv, dict) else None}
            raw[f"run{run}_per_seg"] = per_seg_dump
    finally:
        _CURRENT_SESSION.reset(tok)

    OUT.joinpath("raw.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=1))
    print(f"\n原始数据 → {OUT}/raw.json")


if __name__ == "__main__":
    main()
