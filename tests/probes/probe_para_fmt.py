"""验证：开 include_formatting 之后，按段落切到底能切出多少。

发现：`trafilatura.extract(favor_precision=True)` 抽出来的正文**没有任何空行**，
25 篇实测 25 个"原始段落"——整篇就是一段，「按段落切」无从谈起。
`include_formatting=True` 会把段落结构带回来（实测空行 0 → 100）。

本脚本对若干真实网页各试两种抽取，统计：
  · 原始段落数、能完整切出的比例、超长段的数量
  · 段落切分 vs 现有 RecursiveSplitter 的段长分布
"""

from __future__ import annotations

import re
import statistics as st
import sys

import httpx
import trafilatura

sys.path.insert(0, "/Users/wd/Documents/Deep Research Agent")
from research_agent.ingest.split import count_tokens  # noqa: E402

MAX_TOKENS = 2000
HARD_TOKENS = 6000
PARA_RE = re.compile(r"\n\s*\n+")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

URLS = [
    "https://manlybattery.com/top-solid-state-battery-companies/",
    "https://www.evinfrastructurenews.com/ev-battery-safety-solid-state",
    "https://eureka.patsnap.com/blog/research-report-sulfide-solid-electrolyte",
    "https://spectrum.ieee.org/semi-solid-state-battery",
    "https://www.dcebattery.com/",
    "https://caifuhao.eastmoney.com/news/202503061598374551",
    "https://www.stdaily.com/web/gdxw/2025-11/10/content_429798.html",
    "https://www.cls.cn/detail/2207128",
]


def paras(text: str) -> list[str]:
    return [p.strip() for p in PARA_RE.split(text) if p.strip()]


def split_oversize(p: str) -> list[str]:
    n = count_tokens(p)
    if n <= MAX_TOKENS:
        return [p]
    if n > HARD_TOKENS:
        step = MAX_TOKENS * 2
        return [p[i:i + step] for i in range(0, len(p), step)]
    out, cur = [], ""
    for sent in re.split(r"(?<=[。！？；.!?;])", p):
        if sent and count_tokens(cur + sent) > MAX_TOKENS:
            if cur:
                out.append(cur.strip())
            cur = sent
        else:
            cur += sent
    if cur.strip():
        out.append(cur.strip())
    return out


print(f"{'网页':<42}{'字符':>8}{'段落':>5}{'完整':>5}{'超长':>5}{'切后段':>6}")
print("-" * 76)
agg = {"char": 0, "para": 0, "intact": 0, "over": 0, "seg": 0, "seglens": []}
for u in URLS:
    try:
        html = httpx.get(u, timeout=25, follow_redirects=True, headers=UA).text
        t = trafilatura.extract(html, favor_precision=True,
                                include_formatting=True) or ""
        if len(t) < 500:
            print(f"{u[:40]:<42}{'内容过短，跳过':>28}")
            continue
    except Exception as e:
        print(f"{u[:40]:<42}  抓取失败 {type(e).__name__}")
        continue
    ps = paras(t)
    segs = []
    over = 0
    for p in ps:
        parts = split_oversize(p)
        if len(parts) > 1:
            over += 1
        segs.extend(parts)
    agg["char"] += len(t)
    agg["para"] += len(ps)
    agg["over"] += over
    agg["seg"] += len(segs)
    agg["seglens"] += [count_tokens(s) for s in segs]
    print(f"{u[:40]:<42}{len(t):>8,}{len(ps):>5}{len(ps)-over:>5}{over:>5}{len(segs):>6}")

L = sorted(agg["seglens"])
print("-" * 76)
if L:
    print(f"合计 {agg['char']:,} 字符 → {agg['para']} 个原始段落"
          f"（完整切出 {agg['para']-agg['over']} = "
          f"{(agg['para']-agg['over'])/max(1,agg['para']):.0%}）"
          f" → 切成 {agg['seg']} 段")
    print(f"段 token：最短 {L[0]}　中位 {L[len(L)//2]}　平均 {sum(L)//len(L)}　最长 {L[-1]}")
    print(f"超过 {MAX_TOKENS} token 的段：{sum(1 for x in L if x>MAX_TOKENS)} 个")
    print(f"\n对照现有 RecursiveSplitter(120–500)：上一场 25 个来源 → 93 段，"
          f"平均 878 token。")
