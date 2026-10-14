"""一次性回填：`source_nature` 为空的来源重新判一次性质。

**为什么需要**
------------
2026-10-14 发现 `extract_batch._run_nature_batch` 的下标体系分叉：
`_render_nature` 渲染给模型看的是**全局**下标（`items` 里的位置），
校验时却拿它比 `len(batch)`（**切片**长度）——
于是第 2 批起必然越界、被 `continue` 整批静默丢弃。

后果不只是来源库里多一个「未判」组：这些来源的 `nature` 也会喂进
写作提示词（`nodes.py:1240`，写作时看到的是「性质未判」）。

已跑的会话拿不回那批 LLM 结果（`llm_call` 只记 token，不存响应体），
所以只能**用同一套判定逻辑重跑一遍**。判定输入从库里重建：
整页正文 = 该来源所有 chunk 的 content 按插入顺序拼起来
（实测 chunk 总字数 ≈ `source.char_count`，差的是切分时的空白），
段概括直接取 `chunk.summary`——和正常流程喂给模型的是同一份材料。

跑：
    .venv/bin/python tests/backfill_source_nature.py --db /tmp/ra_xj189_v5.db --dry-run
    .venv/bin/python tests/backfill_source_nature.py --db /tmp/ra_xj189_v5.db
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from research_agent.graph.subgraphs import extract_batch as eb  # noqa: E402


def load_missing(db: str, session: str | None) -> list[dict]:
    """取 `source_nature` 为空的来源，连同重建的判定输入。"""
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    where = "COALESCE(s.source_nature,'') IN ('','unknown')"
    args: list = []
    if session:
        where += " AND s.session_id=?"
        args.append(session)
    srcs = [dict(r) for r in c.execute(
        f"SELECT s.id, s.title, s.url, s.domain, s.char_count "
        f"FROM source s WHERE {where} ORDER BY s.rowid", args)]
    for s in srcs:
        # rowid = 切分时的插入顺序，拼起来就是原文顺序
        ch = c.execute(
            "SELECT id, content, summary FROM chunk "
            "WHERE source_id=? ORDER BY rowid", (s["id"],)).fetchall()
        s["source_id"] = s["id"]        # 正常流程里这个键就叫 source_id
        s["text"] = "\n\n".join(r["content"] or "" for r in ch)
        s["segments"] = [{"chunk_id": r["id"],
                          "text": r["content"] or "",
                          "heading": "",
                          "summary": r["summary"] or ""} for r in ch]
    c.close()
    return srcs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--session", default=None,
                    help="只回填这一个会话；不给就是整库")
    ap.add_argument("--dry-run", action="store_true",
                    help="只报要做什么，不调 LLM、不写库")
    ap.add_argument("--limit", type=int, default=0,
                    help="只处理前 N 个（试水用）")
    args = ap.parse_args()

    items = load_missing(args.db, args.session)
    if args.limit:
        items = items[:args.limit]
    if not items:
        print("没有需要回填的来源。")
        return

    batches = eb.make_batches(eb._make_nature_items(items))
    print(f"待回填 {len(items)} 个来源 → {len(batches)} 批"
          f"（每批 {eb.NATURE_BATCH} 个，约 {len(batches)} 次 LLM 调用）")
    for s in items[:8]:
        print(f"  {s['domain']:<34} 段 {len(s['segments']):>2}  "
              f"重建正文 {len(s['text']):>6} 字 / 原始 char_count {s['char_count']}")
    if len(items) > 8:
        print(f"  …还有 {len(items) - 8} 个")
    if args.dry_run:
        print("\n--dry-run：到此为止，没调 LLM、没写库。")
        return

    repo = __import__("research_agent.db.repo", fromlist=["Repo"]).get_repo(args.db)
    judged: list[tuple[str, str, str]] = []
    missed: list[str] = []
    for bi, batch in enumerate(batches):
        try:
            out, missing = eb._run_nature_batch(batch)
        except Exception as e:                       # noqa: BLE001
            print(f"  批 {bi} 失败：{type(e).__name__}: {e}")
            missed.extend(b["source_id"] for b in batch)
            continue
        for r in out:
            judged.append((r["source_id"],
                           r.get("source_nature") or "unknown",
                           r.get("nature_reason") or ""))
        missed.extend(missing)
        print(f"  批 {bi}: 判到 {len(out)}，未判定 {len(missing)}")

    for sid, nature, reason in judged:
        repo.set_source_nature(sid, nature, reason)

    print(f"\n已写库 {len(judged)} 个，未判定 {len(missed)} 个")
    if missed:
        print(f"  未判定的 id：{missed}")

    # 回读核对——不核对就是「写完就当成了」
    c = sqlite3.connect(args.db)
    left = c.execute(
        "SELECT COUNT(*) FROM source WHERE COALESCE(source_nature,'') IN ('','unknown')"
    ).fetchone()[0]
    print(f"回读：库里还剩 {left} 个没有性质")
    for r in c.execute(
        "SELECT COALESCE(source_nature,'NULL') v, COUNT(*) n FROM source GROUP BY 1"
    ):
        print(f"  {r[0]:<12} {r[1]}")
    c.close()


if __name__ == "__main__":
    main()
