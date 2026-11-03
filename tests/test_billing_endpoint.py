"""GET /api/sessions/{sid}/billing 端点守门测试（离线）。

数据全部由种子库 SQL 独立计算核对，不走被测代码的同一路径。
用法：.venv/bin/python tests/test_billing_endpoint.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("MINIMAX_API_KEY", "stub-not-used")
os.environ["RA_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "t.db")

from research_agent.db.repo import get_repo                        # noqa: E402

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}"
          + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def seed(repo, sid: str, mode: str = "standard") -> None:
    # node 行（含 maintenance.* 与一条损坏 detail_json）
    rows = [
        (0, "node:probe", {"node": "probe", "ms": 1200}),
        (0, "node:retrieve", {"node": "retrieve", "ms": 5000}),
        (1, "node:retrieve", {"node": "retrieve", "ms": 3000}),
        (0, "node:write", {"node": "write", "ms": 9000}),
        (0, "node:finalize", {"node": "finalize", "ms": 800}),
        (0, "node:maintenance.prune", {"node": "maintenance.prune", "ms": 999}),
        (0, "node:mystery", {"node": "mystery", "ms": 7}),
    ]
    for rnd, kind, d in rows:
        repo.x("INSERT INTO run_trace(session_id, round, at, kind, detail_json) "
               "VALUES (?,?,?,?,?)",
               (sid, rnd, "2026-10-20T00:00:00.000+00:00", kind, json.dumps(d)))
    repo.x("INSERT INTO run_trace(session_id, round, at, kind, detail_json) "
           "VALUES (?,?,?,?,?)",
           (sid, 9, "2026-10-20T00:00:01.000+00:00", "node:broken", "{坏json"))

    # llm_call：ok×4 / retry×1 / error×1
    repo.log_llm_call(sid, "probe_fetch", "mA", 100, 50, latency_ms=400,
                      cache_read_tokens=80, cache_creation_tokens=20)
    repo.log_llm_call(sid, "summarize", "mA", 200, 100, latency_ms=600)
    repo.log_llm_call(sid, "write_section", "mB", 300, 150, latency_ms=900)
    repo.log_llm_call(sid, "write_section", "mB", 400, 200, latency_ms=700,
                      cache_read_tokens=60)
    repo.log_llm_call(sid, "summarize", "mA", 0, 0, status="retry")
    repo.log_llm_call(sid, "extract", "mA", 0, 0, status="error",
                      error="boom")


def main() -> int:
    repo = get_repo()
    sid = repo.create_session("t-bill", "q")
    seed(repo, sid)

    sid_fast = repo.create_session("t-fast", "q")
    repo.x("UPDATE session SET mode='fast' WHERE id=?", (sid_fast,))
    repo.x("INSERT INTO run_trace(session_id, round, at, kind, detail_json) "
           "VALUES (?,?,?,?,?)",
           (sid_fast, 0, "2026-10-20T00:00:00.000+00:00", "node:answer",
            json.dumps({"node": "answer", "ms": 500})))
    repo.log_llm_call(sid_fast, "fast_answer", "mC", 10, 20)

    sid_empty = repo.create_session("t-empty", "q")

    from fastapi.testclient import TestClient
    from research_agent.server import app
    c = TestClient(app)

    # ---------- 标准档 ----------
    d = c.get(f"/api/sessions/{sid}/billing").json()
    check("① 200+available", c.get(f"/api/sessions/{sid}/billing").status_code == 200
          and d["available"] is True)
    ph = {p["key"]: p for p in d["phases"]}
    # 独立手算：planning=probe 1200；research=retrieve 8000；
    # writing=write 9000；wrap=finalize 800；other=mystery 7+broken(损坏跳过,0)
    check("① planning ms=1200", ph["planning"]["ms"] == 1200)
    check("① research ms=8000 nodes合并runs=2",
          ph["research"]["ms"] == 8000
          and ph["research"]["nodes"][0] == {"node": "retrieve", "ms": 8000, "runs": 2})
    check("① writing ms=9000 label=写作",
          ph["writing"]["ms"] == 9000 and ph["writing"]["label"] == "写作")
    check("① wrap ms=800", ph["wrap"]["ms"] == 800)
    check("① other ms=7（mystery+broken损坏行跳过）",
          ph["other"]["ms"] == 7)
    check("① maintenance 被排除",
          not any(n["node"].startswith("maintenance")
                  for p in d["phases"] for n in p["nodes"]))
    check("① totals.ms=各 phase 之和",
          d["totals"]["ms"] == sum(p["ms"] for p in d["phases"]) == 19007)
    # token/calls：ok 4 次（100+200+300+400 in, 50+100+150+200 out）
    t = d["totals"]
    check("① calls=4 retry/error 不进", t["calls"] == 4)
    check("① retries=1 errors=1", t["retries"] == 1 and t["errors"] == 1)
    check("① tokens 1000/500/1500",
          t["input_tokens"] == 1000 and t["output_tokens"] == 500
          and t["total_tokens"] == 1500)
    check("① cache 140/20 hit=140/1160",
          t["cache_read_tokens"] == 140 and t["cache_creation_tokens"] == 20
          and t["cache_hit_ratio"] == round(140 / 1160, 4))
    check("① writing tokens=write_section 700/350",
          ph["writing"]["input_tokens"] == 700
          and ph["writing"]["output_tokens"] == 350
          and ph["writing"]["calls"] == 2)
    check("① by_stage write_section 聚合",
          any(x["stage"] == "write_section" and x["model"] == "mB"
              and x["input_tokens"] == 700 and x["calls"] == 2
              and x["latency_ms"] == 1600
              for x in d["by_stage"]), str(d["by_stage"]))

    # ---------- 快速档 ----------
    df = c.get(f"/api/sessions/{sid_fast}/billing").json()
    w = next((p for p in df["phases"] if p["key"] == "writing"), None)
    check("② fast writing label=整理作答",
          w is not None and w["label"] == "整理作答", str(w))

    # ---------- 空/404 ----------
    de = c.get(f"/api/sessions/{sid_empty}/billing").json()
    check("③ 无数据 available=false", de["available"] is False)
    check("④ 不存在 sid→404",
          c.get("/api/sessions/s_no_such/billing").status_code == 404)
    check("⑤ 损坏 detail_json 未 500", d is not None)

    print("\n" + ("全部通过" if not FAILED else f"失败 {len(FAILED)}: {FAILED}"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
