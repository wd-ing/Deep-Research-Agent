"""用真实正文测「按段落切」的可行性。

要回答的问题：把切分边界从「任意语义分隔符」换成「段落边界」之后，
  1. 多少段落能**完整**切出来（不跨段落边界）？
  2. 剩下的超长段落按最大值切，切口落在哪里？
  3. 段长分布变成什么样？写作时够不够一个论点？

数据：/tmp/ra_write_real3.db 的 fetch_cache（25 条原始正文，163,547 字符）
"""

from __future__ import annotations

import re
import sqlite3
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, "/Users/wd/Documents/Deep Research Agent")
from research_agent.ingest.split import count_tokens  # noqa: E402

MAX_TOKENS = 2000          # 超长段落的上限，按用户说的「设一个较大的上限」
HARD_TOKENS = 6000         # 病态保护，超过这个连句子边界都不找了

# 段落边界：空行 / 全角空格缩进 / 常见中文段落起首
PARA_RE = re.compile(r"\n\s*\n+")


def split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in PARA_RE.split(text) if p.strip()]


def split_oversize(p: str) -> list[str]:
    """超长段落：优先按句子边界切，其次逗号，最后硬切。"""
    if count_tokens(p) <= MAX_TOKENS:
        return [p]
    if count_tokens(p) > HARD_TOKENS:
        # 病态：直接按 token 预算硬切，不再找边界
        return [p[i:i + MAX_TOKENS * 2] for i in range(0, len(p), MAX_TOKENS * 2)]
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


def main() -> None:
    c = sqlite3.connect("/tmp/ra_write_real3.db")
    rows = c.execute("SELECT url, content FROM fetch_cache ORDER BY LENGTH(content) DESC").fetchall()

    total_para = intact = 0
    seg_lens, oversize, sources = [], [], []
    for url, text in rows:
        paras = split_paragraphs(text)
        segs = []
        for p in paras:
            total_para += 1
            parts = split_oversize(p)
            if len(parts) == 1:
                intact += 1
            else:
                oversize.append((count_tokens(p), len(parts), url[:44], p[:70]))
            segs.extend(parts)
        for s in segs:
            seg_lens.append(count_tokens(s))
        sources.append((len(paras), len(segs), len(text), url[:44]))

    seg_lens.sort()
    n = len(seg_lens)
    print("=" * 72)
    print("按段落边界切分的结果")
    print("=" * 72)
    print(f"来源 {len(rows)} 个，原始段落 {total_para} 个")
    print(f"  其中**完整切出**（未跨段落）：{intact} 个 = {intact/total_para:.1%}")
    print(f"  超长需二次切分：          {total_para-intact} 个 = {(total_para-intact)/total_para:.1%}")
    print(f"\n最终段数 {n}")
    print(f"  token：最短 {seg_lens[0]}　中位 {seg_lens[n//2]}　"
          f"平均 {sum(seg_lens)//n}　最长 {seg_lens[-1]}")
    print(f"  超过 2000 token 的段：{sum(1 for x in seg_lens if x > MAX_TOKENS)} 个")

    print(f"\n需要二次切分的超长段落（全部）:")
    for tok, parts, url, head in sorted(oversize, reverse=True):
        print(f"  {tok:>5} token → 切 {parts} 段 | {url}")
        print(f"         「{head}…」")

    print(f"\n切出段数最多的来源:")
    for para, seg, chars, url in sorted(sources, key=lambda x: -x[1])[:5]:
        print(f"  {url[:44]:<46} {chars:>6,} 字 → {para:>3} 段 → 切成 {seg:>3} 段")

    print(f"\n对照：现有 RecursiveSplitter(120–500) 的结果是 93 段，平均 878 token。")
    print(f"     按段落切得到 {n} 段，平均 {sum(seg_lens)//n} token。")


if __name__ == "__main__":
    main()
