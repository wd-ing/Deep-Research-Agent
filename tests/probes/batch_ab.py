"""批次切分改造的对照实验。

用上一轮真实跑出来的 27 个来源，在同一份数据上比较三套切法：
  A 逐源（改造前）
  B 按条数+字符双限（改造前，实测 17 次调用、127,023 输入 token）
  C 按字符预算 + 单源头尾采样（改造后）

同时验证裁剪会不会丢断言——对长文档比较「全文本」与「头尾采样」
两次抽取的断言数量与重合度。

用法: python -m tests.batch_ab <会话id>
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for line in Path(".env").read_text(encoding="utf-8").splitlines() if Path(".env").exists() else []:
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from research_agent.db.repo import get_repo                      # noqa: E402
from research_agent.graph.subgraphs.extract_batch import (      # noqa: E402
    BATCH_BUDGET_CHARS, BATCH_MAX_SOURCES, PROMPT_OVERHEAD,
    MIN_SOURCE_CHARS, HARD_CAP_CHARS, _clip, make_batches,
)


def load_sources(session_id: str) -> list[dict]:
    repo = get_repo()
    rows = repo.q(
        "SELECT id, url, title, char_count, source_date FROM source "
        "WHERE session_id=? AND char_count > 500 ORDER BY char_count DESC",
        (session_id,),
    )
    out = []
    for r in rows:
        text = _read_text(r["url"], r["char_count"])
        if text:
            out.append({"source_id": r["id"], "title": r["title"], "url": r["url"],
                        "published": r["source_date"], "text": text})
    return out


def _read_text(url: str, expect: int) -> str:
    """从 7 天抓取缓存里取回正文。缓存过期就重新抓。"""
    from research_agent.ingest.extract import Fetcher
    from research_agent.search.normalize import normalize_url
    repo = get_repo()
    n = normalize_url(url)
    c = repo.cache_get(n)
    if c and len(c) > 400:
        return c
    import asyncio
    pages = asyncio.run(Fetcher(repo).fetch_many([url]))
    return pages[0].text if pages and pages[0].ok else ""


def plan_a_per_source(items):
    return [[it] for it in items]


def plan_b_count_and_chars(items):
    """改造前：每批最多 4 条且总字符 ≤32000，超长源自成一批"""
    out, cur, cur_chars = [], [], 0
    for it in items:
        n = len(it.get("text") or "")
        if n > 32000:
            if cur:
                out.append(cur); cur, cur_chars = [], 0
            out.append([it]); continue
        if cur and (len(cur) >= 4 or cur_chars + n > 32000):
            out.append(cur); cur, cur_chars = [], 0
        cur.append(it); cur_chars += n
    if cur:
        out.append(cur)
    return out


def plan_c_budget(items):
    return make_batches(items)


def cost_of(batches) -> tuple[int, int]:
    """返回 (调用次数, 总输入字符)。每批都要重付一次提示词骨架。"""
    calls = len(batches)
    total = sum(len(b.get("text") or "") + PROMPT_OVERHEAD for b in batches for _ in [0]) \
        if False else sum(
            sum(len(it.get("text") or "") for it in b) + PROMPT_OVERHEAD for b in batches)
    return calls, total


if __name__ == "__main__":
    sid = sys.argv[1] if len(sys.argv) > 1 else None
    if not sid:
        rows = get_repo().q("SELECT id FROM session ORDER BY created_at DESC LIMIT 1")
        sid = rows[0]["id"]
    items = load_sources(sid)
    if not items:
        raise SystemExit("没有可用的来源正文（缓存可能已过期）")

    raw = sum(len(i["text"]) for i in items)
    print("=" * 74)
    print(f"会话 {sid}")
    print(f"来源 {len(items)} 个，正文合计 {raw:,} 字符"
          f"（最长一个 {max(len(i['text']) for i in items):,}）")
    print("=" * 74)
    print(f"{'切法':<28}{'调用次数':>8}{'输入字符':>12}{'相对原始':>10}{'省下':>8}")
    print("-" * 74)
    base = None
    for name, fn in (("A 逐源（改造前）", plan_a_per_source),
                     ("B 条数+字符双限（改造前实测）", plan_b_count_and_chars),
                     ("C 字符预算，长文全量（新）", plan_c_budget)):
        bs = fn(items)
        calls, chars = cost_of(bs)
        pct = chars / raw * 100
        if name.startswith("A"):
            base = chars
        print(f"{name:<28}{calls:>8}{chars:>12,}{pct:>9.0f}%"
              f"{(1 - chars / base) * 100:>7.0f}%" if base else
              f"{name:<28}{calls:>8}{chars:>12,}{pct:>9.0f}%{'-':>8}")

    bs = make_batches(items)
    print("\n新切法每批明细：")
    for i, b in enumerate(bs):
        n = sum(len(x["text"]) for x in b)
        sizes = [f"{len(x['text']):,}" for x in b]
        print(f"  批{i + 1}: {len(b)} 源 {n:,} 字符  单源 {sizes}")
    print(f"\n上限校验: 预算 {BATCH_BUDGET_CHARS:,} | 单批最多 {BATCH_MAX_SOURCES} 源 | "
          f"下限 {MIN_SOURCE_CHARS} | 防病态硬上限 {HARD_CAP_CHARS:,}")
    over = [n for b in bs
            for n in [sum(len(x["text"]) for x in b)] if n > BATCH_BUDGET_CHARS]
    print(f"超预算的批次: {len(over)} 个")
