"""验收：用**生产代码** `write._run_task` 在真实 API 上跑 v13 的真实任务。

和 exp_submit_section.py 的区别：那个是自己拼调用的对照实验（A/B/C/D），
这个直接调生产入口——验证的是「改完之后线上到底怎样」。
输入还原、污染检测沿用 exp_submit_section（同一批任务、同一把尺子）。

用法：.venv/bin/python tests/experiments/exp_submit_prod.py [n=5] [cases=t7,t5r,t2]
"""
from __future__ import annotations

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import exp_submit_section as E  # noqa: E402  (导入即还原输入、拷贝库)

W = E.W
_tl = threading.local()
_real = W.run_tool_loop


def _wrap(llm, stage, system, messages, provider, **kw):
    px = E.Proxy(llm)
    _tl.px = px
    meta = kw.get("meta")
    if meta is None:
        meta = kw["meta"] = {}
    _tl.meta = meta
    return _real(px, stage, system, messages, provider, **kw)


W.run_tool_loop = _wrap

# 实验变体（payload 层改写，线程安全）：
#   EXP_P=P0 现状；P1 非强制轮 max_tokens 压到 EXP_CAP；
#   P2 = P1 + 强制交卷轮在末条 user 消息后追加「额度用完、立即交卷」。
P = os.environ.get("EXP_P", "P0")
CAP = int(os.environ.get("EXP_CAP", "2048"))
NUDGE = ("证据读取额度已用完。不要再调用任何读取工具；"
         "请基于你已经读到的内容，立即调用 submit_section 交卷。")
_orig_once = W.LLM._minimax_once


def _patched_once(cfg, url, headers, payload):
    if cfg.stage in ("write_section", "write_revise"):
        if P in ("P1", "P2") and not payload.get("tool_choice"):
            payload["max_tokens"] = min(payload["max_tokens"], CAP)
        if P == "P2" and payload.get("tool_choice"):
            msgs = [dict(m) for m in payload["messages"]]
            last = msgs[-1]
            if isinstance(last["content"], str):
                last["content"] = last["content"] + "\n\n" + NUDGE
            else:
                last["content"] = list(last["content"]) + [{"type": "text", "text": NUDGE}]
            payload["messages"] = msgs
    return _orig_once(cfg, url, headers, payload)


W.LLM._minimax_once = _patched_once


def one(case: str, i: int) -> dict:
    task, revise = E._case(case)
    t0 = time.time()
    secs, err = W._run_task(E.STATE, task, revise)
    body = "\n\n".join(secs.values())
    px = getattr(_tl, "px", None)
    calls = px.calls if px else []
    return {"case": case, "i": i, "via": (getattr(_tl, "meta", {}) or {}).get("via"),
            "ok": bool(secs), "err": (err or "")[:80], "chars": len(body),
            "flags": E.contam(body) if secs else ["failed"],
            "out_tokens": sum(c["out_tokens"] for c in calls),
            "secs": round(time.time() - t0, 1), "ncalls": len(calls),
            "body": body if (secs and len(body) < 1600) else "",
            "calls": [(c["forced"], c.get("thinking"), [b[1] or b[0] for b in c["blocks"]],
                       c["out_tokens"]) for c in calls]}


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    cases = (sys.argv[2] if len(sys.argv) > 2 else "t7,t5r,t2").split(",")
    jobs = [(c, i) for c in cases for i in range(n)]
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(lambda j: one(*j), jobs))
    for r in res:
        print({k: v for k, v in r.items() if k != "calls"})
        if r.get("body"):
            print("   SHORT BODY:", repr(r["body"][:300]), "...", repr(r["body"][-150:]))
        if not r["ok"] or r["via"] == "forced_retry":
            for c in r["calls"]:
                print("     ", c)
    print()
    for c in cases:
        rs = [r for r in res if r["case"] == c]
        via = {}
        for r in rs:
            via[r["via"]] = via.get(r["via"], 0) + 1
        print(c, "ok", sum(r["ok"] for r in rs), "/", len(rs),
              "clean", sum(1 for r in rs if not r["flags"]), via,
              "avg_tok", sum(r["out_tokens"] for r in rs) // len(rs),
              "avg_s", round(sum(r["secs"] for r in rs) / len(rs)))


if __name__ == "__main__":
    main()
