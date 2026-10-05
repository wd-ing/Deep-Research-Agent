"""Agnes thinking 开关的精确探测（probe_agnes.py 的第二版）。

第一版测出什么
--------------
两个模型都通（2.5-flash 6.6s / 3.0-flash 1.8s）。
但用一道几何题测 thinking 时，「不传」和 `enable_thinking=false` 的正文长度
几乎一样（3488 / 3460 字），而且**都打满 max_tokens=2048**；
`enable_thinking=true` 更是直接 120 秒 ReadTimeout。

这指向一个很不一样的结论：**默认就在思考，而且思考很可能混在 content 里。**
对 write 阶段是致命的——正文里混着思考过程。

这一版要回答的
--------------
1. **默认状态下 content 里是不是混了思考**（决定能不能直接用）
2. `enable_thinking` 到底认不认（true / false 分别什么行为）
3. 有没有独立的 reasoning / thinking 字段可以摘出来
4. 简单任务 + 小 max_tokens 下的真实输出长度（排除打满上限的干扰）
5. 严格 JSON、工具调用、Messages 格式

所有步骤单独 try，一个失败不毁掉整个探针。

用法：.venv/bin/python tests/probes/probe_agnes_thinking.py
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
MODEL = "agnes-3.0-flash"
AUTH = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def call(label: str, payload: dict, path: str = "/chat/completions",
         headers: dict | None = None, timeout: int = 90) -> dict | None:
    p = {"model": MODEL, **payload}
    t0 = time.time()
    try:
        r = httpx.post(f"{BASE}{path}", json=p, headers=headers or AUTH, timeout=timeout)
    except Exception as e:
        print(f"  {label:<34} ❌ {type(e).__name__} {str(e)[:60]}")
        return None
    dt = round(time.time() - t0, 1)
    try:
        data = r.json()
    except Exception:
        print(f"  {label:<34} HTTP {r.status_code} {dt}s 非 JSON 响应")
        return None
    if r.status_code != 200:
        print(f"  {label:<34} HTTP {r.status_code} {dt}s  {str(data)[:110]}")
        return None
    msg = ((data.get("choices") or [{}])[0].get("message") or {})
    txt = (msg.get("content") or "")
    u = data.get("usage") or {}
    print(f"  {label:<34} {dt:>6}s  content {len(txt):>5} 字  "
          f"out_tok {u.get('completion_tokens', '?')}  "
          f"其它键 {sorted(set(data) - {'id','object','model','choices','usage'}) or '无'}")
    return data


def text_of(data: dict) -> str:
    try:
        return (data["choices"][0]["message"].get("content") or "").strip()
    except Exception:
        return ""


def main() -> None:
    simple = {"messages": [{"role": "user", "content": "中国的首都是哪里？只回答城市名。"}],
              "max_tokens": 128}

    print("=" * 74)
    print("1. 默认状态下 content 里混不混思考（简单任务 + 小 max_tokens）")
    print("=" * 74)
    d = call("默认（不传任何 thinking 参数）", simple)
    if d:
        t = text_of(d)
        print(f"    完整输出：{t[:300]!r}")
        print(f"    长度 {len(t)}，看起来{'像混了思考' if len(t) > 200 else '干净'}")
        msg = d["choices"][0]["message"]
        print(f"    message 的键：{sorted(msg)}")
        print(f"    finish_reason：{d['choices'][0].get('finish_reason')}")
        print(f"    usage：{d.get('usage')}")

    print(f"\n{'='*74}\n2. enable_thinking 真假对比")
    print("=" * 74)
    outs = {}
    for label, extra in [
        ("enable_thinking=False", {"chat_template_kwargs": {"enable_thinking": False}}),
        ("enable_thinking=True", {"chat_template_kwargs": {"enable_thinking": True}}),
        ("thinking.type=disabled", {"thinking": {"type": "disabled"}}),
        ("thinking.type=enabled", {"thinking": {"type": "enabled", "budget_tokens": 1024}}),
        ("reasoning_effort=low", {"reasoning_effort": "low"}),
        ("reasoning_effort=high", {"reasoning_effort": "high"}),
    ]:
        r = call(label, {**simple, **extra})
        if r:
            outs[label] = text_of(r)
    if d:
        outs["默认"] = text_of(d)
    print()
    for k, v in outs.items():
        print(f"    {k:<26} {len(v):>5} 字  {v[:60]!r}")
    uniq = {v for v in outs.values() if v}
    print(f"\n    不同输出数：{len(uniq)}")
    if len(uniq) == 1:
        print("    ⇒ 所有 thinking 参数都被忽略，**只有开/关不是参数而是默认行为**")
    elif len(uniq) <= 2:
        print("    ⇒ 最多两档，与「只有开关没有档位」的判断一致")
    else:
        print("    ⇒ 参数是分档生效的，与判断不符")

    print(f"\n{'='*74}\n3. 严格 JSON（写规约/任务列表全靠它）")
    print("=" * 74)
    task = {"messages": [{"role": "user", "content":
                          '用 JSON 回答：中国的首都是哪里？只输出 {"city": "..."}，不要解释。'}],
            "max_tokens": 256}
    for label, extra in [("裸 JSON", {}),
                         ("response_format=json_object",
                          {"response_format": {"type": "json_object"}})]:
        r = call(label, {**task, **extra})
        if r:
            t = text_of(r)
            try:
                json.loads(t)
                ok = "可直接 parse ✓"
            except Exception as e:
                ok = f"parse 失败（{type(e).__name__}）"
            print(f"      {ok}   {t[:80]!r}")

    print(f"\n{'='*74}\n4. 工具调用（write 阶段全靠它）")
    print("=" * 74)
    tools = [{"type": "function", "function": {
        "name": "read_segments",
        "description": "按段号读取证据段落全文",
        "parameters": {"type": "object", "properties": {
            "segments": {"type": "array", "items": {"type": "string"}}},
            "required": ["segments"]}}}]
    r = call("带 tools", {"messages": [{"role": "user",
                    "content": "请读取段落 S1·5 和 S2·3 的全文，不要自己编内容。"}],
                    "max_tokens": 512, "tools": tools})
    if r:
        calls = (r["choices"][0].get("message", {}).get("tool_calls") or [])
        print(f"      tool_calls {len(calls)} 次")
        for c in calls:
            fn = c.get("function") or {}
            print(f"        {fn.get('name')}({fn.get('arguments')})")
        if not calls:
            print(f"      退回文本：{text_of(r)[:150]!r}")

    print(f"\n{'='*74}\n5. Anthropic Messages 格式（决定用哪套实现）")
    print("=" * 74)
    hdr = {"x-api-key": KEY, "anthropic-version": "2023-06-01",
           "Content-Type": "application/json"}
    r = call("POST /messages", {"max_tokens": 128,
              "messages": [{"role": "user", "content": "中国的首都是哪里？只回答城市名。"}]},
             path="/messages", headers=hdr)
    if r:
        blocks = r.get("content") or []
        print(f"      content 块类型：{[b.get('type') for b in blocks]}")
        for b in blocks:
            if b.get("type") == "text":
                print(f"      文本：{b.get('text','')[:100]!r}")

    print(f"\n{'='*74}\n6. 2.5 与 3.0 的速度/长度对照（同一个几何题）")
    print("=" * 74)
    geo = {"messages": [{"role": "user", "content":
           "正六边形面积 6 cm²，沿对角线分成两个全等三角形，求每个三角形的周长。"
           "给出精确表达式。"}], "max_tokens": 2048}
    for m in ("agnes-2.5-flash", "agnes-3.0-flash"):
        t0 = time.time()
        try:
            r = httpx.post(f"{BASE}/chat/completions", json={"model": m, **geo},
                           headers=AUTH, timeout=150)
            dt = round(time.time() - t0, 1)
            data = r.json()
            msg = ((data.get("choices") or [{}])[0].get("message") or {})
            txt = (msg.get("content") or "")
            u = data.get("usage") or {}
            print(f"  {m:<20} HTTP {r.status_code} {dt:>6}s  content {len(txt):>5} 字  "
                  f"out_tok {u.get('completion_tokens','?')}  "
                  f"finish {data['choices'][0].get('finish_reason')}")
            print(f"      开头：{txt[:160]!r}")
        except Exception as e:
            print(f"  {m:<20} ❌ {type(e).__name__} {str(e)[:70]}")


if __name__ == "__main__":
    main()
