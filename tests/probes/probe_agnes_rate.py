"""慢速复测：搞清 Agnes 免费额度的限流策略，并补齐没测完的项。

为什么要慢
----------
第一版把限流打满了（HTTP 429 "You’ve reached the API rate limit for free users"）。
这直接关系到「第二套模型能不能当降级通道」：
如果主模型挂掉的时候降级通道也在限流，那这套方案在最需要的时候没用。
**所以限流策略是必须先搞清楚的前提，不是附注。**

已知（第一版与第二版探针）
--------------------------
- 两个模型都通，2–4 秒返回
- 默认输出干净（'北京'，2 字），content 里没混思考——我第一版的猜测是错的
- `enable_thinking` 是有效开关：True → out_tok 45，False → out_tok 2，
  **默认就是关闭**
- `reasoning_effort` low / high 的 out_tok 都是 21，**不分档**（用户观察正确）
- `thinking.type` enabled / disabled 也被识别（32 / 24）

这一版测的
----------
1. 限流是 QPS 还是每分钟？单发能不能过？
2. 严格 JSON：有没有 response_format，裸 JSON 一次能成吗
3. 工具调用能不能用
4. Anthropic Messages 格式通不通（决定用哪套实现）
5. 同一个难题下 2.5 / 3.0 的实际思考量与答案质量

用法：.venv/bin/python tests/probes/probe_agnes_rate.py [间隔秒数]
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
AUTH = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
GAP = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0


def call(label: str, model: str, payload: dict, path: str = "/chat/completions",
         headers: dict | None = None, timeout: int = 180):
    t0 = time.time()
    try:
        r = httpx.post(f"{BASE}{path}", json={"model": model, **payload},
                       headers=headers or AUTH, timeout=timeout)
    except Exception as e:
        print(f"  {label:<30} ❌ {type(e).__name__} {str(e)[:60]}")
        return None
    dt = round(time.time() - t0, 1)
    try:
        data = r.json()
    except Exception:
        print(f"  {label:<30} HTTP {r.status_code} {dt}s 非 JSON")
        return None
    if r.status_code != 200:
        err = (data.get("error") or {}).get("message", str(data))[:90]
        print(f"  {label:<30} HTTP {r.status_code} {dt}s  {err}")
        return None
    msg = ((data.get("choices") or [{}])[0].get("message") or {})
    txt = (msg.get("content") or "")
    u = data.get("usage") or {}
    print(f"  {label:<30} {dt:>6}s  content {len(txt):>5} 字  "
          f"out_tok {u.get('completion_tokens','?')}")
    return data


def main() -> None:
    M = "agnes-3.0-flash"
    print(f"间隔 {GAP} 秒。\n{'='*74}\n1. 单发能否过限流")
    print("=" * 74)
    d = call("单发（间隔后第一次）", M, {
        "messages": [{"role": "user", "content": "中国的首都是哪里？只回答城市名。"}],
        "max_tokens": 64})
    time.sleep(GAP)

    print(f"\n{'='*74}\n2. 严格 JSON")
    print("=" * 74)
    task = {"messages": [{"role": "user", "content":
           '用 JSON 回答：中国的首都是哪里？只输出 {"city": "..."}，不要解释。'}],
           "max_tokens": 256}
    r = call("裸 JSON", M, task)
    if r:
        t = (r["choices"][0]["message"].get("content") or "").strip()
        try:
            json.loads(t)
            print(f"      ✓ 可直接 parse   {t[:80]!r}")
        except Exception as e:
            print(f"      ✗ parse 失败（{type(e).__name__}）  {t[:120]!r}")
    time.sleep(GAP)
    r = call("response_format=json_object", M,
             {**task, "response_format": {"type": "json_object"}})
    if r:
        t = (r["choices"][0]["message"].get("content") or "").strip()
        try:
            json.loads(t)
            print(f"      ✓ 可直接 parse   {t[:80]!r}")
        except Exception as e:
            print(f"      ✗ parse 失败（{type(e).__name__}）  {t[:120]!r}")
    time.sleep(GAP)

    print(f"\n{'='*74}\n3. 工具调用")
    print("=" * 74)
    tools = [{"type": "function", "function": {
        "name": "read_segments", "description": "按段号读取证据段落全文",
        "parameters": {"type": "object", "properties": {
            "segments": {"type": "array", "items": {"type": "string"}}},
            "required": ["segments"]}}}]
    r = call("带 tools", M, {"messages": [{"role": "user",
                    "content": "请读取段落 S1·5 和 S2·3 的全文，不要自己编内容。"}],
                    "max_tokens": 512, "tools": tools})
    if r:
        calls = (r["choices"][0].get("message", {}).get("tool_calls") or [])
        print(f"      tool_calls {len(calls)} 次")
        for c in calls:
            fn = c.get("function") or {}
            print(f"        {fn.get('name')}({fn.get('arguments')})")
        if not calls:
            print(f"      退回文本：{(r['choices'][0]['message'].get('content') or '')[:150]!r}")
    time.sleep(GAP)

    print(f"\n{'='*74}\n4. Anthropic Messages 格式")
    print("=" * 74)
    r = call("POST /messages", M, {"max_tokens": 128, "messages": [
        {"role": "user", "content": "中国的首都是哪里？只回答城市名。"}]},
        path="/messages",
        headers={"x-api-key": KEY, "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"})
    if r:
        blocks = r.get("content") or []
        print(f"      content 块类型：{[b.get('type') for b in blocks]}")
        for b in blocks:
            if b.get("type") == "text":
                print(f"      文本：{b.get('text','')[:80]!r}")
    time.sleep(GAP)

    print(f"\n{'='*74}\n5. 难题对照：2.5 vs 3.0（开思考）")
    print("=" * 74)
    geo = {"messages": [{"role": "user", "content":
            "正六边形面积 6 cm²，沿对角线分成两个全等三角形，求每个三角形的周长。"
            "给出精确表达式。"}],
           "max_tokens": 2048,
           "chat_template_kwargs": {"enable_thinking": True}}
    for m in ("agnes-3.0-flash", "agnes-2.5-flash"):
        r = call(m, m, geo)
        if r:
            t = r["choices"][0]["message"].get("content") or ""
            u = r.get("usage") or {}
            print(f"      开头：{t[:200]!r}")
            print(f"      finish={r['choices'][0].get('finish_reason')}  usage={u}")
        time.sleep(GAP)

    print(f"\n{'='*74}\n6. 限流窗口探测：连发 3 次看第几次开始 429")
    print("=" * 74)
    for i in range(3):
        d = call(f"连发第 {i+1} 次", M, {
            "messages": [{"role": "user", "content": f"说出第 {i+1} 个中国的省级行政区名。"}],
            "max_tokens": 32})
        if d is None:
            print("      ↑ 开始限流")
            break
        time.sleep(1.5)


if __name__ == "__main__":
    main()
