"""诊断：v14b 重跑里 t2「交卷但正文全空」的机制。

用 v14b 的写作子图最终状态里的 t2 任务，直接跑生产 `_run_task` × N，
并在 `_minimax_once`（payload 定型处）记录每次调用的形状与返回：
tool_choice / max_tokens / stop_reason / output_tokens / 交卷参数各字段长度。
在 DB 副本上跑。用法：.venv/bin/python tests/exp_t2_empty.py [n=6] [task=t2]
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import threading
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
TMP = "/tmp/ra_t2_probe.db"
shutil.copy("/tmp/ra_v14_submit.db", TMP)
os.environ["RA_DB_PATH"] = TMP
N = int(sys.argv[1]) if len(sys.argv) > 1 else 6
TID = sys.argv[2] if len(sys.argv) > 2 else "t2"

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer  # noqa: E402

from research_agent.graph.subgraphs import write as W  # noqa: E402

ser = JsonPlusSerializer()
c = sqlite3.connect(ROOT / "data/checkpoints.db")
STATE = None
for typ, blob in c.execute("select type,checkpoint from checkpoints where thread_id=? "
                           "and checkpoint_ns like 'write:%' order by checkpoint_id desc", (SID,)):
    cv = dict(ser.loads_typed((typ, blob))["channel_values"])
    if cv.get("assembled") and cv.get("tasks"):
        STATE = cv
        break
TASK = next(t for t in STATE["tasks"] if t["id"] == TID)
print("task", TID, TASK["covers"], len(TASK["segments"]), "段", flush=True)

_tl = threading.local()
_orig = W.LLM._minimax_once


def _spy(cfg, url, headers, payload):
    status, body, parsed = _orig(cfg, url, headers, payload)
    rec = {"stage": cfg.stage, "tool_choice": bool(payload.get("tool_choice")),
           "max_tokens": payload.get("max_tokens"),
           "thinking": (payload.get("thinking") or {}).get("type"),
           "status": status}
    if status == 200:
        rec["stop"] = parsed.get("stop_reason")
        rec["out"] = (parsed.get("usage") or {}).get("output_tokens")
        bl = []
        for b in parsed.get("content") or []:
            t = b.get("type")
            if t == "tool_use":
                inp = b.get("input")
                if isinstance(inp, dict):
                    secs = inp.get("sections")
                    bl.append(("tool_use", b.get("name"), "keys=" + ",".join(inp),
                               "sections=" + (str(len(secs)) if isinstance(secs, list) else type(secs).__name__),
                               "bodies=" + str([len(str((s or {}).get("body") or "")) for s in secs]) if isinstance(secs, list) else "",
                               "notes=" + str(len(str(inp.get("notes") or "")))))
                else:
                    bl.append(("tool_use", b.get("name"), type(inp).__name__, len(str(inp))))
            elif t == "text":
                bl.append(("text", len(b.get("text") or "")))
            elif t == "thinking":
                bl.append(("thinking", len(b.get("thinking") or "")))
        rec["blocks"] = bl
    getattr(_tl, "calls", []).append(rec)
    return status, body, parsed


W.LLM._minimax_once = _spy


def one(i):
    _tl.calls = []
    meta = {}
    try:
        secs, err = W._run_task(STATE, TASK, None)
    except Exception as e:  # noqa: BLE001
        secs, err = {}, f"EXC {type(e).__name__}: {e}"[:200]
    return {"i": i, "ok": bool(secs), "err": err,
            "chars": {k: len(v) for k, v in (secs or {}).items()}, "calls": _tl.calls}


with ThreadPoolExecutor(max_workers=N) as ex:
    for r in ex.map(one, range(N)):
        print(json.dumps({k: v for k, v in r.items() if k != "calls"}, ensure_ascii=False), flush=True)
        for cl in r["calls"]:
            print("    ", cl, flush=True)
