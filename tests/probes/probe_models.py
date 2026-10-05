"""OpenCode 三个免费模型的可用性与速度探测。

要验三件事：
  1. 能否从外部直连调用（免费层是否只允许 OpenCode 客户端内部使用）
  2. 是否支持 JSON 模式（抽取与去重环节强依赖）
  3. 速度（首 token 与总耗时）
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

BASE = "https://opencode.ai/zen/v1"
KEY = os.environ.get("OPENCODE_API_KEY", "")
MODELS = [
    "muse-spark-1.3-contributor-free",
    "longcat-2.5-preview-free",
    "ling-3.1-flash-free",
]

JSON_PROMPT = (
    '抽取这句话里的事实，严格输出 JSON，格式为 '
    '{"claims":[{"text": str, "entities": [str]}]}。'
    '句子：JEV 是 TypeSafe AI 于 2026 年 9 月 15 日发布的决策模型。'
)
PLAIN_PROMPT = "用一句话说明什么是元搜索引擎。"


def _post(client, model, payload, session="probe-1"):
    h = {
        "Authorization": f"Bearer {KEY}",
        "Content-Type": "application/json",
        "x-opencode-session": session,
    }
    t0 = time.time()
    r = client.post(f"{BASE}/chat/completions", headers=h, json=payload)
    return r, time.time() - t0


def probe(model: str) -> dict:
    out = {"model": model}
    with httpx.Client(timeout=60.0) as c:
        # 1) 基础可用性
        try:
            r, dt = _post(c, model, {
                "model": model, "max_tokens": 120, "temperature": 0,
                "messages": [{"role": "user", "content": PLAIN_PROMPT}],
            })
            if r.status_code != 200:
                out["status"] = f"HTTP {r.status_code}: {r.text[:160]}"
                return out
            data = r.json()
            txt = (data["choices"][0]["message"].get("content") or "")
            rc = data["choices"][0]["message"].get("reasoning_content")
            out["basic"] = {
                "ok": True,
                "total_s": round(dt, 2),
                "chars": len(txt),
                "has_reasoning": bool(rc),
                "reasoning_chars": len(rc) if rc else 0,
                "usage": data.get("usage", {}),
            }
        except Exception as e:
            out["status"] = f"{type(e).__name__}: {e}"
            return out

        # 2) JSON 模式
        try:
            r2, dt2 = _post(c, model, {
                "model": model, "max_tokens": 400, "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": JSON_PROMPT}],
            })
            if r2.status_code != 200:
                out["json_mode"] = {"ok": False, "err": f"HTTP {r2.status_code}: {r2.text[:120]}"}
            else:
                txt2 = r2.json()["choices"][0]["message"].get("content") or ""
                parsed = None
                try:
                    parsed = json.loads(txt2.strip())
                except Exception:
                    m = txt2.find("{")
                    if m >= 0:
                        try:
                            parsed = json.loads(txt2[m:txt2.rfind("}") + 1])
                        except Exception:
                            parsed = None
                out["json_mode"] = {
                    "ok": parsed is not None,
                    "s": round(dt2, 2),
                    "parsed_keys": list(parsed.keys()) if isinstance(parsed, dict) else None,
                    "claims": len(parsed.get("claims", [])) if isinstance(parsed, dict) else 0,
                    "raw_head": txt2[:120],
                }
        except Exception as e:
            out["json_mode"] = {"ok": False, "err": f"{type(e).__name__}: {e}"}

        # 3) 重复三次测速度稳定性
        times = []
        for i in range(3):
            _, d = _post(c, model, {
                "model": model, "max_tokens": 200, "temperature": 0,
                "messages": [{"role": "user", "content": f"用一句话解释概念{i}。"}],
            })
            times.append(round(d, 2))
        out["speed_runs_s"] = times
    return out


if __name__ == "__main__":
    if not KEY:
        raise SystemExit("未设置 OPENCODE_API_KEY")
    print("=" * 70)
    for m in MODELS:
        r = probe(m)
        print(f"\n### {m}")
        if "status" in r:
            print(f"  ✗ 不可用：{r['status']}")
            continue
        b = r["basic"]
        print(f"  ✓ 直连可用   总耗时 {b['total_s']}s  输出 {b['chars']} 字符")
        print(f"    reasoning_content: {'有' if b['has_reasoning'] else '无'}"
              + (f"（{b['reasoning_chars']} 字符）" if b['has_reasoning'] else ""))
        print(f"    usage: {b['usage']}")
        j = r["json_mode"]
        print(f"  {'✓' if j['ok'] else '✗'} JSON 模式  {j.get('s', j.get('err'))}"
              + (f"  解析出 {j['claims']} 条 claim，顶层键 {j['parsed_keys']}" if j["ok"]
                 else ""))
        if not j["ok"] and "raw_head" in j:
            print(f"    返回片段：{j['raw_head']!r}")
        print(f"  速度三轮：{r['speed_runs_s']}")
