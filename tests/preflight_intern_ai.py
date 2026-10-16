"""中国信通院 discovery-api 接入预检。

背景：xj189 平台维护（2026-10-15 起，19+ 小时同形态未恢复），
换第二套方案。模型 `deepseek-v4-flash-vision`，OpenAI 协议
`https://discovery-api.intern-ai.org.cn/v1`。

**为什么必须预检而不是直接改配置**：
上一个平台（xj189）的行为全是实测出来的，没有一条能靠猜 ——
省略 reasoning_effort 到底是开还是关、usage 里的缓存字段在哪、
SSE 的 data 前缀带不带空格、工具 arguments 是不是分片到达。
这几个任意一个猜错，症状都是「静默失败」，不报错但结果错。

⚠️ **流式解析复用生产的 `_sse_lines` / `SSEAccum`**，不手搓。
上一轮的手搓探针因为 `line.startswith("data: ")`（多了个空格）
把每一行都跳过，测出「内容恒为 0」的假数据，差点当成真结论。

用法：
    .venv/bin/python tests/preflight_intern_ai.py            # 全量
    .venv/bin/python tests/preflight_intern_ai.py --quick    # 跳过耗时的
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from research_agent.llm.provider import SSEAccum, _sse_lines  # noqa: E402

BASE = "https://discovery-api.intern-ai.org.cn/v1"
MODEL = "deepseek-v4-flash-vision"

PASS, FAIL, SKIP = "✅", "❌", "⏭️"
_results: list[tuple[str, str, str]] = []


def load_env() -> None:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def headers() -> dict:
    return {"Authorization": "Bearer " + os.environ["INTERN_AI_API_KEY"],
            "Content-Type": "application/json"}


def report(tag: str, ok: str, detail: str) -> None:
    _results.append((tag, ok, detail))
    print(f"{ok} {tag}\n   {detail}\n")


def chat(client: httpx.Client, payload: dict, timeout: float = 180.0) -> dict:
    """非流式调用，返回原始 JSON。异常原样抛给调用方判。"""
    r = client.post(f"{BASE}/chat/completions", json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def t01_models(client: httpx.Client) -> None:
    """模型清单：确认 key 有权限、模型 id 拼写对。"""
    try:
        r = client.get(f"{BASE}/models", timeout=30)
        if r.status_code != 200:
            report("① 模型清单", SKIP, f"HTTP {r.status_code} —— 不支持枚举，不影响调用")
            return
        data = r.json().get("data", [])
        ids = [m.get("id") for m in data]
        hit = [i for i in ids if MODEL in (i or "")]
        report("① 模型清单", PASS if hit else FAIL,
               f"{len(ids)} 个模型；`{MODEL}` "
               + ("在清单里 ✅" if hit else f"**不在清单里**；相近的有 {ids[:5]}"))
    except Exception as e:
        report("① 模型清单", SKIP, f"{type(e).__name__}: {e}")


def t02_basic(client: httpx.Client) -> str:
    """最小可用调用 + usage 字段形状。"""
    payload = {"model": MODEL, "max_tokens": 256,
               "messages": [{"role": "user", "content": "用一句话说明什么是固态电池。"}]}
    t0 = time.time()
    try:
        d = chat(client, payload)
    except Exception as e:
        report("② 基础调用", FAIL, f"{type(e).__name__}: {str(e)[:300]}")
        return ""
    dt = time.time() - t0
    usage = d.get("usage") or {}
    msg = (d["choices"][0].get("message") or {}) if d.get("choices") else {}
    txt = msg.get("content") or ""
    # usage 的形状每个平台都不一样，必须原样记下来
    report("② 基础调用", PASS if txt else FAIL,
           f"{dt:.1f}s｜正文 {len(txt)} 字\n"
           f"   usage 字段: {json.dumps(usage, ensure_ascii=False)}\n"
           f"   message 字段: {sorted(msg.keys())}\n"
           f"   正文片段: {txt[:80]!r}")
    return txt


def t03_stream(client: httpx.Client) -> None:
    """流式：SSE 前缀实际形态、reasoning_content、usage 落在哪。"""
    payload = {"model": MODEL, "max_tokens": 512, "stream": True,
               "messages": [{"role": "user", "content": "数到五，每个数字单独一行。"}]}
    try:
        with client.stream("POST", f"{BASE}/chat/completions", json=payload,
                           timeout=180) as resp:
            if resp.status_code != 200:
                resp.read()
                report("③ 流式 SSE", FAIL,
                       f"HTTP {resp.status_code}: {resp.text[:200]}")
                return
            raw_prefixes: dict[str, int] = {}
            for line in resp.iter_lines():
                if line and line[:5] == "data:":
                    k = "data: " if line[5:6] == " " else "data:"
                    raw_prefixes[k] = raw_prefixes.get(k, 0) + 1
            accum = SSEAccum()
            # 重新流一遍，用生产的解析器实际累加
            with client.stream("POST", f"{BASE}/chat/completions", json=payload,
                               timeout=180) as r2:
                for obj in _sse_lines(r2):
                    accum.feed(obj)
    except Exception as e:
        report("③ 流式 SSE", FAIL, f"{type(e).__name__}: {str(e)[:300]}")
        return

    text = accum.text_only()
    ok = bool(text.strip())
    report("③ 流式 SSE", PASS if ok else FAIL,
           f"data 前缀形态: {raw_prefixes or '(无)'}\n"
           f"   正文 {len(text)} 字｜思考 {len(''.join(accum.reasoning))} 字"
           f"｜usage: {json.dumps(accum.usage, ensure_ascii=False)}\n"
           f"   正文片段: {text[:80]!r}")


def t04_effort(client: httpx.Client) -> None:
    """reasoning_effort 各档：这个平台省略到底等于开还是关。

    **xj189 的经验**：省略 = 关，传任何值（含 none）= 开，与平台文档相反。
    新平台必须重测，不能沿用。
    """
    rows = []
    for eff in ["<省略>", "none", "low", "high"]:
        payload = {"model": MODEL, "max_tokens": 1024,
                   "messages": [{"role": "user", "content": "27×31 等于几？只答数字。"}]}
        if eff != "<省略>":
            payload["reasoning_effort"] = eff
        t0 = time.time()
        try:
            d = chat(client, payload)
            msg = d["choices"][0].get("message") or {}
            rc = msg.get("reasoning_content")
            rc_len = len(rc) if isinstance(rc, str) else 0
            txt = msg.get("content") or ""
            rows.append((eff, f"{time.time()-t0:.1f}s", f"reasoning {rc_len} 字",
                         f"正文 {len(txt)} 字", "有" if rc else "无",
                         txt[:12].replace("\n", " ")))
        except Exception as e:
            rows.append((eff, f"{time.time()-t0:.1f}s", f"❌ {type(e).__name__}", "", "", str(e)[:60]))
    print("④ reasoning_effort 档位")
    print(f"   {'档位':<10}{'耗时':<9}{'思考':<16}{'正文':<14}{'rc字段':<8}片段")
    for r in rows:
        print(f"   {r[0]:<10}{r[1]:<9}{r[2]:<16}{r[3]:<14}{r[4]:<8}{r[5]}")
    report("④ reasoning_effort 档位", PASS,
           "上表是实测；**省略行的思考长度是判断「省略=开还是关」的唯一依据**")


def t05_json(client: httpx.Client) -> None:
    """严格 JSON：extract / summarize / assess 都依赖它。"""
    for rf in [None, {"type": "json_object"}]:
        payload = {"model": MODEL, "max_tokens": 512,
                   "messages": [{"role": "user",
                                 "content": '只输出 JSON：{"name":"张三","age":30}'}]}
        if rf:
            payload["response_format"] = rf
        tag = "response_format=json_object" if rf else "不带 response_format"
        try:
            d = chat(client, payload)
            txt = (d["choices"][0].get("message") or {}).get("content") or ""
            ok = False
            try:
                json.loads(txt.strip().removeprefix("```json").removeprefix("```").rstrip("```").strip())
                ok = True
            except Exception:
                pass
            report(f"⑤ 严格 JSON（{tag}）", PASS if ok else FAIL,
                   f"{len(txt)} 字｜{txt[:100]!r}")
        except Exception as e:
            report(f"⑤ 严格 JSON（{tag}）", FAIL, f"{type(e).__name__}: {str(e)[:200]}")


def t06_tools(client: httpx.Client) -> None:
    """工具调用 —— 直接验那个未验证的假设：这个平台会不会真的发 tool_calls。

    非流式与流式都要测。**流式尤其重要**：`function.arguments` 是分片到达的
    （xj189 实测 570 片拼成 2430 字符），而工具失效是静默的 ——
    降级成单次调用、不报错，拼错了极难发现。
    """
    tools = [{"type": "function", "function": {
        "name": "read_segments",
        "description": "按段号读取证据原文",
        "parameters": {"type": "object", "properties": {
            "ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["ids"]}}}]
    msgs = [{"role": "user",
             "content": "请调用 read_segments 读取 S1·1 和 S2·3，不要自己回答。"}]
    base = {"model": MODEL, "max_tokens": 1024, "tools": tools,
            "tool_choice": "auto", "messages": msgs}

    # 非流式
    try:
        d = chat(client, base)
        msg = (d["choices"][0].get("message") or {})
        tcs = msg.get("tool_calls") or []
        ft = msg.get("finish_reason")
        detail = (f"finish_reason={ft}｜{len(tcs)} 个 tool_calls"
                  + (f"｜{tcs[0]['function']['name']}({tcs[0]['function']['arguments'][:60]})"
                     if tcs else ""))
        report("⑥ 工具调用（非流式）", PASS if tcs else FAIL, detail)
    except Exception as e:
        report("⑥ 工具调用（非流式）", FAIL, f"{type(e).__name__}: {str(e)[:200]}")

    # 流式：走生产的 SSEAccum，看分片能不能正确拼起来
    payload = dict(base, stream=True)
    payload["messages"] = msgs + [{"role": "assistant", "content": "好的。"}]
    try:
        with client.stream("POST", f"{BASE}/chat/completions", json=payload,
                           timeout=180) as resp:
            accum = SSEAccum()
            n_obj = 0
            for obj in _sse_lines(resp):
                accum.feed(obj)
                n_obj += 1
        blocks = accum.blocks()
        tools_out = [b for b in blocks if b.get("type") == "tool_use"]
        if tools_out:
            b = tools_out[0]
            report("⑥ 工具调用（流式）", PASS,
                   f"{n_obj} 个 SSE 对象｜工具块 {len(tools_out)} 个"
                   f"｜{b['name']}({json.dumps(b['input'], ensure_ascii=False)[:80]})")
        else:
            report("⑥ 工具调用（流式）", FAIL,
                   f"{n_obj} 个 SSE 对象｜**工具块为 0**"
                   f"｜正文 {len(accum.text_only())} 字"
                   f"｜思考 {len(''.join(accum.reasoning))} 字")
    except Exception as e:
        report("⑥ 工具调用（流式）", FAIL, f"{type(e).__name__}: {str(e)[:200]}")


def t07_shape(client: httpx.Client) -> None:
    """把一条真实响应整个 dump 出来，看有没有我们不认识的字段。

    平台加字段是常态（prompt_cache_key、reasoning_tokens_details…），
    不看一次就只能等出问题再猜。
    """
    payload = {"model": MODEL, "max_tokens": 128,
               "messages": [{"role": "user", "content": "hi"}]}
    try:
        d = chat(client, payload)
        print("⑦ 响应全字段")
        print("   " + json.dumps(d, ensure_ascii=False)[:1200])
        report("⑦ 响应字段", PASS, "见上方完整 JSON")
    except Exception as e:
        report("⑦ 响应字段", FAIL, f"{type(e).__name__}: {str(e)[:200]}")


QUICK = {"t03_stream", "t04_effort", "t05_json", "t06_tools"}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="跳过耗时的测试")
    args = ap.parse_args()

    load_env()
    print(f"=== 中国信通院 discovery-api 预检  {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    print(f"模型 {MODEL}｜协议 {BASE}（OpenAI 兼容）\n")

    # 长生成会撞网关超时（xj189 撞过），超时给足
    with httpx.Client(trust_env=False, headers=headers(),
                      timeout=httpx.Timeout(180.0, connect=20.0)) as c:
        t01_models(c)
        t02_basic(c)
        if not args.quick:
            t07_shape(c)
        t03_stream(c) if not args.quick else None
        t04_effort(c) if not args.quick else None
        if not args.quick:
            t05_json(c)
            t06_tools(c)

    print("=" * 60)
    bad = [r for r in _results if r[1] == FAIL]
    print(f"共 {len(_results)} 项，失败 {len(bad)} 项")
    for tag, ok, _ in bad:
        print(f"  {ok} {tag}")
    sys.exit(1 if bad else 0)
