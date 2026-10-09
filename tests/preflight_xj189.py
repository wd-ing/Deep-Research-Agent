"""xj189 / DeepSeek-v4-flash 接入前的**预检**：文档没写明的四件事，自己测出来。

要测什么（按「不确定会不会致命」排）
--------------------------------
1. **严格 JSON** —— 本项目 14 个环节全靠 `complete_json` + `_parse_json`。
   DeepSeek 系历来对 `response_format: json_object` 支持不稳，
   **这条不通就等于整个平台没法用**。
2. **工具调用** —— write 子 agent 靠 `read_segments` / `read_context` /
   `read_source` 往下挖证据。实测要确认三件事：
     a. 返回是不是 OpenAI 标准 `choices[0].message.tool_calls`
     b. **一个回合能不能返回多个**（write_plan 那轮实测一次返回 4 个）
     c. 参数是不是 JSON **字符串**（有些平台直接给对象，会 parse 失败）
3. **reasoning_effort 合法档位** —— 平台文档说「传非法值由上游校验返回错误」，
   所以逐档试，把能用的记下来。
4. **缓存字段** —— 平台说支持缓存，但字段名要实测。
   `_cache_read` 同时认 `cache_read_input_tokens`（Anthropic 系）和
   `prompt_tokens_details.cached_tokens`（OpenAI 系），
   实测确认走的是哪一条，否则缓存命中率会永远显示 0。

用法：
    .venv/bin/python tests/preflight_xj189.py            # 全测
    .venv/bin/python tests/preflight_xj189.py --only json # 只测某项
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import httpx  # noqa: E402

from research_agent.llm.provider import _cache_read  # noqa: E402

BASE = "https://ai.xj.189.cn/api/v1"
MODEL = "deepseek-v4-flash-0731"
KEY = os.environ.get("XJ189_API_KEY", "")
ONLY = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
C = httpx.Client(timeout=180)


def _post(payload: dict) -> tuple[int, dict, float]:
    t0 = time.time()
    r = C.post(f"{BASE}/chat/completions",
               headers={"Authorization": f"Bearer {KEY}",
                        "Content-Type": "application/json"},
               json=payload)
    try:
        return r.status_code, r.json(), time.time() - t0
    except Exception:
        return r.status_code, {"_raw": r.text[:400]}, time.time() - t0


def _base(**kw) -> dict:
    p = {"model": MODEL, "temperature": 0.0, "max_tokens": 2048,
         "messages": [{"role": "user", "content": "说一句话：收到"}]}
    p.update(kw)
    return p


def hdr(t: str) -> None:
    print(f"\n{'=' * 70}\n{t}\n{'=' * 70}")


# ---------------------------------------------------------------- 1 严格 JSON
def test_json() -> None:
    hdr("1. 严格 JSON（response_format: json_object）")
    for use_rf in (True, False):
        kw = {"response_format": {"type": "json_object"}} if use_rf else {}
        code, d, dt = _post(_base(
            messages=[{"role": "system", "content": "只输出 JSON。"},
                      {"role": "user",
                       "content": '输出 {"ok": true, "n": 3, "items": ["a","b"]}'}],
            **kw))
        tag = "带 response_format" if use_rf else "不带（纯提示词约束）"
        if code != 200:
            print(f"  {tag:<22} ✗ HTTP {code}: {str(d.get('_raw') or d)[:120]}")
            continue
        txt = (d["choices"][0]["message"].get("content") or "").strip()
        txt = txt.split("```")[1] if txt.startswith("```") else txt
        try:
            obj = json.loads(txt.replace("json", "").strip() if txt.startswith("```json") else txt)
            ok = obj.get("ok") is True and obj.get("n") == 3
            print(f"  {tag:<22} ✓ 可解析 n={obj.get('n')} items={obj.get('items')}"
                  f"  ({dt:.1f}s)")
        except Exception as e:
            print(f"  {tag:<22} ✗ 解析失败 {type(e).__name__}: {str(e)[:70]}")
            print(f"     原文前 160 字：{txt[:160]!r}")


# ---------------------------------------------------------------- 2 工具调用
def test_tools() -> None:
    hdr("2. 工具调用")
    tools = [{
        "type": "function",
        "function": {
            "name": "read_segments",
            "description": "按段号查看段落全文。段号形如 S3·5。",
            "parameters": {"type": "object", "properties": {
                "segments": {"type": "array", "items": {"type": "string"},
                             "description": "段号列表"}},
                "required": ["segments"]},
        },
    }, {
        "type": "function",
        "function": {
            "name": "read_source",
            "description": "查看某个来源的完整正文。要判断来源整体质量时用它。",
            "parameters": {"type": "object", "properties": {
                "source": {"type": "string", "description": "来源号，如 S3"}},
                "required": ["source"]},
        },
    }]
    # ⚠️ 第一版这个测试是错的：我让它「同时读 S3·5、S3·6、S7·1」，
    # 它**正确地**把三个段号放进了一次调用的 segments 数组里——
    # 返回 1 个 tool_calls 是模型的选择，不是平台不支持并行。
    # 测并行必须给**两个语义不同、无法合并**的工具任务。
    code, d, dt = _post(_base(
        messages=[{"role": "user",
                   "content": "先看 S3 的第 5 和第 6 段，再看整个 S7 来源。"}],
        tools=tools, tool_choice="auto"))
    if code != 200:
        print(f"  ✗ HTTP {code}: {str(d.get('_raw') or d)[:200]}")
        return
    msg = d["choices"][0]["message"]
    tcs = msg.get("tool_calls") or []
    names = [(tc.get("function") or {}).get("name") for tc in tcs]
    print(f"  返回 {len(tcs)} 个 tool_calls：{names}")
    print(f"  → 并行能力：{'✓ 一个回合返回多个工具调用' if len(tcs) > 1 else '? 只回了一个（可能模型自己合并了）'}")
    print(f"  → 格式：id/type/arguments 是否 OpenAI 标准")
    for tc in tcs:
        fn = tc.get("function") or {}
        a = fn.get("arguments")
        good_id = bool(tc.get("id"))
        good_type = tc.get("type") == "function"
        good_args_str = isinstance(a, str)
        try:
            parsed = json.loads(a) if isinstance(a, str) else None
            parses = isinstance(parsed, dict)
        except Exception:
            parses = False
        print(f"    · id={tc.get('id')!r} 有id={good_id} type={tc.get('type')!r} "
              f"标准type={good_type} args是字符串={good_args_str} 可解析={parses}")
        print(f"      args={str(a)[:110]}")


# ---------------------------------------------------------------- 3 思考档位
def test_effort() -> None:
    hdr("3. reasoning_effort 合法档位")
    ok, bad = [], []
    for v in ("low", "high", "max", "medium", "minimal", "none", "xhigh"):
        code, d, dt = _post(_base(
            reasoning_effort=v,
            messages=[{"role": "user", "content": "1+1等于几？只回数字。"}]))
        good = code == 200
        (ok if good else bad).append(v)
        mark = "✓" if good else "✗"
        extra = ""
        if good:
            u = d.get("usage") or {}
            rt = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
            extra = f" out={u.get('completion_tokens')}" + (f" reasoning={rt}" if rt else "")
        else:
            extra = f" {str(d.get('_raw') or d.get('error'))[:90]}"
        print(f"  {mark} {v:<9} HTTP {code} ({dt:.1f}s){extra}")
    print(f"\n  可用：{ok}")
    print(f"  不可用：{bad}")


# ---------------------------------------------------------------- 4 缓存字段
def test_cache() -> None:
    hdr("4. 缓存字段（同样的长前缀连发三次）")
    # ⚠️ 第一版这里前缀只有 740 token，读到 cached_tokens=0 就下了
    # 「平台不支持缓存」的结论。**两个错**：一是字段在最外层不是
    # prompt_tokens_details 里；二是前缀可能根本没到缓存门槛。
    # DeepSeek 系自动缓存通常要求前缀 ≥ 1024 token，所以这次拉到 ~3000。
    prefix = ("以下是一段固定的背景资料，请完整记住：\n"
              + "固态电池的产业化路径分为实验室、中试、装车与量产四个阶段。"
                "电解质路线分硫化物、氧化物、聚合物三类，各有电导率与工艺上的取舍。"
                "放大制造的难点在一致性、良率与设备投资，而不只是单个电芯的性能。"
                "安全性方面，热失控的机理与抑制手段是学界与产业界共同关注的问题。\n"
              ) * 40
    payload = _base(messages=[{"role": "user", "content": prefix + "\n请回一个字：好"}],
                    max_tokens=64)
    hits, toks = [], []
    for i in range(3):
        code, d, dt = _post(payload)
        if code != 200:
            print(f"  第{i+1}次 HTTP {code}: {str(d.get('_raw') or d)[:150]}")
            return
        u = d.get("usage") or {}
        hits.append(_cache_read(u))
        toks.append(u.get("prompt_tokens"))
        print(f"  第{i+1}次 usage = {json.dumps(u, ensure_ascii=False)}")
    print(f"\n  prompt_tokens = {toks}   缓存命中 = {hits}")
    if hits[-1] > 0:
        print(f"  → ✓ 缓存生效，第 3 次命中 {hits[-1]} token，"
              f"字段识别正确（`_cache_read` 能读到）")
    else:
        print(f"  → ⚠️ 前缀 {toks[0]} token 仍未命中缓存。可能是："
              f"缓存门槛更高 / 需要平台侧开通 / 该模型不开自动缓存。")
        print(f"     注意：**读不到 ≠ 不支持**——先看原始 usage 字段"
              f"（上面已打印），别照着 OpenAI 标准的路径去找")


def main() -> None:
    if not KEY:
        sys.exit("未设置 XJ189_API_KEY")
    print("=" * 70)
    print(f"xj189 预检  model={MODEL}")
    print("=" * 70)
    items = {"json": test_json, "tools": test_tools,
             "effort": test_effort, "cache": test_cache}
    for k, fn in items.items():
        if ONLY and ONLY != k:
            continue
        try:
            fn()
        except Exception as e:
            print(f"\n  ❌ {k} 测试本身抛错：{type(e).__name__}: {str(e)[:120]}")
    print(f"\n{'=' * 70}\n预检结束\n{'=' * 70}")


if __name__ == "__main__":
    main()