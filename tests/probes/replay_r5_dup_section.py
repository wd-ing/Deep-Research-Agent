"""用 R5 的真实报告验证：装配重复正文这个 bug 修好了没有。

测的东西
--------
1. R5 那份报告里 sec7 与 sec8 的正文是不是真的逐字相同（复现旧 bug）
2. 新的 `_assemble_warnings` 能不能抓到这种重复
3. 新的 `_split_sections` 遇到「没分节标记的旧格式产出」会怎么做
   ——必须是**如实报错**，不能像旧逻辑那样默默复制

用法：.venv/bin/python tests/probes/replay_r5_dup_section.py [db路径]

结论（2026-10-05）
-----------------
sec7 与 sec8 各 1851 字，逐字相同，开头都是
`### sec8 证据审计：高能量密度样品、企业披露与规模交付不能等同`
——即 t8 的一份产出被原样贴进了两节，R5 审查发现的
「sec7 内容错置 + sec8 重复两次」就是它。

新逻辑：自检报出「小节 sec7 与 sec8 的正文逐字相同」；
切分时如实说「没有分节标记，无法确定哪段属于哪一节」，不给内容。
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from research_agent.graph.subgraphs import write as W  # noqa: E402

DB = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ra_r5_e2e.db"
SID = "s_a9f5bc761016"
SEC7_TITLE = "## 电极材料与能量密度上限"
SEC8_TITLE = "## 高能量密度样品与量产成熟度的距离"


def main() -> int:
    if not Path(DB).exists():
        print(f"❌ 找不到 {DB}——R5 那轮的测试库已经不在了")
        return 1
    db = sqlite3.connect(DB)
    row = db.execute("SELECT markdown FROM report WHERE session_id=?", (SID,)).fetchone()
    if not row:
        print(f"❌ 库里没有 session {SID} 的报告")
        return 1
    md = row[0]

    heads = [(m.start(), m.end(), m.group()) for m in re.finditer(r"^## .*$", md, re.M)]
    idx = {t.strip(): i for i, (_, _, t) in enumerate(heads)}
    if SEC7_TITLE not in idx or SEC8_TITLE not in idx:
        print("❌ 报告的小节标题对不上，这份库可能不是 R5 那轮")
        return 1

    def body(title: str) -> str:
        i = idx[title]
        return md[heads[i][1]:heads[i + 1][0]].strip()

    s7, s8 = body(SEC7_TITLE), body(SEC8_TITLE)
    same = s7 == s8
    print("=" * 68)
    print("R5 真实报告里的 sec7 / sec8")
    print("=" * 68)
    print(f"  sec7 {len(s7)} 字   sec8 {len(s8)} 字")
    print(f"  两节逐字相同: {same}   ← 这就是 R5 的事故")
    print(f"  sec7 开头: {s7.splitlines()[0][:56]}")
    print(f"  sec8 开头: {s8.splitlines()[0][:56]}")

    warns = W._assemble_warnings(
        {"sec7": "<!-- task:t8 section:sec7 -->\n" + s7,
         "sec8": "<!-- task:t8 section:sec8 -->\n" + s8},
        {"t8": {"sec7": s7, "sec8": s8}},
        {"t8": {"covers": ["sec7", "sec8"]}})
    print(f"\n  新的装配自检命中 {len(warns)} 条:")
    for w in warns:
        print(f"    • {w}")

    secs, note = W._split_sections(s7, ["sec7", "sec8"])
    print(f"\n  新逻辑拿到这份旧格式产出 → 切出 {list(secs)}")
    print(f"    上报原因: {note}")

    ok = same and any("逐字相同" in w for w in warns) and not secs
    print(f"\n{'✅' if ok else '❌'} bug 复现 + 新逻辑都按预期")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
