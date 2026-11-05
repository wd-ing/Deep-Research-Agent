"""诊断 2：write_audit 关思考后，能不能用？

承接 exp_audit_think.py（开思考基线：输出 1.6~2.5 万 token、217~349s、
结论不稳、1/5 JSON 解析失败）。同一份 v14 稿子、同一个 payload，
两个变体都 thinking=disabled、max_tokens=16384：

  V1  强制工具 submit_audit（tool_choice 指名，提示词点名工具）
  V2  自由文本 JSON（现行格式，只是关了思考）

对照项：output_tokens / 耗时 / 是否真调用了工具（V1）/ 结果是否可解析 /
findings 与 high 清单 / need_rework。内容正确性靠人读（见 /tmp/exp_audit2）。

用法：.venv/bin/python tests/experiments/exp_audit_nothink.py [n=5]
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

for _line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        k, v = _line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

SID = "s_2571e8f58f50"
os.environ["RA_DB_PATH"] = "/tmp/ra_v14_submit.db"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 5
BUDGET = 16384

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer  # noqa: E402

from research_agent.graph.subgraphs import write as W  # noqa: E402
from research_agent.llm.provider import _parse_json  # noqa: E402

OUT = Path("/tmp/exp_audit2")
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
assert STATE
STATE["rework_rounds"] = 0
STATE.setdefault("question", "固态电池的产业化进展，以及各家技术路线的分歧点")

got: dict = {}


class _Stop(Exception):
    pass


class _Cap:
    registry = W.LLM.registry

    def complete_json(self, stage, prompt, system=None):
        got.update(prompt=prompt, system=system)
        raise _Stop


_real = W.LLM
W.LLM = _Cap()
try:
    W.node_audit(STATE)
except _Stop:
    pass
W.LLM = _real
PROMPT, SYSTEM = got["prompt"], got["system"]

cfg = _real.registry.stage("write_audit")
key = os.environ[cfg.api_key_env]
URL = f"{cfg.base_url.rstrip('/')}/v1/messages"
HEAD = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "x-api-key": key}

_STR = {"type": "string"}
SUBMIT_AUDIT = {
    "name": "submit_audit",
    "description": "审完后调用它提交审查结论。结论只放在参数里，对话文本里不要写 JSON。",
    "input_schema": {
        "type": "object",
        "properties": {
            "rework_check": {"type": "array", "items": {"type": "object", "properties": {
                "task": _STR, "section": _STR,
                "status": {"type": "string", "enum": ["fixed", "still_there", "changed_form"]}}}},
            "need_rework": {"type": "boolean"},
            "reason": _STR,
            "spec_issue": {"type": ["string", "null"]},
            "findings": {"type": "array", "items": {"type": "object", "properties": {
                "task": _STR, "section": _STR,
                "severity": {"type": "string", "enum": ["high", "mid", "low"]},
                "problem": _STR, "fix": _STR},
                "required": ["task", "section", "severity", "problem", "fix"]}},
            "blocked": {"type": "array", "items": {"type": "object", "properties": {
                "task": _STR, "section": _STR,
                "kind": {"type": "string", "enum": ["no_material", "bad_assignment", "spec_conflict"]},
                "issue": _STR}}},
        },
        "required": ["need_rework", "reason", "findings", "blocked"],
    },
}


def run(variant: str, i: int) -> dict:
    payload = {"model": cfg.model, "max_tokens": BUDGET, "temperature": cfg.temperature,
               "system": SYSTEM, "stream": True, "thinking": {"type": "disabled"}}
    if variant == "V1":
        payload["messages"] = [{"role": "user", "content":
            PROMPT + "\n\n审完后**调用 `submit_audit` 工具**提交结论，"
                     "结论只放在工具参数里，不要在对话文本里写 JSON。"}]
        payload["tools"] = [SUBMIT_AUDIT]
        payload["tool_choice"] = {"type": "tool", "name": "submit_audit"}
    else:
        payload["messages"] = [{"role": "user", "content":
            PROMPT + "\n\n只输出 JSON，不要任何解释文字或代码围栏。"}]
    t0 = time.time()
    try:
        status, body, parsed = _real._minimax_once(cfg, URL, HEAD, payload)
    except Exception as e:  # noqa: BLE001
        return {"v": variant, "i": i, "error": f"{type(e).__name__}: {e}"[:200]}
    if status != 200:
        return {"v": variant, "i": i, "error": f"HTTP {status}: {body[:200]}"}
    blocks = parsed.get("content") or []
    usage = parsed.get("usage") or {}
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
    think = "".join(b.get("thinking", "") for b in blocks if b.get("type") == "thinking")
    tool = next((b for b in blocks if b.get("type") == "tool_use"), None)
    d, via, err = None, None, ""
    try:
        if tool is not None:
            inp = tool.get("input")
            d, via = (json.loads(inp) if isinstance(inp, str) else inp), "tool"
        else:
            d, via = _parse_json(text), "text"
    except Exception as e:  # noqa: BLE001
        err = str(e)[:100]
    r = {"v": variant, "i": i, "sec": round(time.time() - t0, 1),
         "out": usage.get("output_tokens"), "think_chars": len(think),
         "stop": parsed.get("stop_reason"), "via": via or ("unparsed:" + err),
         "need_rework": (d or {}).get("need_rework"),
         "findings": len((d or {}).get("findings") or []) if d else None,
         "high": [(f.get("task"), f.get("section")) for f in (d or {}).get("findings") or []
                  if f.get("severity") == "high"] if d else None,
         "blocked": len((d or {}).get("blocked") or []) if d else None}
    (OUT / f"{variant}_{i}.json").write_text(json.dumps(
        {**r, "text": text, "tool_input": tool and tool.get("input"), "parsed": d},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return r


jobs = [(v, i) for v in ("V1", "V2") for i in range(N)]
with ThreadPoolExecutor(max_workers=5) as ex:
    for r in ex.map(lambda a: run(*a), jobs):
        print(r, flush=True)
