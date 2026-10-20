"""`node_finalize` 的 run_health 事件守门测试（2026-10-20）。

口径：
  · llm_by_stage——`status='retry'` 行单列 retries/retry_wait_s_total
    （wait 从固定格式 `| wait Ns` 解析），不计入 calls_ok/errors；
  · unsummarized_on_topic_chunks——on_topic=1 且 summary 空；
  · gate_degraded_chunks——gate_method 含 'degraded'；
  · unjudged_sources——source_nature 空（与 extract 事件同口径）；
  · failed_tasks——state.events 里最后一条 kind='write' 的 failed；
  · run_trace upsert → 重入两次不重复落库。

用法：
    .venv/bin/python tests/test_run_health.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("MINIMAX_API_KEY", "stub-not-used")
os.environ["RA_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "t.db")

from research_agent.db.repo import get_repo            # noqa: E402
from research_agent.graph import nodes as N           # noqa: E402

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}"
          + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


repo = get_repo()
sid = repo.create_session("t", "q")


def _seed():
    # llm_call：ok×1、retry×2（wait 10+20s）、error×1，跨两个 stage
    repo.log_llm_call(sid, "summarize", "minimax/M3", 100, 50, latency_ms=1000)
    repo.log_llm_call(
        sid, "summarize", "minimax/M3", 0, 0, latency_ms=500, status="retry",
        error="MiniMax 529 overloaded | attempt 1/5 | wait 10s")
    repo.log_llm_call(
        sid, "summarize", "minimax/M3", 0, 0, latency_ms=500, status="retry",
        error="MiniMax 529 overloaded | attempt 2/5 | wait 20s")
    repo.log_llm_call(sid, "relevance", "minimax/M3", 0, 0, latency_ms=300,
                      status="error", error="RuntimeError: MiniMax 529: x")
    # chunk：on_topic=1 共 3 段（1 段无概括），on_topic=0 一段不算
    repo.x("INSERT INTO source (id,session_id,url,url_normalized,domain,"
           "source_type,fetched_at,on_topic) VALUES (?,?,?,?,?,?,?,?)",
           (sid, sid, "u://a", "u://a/", "a.com", "media", "now", 1))
    repo.x("INSERT INTO source (id,session_id,url,url_normalized,domain,"
           "source_type,fetched_at,on_topic,source_nature) "
           "VALUES (?,?,?,?,?,?,?,?,?)",
           ("src2", sid, "u://b", "u://b/", "b.com", "media", "now", 1,
            "官方"))
    repo.x("INSERT INTO chunk (id,session_id,source_id,origin,heading_path,"
           "content,summary,token_count,status,ingested_at,on_topic,"
           "gate_method) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
           ("c1", sid, sid, "web", "h", "正文", "有概括", 10, "pending",
            "now", 1, "llm"))
    repo.x("INSERT INTO chunk (id,session_id,source_id,origin,heading_path,"
           "content,summary,token_count,status,ingested_at,on_topic,"
           "gate_method) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
           ("c2", sid, "src2", "web", "h", "正文", "", 10, "pending",
            "now", 1, "llm_degraded"))
    repo.x("INSERT INTO chunk (id,session_id,source_id,origin,heading_path,"
           "content,summary,token_count,status,ingested_at,on_topic,"
           "gate_method) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
           ("c3", sid, "src2", "web", "h", "正文", "有", 10, "pending",
            "now", 0, "rule"))


def t1_fields():
    _seed()
    # report 表有一行 → 走完整 finalize 路径；先插 report
    repo.save_report(sid, "# 报告\n")
    st = {"session_id": sid, "events": [
        {"kind": "write", "failed": 2},
        {"kind": "write", "failed": 0},   # 最后一条为准（revise 清完）
    ]}
    out = N.node_finalize(st)
    ev = next(e for e in out["events"] if e.get("kind") == "run_health")
    st_sum = ev["llm_by_stage"]["summarize"]
    check("① summarize：calls_ok=1 retries=2 errors=0 wait=30",
          st_sum["calls_ok"] == 1 and st_sum["retries"] == 2
          and st_sum["errors"] == 0
          and st_sum["retry_wait_s_total"] == 30.0, str(st_sum))
    check("① relevance：errors=1",
          ev["llm_by_stage"]["relevance"]["errors"] == 1)
    u = ev["unsummarized_on_topic_chunks"]
    check("① 无概括段 1/2（on_topic=1 口径，source 数=1）",
          u == {"missing": 1, "total": 2, "sources": 1}, str(u))
    check("① 闸门降级 1（llm_degraded）",
          ev["gate_degraded_chunks"] == 1)
    check("① 未判来源 1（source_nature 空，'官方' 不算）",
          ev["unjudged_sources"] == 1)
    check("① failed_tasks 取最后一条 write 事件 =0",
          ev["failed_tasks"] == 0)
    return ev


def t2_reentry(ev1):
    out = N.node_finalize({"session_id": sid,
                           "events": [{"kind": "write", "failed": 0}]})
    ev2 = next(e for e in out["events"] if e.get("kind") == "run_health")
    check("② 重入字段一致",
          ev2["llm_by_stage"] == ev1["llm_by_stage"]
          and ev2["unsummarized_on_topic_chunks"]
          == ev1["unsummarized_on_topic_chunks"])
    rows = repo.q("SELECT COUNT(*) n FROM run_trace "
                  "WHERE session_id=? AND kind='run_health'", (sid,))
    check("② run_trace 不重复落库（仍 1 行）", rows[0]["n"] == 1,
          str(rows[0]["n"]))


def t3_clean():
    sid2 = repo.create_session("t2", "q")
    repo.log_llm_call(sid2, "relevance", "minimax/M3", 1, 1)
    ev = N._run_health(repo, sid2, {"session_id": sid2, "events": []})
    u = ev["unsummarized_on_topic_chunks"]
    check("③ 干净会话：无概括 0/0、降级 0、未判 0、失败任务 0",
          u["missing"] == 0 and ev["gate_degraded_chunks"] == 0
          and ev["unjudged_sources"] == 0 and ev["failed_tasks"] == 0)


def main() -> int:
    print("=== run_health 运行健康度 ===")
    ev = t1_fields()
    t2_reentry(ev)
    t3_clean()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败：")
        for f in FAILED:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
