"""诊断：write_audit 在 MiniMax-M3 上为什么思考到上限。

输入：v14 的写作子图 checkpoint（真实规约 + 全稿 + 任务分派），
payload 由生产的 `node_audit` 原样构造（桩住 LLM.complete_json 抓取）。
然后直接对 M3 发 N 次请求，预算放宽到 BUDGET，**不走 guard / 重试**，
分别记下思考与正文的字符数、output_tokens、stop_reason、JSON 是否可解析，
并把完整思考原文落盘，供人读。

用法：
    .venv/bin/python tests/exp_audit_think.py [n=5] [budget=32768]
结果：stdout 汇总 + /tmp/exp_audit/run_<i>.json
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        k, v = _line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

SID = "s_2571e8f58f50"
os.environ["RA_DB_PATH"] = "/tmp/ra_v14_submit.db"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 5
BUDGET = int(sys.argv[2]) if len(sys.argv) > 2 else 32768

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer  # noqa: E402

from research_agent.graph import prompts  # noqa: E402
from research_agent.graph.subgraphs import write as W  # noqa: E402
from research_agent.llm.provider import _parse_json  # noqa: E402

OUT = Path("/tmp/exp_audit")
OUT.mkdir(exist_ok=True)

_ser = JsonPlusSerializer()
_c = sqlite3.connect(ROOT / "data/checkpoints.db")
STATE = None
for typ, blob in _c.execute(
        "select type,checkpoint from checkpoints where thread_id=? and "
        "checkpoint_ns like 'write:%' order by checkpoint_id desc", (SID,)):
    cv = dict(_ser.loads_typed((typ, blob))["channel_values"])
    if cv.get("assembled") and cv.get("spec"):
        STATE = cv
        break
assert STATE, "没找到带 assembled 的写作子图快照"
STATE["rework_rounds"] = 0
STATE.setdefault("question", "固态电池的产业化进展，以及各家技术路线的分歧点")
print(f"state keys={sorted(STATE)}  assembled={len(STATE['assembled'])}字符  "
      f"rework_rounds(原)={STATE.get('rework_rounds')}", flush=True)

got: dict = {}


class _Stop(Exception):
    pass


class _Cap:
    registry = W.LLM.registry

    def complete_json(self, stage, prompt, system=None):
        got.update(stage=stage, prompt=prompt, system=system)
        raise _Stop


_real = W.LLM
W.LLM = _Cap()
try:
    W.node_audit(STATE)
except _Stop:
    pass
W.LLM = _real
assert got, "没抓到 audit 的 payload"
PROMPT, SYSTEM = got["prompt"], got["system"]
print(f"audit payload = {len(PROMPT)} 字符；system = {len(SYSTEM or '')} 字符", flush=True)

cfg = _real.registry.stage("write_audit")
key = os.environ[cfg.api_key_env]
URL = f"{cfg.base_url.rstrip('/')}/v1/messages"
HEAD = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "x-api-key": key}


def one(i: int) -> dict:
    payload = {
        "model": cfg.model, "max_tokens": BUDGET, "temperature": cfg.temperature,
        "system": SYSTEM, "stream": True,
        "thinking": {"type": "adaptive"},
        "messages": [{"role": "user",
                      "content": PROMPT + "\n\n只输出 JSON，不要任何解释文字或代码围栏。"}],
    }
    t0 = time.time()
    try:
        status, body, parsed = _real._minimax_once(cfg, URL, HEAD, payload)
    except Exception as e:  # noqa: BLE001
        return {"i": i, "error": f"{type(e).__name__}: {e}"[:300]}
    if status != 200:
        return {"i": i, "error": f"HTTP {status}: {body[:300]}"}
    blocks = parsed.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
    think = "".join(b.get("thinking", "") for b in blocks if b.get("type") == "thinking")
    usage = parsed.get("usage") or {}
    ok, nfind = False, None
    try:
        d = _parse_json(text)
        ok, nfind = True, len([f for f in d.get("findings") or []])
    except Exception:  # noqa: BLE001
        pass
    r = {"i": i, "sec": round(time.time() - t0, 1), "think_chars": len(think),
         "text_chars": len(text), "out_tokens": usage.get("output_tokens"),
         "stop": parsed.get("stop_reason"), "json_ok": ok, "findings": nfind}
    (OUT / f"run_{i}.json").write_text(json.dumps(
        {**r, "thinking": think, "text": text}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    return r


with ThreadPoolExecutor(max_workers=min(N, 5)) as ex:
    res = list(ex.map(one, range(N)))
for r in res:
    print(r, flush=True)
