"""outline / probe_outline 的 M3 vs M3.1-Flash low A/B（2026-10-25 时间优化）。

变体：
  A = 现行配置（defaults → M3）
  B = `provider: minimax31` + reasoning_effort=low + thinking_effort=low
      （outline 的预算同步给 16384 一次给够，guard 上限同值）

输入是 v18 基线的真实载荷：
  outline       ← session.probe_json 里的 signals（/tmp/outline_input.json）
  probe_outline ← checkpoints.db probe:* 快照的 deep_pages（/tmp/probe_deep_pages.json）

每格 n=3 串行（不并发：要的是干净的单调用耗时与护栏行为）。
用量记账走 session_scope + 假 session 行（llm_call 有外键）。

判定指标按接收判据打：JSON/工具成功率、节数、grounded_in 通过率、
queries zh/en、subtopics/entities/jargon/controversies、耗时中位。

用法：
  .venv/bin/python tests/experiments/exp_outline_ab.py <db副本> [out_dir]
环境变量：AB_N=3  AB_ONLY=outline|probe_outline
"""

from __future__ import annotations

import copy
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
DB = sys.argv[1]
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/exp_outline_ab")
OUT.mkdir(parents=True, exist_ok=True)
os.environ["RA_DB_PATH"] = DB
for _line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        k, v = _line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
N_PER = int(os.environ.get("AB_N", "3"))
ONLY = os.environ.get("AB_ONLY", "")

from research_agent.db.repo import get_repo          # noqa: E402
from research_agent.graph import nodes as N          # noqa: E402
from research_agent.graph import prompts             # noqa: E402
from research_agent.graph.subgraphs import probe as PB  # noqa: E402
from research_agent.llm.provider import session_scope  # noqa: E402

LLM = N.LLM
STAGES = ("outline", "probe_outline")
ORIG = {s: copy.deepcopy(LLM.registry.raw["stages"][s]) for s in STAGES}


def set_variant(variant: str) -> None:
    reg = LLM.registry
    for s in STAGES:
        base = copy.deepcopy(ORIG[s])
        if variant == "B":
            base.update({"provider": "minimax31",
                         "reasoning_effort": "low",
                         "thinking_effort": "low",
                         "max_output_tokens": 16384})
            base["guard"] = {**(base.get("guard") or {}),
                             "max_output_tokens": 16384}
        reg.raw["stages"][s] = base
        reg._cache.pop(s, None)
    for s in STAGES:
        cfg = reg.stage(s)      # validate 当场跑，配置不合法立即炸
        print(f"  [{variant}] {s}: {cfg.provider}/{cfg.model} "
              f"effort={cfg.reasoning_effort} think={cfg.thinking_effort} "
              f"budget={cfg.max_output_tokens} stream={cfg.stream}",
              flush=True)


def _llm_rows(sid: str) -> list:
    return get_repo().q(
        "SELECT stage,status,latency_ms,ttfb_ms,wait_ms,input_tokens,"
        "output_tokens,error,started_at FROM llm_call WHERE session_id=?"
        " ORDER BY created_at", (sid,))


# --------------------------------------------------------------- 载荷构造

def outline_prompt() -> str:
    d = json.loads(Path("/tmp/outline_input.json").read_text(encoding="utf-8"))
    return (f"研究问题：{d['question']}\n"
            + (f"用户已澄清指向：{d['user_answer']}\n"
               if d.get("user_answer") else "")
            + "\n探针阶段实际观察到的领域轮廓：\n"
            + json.dumps(d["signals"], ensure_ascii=False)[:12000])


def probe_prompt() -> str:
    d = json.loads(Path("/tmp/probe_deep_pages.json").read_text(encoding="utf-8"))
    pages = d["deep_pages"]
    blob = "\n\n".join(
        f"### 来源{i + 1}：{p['title']}\nURL：{p['url']}\n\n{p['text'][:6000]}"
        for i, p in enumerate(pages))
    src = f"\n读到的正文：\n{blob[:20000]}"
    return (f"研究对象：{d['question']}\n"
            + (f"用户已澄清指向：{d['user_choice']}\n"
               if d.get("user_choice") else "")
            + src
            + "\n\n读完后**调用 `submit_outline` 工具**提交结论，"
              "结论只放在工具参数里，不要在对话文本里写 JSON。")


# --------------------------------------------------------------- 业务校验

def outline_score(plan: dict) -> dict:
    """与 node_outline 同款判据：grounded_in 通过率 + queries 条数。"""
    secs = plan.get("sections") or []
    ok = sum(1 for s in secs
             if isinstance(s.get("grounded_in"), list) and s["grounded_in"])
    q = plan.get("queries") or {}
    return {"sections": len(secs), "grounded_ok": ok,
            "q_zh": len(q.get("zh") or []), "q_en": len(q.get("en") or [])}


def probe_score(data: dict) -> dict:
    return {"subtopics": len(data.get("subtopics") or []),
            "entities": len(data.get("entities") or []),
            "jargon": len(data.get("jargon") or []),
            "controversies": len(data.get("controversies") or []),
            "quality": data.get("info_quality")}


_sid_n = [0]


def _new_sid(variant: str, stage: str, i: int) -> str:
    _sid_n[0] += 1
    # create_session 的第一参是 title、id 由它生成 s_xxx——
    # 必须用返回值，否则 llm_call 的 session 外键会让行被静默丢。
    return get_repo().create_session(f"ab_{variant}_{stage}_{i}",
                                     f"ab {stage}")


def trial_outline(i: int) -> dict:
    sid = _new_sid("X", "outline", i)
    t0 = time.time()
    err = ""
    try:
        with session_scope(sid):
            plan = LLM.complete_json("outline", outline_prompt(),
                                     system=prompts.PLAN_OUTLINE)
        sc = outline_score(plan)
    except Exception as e:
        plan, sc, err = None, {}, f"{type(e).__name__}: {e}"[:200]
    wall = time.time() - t0
    rows = _llm_rows(sid)
    return {"wall_s": round(wall, 1), "err": err, "score": sc, "rows": rows}


def trial_probe(i: int) -> dict:
    sid = _new_sid("X", "probe_outline", i)
    t0 = time.time()
    err = ""
    raw = None
    try:
        with session_scope(sid):
            raw = LLM.complete_json_forced(
                "probe_outline", probe_prompt(), "submit_outline",
                PB.OUTLINE_SCHEMA, system=prompts.PROBE_OUTLINE)
        data = PB._normalize_outline_input(raw) or {}
        sc = probe_score(data)
    except Exception as e:
        sc, err = {}, f"{type(e).__name__}: {e}"[:200]
    wall = time.time() - t0
    rows = _llm_rows(sid)
    return {"wall_s": round(wall, 1), "err": err, "score": sc, "rows": rows}


def run_variant(variant: str) -> dict:
    set_variant(variant)
    out: dict = {"outline": [], "probe_outline": []}
    if ONLY in ("", "outline"):
        for i in range(N_PER):
            r = trial_outline(i)
            out["outline"].append(r)
            ok = [x for x in r["rows"] if x["status"] == "ok"]
            print(f"  [{variant}] outline#{i}: wall={r['wall_s']}s "
                  f"score={r['score']} err={r['err']} "
                  f"rows={[(x['status'], x['latency_ms'], x['error'] or '')[:3] for x in r['rows']]}",
                  flush=True)
    if ONLY in ("", "probe_outline"):
        for i in range(N_PER):
            r = trial_probe(i)
            out["probe_outline"].append(r)
            print(f"  [{variant}] probe_outline#{i}: wall={r['wall_s']}s "
                  f"score={r['score']} err={r['err']} "
                  f"rows={[(x['status'], x['latency_ms'], x['error'] or '')[:3] for x in r['rows']]}",
                  flush=True)
    return out


def main() -> None:
    print(f"DB={DB} OUT={OUT} n={N_PER}", flush=True)
    res = {}
    for variant in ("A", "B"):
        print(f"\n===== variant {variant} =====", flush=True)
        res[variant] = run_variant(variant)

    fp = OUT / "raw.json"
    fp.write_text(json.dumps(res, ensure_ascii=False, default=str, indent=1),
                  encoding="utf-8")
    print(f"\n原始数据 → {fp}", flush=True)

    # ---- 摘要 ----
    def summ(stage: str):
        line = [f"\n--- {stage} ---"]
        for v in ("A", "B"):
            ts = [t["wall_s"] for t in res[v][stage]]
            errs = [t["err"] for t in res[v][stage] if t["err"]]
            retry = sum(1 for t in res[v][stage]
                        for r in t["rows"] if r["status"] == "retry")
            ok_toks = [r["output_tokens"] for t in res[v][stage]
                       for r in t["rows"] if r["status"] == "ok"]
            scores = [t["score"] for t in res[v][stage]]
            line.append(
                f"[{v}] n={len(ts)} wall={ts} "
                f"med={statistics.median(ts) if ts else '-'}s "
                f"errors={errs} retry_rows={retry} out_tok={ok_toks} "
                f"scores={scores}")
        print("\n".join(line), flush=True)

    for s in STAGES:
        summ(s)


if __name__ == "__main__":
    main()
