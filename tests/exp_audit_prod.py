"""write_audit 生产入口验收：真实 node_audit（不打补丁）× N，在 DB 副本上跑。

输入同 exp_audit_think.py（v14 的写作子图 checkpoint）。记录 attempts / 结论 /
耗时，并从副本库的 llm_call 汇总 token。用法：
    .venv/bin/python tests/exp_audit_prod.py [n=3]
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

SID = "s_2571e8f58f50"
TMP = "/tmp/ra_audit_prod.db"
shutil.copy("/tmp/ra_v14_submit.db", TMP)
os.environ["RA_DB_PATH"] = TMP
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer  # noqa: E402

from research_agent.graph.subgraphs import write as W  # noqa: E402

ser = JsonPlusSerializer()
c = sqlite3.connect(ROOT / "data/checkpoints.db")
STATE = None
for typ, blob in c.execute("select type,checkpoint from checkpoints where thread_id=? "
                           "and checkpoint_ns like 'write:%' order by checkpoint_id desc", (SID,)):
    cv = dict(ser.loads_typed((typ, blob))["channel_values"])
    if cv.get("assembled") and cv.get("spec"):
        STATE = cv
        break
assert STATE
STATE["rework_rounds"] = 0
STATE.setdefault("question", "固态电池的产业化进展，以及各家技术路线的分歧点")


def one(i: int) -> dict:
    t0 = time.time()
    out = W.node_audit(dict(STATE))
    ev = out["events"][0]
    a = out["audit"]
    return {"i": i, "sec": round(time.time() - t0, 1), "kind": ev["kind"],
            "attempts": ev.get("attempts"), "need_rework": a.get("need_rework"),
            "findings": len(a.get("findings") or []),
            "high": sorted({(t["task"], t["section"]) for t in a.get("targets") or []}),
            "blocked": len(a.get("blocked") or []), "error": a.get("error")}


with ThreadPoolExecutor(max_workers=N) as ex:
    for r in ex.map(one, range(N)):
        print(r, flush=True)
d = sqlite3.connect(TMP)
print("llm_call write_audit:", d.execute(
    "select status,output_tokens,latency_ms,model from llm_call where stage='write_audit' "
    "and created_at > datetime('now','-1 hour') order by rowid").fetchall())
