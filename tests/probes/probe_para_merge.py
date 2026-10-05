"""按确认的参数测一轮：段落边界 + 合并到 1000 token。

参数（2026-10-05 确认）：
  · 抽取开 include_formatting=True —— 段落结构才回得来（实测空行 0 → 100）
  · 切分目标 1000 token，在**段落边界**落刀
  · 相邻短段合并到接近目标
  · 单段超过 2000 token 才二次切（句子边界），病态段 6000 直接硬切

对照：现有 RecursiveSplitter(120–500) 上一场 25 个来源 → 93 段，平均 878 token。
"""

from __future__ import annotations

import re
import sys
import time

import httpx
import trafilatura

sys.path.insert(0, "/Users/wd/Documents/Deep Research Agent")
from research_agent.ingest.split import count_tokens  # noqa: E402

TARGET = 1000      # 累积到这个量级就落刀
MAX_PARA = 2000    # 单段超过这个才二次切
HARD = 6000        # 病态段
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


def split_blocks(text: str) -> tuple[list[str], dict]:
    paras = [p.strip() for p in PARA_RE.split(text) if p.strip()]
    stat = {"paras": len(paras), "oversize_paras": 0, "merged_blocks": 0, "cut_blocks": 0}

    units: list[str] = []
    for p in paras:
        parts = split_oversize(p)
        if len(parts) > 1:
            stat["oversize_paras"] += 1
        units.extend(parts)

    blocks, cur, cur_t = [], [], 0
    for u in units:
        ut = count_tokens(u)
        # 超大单元自己成块，不要拖着邻居一起超
        if ut > MAX_PARA:
            if cur:
                blocks.append("\n\n".join(cur)); stat["merged_blocks"] += 1
                cur, cur_t = [], 0
            blocks.append(u); stat["cut_blocks"] += 1
            continue
        if cur_t + ut > TARGET and cur:
            blocks.append("\n\n".join(cur)); stat["merged_blocks"] += 1
            cur, cur_t = [], 0
        cur.append(u); cur_t += ut
    if cur:
        blocks.append("\n\n".join(cur)); stat["merged_blocks"] += 1
    return blocks, stat


URLS = [
    "https://manlybattery.com/top-solid-state-battery-companies/",
    "https://spectrum.ieee.org/semi-solid-state-battery",
    "https://www.stdaily.com/web/gdxw/2025-11/10/content_429798.html",
    "https://www.cls.cn/detail/2207128",
    "https://eureka.patsnap.com/blog/research-report-sulfide-solid-electrolyte",
    "https://www.evinfrastructurenews.com/ev-battery-safety-solid-state",
    "https://www.dcebattery.com/",
    "https://energy.sustainability-directory.com/",
    "https://www.xueqiu.com/",
    "https://caifuhao.eastmoney.com/news/202503061598374551",
]

print(f"{'网页':<40}{'字符':>8}{'段落':>5}{'合并块':>7}{'切后块':>7}{'均值':>6}")
print("-" * 76)
agg = {"char": 0, "paras": 0, "blocks": 0, "over": 0, "lens": [], "t0": 0.0, "n": 0}
for u in URLS:
    try:
        html = httpx.get(u, timeout=25, follow_redirects=True, headers=UA).text
        t0 = time.time()
        t = trafilatura.extract(html, favor_precision=True,
                                include_formatting=True) or ""
        agg["t0"] += time.time() - t0
    except Exception as e:
        print(f"{u[:38]:<40}  抓取失败 {type(e).__name__}")
        continue
    if len(t) < 400:
        print(f"{u[:38]:<40}{len(t):>8,}   内容过短，跳过")
        continue
    blocks, st = split_blocks(t)
    lens = [count_tokens(b) for b in blocks]
    agg["char"] += len(t); agg["paras"] += st["paras"]
    agg["blocks"] += len(blocks); agg["over"] += st["oversize_paras"]
    agg["lens"] += lens; agg["n"] += 1
    print(f"{u[:38]:<40}{len(t):>8,}{st['paras']:>5}"
          f"{st['merged_blocks']:>7}{len(blocks):>7}{sum(lens)//len(lens):>6}")

L = sorted(agg["lens"])
print("-" * 76)
if L:
    print(f"{agg['n']} 个来源，{agg['char']:,} 字符")
    print(f"  原始段落 {agg['paras']} 个（其中超长需二次切 {agg['over']} 个）")
    print(f"  合并后切成 {agg['blocks']} 块")
    print(f"  块 token：最短 {L[0]}　中位 {L[len(L)//2]}　平均 {sum(L)//len(L)}　最长 {L[-1]}")
    print(f"  超过 {MAX_PARA} token 的块：{sum(1 for x in L if x > MAX_PARA)} 个")
    print(f"\n对照 RecursiveSplitter(120–500)：上一场 25 来源 → 93 段，平均 878 token")
    ratio = (sum(L) / len(L)) / 878
    print(f"     本方案块长 {sum(L)//len(L)} token，约为原来的 {ratio:.1f} 倍")
    print(f"     段落压缩比：{agg['paras']}/{agg['blocks']} = "
          f"{agg['paras']/max(1,agg['blocks']):.1f} 段合成 1 块")
