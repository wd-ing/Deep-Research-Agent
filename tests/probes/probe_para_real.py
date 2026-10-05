"""用真实语料的 25 条 URL 重测：开 include_formatting + 段落合并到 1000 token。

上一轮我用的是猜的 URL，只有 5 条能抓到。这次用 /tmp/ra_write_real3.db
里 fetch_cache 存的**真实 URL**——同一批来源，能直接和旧切分结果对照。

旧结果（no formatting + RecursiveSplitter 120-500）：25 来源 → 93 段，平均 878 token。
"""

from __future__ import annotations

import re
import sqlite3
import sys
import time

import httpx
import trafilatura

sys.path.insert(0, "/Users/wd/Documents/Deep Research Agent")
from research_agent.ingest.split import count_tokens  # noqa: E402

TARGET, MAX_PARA, HARD = 1000, 2000, 6000
PARA_RE = re.compile(r"\n\s*\n+")
SENT_RE = re.compile(r"(?<=[。！？；.!?;])")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}


def split_oversize(p: str) -> list[str]:
    if count_tokens(p) <= MAX_PARA:
        return [p]
    if count_tokens(p) > HARD:
        step = MAX_PARA * 2
        return [p[i:i + step] for i in range(0, len(p), step)]
    out, cur = [], ""
    for s in SENT_RE.split(p):
        if s and count_tokens(cur + s) > MAX_PARA:
            if cur:
                out.append(cur.strip())
            cur = s
        else:
            cur += s
    if cur.strip():
        out.append(cur.strip())
    return out


def split_blocks(text: str):
    paras = [p.strip() for p in PARA_RE.split(text) if p.strip()]
    over = 0
    units = []
    for p in paras:
        parts = split_oversize(p)
        if len(parts) > 1:
            over += 1
        units.extend(parts)
    blocks, cur, cur_t = [], [], 0
    for u in units:
        ut = count_tokens(u)
        if ut > MAX_PARA:
            if cur:
                blocks.append("\n\n".join(cur)); cur, cur_t = [], 0
            blocks.append(u); continue
        if cur_t + ut > TARGET and cur:
            blocks.append("\n\n".join(cur)); cur, cur_t = [], 0
        cur.append(u); cur_t += ut
    if cur:
        blocks.append("\n\n".join(cur))
    return blocks, len(paras), over


c = sqlite3.connect("/tmp/ra_write_real3.db")
rows = c.execute("SELECT url, content FROM fetch_cache "
                 "ORDER BY LENGTH(content) DESC").fetchall()

print(f"{'来源':<46}{'旧字符':>8}{'新字符':>8}{'段落':>5}{'块':>4}{'均tok':>6}")
print("-" * 80)
ok = old_chars = new_chars = 0
all_lens, all_paras, all_blocks, all_over = [], 0, 0, 0
for url, old in rows:
    try:
        html = httpx.get(url, timeout=20, follow_redirects=True, headers=UA).text
        new = trafilatura.extract(html, favor_precision=True,
                                  include_formatting=True) or ""
    except Exception:
        print(f"{url[:44]:<46}{len(old):>8,}   抓取失败")
        continue
    if len(new) < 400:
        print(f"{url[:44]:<46}{len(old):>8,}{len(new):>8,}   抓到的内容过短")
        continue
    blocks, npara, over = split_blocks(new)
    lens = [count_tokens(b) for b in blocks]
    ok += 1; old_chars += len(old); new_chars += len(new)
    all_lens += lens; all_paras += npara; all_blocks += len(blocks); all_over += over
    print(f"{url[:44]:<46}{len(old):>8,}{len(new):>8,}{npara:>5}{len(blocks):>4}"
          f"{sum(lens)//len(lens):>6}")

print("-" * 80)
if all_lens:
    L = sorted(all_lens)
    print(f"成功重抓 {ok}/{len(rows)} 个来源")
    print(f"字符：旧 {old_chars:,} → 新 {new_chars:,}"
          f"（多出 {new_chars-old_chars:+,}，{(new_chars-old_chars)/old_chars:+.1%}）")
    print(f"原始段落 {all_paras} 个（超长需二次切 {all_over} 个 = {all_over/all_paras:.0%}）")
    print(f"切成 {all_blocks} 块，压缩比 {all_paras/max(1,all_blocks):.1f} 段 → 1 块")
    print(f"块 token：最短 {L[0]}　中位 {L[len(L)//2]}　平均 {sum(L)//len(L)}　最长 {L[-1]}")
    print(f"超过 {MAX_PARA} token 的块：{sum(1 for x in L if x>MAX_PARA)} 个")
    print(f"\n对照旧方案（no formatting + RecursiveSplitter 120–500）："
          f"同一批 {len(rows)} 个来源 → 93 段，平均 878 token")
    if ok == len(rows):
        print(f"     本方案 → {all_blocks} 块，平均 {sum(L)//len(L)} token")
