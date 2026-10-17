"""形态 B 的决定性实验：抬高预算 vs 压低思考量，到底哪个管用。

背景（2026-10-17）
------------------
`write_plan` 在预算 16,384 下实测 **成功 1/6**。六次的原始数据：

    1: content    0 / reasoning 44595 / finish=length / 166.5s
    2: content    0 / reasoning 23483 / finish=length / 152.7s
    3: content    0 / reasoning 22798 / finish=length / 156.3s
    4: content    0 / reasoning 44712 / finish=length / 187.4s
    5: content 2843 / reasoning     0 / finish=stop    /  14.8s   ← 唯一成功
    6: content    0 / reasoning 26517 / finish=length / 179.0s

⚠️ **唯一成功的那次 reasoning = 0，只花了 14.8 秒**；
五次失败全是思考 22,798~44,712 字然后撞 `finish=length`。

「抬预算」这条路的实测记录（说明它**只是把墙往后挪**）：
    write_plan   4,096 → 16,384：成功 2/3（当时以为好了）
    write_plan  16,384（本轮）  ：成功 1/6，又撞上限
    write_audit  8,192 → 16,384：两次全空，reasoning 占 16384/16384

所以要分清两件事：
    · 预算是**上限**，不是思考档位 —— 抬它能不能救，取决于「思考量是否会随预算一起涨」
    · 若模型在这个平台上**思考量本来就远超预算**，再抬预算只是换个数字撞墙

**本脚本把这两条路正面对撞**，同一份载荷、同一模型，只变一个变量：

    A  effort=high   预算  16,384   ← 基线（已知 1/6）
    B  effort=high   预算  65,536   ← 「抬预算」这条路
    C  effort=medium 预算  16,384   ← 「压思考」这条路
    D  effort=low    预算  16,384   ← 同上，更保守
    E  effort=disabled 预算 16,384  ← 关掉思考作对照（写不出质量，但能看机制）

用法：.venv/bin/python tests/probe_plan_tasks_modeB.py [每格次数]
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

for _line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        k, v = _line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import httpx  # noqa: E402
import yaml  # noqa: E402

from research_agent.graph import prompts  # noqa: E402
from research_agent.llm.provider import _sse_lines  # noqa: E402
from probe_plan_tasks_empty import build_prompt  # noqa: E402

CELLS = [
    # 标签,              effort,       预算
    ("A 基线 high",        "high",      16384),
    ("B high + 大预算",    "high",      65536),
    ("C medium",          "medium",    16384),
    ("D low",             "low",       16384),
    ("E 关思考(对照)",     "disabled",  16384),
]


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    cfg = yaml.safe_load((ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))
    prov_name = cfg["defaults"]["provider"]
    p = cfg["providers"][prov_name]
    st = cfg["stages"]["write_plan"]
    key = os.environ.get(p["api_key_env"], "")
    msgs = [{"role": "system", "content": prompts.WRITE_PLAN_TASKS},
            {"role": "user", "content": build_prompt()}]

    print(f"=== write_plan 形态 B 消融  {p['model']} @ {prov_name}  每格 n={n} ===")
    print("同一份载荷，只变 effort 与预算\n")

    rows = {}
    with httpx.Client(trust_env=False,
                      headers={"Authorization": f"Bearer {key}",
                               "Content-Type": "application/json"},
                      timeout=600.0) as c:
        for label, effort, budget in CELLS:
            eff = None if effort == "disabled" else effort
            body = {"model": p["model"], "messages": msgs,
                    "max_tokens": budget, "temperature": st["temperature"],
                    "response_format": {"type": "json_object"},
                    "stream": True, "stream_options": {"include_usage": True}}
            if eff:
                body["reasoning_effort"] = eff
            res = []
            for i in range(n):
                t0 = time.time()
                text = reason = fin = ""
                used = None
                try:
                    with c.stream("POST",
                                  f"{p['base_url'].rstrip('/')}/chat/completions",
                                  json=body) as r:
                        r.raise_for_status()
                        for obj in _sse_lines(r):
                            if obj.get("usage"):
                                used = obj["usage"]
                            for ch in obj.get("choices") or []:
                                d = ch.get("delta") or {}
                                text += d.get("content") or ""
                                reason += d.get("reasoning_content") or ""
                                fin = ch.get("finish_reason") or fin
                except Exception as e:
                    # 平台故障/限流**不计入成功率** —— 混进来会把基础设施问题
                    # 算成模型问题（坑 33 的同款）
                    print(f"  {label:<16} #{i+1} ⚠️  {type(e).__name__}: {str(e)[:70]}")
                    time.sleep(5)
                    continue
                ok = bool(text.strip())
                res.append({"ok": ok, "content": len(text), "reasoning": len(reason),
                            "finish": fin, "t": round(time.time() - t0, 1),
                            "completion_tokens": (used or {}).get("completion_tokens"),
                            "reasoning_tokens": ((used or {}).get("completion_tokens_details")
                                                 or {}).get("reasoning_tokens")})
                print(f"  {label:<16} #{i+1} {'✅' if ok else '❌'} "
                      f"content {len(text):>5} / reasoning {len(reason):>6} / "
                      f"finish={fin or '-':<7} / {time.time()-t0:6.1f}s / "
                      f"tokens={res[-1]['completion_tokens']}"
                      + (f" (思考token {res[-1]['reasoning_tokens']})"
                         if res[-1]["reasoning_tokens"] is not None else ""))
                time.sleep(2)
            rows[label] = res

    print("\n=== 汇总（先看组内方差，再谈差别）===")
    print(f"{'格':<16}{'成功':<8}{'思考字数中位':<16}{'思考token中位':<16}{'正文中位':<12}{'耗时中位'}")
    for label, _, _ in CELLS:
        rs = rows[label]
        ok = sum(1 for r in rs if r["ok"])
        if not rs:
            print(f"{label:<16}(全失败)")
            continue
        rc = [r["reasoning"] for r in rs]
        rt = [r["reasoning_tokens"] for r in rs if r["reasoning_tokens"] is not None]
        tx = [r["content"] for r in rs]
        ts = [r["t"] for r in rs]
        med = lambda a: f"{statistics.median(a):.0f}" if a else "-"
        print(f"{label:<16}{ok}/{len(rs):<6}{med(rc):<16}{med(rt):<16}{med(tx):<12}"
              f"{statistics.median(ts):.1f}s")
    print(json.dumps(rows, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
