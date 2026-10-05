"""agnes-3.0-flash 开思考到底能不能用——是预算不够还是根本不支持。

上一轮探针测到（一次观测，不能当结论）：
    agnes-3.0-flash  enable_thinking=true  max_tokens=2048
      → content 0 字，usage {reasoning_tokens: 2048, text_tokens: 0}，finish=length
    agnes-2.5-flash  同样参数
      → content 849 字，{reasoning_tokens: 977, text_tokens: 519}，finish=stop

「3.0 思考起来停不下来」和「3.0 只是需要更大预算」是两个完全不同的结论：
前者说明 3.0 开思考不可用，后者说明 2048 这个预算值配错了。
**这两者会导出两套完全不同的第二套模型方案，所以必须测清楚。**

测的东西
--------
1. 3.0 开思考 + 递增的 max_tokens（2048 / 4096 / 8192 / 16384 / 32768）
2. 3.0 不开思考的同一道题（对照：是不是思考把预算吃光了）
3. 2.5 开思考 + 同样的大预算（2.5 会不会也烧光）
4. 一次更容易的题：看是不是「难题才触发长思考」

用法：.venv/bin/python tests/probes/probe_agnes3_think_budget.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import httpx  # noqa: E402

BASE = "https://apihub.agnes-ai.com/v1"
AUTH = {"Authorization": f"Bearer {os.environ['AGNES_API_KEY']}",
        "Content-Type": "application/json"}

HARD = ("正六边形面积 6 cm²，沿对角线分成两个全等三角形，求每个三角形的周长。"
        "给出精确表达式。")
EASY = "一个正方形的边长是 3 米，求它的面积。只回答数字和单位。"


def run(model: str, thinking: bool, max_tokens: int, task: str, gap: int = 10):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": task}],
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": thinking},
    }
    t0 = time.time()
    try:
        r = httpx.post(f"{BASE}/chat/completions", json=payload,
                       headers=AUTH, timeout=240)
    except Exception as e:
        print(f"  {model} think={thinking!s:<5} max={max_tokens:<6} ❌ {type(e).__name__}")
        time.sleep(gap)
        return None
    dt = round(time.time() - t0, 1)
    if r.status_code != 200:
        print(f"  {model} think={thinking!s:<5} max={max_tokens:<6} HTTP {r.status_code}"
              f"  {str(r.json())[:80]}")
        time.sleep(gap)
        return None
    d = r.json()
    ch = (d.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    txt = msg.get("content") or ""
    u = d.get("usage") or {}
    det = u.get("completion_tokens_details") or {}
    print(f"  {model} think={thinking!s:<5} max={max_tokens:<6} {dt:>6}s  "
          f"正文 {len(txt):>5} 字  reason {det.get('reasoning_tokens','?'):>6}  "
          f"text {det.get('text_tokens','?'):>6}  finish {ch.get('finish_reason')}")
    time.sleep(gap)
    return {"text": txt, "reason": det.get("reasoning_tokens", 0),
            "finish": ch.get("finish_reason"), "dt": dt}


def main() -> None:
    print("=" * 80)
    print("1. agnes-3.0-flash 开思考 × 递增预算（难题）")
    print("=" * 80)
    results = {}
    for mt in (2048, 4096, 8192, 16384, 32768):
        results[mt] = run("agnes-3.0-flash", True, mt, HARD)
        if results[mt] and results[mt]["text"]:
            break

    print(f"\n{'='*80}\n2. agnes-3.0-flash 不开思考（同一道难题，对照）")
    print("=" * 80)
    off = run("agnes-3.0-flash", False, 2048, HARD)
    if off:
        print(f"      开头：{off['text'][:180]!r}")

    print(f"\n{'='*80}\n3. agnes-2.5-flash 开思考 × 预算（难题）")
    print("=" * 80)
    for mt in (2048, 8192):
        r = run("agnes-2.5-flash", True, mt, HARD)
        if r and mt == 2048:
            print(f"      开头：{r['text'][:180]!r}")

    print(f"\n{'='*80}\n4. 简单题：3.0 开思考会不会也烧光")
    print("=" * 80)
    for model in ("agnes-3.0-flash", "agnes-2.5-flash"):
        r = run(model, True, 2048, EASY)
        if r:
            print(f"      回答：{r['text'][:120]!r}")

    print(f"\n{'='*80}\n结论")
    print("=" * 80)
    for mt, r in results.items():
        if r is None:
            print(f"  3.0 开思考 max={mt:<6} 调用失败")
        elif not r["text"]:
            print(f"  3.0 开思考 max={mt:<6} 思考烧光（reason {r['reason']}，正文空）")
        else:
            print(f"  3.0 开思考 max={mt:<6} ✓ 出正文了（reason {r['reason']}）")
            break
    print("\n  判读：")
    print("  · 若某个预算下 3.0 出了正文 → 是预算问题，方案里给 3.0 配大 max_tokens 即可")
    print("  · 若一路到 32768 都空      → 3.0 开思考不可用，第二套里 3.0 只能关思考用")


if __name__ == "__main__":
    main()
