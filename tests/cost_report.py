"""一轮研究的成本与流程分析：钱花在哪、时间花在哪、流程有没有空转。

给下一轮的三件事提供依据：
  1. 成本优化——哪些环节的 token 占大头，缓存有没有命中
  2. 流程瘦身——哪些节点慢、哪些调用是白花的、流程绕了几圈
  3. 稳健性——重试了几次、哪些环节反复失败

数据来源
--------
- `llm_call`：每次 LLM 调用的 stage / token / 缓存 / 耗时
- `run_trace`：每个节点的墙钟耗时（kind = `node:<名字>`）、每轮 assess/retrieve 的决策
- `session`：总成本与状态

用法：
  .venv/bin/python tests/cost_report.py <db路径> [session_id]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DB = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ra_e2e_run.db"


def q(sql, *a):
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, a)]
    finally:
        c.close()


def main() -> None:
    sid = sys.argv[2] if len(sys.argv) > 2 else None
    if not sid:
        rows = q("SELECT id FROM session ORDER BY created_at DESC LIMIT 1")
        if not rows:
            sys.exit(f"{DB} 里没有 session")
        sid = rows[0]["id"]
    sess = q("SELECT * FROM session WHERE id=?", sid)[0]

    print("=" * 78)
    print(f"session {sid}")
    print(f"问题：{sess['question']}")
    print("=" * 78)

    # ---------- 1. 节点墙钟时间 ----------
    nodes = q("SELECT round, kind, detail_json FROM run_trace "
              "WHERE session_id=? AND kind LIKE 'node:%' ORDER BY rowid", sid)
    ntime = defaultdict(float)
    ncnt = defaultdict(int)
    for n in nodes:
        d = json.loads(n["detail_json"] or "{}")
        ntime[d.get("node", n["kind"][5:])] += d.get("ms", 0) / 1000
        ncnt[d.get("node", n["kind"][5:])] += 1
    total_node = sum(ntime.values())

    # ---------- 2. 每阶段的 LLM 成本 ----------
    stages = q(
        "SELECT stage, COUNT(*) n, SUM(input_tokens) it, SUM(output_tokens) ot, "
        "       SUM(cache_read_tokens) cr, SUM(cache_creation_tokens) cc, "
        "       ROUND(AVG(latency_ms)/1000.0,1) sec, "
        "       ROUND(SUM(latency_ms)/1000.0,1) tot_sec "
        "FROM llm_call WHERE session_id=? GROUP BY stage ORDER BY it DESC", sid)
    tot_in = sum(s["it"] or 0 for s in stages)
    tot_out = sum(s["ot"] or 0 for s in stages)
    tot_cr = sum(s["cr"] or 0 for s in stages)
    tot_llm = sum(s["tot_sec"] or 0 for s in stages)

    print(f"\n【一】节点墙钟时间（合计 {total_node:.0f} 秒）")
    print(f"  {'节点':<12}{'次数':>5}{'总秒':>9}{'占比':>8}{'均秒':>8}")
    for k, v in sorted(ntime.items(), key=lambda x: -x[1]):
        print(f"  {k:<12}{ncnt[k]:>5}{v:>9.0f}{v/max(total_node,1):>8.0%}"
              f"{v/max(ncnt[k],1):>8.1f}")
    print(f"  {'—':<12}{'':>5}{'LLM 调用合计':>9}{tot_llm:>9.0f}")
    print(f"  说明：LLM 调用是并发的，总和会大于墙钟时间，正常。")

    print(f"\n【二】LLM 成本（输入 {tot_in:,} / 输出 {tot_out:,} / "
          f"缓存读 {tot_cr:,}）")
    print(f"  {'阶段':<16}{'次数':>5}{'输入':>10}{'输出':>9}{'缓存读':>10}"
          f"{'均秒':>7}{'输入占比':>9}")
    for s in stages:
        share = (s["it"] or 0) / max(tot_in, 1)
        print(f"  {s['stage']:<16}{s['n']:>5}{(s['it'] or 0):>10,}"
              f"{(s['ot'] or 0):>9,}{(s['cr'] or 0):>10,}{s['sec']:>7}"
              f"{share:>9.0%}")
    hit = tot_cr / max(tot_in + tot_cr, 1)
    print(f"\n  缓存命中率（缓存读 / 总输入）：{hit:.1%}")
    print(f"  session.cost_tokens = {sess['cost_tokens']:,}"
          f"（= 输入+输出，与上表合计 {tot_in+tot_out:,} 对照）")

    print(f"\n【三】重试与失败（稳健性依据）")
    warns = q("SELECT stage, COUNT(*) n FROM llm_call WHERE session_id=? "
              "AND output_tokens=0 GROUP BY stage", sid)
    slow = q("SELECT stage, COUNT(*) n, ROUND(MAX(latency_ms)/1000.0,0) mx "
             "FROM llm_call WHERE session_id=? AND latency_ms>60000 "
             "GROUP BY stage ORDER BY mx DESC", sid)
    print(f"  空输出（触发过预算翻倍重试）的阶段："
          f"{[(w['stage'], w['n']) for w in warns] or '无'}")
    print(f"  单次超过 60 秒的阶段：")
    for s in slow:
        print(f"    {s['stage']:<16}{s['n']:>3} 次，最长 {s['mx']} 秒")
    # 60 秒这条线现在只对**非流式**环节有意义：xj189 前面 nginx 有
    # proxy_read_timeout 60s，而开了流式的环节靠持续推数据块绕开了它
    # （实测 assess 跑过 86.8 秒）。所以这里必须标出哪些是流式的，
    # 否则会误报。流式状态从配置读，不从 DB 读（DB 里没有这个字段）。
    from research_agent.llm.provider import ModelRegistry
    streaming = {s for s, c in
                 ((s, ModelRegistry().stage(s)) for s in
                  (r["stage"] for r in q(
                      "SELECT DISTINCT stage FROM llm_call WHERE session_id=?", sid)))
                 if c.stream}
    if streaming:
        print(f"  ⚠️ 其中开了流式、不受 60 秒墙限制的：{sorted(streaming)}")
    print(f"  ⚠️ **失败的调用（504/429）在 DB 里没有任何记录**——"
          f"它们是异常不是记账行，只能从 stdout 的 ⚠️ 行数统计。")

    print(f"\n【四】流程走了几圈")
    assess = [n for n in q(
        "SELECT round, detail_json FROM run_trace WHERE session_id=? "
        "AND kind='assess' ORDER BY rowid", sid)]
    retr = [n for n in q(
        "SELECT round, detail_json FROM run_trace WHERE session_id=? "
        "AND kind='retrieve' ORDER BY rowid", sid)]
    print(f"  检索轮数 {len(retr)}，assess 调用 {len(assess)} 次")
    for a in assess:
        d = json.loads(a["detail_json"] or "{}")
        qs = d.get("queries") or {}
        print(f"    第 {a['round']} 轮：段落 {d.get('segments_total')}、"
              f"新增 {d.get('new_segments')}、streak {d.get('streak')}、"
              f"缺口 {len(d.get('gaps') or [])} 个、"
              f"下一轮查询 {len(qs.get('zh') or [])} 条")
        print(f"      停止原因预测：{d.get('stop_would_be') or '（会继续）'}")
    for r in retr:
        d = json.loads(r["detail_json"] or "{}")
        rep = d.get("repeat_queries") or []
        print(f"    第 {r['round']} 轮检索：命中 {d.get('hits')}、"
              f"接纳 {d.get('accepted')}、累计已抓 {d.get('fetched_total')}"
              + (f"、🚨与历史重复 {len(rep)} 条" if rep else ""))

    print(f"\n【五】产出")
    n_src = q("SELECT COUNT(*) c FROM source WHERE session_id=?", sid)[0]["c"]
    n_ch = q("SELECT COUNT(*) c FROM chunk WHERE session_id=?", sid)[0]["c"]
    n_on = q("SELECT COUNT(*) c FROM chunk WHERE session_id=? AND on_topic=1",
             sid)[0]["c"]
    n_ns = q("SELECT COUNT(*) c FROM chunk WHERE session_id=? "
             "AND (summary IS NULL OR summary='')", sid)[0]["c"]
    rep = q("SELECT LENGTH(markdown) L FROM report WHERE session_id=?", sid)
    print(f"  来源 {n_src}、段落 {n_ch}（过闸门 {n_on}、无概括 {n_ns}）")
    if rep:
        print(f"  报告 {rep[0]['L']:,} 字")
    print(f"  墙钟总时长 ≈ {total_node:.0f} 秒 "
          f"（{total_node/60:.1f} 分钟），LLM 调用累计 {tot_llm:.0f} 秒")

    # ---------- 6. 对照卡：两库 diff 这一段就能看出改了什么 ----------
    # 每项一行 key=value，顺序固定。成本优化的 before/after 只看这里，
    # 不用在全文里捞数字（阶段一改动 0，2026-10-18）。
    methods = q("SELECT COALESCE(gate_method,'?') m, COUNT(*) n "
                "FROM chunk WHERE session_id=? GROUP BY m", sid)
    gm = {r["m"]: r["n"] for r in methods}
    errs = q("SELECT COUNT(*) n FROM llm_call WHERE session_id=? "
             "AND status='error'", sid)[0]["n"]
    calls = q("SELECT COUNT(*) n FROM llm_call WHERE session_id=?", sid)[0]["n"]
    st = {s["stage"]: s for s in stages}
    print(f"\n【六】对照卡（before/after 只 diff 这段）")
    kpi = [
        ("wall_clock_s",      f"{total_node:.0f}"),
        ("llm_calls",         calls),
        ("llm_errors",        errs),
        ("tokens_in",         tot_in),
        ("tokens_out",        tot_out),
        ("cache_read",        tot_cr),
        ("cache_hit_pct",     f"{hit:.1%}"),
        ("parallelism",       f"{tot_llm/max(total_node,1):.2f}"),
        ("retrieve_rounds",   len(retr)),
        ("gate_rule_free",    gm.get("rule", 0) + gm.get("rule_weak", 0)),
        ("gate_llm_judged",   gm.get("llm", 0) + gm.get("fallback", 0)),
        ("gate_degraded",     sum(v for k, v in gm.items()
                                  if "degraded" in k)),
        ("gate_off_topic",    n_ch - n_on),
        ("chunks",            n_ch),
        ("no_summary",        n_ns),
        ("report_chars",      rep[0]["L"] if rep else 0),
        ("relevance_calls",   st.get("relevance", {}).get("n", 0)),
        ("relevance_in",      st.get("relevance", {}).get("it", 0) or 0),
        ("summarize_calls",   st.get("summarize", {}).get("n", 0)),
        ("summarize_in",      st.get("summarize", {}).get("it", 0) or 0),
        ("write_section_in",  st.get("write_section", {}).get("it", 0) or 0),
        ("write_section_out", st.get("write_section", {}).get("ot", 0) or 0),
    ]
    for k, v in kpi:
        print(f"  {k}={v}")


if __name__ == "__main__":
    main()