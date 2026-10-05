"""R5 第一步验证：真实语料上跑切分 + 段落概括。

要看的两件事：
  1. 切分：块长分布、是否都在段落边界落刀
  2. 概括：写出来的一句话，能不能替代读原文做判断

用 /tmp/ra_write_real3.db 里 fetch_cache 存的真实 URL 重抓，
走完整链路：include_formatting → 段落切分 → 概括 → 落库。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
os.environ.setdefault("RA_DB_PATH", "/tmp/ra_r5_verify.db")

import sqlite3  # noqa: E402
import time  # noqa: E402

import httpx  # noqa: E402

from research_agent.db.repo import Repo  # noqa: E402
from research_agent.graph.subgraphs import extract_batch as EB  # noqa: E402
from research_agent.ingest.split import RecursiveSplitter, count_tokens  # noqa: E402

DB = "/tmp/ra_r5_verify.db"
for s in ("", "-wal", "-shm"):
    q = Path(DB + s)
    if q.exists():
        q.unlink()

SP = RecursiveSplitter(max_tokens=2000, target_tokens=1000)
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

src = sqlite3.connect("/tmp/ra_write_real3.db")
urls = [r[0] for r in src.execute("SELECT url FROM fetch_cache "
                                  "ORDER BY LENGTH(content) DESC LIMIT 6")]

repo = Repo(DB)
repo.init_schema()
sid = repo.create_session("R5 切分与概括验证", "固态电池的技术路线与产业化")
items, all_chunks = [], []
t0 = time.time()

for u in urls:
    try:
        from research_agent.ingest.extract import Fetcher
        f = Fetcher(repo)
        import asyncio
        page = asyncio.run(f.fetch_many([u], bypass_cache=True))[0]
    except Exception as e:
        print(f"  跳过 {u[:44]}：{type(e).__name__}")
        continue
    if not page.ok or len(page.text) < 400:
        print(f"  跳过 {u[:44]}：抓取失败 {page.reason}")
        continue
    s = repo.add_source(session_id=sid, url=u, url_normalized=u,
                        domain=u.split("/")[2], title=page.title or u[:40],
                        source_type="unknown", char_count=len(page.text))
    segs = []
    for c in SP.split(page.text):
        cid = repo.add_chunk(session_id=sid, source_id=s, origin="web",
                             content=c.content, heading_path=c.heading_path,
                             token_count=c.token_count)
        segs.append({"chunk_id": cid, "text": c.content,
                     "heading_path": c.heading_path, "token_count": c.token_count})
        all_chunks.append((cid, c))
    items.append({"source_id": s, "title": page.title, "url": u,
                  "published": None, "text": page.text, "segments": segs})
    print(f"  ✓ {u[:48]:<50} {len(page.text):>6,} 字 → {len(segs)} 块")

print(f"\n切分完成 {len(all_chunks)} 块，用时 {time.time()-t0:.1f}s")
lens = sorted(count_tokens(c.content) for _, c in all_chunks)
print(f"块 token：最短 {lens[0]}　中位 {lens[len(lens)//2]}　"
      f"平均 {sum(lens)//len(lens)}　最长 {lens[-1]}")

print(f"\n{'='*74}\n段落概括（这一步原先在报告写完之后才跑）\n{'='*74}")
t0 = time.time()
out = EB.summarize_segments([s for it in items for s in it["segments"]])
print(f"用时 {time.time()-t0:.1f}s，产出 {len(out['summaries'])}/{len(all_chunks)} 条概括，"
      f"失败批次 {out['failed']}")
repo.mark_summaries(out["summaries"])

rows = repo.q("SELECT c.id, c.summary, c.content, c.token_count FROM chunk c "
              "WHERE c.session_id=? ORDER BY c.ingested_at", (sid,))
for i, r in enumerate(rows, 1):
    if not r["summary"]:
        continue
    print(f"\n[{i}] 块 {r['token_count']} token ｜ 概括 {len(r['summary'])} 字")
    print(f"  原文: {r['content'][:170].replace(chr(10), ' ')}…")
    print(f"  概括: {r['summary']}")
print(f"\nsession_id = {sid}")
