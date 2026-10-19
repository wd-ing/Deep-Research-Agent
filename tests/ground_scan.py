"""终稿数字/年份 grounding 扫描（只读，宽松口径）。

对写作检查点里的 drafts：逐任务取其分配 segments 全文，核对草稿正文里
「带单位的数字 + 年份」的**数字部分**是否出现在该任务证据里。
宽松口径 = 只比数字串本身（去千分位逗号），不比单位写法，
所以 ℃/°C、USD/US dollars/美元、万/亿 换算这类不会误报；代价是会漏掉
「数字对得上、单位/对象错了」的情形——这是下界估计，不是上界。

用法:
  .venv/bin/python tests/ground_scan.py <db路径> <session_id> [checkpoint_ns]
未给 checkpoint_ns 时取该 thread 下最新的、同时有 tasks 和 drafts 的 write 快照。
输出：总数、未支持数、逐条（任务/数字/上下文），以及这些数字是否出现在规划产物
（spec/tasks）里——用来区分「规划带进来的」和「写手自己冒出来的」。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB, SID = sys.argv[1], sys.argv[2]
NS = sys.argv[3] if len(sys.argv) > 3 else None
os.environ["RA_DB_PATH"] = DB

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer  # noqa: E402

from research_agent.db.repo import get_repo  # noqa: E402
from research_agent.tools.evidence import EvidenceTools  # noqa: E402

UNITS = (r"(?:wh/kg|w·h·kg-1|wh·kg-1|gwh|mwh|kwh|wh|ah|ms/cm|s/cm|usd(?:/kwh)?|"
         r"美元|元|%|倍|℃|°c|mm|μm|nm|mpa|gpa|v|圈|项|万辆|辆|gw|万|亿|km|"
         r"分钟|小时|kg|g|l|次|年)")
CIT = re.compile(r"\[S\d+(?:·\d+)?\]")
NUM_UNIT = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:[–—~～-]\s*(\d+(?:\.\d+)?))?\s*" + UNITS,
    re.I)


def load_snapshot() -> dict:
    ser = JsonPlusSerializer()
    c = sqlite3.connect(ROOT / "data/checkpoints.db")
    q = ("select checkpoint_ns,type,checkpoint from checkpoints where thread_id=? "
         + ("and checkpoint_ns=? " if NS else "and checkpoint_ns like 'write:%' ")
         + "order by checkpoint_id desc")
    args = (SID, NS) if NS else (SID,)
    for ns, typ, blob in c.execute(q, args):
        cv = dict(ser.loads_typed((typ, blob))["channel_values"])
        if cv.get("tasks") and cv.get("drafts"):
            print("snapshot ns =", ns, flush=True)
            return cv
    sys.exit("没找到同时有 tasks 和 drafts 的 write 快照")


def main() -> None:
    snap = load_snapshot()
    tasks = {t["id"]: t for t in snap["tasks"]}
    plan = re.sub(r"(?<=\d),(?=\d{3})", "", json.dumps(
        {"spec": snap.get("spec"), "tasks": snap["tasks"]}, ensure_ascii=False))
    et = EvidenceTools(get_repo(), SID)

    def seg(refs: list[str]) -> str:
        r = et.dispatch([{"name": "read_segments", "input": {"segments": refs}}])
        return "\n".join(str(g.get("全文") or "") for g in
                         (json.loads(r[0]["content"]).get("段落") or []))

    tot, bad = 0, []
    for tid, secs in snap["drafts"].items():
        t = tasks.get(tid)
        if not t:
            continue
        ev = re.sub(r"(?<=\d),(?=\d{3})", "", seg(t.get("segments") or []))
        for sec, body in secs.items():
            for line in CIT.sub("", body).splitlines():
                if line.lstrip().startswith("#"):
                    continue
                line = re.sub(r"(?<=\d),(?=\d{3})", "", line)
                for m in NUM_UNIT.finditer(line):
                    for g in (m.group(1), m.group(2)):
                        if not g:
                            continue
                        tot += 1
                        pat = r"(?<![\d.])" + re.escape(g) + r"(?![\d])"
                        if re.search(pat, ev) is None:
                            bad.append((tid, sec, g,
                                        bool(re.search(pat, plan)),
                                        line[max(0, m.start() - 30):m.end() + 30]))
    print(f"数字/年份总数 {tot}，证据里找不到数字串的 {len(bad)} "
          f"（去重后 {len({(b[0], b[2]) for b in bad})}）")
    print("其中规划产物(spec/tasks)里也出现的：",
          sum(1 for b in bad if b[3]), "/", len(bad))
    seen = set()
    for tid, sec, g, inplan, ctx in bad:
        if (tid, g) in seen:
            continue
        seen.add((tid, g))
        print(f"[{tid}/{sec}] {g}  规划里有={inplan} :: {ctx}")


if __name__ == "__main__":
    main()
