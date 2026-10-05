"""并发建缓存的行为探测。

write 环节要并发扇出 12 个子 agent，它们共享同一段前缀。
如果并发请求各自建缓存（而不是先建后读），那么扇出时会有 12 次全额计费，
缓存等于没用；只有「先串行建一次、其余并发读」才划算。

本测试发 N 个完全相同前缀的并发请求，看 cache_creation / cache_read 的分布。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

KEY = os.environ["MINIMAX_API_KEY"]
URL = "https://api.minimaxi.com/anthropic/v1/messages"
M31 = "MiniMax-M3.1-Flash-Preview"

c = sqlite3.connect("/tmp/ra_e2e7.db")
rows = [r[0][:300] for r in c.execute(
    "SELECT content FROM chunk WHERE session_id IS NOT NULL LIMIT 40")]
PREFIX = "\n\n".join([f"[摘要{i}] {t}" for i, t in enumerate(rows)])


def one(i: int) -> dict:
    payload = {
        "model": M31, "max_tokens": 128, "temperature": 0.0,
        "system": [{"type": "text", "text": PREFIX}],
        "messages": [{"role": "user", "content": f"把第 {i} 段摘要压缩成一句话。"}],
    }
    t0 = time.time()
    try:
        r = httpx.post(URL, headers={"x-api-key": KEY,
                                    "Authorization": f"Bearer {KEY}",
                                    "Content-Type": "application/json",
                                    "anthropic-version": "2023-06-01"},
                       json=payload, timeout=180.0)
    except Exception as e:
        return {"i": i, "err": type(e).__name__}
    if r.status_code != 200:
        return {"i": i, "err": f"HTTP {r.status_code}"}
    u = r.json().get("usage") or {}
    return {"i": i, "sec": round(time.time() - t0, 2),
            "in": u.get("input_tokens"),
            "create": u.get("cache_creation_input_tokens"),
            "read": u.get("cache_read_input_tokens")}


def report(title: str, fn) -> None:
    print(f"\n=== {title} ===")
    for r in fn():
        if "err" in r:
            print(f"  #{r['i']} ✗ {r['err']}")
        else:
            print(f"  #{r['i']} {r['sec']:>6.2f}s  in={r['in']:<6} "
                  f"建缓存={r['create']:<6} 读缓存={r['read']}")
    cre = sum(1 for r in fn.__self__ if False)  # 占位，避免重复执行


# 第一步：清空缓存状态（换个前缀）
PREFIX = "热身专用前缀 " * 2000
one(0)   # 这一次把缓存建起来
time.sleep(1)

# 第二步：8 个完全并发的请求，前缀已存在
print(f"固定前缀 {len(PREFIX)} 字符")
print("\n=== 8 个并发请求（缓存已由上一次预热建立）===")
t0 = time.time()
with ThreadPoolExecutor(max_workers=8) as ex:
    for r in ex.map(one, range(1, 9)):
        print(f"  #{r['i']} {r['sec']:>6.2f}s  in={r['in']:<6} "
              f"建缓存={r['create']:<6} 读缓存={r['read']}")
print(f"  墙钟 {time.time()-t0:.1f}s")

# 第三步：换一个全新前缀，8 个并发，观察是否各自建缓存
PREFIX = "全新前缀-并发建缓存测试 " * 2000
print("\n=== 8 个并发请求（全新前缀，缓存尚不存在）===")
t0 = time.time()
with ThreadPoolExecutor(max_workers=8) as ex:
    for r in ex.map(one, range(1, 9)):
        print(f"  #{r['i']} {r['sec']:>6.2f}s  in={r['in']:<6} "
              f"建缓存={r['create']:<6} 读缓存={r['read']}")
print(f"  墙钟 {time.time()-t0:.1f}s")
