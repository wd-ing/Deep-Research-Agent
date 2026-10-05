"""RPM 10 的窗口到底怎么算：按发送时刻，还是按在途请求数？

为什么必须测
------------
用户问的场景：
  · 第 1 秒发 10 个，第 130 秒结束
  · 第 70 秒再发 10 个，第 200 秒结束
会不会撞 RPM 10？

两种语义导出完全不同的结论：
  A. **按发送时刻**（滚动 60 秒窗口数发送数）→ 不会撞。慢请求不占槽位。
     ⇒ relevance（577 次）和 write（单次 100–130 秒）**可以接入**，
     只是配额用完要等窗口滚过去。
  B. **按在途请求数**（并发上限）→ 会撞。第 70 秒时在途还有 10 个。
     ⇒ 慢环节接不进来。

这决定第二套模型方案里 relevance 和 write 到底能不能用，
**不能靠推理，必须实测**。

之前那次 429 不能当证据：那之前已经先跑了 8 次（1 次可用性 + 7 次 thinking 变体），
第 9、10 次才失败——那更像累计配额用满，而不是并发超限。

测三件事
--------
1. **突发上限**：t=0 连发 12 个（快任务），看第几个开始 429
2. **窗口是否滚动**：等 65 秒后再连发 12 个，看是否恢复
3. **用户的场景**：10 个慢请求在途，第 70 秒再发 10 个，看第二批能不能过

用法：.venv/bin/python tests/probes/probe_agnes_rpm_window.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
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
MODEL = "agnes-3.0-flash"
SENT: list[float] = []          # 全局发送时刻，用来算滚动窗口


def fire(i: int, prompt: str, max_tokens: int, thinking: bool = False) -> dict:
    """发一个请求，记下发送时刻。"""
    payload = {"model": MODEL,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens,
               "chat_template_kwargs": {"enable_thinking": thinking}}
    SENT.append(time.time())
    t0 = time.time()
    try:
        r = httpx.post(f"{BASE}/chat/completions", json=payload,
                       headers=AUTH, timeout=300)
    except Exception as e:
        return {"i": i, "ok": False, "code": type(e).__name__, "dt": time.time() - t0}
    if r.status_code == 200:
        return {"i": i, "ok": True, "code": 200, "dt": time.time() - t0}
    return {"i": i, "ok": False, "code": r.status_code, "dt": time.time() - t0,
            "err": str(r.json())[:120]}


def burst(n: int, label: str, prompt: str, max_tokens: int) -> list[dict]:
    print(f"  {label}：连发 {n} 个")
    res: list[dict] = []
    with ThreadPoolExecutor(max_workers=n) as pool:
        futs = [pool.submit(fire, i, prompt, max_tokens) for i in range(n)]
        for f in futs:
            res.append(f.result())
    ok = [r for r in res if r["ok"]]
    bad = [r for r in res if not r["ok"]]
    print(f"    成功 {len(ok)} / {n}")
    for r in sorted(res, key=lambda x: x["i"]):
        if not r["ok"]:
            print(f"      #{r['i']:<3} ✗ {r['code']}  {r.get('err','')[:90]}")
    return res


def main() -> None:
    t_start = time.time()

    print("=" * 74)
    print("1. 突发上限：连发 12 个快任务")
    print("=" * 74)
    r1 = burst(12, "第一批", "中国的首都是哪里？只回答城市名。", 32)
    ok1 = sum(r["ok"] for r in r1)

    print(f"\n  ⇒ 12 个里有 {ok1} 个成功，{12-ok1} 个被拒")
    if ok1 == 12:
        print("  ⇒ **不是 12 个硬上限**——可能限的是每分钟，且窗口按发送时刻滚动")
    elif ok1 == 10:
        print("  ⇒ **确认：滚动 60 秒内最多 10 个，按发送时刻计**")
    else:
        print(f"  ⇒ 成功数是 {ok1}，既不是 10 也不是 12，需要更多数据")

    print(f"\n{'='*74}\n2. 等 65 秒让窗口滚过去，再连发 12 个")
    print("=" * 74)
    wait = 65 - (time.time() - t_start)
    if wait > 0:
        print(f"  等待 {wait:.0f} 秒…")
        time.sleep(wait)
    r2 = burst(12, "第二批（窗口已滚过）", "日本的首都是哪里？只回答城市名。", 32)
    ok2 = sum(r["ok"] for r in r2)
    print(f"\n  ⇒ 第二批成功 {ok2} / 12")
    if ok2 >= 10:
        print("  ⇒ 窗口确实在滚动：上一批用掉的额度会还回来")

    print(f"\n{'='*74}\n3. 用户的场景：10 个慢请求在途，第 70 秒再发 10 个")
    print("=" * 74)
    slow = ("正六边形面积 6 cm²，沿对角线分成两个全等三角形，求每个三角形的周长。"
            "给出精确表达式。")
    print("  发出 10 个慢请求（预计 100 秒以上），让它们一直挂在途…")
    slow_res: list[dict] = []
    inflight = {"n": 0}
    lock = threading.Lock()

    def slow_fire(i: int) -> None:
        with lock:
            inflight["n"] += 1
        r = fire(i, slow, 4096, thinking=True)
        with lock:
            inflight["n"] -= 1
            slow_res.append(r)

    with ThreadPoolExecutor(max_workers=10) as pool:
        for i in range(10):
            pool.submit(slow_fire, i)
        time.sleep(70)
        ok_slow = sum(r["ok"] for r in slow_res)
        # ⚠️ 必须先确认「确实有在途请求」，否则这个测试没有意义。
        # 第一版就是死在这里：慢请求 2 秒内全挂，在途是 0，
        # 脚本却按 0 解读成「第一批跑完了」，得出了完全错误的结论。
        with lock:
            still = inflight["n"]
        print(f"  第 70 秒：慢请求已回 {ok_slow} 个（成功），仍在途 {still} 个")
        if still == 0:
            print("  ⇒ **本次没有在途请求，这一段测不出东西，跳过**")
            print(f"     慢请求成功 {ok_slow}/10；失败原因："
                  f"{[r.get('err', r.get('code')) for r in slow_res if not r['ok']][:3]}")
            return
        print("  ⇒ 现在再连发 10 个（此时确有慢请求挂在途）")
        r3 = burst(10, "第二批（确有在途请求）", slow, 4096)
    ok3 = sum(r["ok"] for r in r3)
    print(f"\n  ⇒ 确有在途慢请求时，第二批成功 {ok3} / 10")
    if ok3 == 10:
        print("  ⇒ **按发送时刻计，不按在途计**。慢请求不占配额槽位。")
        print("     ⇒ relevance 与 write 阶段**可以接入**，配额用完等窗口滚过去即可。")
    else:
        print("  ⇒ 在途请求确实占配额，属于并发上限语义")
        print("     ⇒ 慢环节（write_audit 单次 130 秒）接不进来")

    print(f"\n  慢请求最终结果：成功 {sum(r['ok'] for r in slow_res)}/10，"
          f"耗时 {[round(r['dt']) for r in sorted(slow_res, key=lambda x: x['i'])[:3]]}…")

    print(f"\n{'='*74}\n结论")
    print("=" * 74)
    print(f"  突发 12 个 → 成功 {ok1}")
    print(f"  滚窗后 12 个 → 成功 {ok2}")
    print(f"  在途有慢请求时发 10 个 → 成功 {ok3}")
    if ok3 == 10 and ok1 <= 10:
        print("\n  **限流按发送时刻的滚动窗口计，慢请求不占槽位。**")
        print("  推论：relevance（577 次）与 write 阶段（单次 100–130 秒）")
        print("  都能接入 Agnes，只需要一个令牌桶把发送速率压到 10 次/分钟。")


if __name__ == "__main__":
    from concurrent.futures import ThreadPoolExecutor
    main()
