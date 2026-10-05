"""预检：MiniMax 在 Anthropic 端点下能否稳定吐出可解析的 JSON。

这是整个换模型方案最大的未知。Anthropic 协议没有 `response_format`，
严格 JSON 全靠提示词约束——之前用 space-bunny-free 时，探针①就因为
一次尾逗号把整场研究带崩了。所以换厂商前必须先用真实提示词验一遍。

对每个环节跑一次，模型与 effort 按拟定的档位，检查：
  1. HTTP 成功
  2. 解析成功（含 JSON 修复兜底）
  3. 关键字段齐全
  4. 思考与正文各占多少
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
        k, v = _l.split("/", 1)[0].split("=", 1) if False else _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from research_agent.graph import prompts                              # noqa: E402
from research_agent.llm.provider import _parse_json                   # noqa: E402

KEY = os.environ["MINIMAX_API_KEY"]
URL = "https://api.minimaxi.com/anthropic/v1/messages"
DB = "/tmp/ra_e2e7.db"

QUESTION = "固态电池的产业化进展，以及各家技术路线的分歧点"

M3 = "MiniMax-M3"
M31 = "MiniMax-M3.1-Flash-Preview"


def load():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    chunks = [dict(r) for r in c.execute(
        "SELECT c.id,c.content,c.heading_path,s.title,s.url,s.domain,s.source_type,"
        "s.source_date FROM chunk c LEFT JOIN source s ON s.id=c.source_id "
        "WHERE c.session_id IS NOT NULL LIMIT 60")]
    raw = c.execute("SELECT outline_json FROM session LIMIT 1").fetchone()[0] or "{}"
    plan = json.loads(raw)
    return chunks, (plan.get("sections") if isinstance(plan, dict) else plan) or []


CHUNKS, OUTLINE = load()
SIGNALS = {
    "subtopics": [{"name": "固态电解质材料路线", "what": "硫化物/氧化物/聚合物差异"}],
    "entities": [{"name": "宁德时代", "type": "org"}, {"name": "丰田", "type": "org"}],
    "jargon": [{"term": "固固界面", "en_equivalent": "solid-solid interface",
                "meaning": "无液体的接触界面"}],
    "controversies": [{"topic": "硫化物安全性", "position_a": "更安全",
                       "position_b": "200℃以上剧烈放热"}],
    "common_questions": ["产业化到哪一步了"],
    "one_line": "固态电池尚处中试阶段",
    "info_quality": "medium",
}
EVIDENCE = [{"text": (c["content"] or "")[:220], "entities": [],
             "source": (i % 8) + 1, "nature": "media",
             "confidence": "medium", "date": c["source_date"] or ""}
            for i, c in enumerate(CHUNKS[:16])]

TASKS = [
    ("probe_light  T1", M31, "low", 8192, prompts.PROBE_LIGHT,
     "研究对象：固态电池的产业化进展\n\n搜索结果（只有标题和摘要）：\n" +
     "\n".join(f"[{i}] {c['title']}\n    {(c['content'] or '')[:200]}\n    {c['url']}"
               for i, c in enumerate(CHUNKS[:30])),
     ("depth", "ambiguity")),

    ("probe_outline T2", M31, "high", 16384, prompts.PROBE_OUTLINE,
     f"研究对象：{QUESTION}\n\n读到的正文：\n" + "\n\n".join(
         f"### 来源{i+1}：{c['title']}\nURL：{c['url']}\n\n{(c['content'] or '')[:3000]}"
         for i, c in enumerate(CHUNKS[:3]))[:20000],
     ("subtopics", "entities")),

    ("outline      T2", M31, "high", 16384, prompts.PLAN_OUTLINE,
     f"研究问题：{QUESTION}\n\n探针阶段实际观察到的领域轮廓：\n"
     + json.dumps(SIGNALS, ensure_ascii=False)[:12000],
     ("sections", "queries")),

    ("extract      T1", M31, "low", 8192, prompts.EXTRACT_BATCH,
     f"以下是 3 个独立来源的正文：\n\n" + "\n\n".join(
         f"### [{i}] {c['title']}\nURL：{c['url']}\n\n{(c['content'] or '')[:4000]}"
         for i, c in enumerate(CHUNKS[:3]))[:40000],
     ("items",)),

    ("relevance    T0", M3, None, 4096, prompts.RELEVANCE,
     f"研究对象：{QUESTION}\n\n候选来源标题：{CHUNKS[0]['title']}\n\n"
     f"从这篇材料里抽出的事实：\n{(CHUNKS[0]['content'] or '')[:4000]}",
     ("on_topic", "reason")),

    ("assess       T1", M31, "low", 8192, prompts.ASSESS,
     json.dumps({
         "sections": [{"id": s.get("id"), "title": s.get("title"),
                       "question": s.get("question")} for s in OUTLINE[:6]],
         "claims": [{"t": e["text"][:120], "e": []} for e in EVIDENCE[:40]],
     }, ensure_ascii=False)[:40000],
     ("has_enough", "gaps")),

    ("assign       T1", M31, "low", 8192,
     "你把一批事实断言分派到研究小节。\n**严格输出 JSON**："
     '{"assign": {"sec1": [0,2,5], "sec2": [1,3]}}\n'
     "每条断言可分派到多个小节，也可以一条都不分派。"
     "只输出 小节编号 到 下标数组 的映射，不要任何解释。",
     json.dumps({
         "sections": [{"id": s.get("id"), "title": s.get("title")} for s in OUTLINE[:6]],
         "evidence": [{"i": e["source"], "t": e["text"][:120]} for e in EVIDENCE],
     }, ensure_ascii=False)[:30000],
     ("assign",)),

    ("summarize    T0", M3, None, 8192, prompts.SUMMARIZE,
     f"以下是 6 个报告段落，请为每段写一句话概括：\n\n" + "\n\n".join(
         f"[{i}] {c['heading_path'] or ''}\n{(c['content'] or '')[:3000]}"
         for i, c in enumerate(CHUNKS[:6])),
     ("summaries",)),
]

print(f"{'环节':<20} {'档':<5} {'HTTP':<5} {'解析':<5} {'字段':<5} {'思考':>7} {'正文':>7} {'秒':>6}")
print("-" * 72)

fails = []
for label, model, effort, mt, sys_p, user_p, need in TASKS:
    payload = {
        "model": model, "max_tokens": mt, "temperature": 0.0,
        "system": sys_p,
        "messages": [{"role": "user", "content":
                      user_p + "\n\n只输出 JSON，不要任何解释文字或代码围栏。"}],
    }
    if effort:
        payload["output_config"] = {"effort": effort}

    t0 = time.time()
    try:
        r = httpx.post(URL, headers={"x-api-key": KEY, "Authorization": f"Bearer {KEY}",
                                    "Content-Type": "application/json",
                                    "anthropic-version": "2023-06-01"},
                       json=payload, timeout=300.0)
        dt = round(time.time() - t0, 1)
        if r.status_code != 200:
            print(f"{label:<20} {effort or '关':<5} ✗{r.status_code:<4} "
                  f"{'':5} {'':5} {'':>7} {'':>7} {dt:>6}")
            fails.append((label, f"HTTP {r.status_code} {r.text[:120]}"))
            continue
        blocks = r.json().get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        think = "".join(b.get("thinking", "") for b in blocks if b.get("type") == "thinking")
        try:
            data = _parse_json(text)
            ok_parse = isinstance(data, dict)
        except Exception as e:
            data, ok_parse = None, False
            print(f"    解析失败: {type(e).__name__}: {str(e)[:90]}")
            print(f"    原始开头: {text[:200]!r}")
        ok_fields = ok_parse and all(k in (data or {}) for k in need)
        print(f"{label:<20} {effort or '关':<5} {'✓':<5} "
              f"{'✓' if ok_parse else '✗':<5} {'✓' if ok_fields else '✗':<5} "
              f"{len(think):>7} {len(text):>7} {dt:>6}")
        if not ok_fields:
            fails.append((label, f"字段缺失，拿到 {list((data or {}).keys())[:6]}"))
    except Exception as e:
        print(f"{label:<20} {effort or '关':<5} ✗  {type(e).__name__}: {str(e)[:70]}")
        fails.append((label, f"{type(e).__name__}: {str(e)[:80]}"))

print("-" * 72)
if fails:
    print(f"❌ {len(fails)} 个环节不通过：")
    for n, why in fails:
        print(f"   - {n}: {why}")
else:
    print("✅ 全部环节通过")
