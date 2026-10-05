"""探 Agnes 两个模型的可用性与 thinking 开关的真实行为。

测的东西
--------
1. `agnes-2.5-flash` / `agnes-3.0-flash` 能不能调通（Chat Completions）
2. thinking 的**正确参数名与真实效果**。文档给了两种：
   - OpenAI 格式：`chat_template_kwargs: {enable_thinking: true}`
   - Anthropic 格式：`thinking: {type: "enabled", budget_tokens: N}`
   用户实测 low/high 效果一样，怀疑只有开关。**这里要验证是不是真只有开关。**
3. 顺带看：严格 JSON 能不能一次成功（这个端点如果没有 response_format，
   跟 MiniMax 一样只能靠提示词约束，预检必须知道这件事）
4. 工具调用能不能用（write 阶段全靠它）
5. 响应里有没有 usage、耗时、是否流式必需

用法：.venv/bin/python tests/probes/probe_agnes.py
"""

from __future__ import annotations

import json
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
KEY = os.environ["AGNES_API_KEY"]
MODELS = ["agnes-2.5-flash", "agnes-3.0-flash"]
JSON_TASK = "用 JSON 回答：中国的首都是哪里？只输出 {\"city\": \"...\"}，不要任何解释。"


def post(path: str, payload: dict, headers: dict) -> tuple[int, dict, float]:
    t0 = time.time()
    r = httpx.post(f"{BASE}{path}", json=payload, headers=headers, timeout=120)
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json")
                           else {"_raw": r.text[:400]}), round(time.time() - t0, 1)


def text_of(data: dict) -> str:
    try:
        return (data["choices"][0]["message"].get("content") or "").strip()
    except Exception:
        return ""


def main() -> None:
    auth = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    print("=" * 70)
    print("1. 基础可用性（Chat Completions）")
    print("=" * 70)
    alive = []
    for m in MODELS:
        code, data, dt = post("/chat/completions", {
            "model": m, "messages": [{"role": "user", "content": "回复两个字：可用"}],
            "max_tokens": 64}, auth)
        t = text_of(data)
        print(f"  {m:<20} HTTP {code}  {dt:>5}s  输出={t[:40]!r}")
        if code == 200 and t:
            alive.append(m)
    if not alive:
        print("\n❌ 两个模型都调不通，方案到此为止")
        return
    print(f"\n  可用：{alive}")

    m = alive[-1]
    print(f"\n{'='*70}\n2. thinking 的真实行为（模型 {m}）")
    print("=" * 70)
    ask = ("一个正六边形的面积是 6 平方厘米。把它沿着一条对角线分成两个全等三角形，"
           "求出每个三角形的周长。给出精确的边长表达式。")
    variants = [
        ("不开（不传）", {}),
        ("enable_thinking=false", {"chat_template_kwargs": {"enable_thinking": False}}),
        ("enable_thinking=true", {"chat_template_kwargs": {"enable_thinking": True}}),
        ("thinking.type=disabled", {"thinking": {"type": "disabled"}}),
        ("thinking.type=enabled", {"thinking": {"type": "enabled", "budget_tokens": 4096}}),
        ("reasoning_effort=low", {"reasoning_effort": "low"}),
        ("reasoning_effort=high", {"reasoning_effort": "high"}),
        ("output_config.effort=high", {"output_config": {"effort": "high"}}),
    ]
    lens = {}
    for name, extra in variants:
        payload = {"model": m, "messages": [{"role": "user", "content": ask}],
                   "max_tokens": 2048}
        payload.update(extra)
        code, data, dt = post("/chat/completions", payload, auth)
        t = text_of(data)
        u = data.get("usage") or {}
        lens[name] = len(t)
        flag = "" if code == 200 else f"  ← HTTP {code}: {str(data)[:120]}"
        print(f"  {name:<30} HTTP {code} {dt:>6}s  正文 {len(t):>5} 字  "
              f"out_tok {u.get('completion_tokens', '?')}{flag}")
    distinct = len({v for k, v in lens.items() if v > 0})
    print(f"\n  输出长度分布：{ {k: v for k, v in lens.items()} }")
    print(f"  不同长度数：{distinct} —— 只有开/关两档就会是 2；"
          f"若 low/high 有差异则 > 2")

    print(f"\n{'='*70}\n3. 严格 JSON（写规约/任务列表全靠它）")
    print("=" * 70)
    for name, extra in [("裸 JSON", {}),
                        ("response_format", {"response_format": {"type": "json_object"}})]:
        payload = {"model": m, "messages": [{"role": "user", "content": JSON_TASK}],
                   "max_tokens": 256}
        payload.update(extra)
        code, data, dt = post("/chat/completions", payload, auth)
        t = text_of(data)
        ok = False
        try:
            json.loads(t)
            ok = True
        except Exception:
            pass
        print(f"  {name:<20} HTTP {code} {dt:>5}s  可直接 parse={ok}  {t[:60]!r}")

    print(f"\n{'='*70}\n4. 工具调用（write 阶段全靠它）")
    print("=" * 70)
    tools = [{
        "type": "function",
        "function": {
            "name": "read_segments",
            "description": "按段号读取证据段落全文",
            "parameters": {"type": "object",
                           "properties": {"segments": {"type": "array",
                                                       "items": {"type": "string"}}},
                           "required": ["segments"]},
        },
    }]
    code, data, dt = post("/chat/completions", {
        "model": m, "max_tokens": 512,
        "messages": [{"role": "user",
                      "content": "请读取段落 S1·5 和 S2·3 的全文，不要自己编内容。"}],
        "tools": tools}, auth)
    calls = (data.get("choices") or [{}])[0].get("message", {}).get("tool_calls") or []
    print(f"  HTTP {code} {dt}s  工具调用 {len(calls)} 次")
    for c in calls:
        fn = (c.get("function") or {})
        print(f"    {fn.get('name')}({fn.get('arguments')})")
    if not calls:
        print(f"    消息：{str(data.get('choices', [{}])[0].get('message'))[:200]}")

    print(f"\n{'='*70}\n5. Anthropic Messages 格式是否也通（决定用哪套实现）")
    print("=" * 70)
    for name, hdr in [("x-api-key + anthropic-version",
                       {"x-api-key": KEY, "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json"})]:
        code, data, dt = post("/messages", {
            "model": m, "max_tokens": 256,
            "messages": [{"role": "user", "content": "回复两个字：可用"}]}, hdr)
        txt = ""
        try:
            txt = "".join(b.get("text", "") for b in data.get("content") or [])
        except Exception:
            pass
        print(f"  {name:<32} HTTP {code} {dt}s  输出={txt[:30]!r} {str(data)[:100] if not txt else ''}")


if __name__ == "__main__":
    main()
