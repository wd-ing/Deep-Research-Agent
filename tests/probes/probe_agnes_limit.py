"""二分搜 Agnes 免费额度的真实上限。

起因
----
用户告知「免费用户 RPM 10」。但 `probe_agnes_rpm_window.py` 实测
**12 个请求在 3 秒内连发，12 个全部成功**——与「滚动 60 秒内最多 10 次」矛盾。

三种可能，得区分开：
  A. 限额比 10 宽松（比如 20/min）
  B. 窗口按**自然分钟**对齐，不是滚动窗口——那么 3 秒内连发 12 个
     如果跨过了分钟边界就可能全过
  C. 挂的其实是**每日配额**（TPD），429 是在提示日额度用完

**这三种会导出完全不同的工程结论**，所以必须测出来。

测法
----
1. 在**同一个自然分钟内**按不同数量连发，找成功率掉下去的那个数
2. 记录每个请求失败的 429 原文，看它到底在说什么
3. 跨分钟边界再发一次，看额度是否恢复

注意：每次连发都真实消耗配额，所以步长要粗（8/12/16/24…），
不要一格一格试。

用法：.venv/bin/python tests/probes/probe_agnes_limit.py
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
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
PROMPT = "中国的首都是哪里？只回答城市名。"


def fire(i: int) -> dict:
    payload = {"model": MODEL,
               "messages": [{"role": "user", "content": PROMPT}],
               "max_tokens": 16,
               "chat_template_kwargs": {"enable_thinking": False}}
    try:
        r = httpx.post(f"{BASE}/chat/completions", json=payload,
                       headers=AUTH, timeout=60)
    except Exception as e:
        return {"i": i, "ok": False, "code": type(e).__name__}
    if r.status_code == 200:
        return {"i": i, "ok": True, "code": 200}
    return {"i": i, "ok": False, "code": r.status_code,
            "err": (r.json().get("error") or {}).get("message", str(r.json()))[:150]}


def burst(n: int, label: str) -> dict:
    """尽量在**同一个自然分钟内**完成。"""
    now = datetime.now()
    wait = 60 - now.second
    if wait > 3 and now.second > 10:      # 已经过半了，等到下一分钟，保证整批同窗
        print(f"    （当前已过 {now.second} 秒，等 {wait:.0f} 秒到下一分钟，"
              f"保证整批落在同一个自然分钟内）")
        time.sleep(wait)
    t0 = datetime.now()
    with ThreadPoolExecutor(max_workers=min(n, 16)) as pool:
        res = list(pool.map(fire, range(n)))
    span = (datetime.now() - t0).total_seconds()
    ok = sum(r["ok"] for r in res)
    print(f"  {label}  n={n:<3} 成功 {ok:>3}/{n}  整批用时 {span:.1f}s  "
          f"（起始 {t0.strftime('%H:%M:%S.%S')[:8]}）")
    for r in res:
        if not r["ok"]:
            print(f"      ✗ {r['code']}  {r.get('err','')[:120]}")
    return {"n": n, "ok": ok, "res": res}


def main() -> None:
    print("=" * 76)
    print("在同一自然分钟内，按不同数量连发，找成功率掉下去的位置")
    print("=" * 76)
    trials = []
    for n in (8, 12, 20, 30):
        trials.append(burst(n, f"第 {len(trials)+1} 档"))
        time.sleep(2)

    print(f"\n{'='*76}\n结果")
    print("=" * 76)
    for t in trials:
        print(f"  n={t['n']:<4} 成功 {t['ok']:<4} {'全过' if t['ok']==t['n'] else '有拒绝'}")
    first_fail = next((t for t in trials if t["ok"] < t["n"]), None)
    if first_fail:
        print(f"\n  ⇒ 第一个出现拒绝的数量是 {first_fail['n']}，"
              f"成功 {first_fail['ok']} 个 ⇒ **限额在 {first_fail['ok']}–{first_fail['n']} 之间**")
        errs = [r["err"] for r in first_fail["res"] if not r["ok"]]
        if errs:
            print(f"  ⇒ 429 的原文：{errs[0]}")
    else:
        print("\n  ⇒ 30 个连发全过，**限额不在每分钟计数上**——"
              "更像是 TPD（每日配额）或并发之外的某种额度")

    print(f"\n{'='*76}\n跨分钟边界后额度是否恢复")
    print("=" * 76)
    now = datetime.now()
    if now.second > 5:
        w = 61 - now.second
        print(f"  等 {w:.0f} 秒跨过分钟边界…")
        time.sleep(w)
    b = burst(8, "跨分钟后 8 个")
    print(f"\n  ⇒ 跨分钟后成功 {b['ok']}/8")


if __name__ == "__main__":
    main()
