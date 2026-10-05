"""诊断 write_plan 为什么连续三次连接中断。

已确认的现象：M3.1-Flash-Preview + effort=high，输入约 3 万 token，
连续 3 次 `Server disconnected without sending a response`，
三次用的是**完全相同的请求**——传输重试不换任何参数，所以重试是必然无效的。

这个脚本用**完全相同的 plan 输入**跑三档 effort，测：
  · 是否能完成
  · 思考多长、耗时多久
  · 输出预算够不够装下「思考 + 正文」

注意这与 write_budget.py 是同一类错误的第二次发生：
上一次是猜错了参数名，这次是预算和思考量不匹配。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from research_agent.graph import prompts  # noqa: E402
from research_agent.graph.subgraphs import write as W  # noqa: E402

KEY = os.environ["MINIMAX_API_KEY"]
URL = "https://api.minimaxi.com/anthropic/v1/messages"
M31 = "MiniMax-M3.1-Flash-Preview"


def build_payload() -> dict:
    """与 node_plan 完全一致的输入。"""
    c = sqlite3.connect("/tmp/ra_e2e7.db")
    c.row_factory = sqlite3.Row
    plan = json.loads(c.execute("SELECT outline_json FROM session LIMIT 1")
                      .fetchone()[0] or "{}")
    outline = plan.get("sections") or []
    rows = c.execute(
        "SELECT c.content, s.url, s.title, s.domain, s.source_type, s.source_date, "
        "       s.char_count FROM chunk c LEFT JOIN source s ON s.id=c.source_id "
        "WHERE c.session_id IS NOT NULL LIMIT 400").fetchall()
    sources, seen = [], set()
    for r in rows:
        if r["url"] and r["url"] not in seen:
            seen.add(r["url"])
            sources.append(dict(r))
    src_lines = [f"[S{i+1}] {s['title'] or s['domain']} | {s['domain']} "
                 f"| {s['source_type']} | {s['source_date'] or '日期未知'} "
                 f"| 正文 {s['char_count'] or 0} 字"
                 for i, s in enumerate(sources)]
    url2n = {s["url"]: i + 1 for i, s in enumerate(sources)}
    import re
    ev = []
    for r in c.execute(
            "SELECT c.content, s.url FROM chunk c LEFT JOIN source s ON s.id=c.source_id "
            "WHERE c.session_id IS NOT NULL ORDER BY c.ingested_at LIMIT 260"):
        t = (r["content"] or "").strip()
        if len(t) < 40:
            continue
        for s in [x for x in re.split(r"(?<=[。！？])", t) if len(x) > 12][:2]:
            n = url2n.get(r["url"])
            if n:
                ev.append({"i": len(ev), "text": s.strip()[:300], "entities": [],
                           "source": n, "nature": "unknown",
                           "confidence": "medium", "date": None})
    brief = {
        "研究问题": "固态电池的产业化进展，以及各家技术路线的分歧点",
        "已批准的框架": [{"id": s.get("id"), "title": s.get("title"),
                        "question": s.get("question"), "kind": s.get("kind")}
                       for s in outline],
        "探针轮廓": {},
        "来源清单": src_lines,
        "断言摘要": [{"i": e["i"], "摘要": (e.get("text") or "")[:W.CLAIM_SUMMARY_CHARS],
                     "来源": e.get("source"), "性质": e.get("nature"),
                     "置信": e.get("confidence"), "日期": e.get("date")} for e in ev],
    }
    return {"brief": brief, "n_outline": len(outline),
            "n_sources": len(sources), "n_claims": len(ev)}


B = build_payload()
body = json.dumps(B["brief"], ensure_ascii=False, indent=1)[:48000]
print(f"大纲 {B['n_outline']} 节 | 来源 {B['n_sources']} | 断言 {B['n_claims']}")
print(f"plan 输入 {len(body)} 字符\n")

CASES = [
    ("high（当前配置）", "high", 16384),
    ("medium", "medium", 16384),
    ("low", "low", 16384),
    ("high + 预算翻倍 32768", "high", 32768),
]

for label, effort, mt in CASES:
    payload = {
        "model": M31, "max_tokens": mt, "temperature": 0.0,
        "system": prompts.WRITE_PLAN,
        "messages": [{"role": "user", "content":
                      body + "\n\n只输出 JSON，不要任何解释文字或代码围栏。"}],
    }
    if effort:
        payload["output_config"] = {"effort": effort}
    t0 = time.time()
    try:
        r = httpx.post(URL, headers={"x-api-key": KEY,
                                    "Authorization": f"Bearer {KEY}",
                                    "Content-Type": "application/json",
                                    "anthropic-version": "2023-06-01"},
                       json=payload, timeout=300.0)
        dt = round(time.time() - t0, 1)
        if r.status_code != 200:
            print(f"{label:<24} ✗ HTTP {r.status_code} {r.text[:100]}")
            continue
        blocks = r.json().get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        think = "".join(b.get("thinking", "") for b in blocks if b.get("type") == "thinking")
        u = r.json().get("usage") or {}
        try:
            d = W.LLM.__class__  # noqa: F841
            from research_agent.llm.provider import _parse_json
            ok = bool(_parse_json(text)) if text.strip() else False
        except Exception:
            ok = False
        print(f"{label:<24} {'✓' if text.strip() else '✗空'} {dt:6.1f}s "
              f"思考 {len(think):6d} 正文 {len(text):5d} "
              f"out_tok {u.get('output_tokens')} 预算 {mt} "
              f"可解析={'是' if ok else '否'}")
        if text.strip() and not ok:
            print(f"{'':26}开头: {text[:120]!r}")
    except Exception as e:
        print(f"{label:<24} ✗ {type(e).__name__}: {str(e)[:70]}  ({round(time.time()-t0,1)}s)")
