"""线索级来源机制的效果统计（只读）。

用法：.venv/bin/python tests/offline_stats/clue_stats.py <snapshot.db> [<snapshot.db> ...]
输入是 `sqlite3 .backup` 取的一致快照（别直接读带 -wal 的原库）。

口径（每份报告）：
- 来源编号 = session 内 source 表 rowid 升序（同 nodes._load_numbered_evidence）
- 线索级来源 = domain 命中 quality.clue_grade
- 正文 = report.markdown 去掉文末 `\\n---\\n\\n### 来源\\n` 之后的部分
- 句子 = 按行、再按 。！？ 切；「带引用句」= 句内有 [S..] 引用
- 「线索句」= 带引用句里**至少引一个**线索级来源
- 「独撑含数字句」= write._clue_only_numeric_sentences 的命中（所有被引来源都是线索级、
  含数字、无限定语）——这是机制要压低的指标
- 「已限定线索句」= 线索句里含限定语（_CLUE_QUAL_RE）
"""
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import _isolate  # noqa: F401,E402
from research_agent.search.quality import CLUE_MARK, clue_grade  # noqa: E402
from research_agent.graph.subgraphs import write as W  # noqa: E402


def stats(path: str) -> dict:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    sid = c.execute("select id from session").fetchone()[0]
    md = c.execute("select markdown from report").fetchone()[0] or ""
    cut = md.rfind("\n---\n\n### 来源\n")
    body = md[:cut] if cut >= 0 else md
    srcs = [dict(r) for r in c.execute(
        "select domain, title from source where session_id=? order by rowid", (sid,))]
    src_lines = [f"[S{i}] {s['title']} | {s['domain']}"
                 + (f" | {CLUE_MARK}" if clue_grade(s["domain"]) else "")
                 for i, s in enumerate(srcs, 1)]
    clue = {i for i, ln in enumerate(src_lines, 1) if CLUE_MARK in ln}
    cited = clue_sents = qualified = total = 0
    for line in body.split("\n"):
        for sent in re.split(r"[。！？]", line):
            nums = {int(n) for n in W._CITE_RE.findall(sent)}
            if not nums:
                continue
            total += 1
            if nums & clue:
                clue_sents += 1
                if W._CLUE_QUAL_RE.search(sent):
                    qualified += 1
    flagged = W._clue_only_numeric_sentences(body, src_lines, cap=10**6)
    return {"db": Path(path).name, "chars": len(body), "sources": len(srcs),
            "clue_sources": len(clue), "cited_sents": total,
            "clue_sents": clue_sents, "qualified_clue_sents": qualified,
            "clue_only_numeric": len(flagged),
            "marked_in_final_list": md.count(CLUE_MARK)}


if __name__ == "__main__":
    for p in sys.argv[1:]:
        s = stats(p)
        print(f"{s['db']:<26} chars={s['chars']:>6} src={s['sources']:>3} clue_src={s['clue_sources']:>2} "
              f"cited_sents={s['cited_sents']:>4} clue_sents={s['clue_sents']:>4} "
              f"qualified={s['qualified_clue_sents']:>3} clue_only_numeric={s['clue_only_numeric']:>4} "
              f"marker_lines_in_report={s['marked_in_final_list']}")
