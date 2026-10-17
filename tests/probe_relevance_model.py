"""换模型对跑：`relevance` 闸门判定（真实段落，生产代码路径）。

2026-10-17 加。起因：信通院 DeepSeek 额度将尽，`Atria-Dawn` 限时免费。
而 `write_plan_tasks` 上的实测结论是**不能换**（1/3，两次在 ~665 秒被掐断），
所以问题变成：**额度压力到底压在哪些环节上，Atria-Dawn 能不能顶那些。**

参考轮（`s_efb71e9cbf66`）的调用分布：

    relevance  148 次 / 189 = **78%**      ← 额度压力几乎全在这
    summarize   15 次 /  8%
    write_section 10 次 / 5%
    write_plan    3 次 /  2%              ← 换不了也不心疼

⇒ 只要 `relevance` 能换，额度问题基本就解了，而它恰恰是
**最短、最便宜、关思考**的环节（4096 预算 / effort=disabled / 实测最长 63 秒）。

## 为什么这个测试用 `_llm_judge_seg` 而不是自己拼 prompt

`relevance.py:268` 那个函数**就是生产判定入口**（含异常兜底与放行语义）。
自己拼一段 JSON 判定请求去测，测的不是它；本项目栽过三次这种坑。

用法：
    .venv/bin/python tests/probe_relevance_model.py [每模型段数]
"""

from __future__ import annotations

import os
import sqlite3
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

DB = str(ROOT / "data" / "refs" / "ra_internai_v1.db")
SID = "s_efb71e9cbf66"


def load_segments(n: int) -> list[dict]:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    q = c.execute(
        "select heading_path, summary, content from chunk "
        "where session_id=? and on_topic=1 and summary is not null "
        "and summary!='' order by rowid limit ?", (SID, n)).fetchall()
    return [dict(r) for r in q]


def run(model: str, segs: list[dict], topic: str) -> dict:
    import copy
    from research_agent.graph.subgraphs import relevance as R

    base = R.LLM.registry.stage("relevance")
    alt = copy.copy(base)
    alt.model = model
    R.LLM.registry._cache["relevance"] = alt

    ok, degraded, lat = 0, 0, []
    on_topic = 0
    for s in segs:
        blob = (s.get("content") or "")[:2000]
        t0 = time.time()
        r = R._llm_judge_seg(s, topic, blob, method="llm")
        lat.append(time.time() - t0)
        if r["method"].endswith("_degraded"):
            degraded += 1
        else:
            ok += 1
        if r["on_topic"]:
            on_topic += 1
    R.LLM.registry._cache["relevance"] = base
    return {"model": model, "ok": ok, "degraded": degraded,
            "on_topic": on_topic,
            "med": statistics.median(lat) if lat else 0.0,
            "max": max(lat) if lat else 0.0}


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    topic = c.execute("select question from session where id=?", (SID,)).fetchone()[0]
    segs = load_segments(n)
    print(f"研究对象：{topic[:40]}…  真实段落 {len(segs)} 段\n")
    print(f"{'模型':<26}{'成功':>6}{'降级':>6}{'判为相关':>9}"
          f"{'中位耗时':>10}{'最长':>8}")
    print("-" * 68)
    res = []
    for m in ("deepseek-v4-flash-0731", "Atria-Dawn"):
        r = run(m, segs, topic)
        res.append(r)
        print(f"{m:<26}{r['ok']:>6}{r['degraded']:>6}{r['on_topic']:>9}"
              f"{r['med']:>9.1f}s{r['max']:>7.1f}s", flush=True)
    print()
    a, b = res
    print(f"降级次数：deepseek {a['degraded']} / Atria-Dawn {b['degraded']}"
          f"（降级 = 判定失败后**放行**，会悄悄放进无关段落）")
    print("⚠️ 本脚本只比**成功率与耗时**。两次判定的**内容是否一致**没有"
          "逐段对照——那需要把两边的 reason 逐条比对，是另一件事，"
          "没做就是没做。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
