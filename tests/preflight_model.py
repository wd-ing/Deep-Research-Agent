"""换模型的生产路径预检（打真平台，只走 `LLMClient`）。

2026-10-17 加。起因：信通院的 DeepSeek 额度快用尽，用户要求换
`Atria-Dawn`（限时免费），**先试行不行**。

## 为什么这个脚本不用手搓 payload

本项目的教训已经吃过两次：

- `preflight_xj189.py` / `preflight_intern_ai.py` 都是**手搓标准格式**
  去测的，测通了写「工具调用 ✓」——**测的不是项目真正会发的那种**，
  于是 `to_openai_tools` 缺失这种洞要到生产才炸（坑 41、坑 48 都是这个形态）。
- 手搓探针还用 `line.startswith("data: ")`（多了空格）把每一行跳过，
  测出「内容恒为 0」的**假数据**，差点当成真结论。

所以这里**不手搓**：把目标模型塞进 `registry._cache`，让 `LLMClient`
按真实的 `_opencode` / `_opencode_tools` / `_openai_compat_forced`
三条路径发出去。**发出去的报文与生产逐字一致。**

## 查什么（按对本项目的致命程度排序）

1. **强制工具调用** `tool_choice` —— ④ 的保底路径全押在它上面
2. **普通工具调用** 的 arguments 拼接（分片到达）
3. **流式**（`write_*` 全是 `stream: true`，nginx 墙靠流式绕）
4. **effort 语义**：省略 = 开还是关？`none` 认不认？
   （两个平台**正好相反**，写死字面量会在切平台时反向打开思考）
5. **usage 字段**：`reasoning_tokens` / `cached_tokens` 在哪一层
6. **长输出**：认不认大 `max_tokens`

用法：
    .venv/bin/python tests/preflight_model.py Atria-Dawn
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

from research_agent.llm.provider import (  # noqa: E402
    GuardPolicy, LLMClient, StageConfig, _reasoning_tokens,
)

RESULTS: list[tuple[str, str, str]] = []
MODEL = sys.argv[1] if len(sys.argv) > 1 else "Atria-Dawn"
BASE_STAGE = "write_plan_tasks"

# 🔴 **provider 不再硬编码**（2026-10-17 修）。
#
# 原来 `cfg()` 里写死 `provider="internai"` + `INTERN_AI_API_KEY`。
# 那是为信通院那次换模型写的。默认平台切到 minimax 之后，
# 拿 `MiniMax-M3` 去跑它会得到：
#
#     平台：https://api.minimaxi.com/anthropic      ← base_url 取自默认 stage
#     ❌ ① 非流式基础  RuntimeError: internai 404   ← provider 却还是 internai
#
# **两边各取一半**，于是每个检查都 404，而输出看起来像一次正经预检。
# 更糟的是 ⑤⑥⑦ 把 ✅ 写死（见下面），这份输出会**主动告诉你「没问题」**。
#
# 一个探针在「配置写错」时给出的结论，比没有探针更坏。
PROVIDER: str | None = None      # 由 main() 按模型名解析，或命令行显式给
API_KEY_ENV: str | None = None
BASE_URL: str | None = None


def providers() -> dict:
    """`config/models.yaml` 里的 `providers:` 档案。"""
    from research_agent.llm.provider import ModelRegistry
    return ModelRegistry().raw.get("providers") or {}


def report(tag: str, ok: str, detail: str) -> None:
    RESULTS.append((tag, ok, detail))
    print(f"{ok} {tag}\n   {detail}\n", flush=True)


def anthropic_protocol() -> bool:
    """目标平台是不是走 **Anthropic Messages 协议**（不是 OpenAI 兼容）。

    ⑤⑥⑦ 是**手搓 payload** 的探针（`{base}/chat/completions` +
    `reasoning_effort`），那套只对 OpenAI 兼容平台成立。
    minimax 是 `/v1/messages` + `thinking.type`，发过去只会 404 ——
    而 404 在这两项里长得和「平台不支持」一模一样。

    所以对这类平台必须**报「不适用」**：既不能 ✅（那是替配置背书），
    也不能 ❌（那是在说平台坏了）。本项目自己的文件头写着
    「不手搓」，这三项是残留的手搓。
    """
    return (PROVIDER or "") == "minimax"


def skip_if_shape_mismatch(tag: str) -> bool:
    if not anthropic_protocol():
        return False
    report(tag, "➖", "**不适用**：目标平台走 Anthropic Messages 协议"
                      "（`/v1/messages` + `thinking.type`），"
                      "而这一项是手搓 OpenAI 形状的探针。"
                      "对应的真实语义见 `_minimax` 里的实测对照表。")
    return True


def _resolve_provider(llm: LLMClient) -> tuple[str, str, str] | None:
    """按模型名找出它属于哪个 provider 档案。

    找得到就用档案里的 `provider` / `base_url` / `api_key_env`，
    找**不到**就返回 None —— 让调用者报错，而不是拿猜的地址去打。
    """
    if len(sys.argv) > 2 and sys.argv[2]:
        name = sys.argv[2]
        prof = providers().get(name)
        if not prof:
            print(f"❌ 配置里没有 provider 档案 {name!r}")
            return None
        return name, prof.get("base_url"), prof.get("api_key_env")
    hits = [n for n, p in providers().items() if p.get("model") == MODEL]
    if len(hits) != 1:
        print(f"❌ 模型 {MODEL!r} 在配置里对应 {len(hits)} 个 provider {hits}，"
              f"请用第三个参数显式指定")
        return None
    name = hits[0]
    prof = providers()[name]
    return name, prof.get("base_url"), prof.get("api_key_env")


def cfg(name: str, *, model: str, stream: bool,
        effort: str | None) -> StageConfig:
    return StageConfig(
        stage=name, provider=PROVIDER, model=model,
        max_output_tokens=16384, temperature=0.0,
        reasoning_effort=effort, thinking_effort=None, timeout_s=300.0,
        base_url=BASE_URL, api_key_env=API_KEY_ENV,
        stream=stream, guard=GuardPolicy())


def inject(llm: LLMClient, name: str, **kw) -> None:
    llm.registry._cache[name] = cfg(name, model=MODEL, **kw)


def t01_basic(llm: LLMClient) -> None:
    inject(llm, "_pf_basic", stream=False, effort=None)
    t0 = time.time()
    try:
        out = llm.complete("_pf_basic", "用一句话说明什么是固态电池。")
        u = llm.warnings
        ok = "✅" if out.strip() else "❌"
        report("① 非流式基础", ok,
               f"{len(out)} 字 / {(time.time()-t0):.1f}s / 告警 {len(u)} 条")
    except Exception as e:
        report("① 非流式基础", "❌", f"{type(e).__name__}: {str(e)[:160]}")


def t02_stream(llm: LLMClient) -> None:
    inject(llm, "_pf_stream", stream=True, effort=None)
    t0 = time.time()
    try:
        out = llm.complete("_pf_stream", "用一句话说明什么是固态电池。")
        ok = "✅" if out.strip() else "❌"
        report("② 流式", ok, f"{len(out)} 字 / {(time.time()-t0):.1f}s")
    except Exception as e:
        report("② 流式", "❌", f"{type(e).__name__}: {str(e)[:160]}")


def t03_tools(llm: LLMClient) -> None:
    """普通工具调用。arguments 是分片到达的，拼接错就是静默失败。"""
    inject(llm, "_pf_tools", stream=True, effort=None)
    tool = {"name": "lookup", "description": "查一个词的意思",
            "input_schema": {"type": "object",
                             "properties": {"word": {"type": "string"}},
                             "required": ["word"]}}
    try:
        r = llm.complete_with_tools("_pf_tools", "你必须查一下「固态电池」。",
                                    [{"role": "user", "content": "开始"}], [tool])
        calls = [b for b in r["blocks"] if b.get("type") == "tool_use"]
        if not calls:
            report("③ 普通工具调用", "❌", "模型没调工具（回文本了）")
        else:
            broken = any(c.get("_args_broken") for c in calls)
            report("③ 普通工具调用", "❌" if broken else "✅",
                   f"{[c.get('name') for c in calls]} / input={calls[0].get('input')}")
    except Exception as e:
        report("③ 普通工具调用", "❌", f"{type(e).__name__}: {str(e)[:160]}")


def t04_forced_choice(llm: LLMClient) -> None:
    """🔴 强制工具调用 —— ④ 的保底路径全押在它上面。"""
    inject(llm, "_pf_forced", stream=True, effort=None)
    tool = {"name": "submit_tasks", "description": "交卷",
            "input_schema": {"type": "object",
                             "properties": {"tasks": {
                                 "type": "array",
                                 "items": {"type": "object",
                                           "properties": {
                                               "covers": {"type": "array",
                                                          "items": {"type": "string"}},
                                               "mandate": {"type": "string"}},
                                           "required": ["covers", "mandate"]}}},
                             "required": ["tasks"]}}
    try:
        r = llm.complete_with_tools("_pf_forced", "分派两节：sec1 和 sec2。",
                                    [{"role": "user", "content": "开始"}],
                                    [tool], tool_choice="submit_tasks")
        calls = [b for b in r["blocks"] if b.get("type") == "tool_use"]
        text = "".join(b.get("text") or "" for b in r["blocks"]
                       if b.get("type") == "text")
        if not calls:
            report("④ 🔴强制 tool_choice", "❌",
                   f"平台无视了强制，回了 {len(text)} 字文本 —— "
                   f"**保底路径不成立**")
        else:
            broken = any(c.get("_args_broken") for c in calls)
            report("④ 🔴强制 tool_choice", "❌" if broken else "✅",
                   f"{[c.get('name') for c in calls]} / "
                   f"input={json.dumps(calls[0].get('input'), ensure_ascii=False)[:120]}"
                   f" / 同时回文本 {len(text)} 字")
    except Exception as e:
        report("④ 🔴强制 tool_choice", "❌", f"{type(e).__name__}: {str(e)[:160]}")


def t05_effort_semantics(llm: LLMClient) -> None:
    """省略 effort 是开还是关？两个平台正好相反，写死字面量会反向开思考。"""
    if skip_if_shape_mismatch("⑤ effort 语义"):
        return
    import httpx
    lines = []
    ok_count = 0          # ⚠️ 「测不到」不等于「测过」
    for label, eff in (("省略", None), ("none", "none"), ("high", "high")):
        payload = {"model": MODEL, "max_tokens": 4096, "temperature": 0.0,
                   "messages": [{"role": "user",
                                 "content": "简单回答：2+2 等于几？"}],
                   "stream": False}
        if eff:
            payload["reasoning_effort"] = eff
        try:
            r = httpx.post(
                f"{BASE_URL}/chat/completions",
                headers={"Authorization":
                         "Bearer " + os.environ[API_KEY_ENV]},
                json=payload, timeout=180.0)
            if r.status_code != 200:
                lines.append(f"{label}: HTTP {r.status_code} ❌")
                ok_count = 0
                continue
            ok_count += 1
            u = (r.json().get("usage") or {})
            rt = _reasoning_tokens(u, "")
            has_field = "completion_tokens_details" in u
            lines.append(f"{label}: reasoning={rt} "
                         f"(有details字段={has_field}, completion={u.get('completion_tokens')})")
        except Exception as e:
            lines.append(f"{label}: {type(e).__name__} ❌")
    # ⚠️ **原来这里写死了 `report(..., "✅", ...)`** —— 永远不会失败。
    # 后果是：provider 配错时每个检查都 404，而这一项**主动报告「没问题」**。
    # 「测不到」必须算「不通过」，否则预检就是在替配置背书。
    report("⑤ effort 语义", "✅" if ok_count else "❌", "\n     ".join(lines))
    if ok_count:
        print("   ⚠️ 省略=0 且 none=0 ⇒ 该平台「省略即关思考」；"
              "省略>0 而 none≈0 ⇒ 「省略即开、须显式关」。\n", flush=True)
    else:
        print(f"   ❌ 三次探测全部失败，**这一项什么都没测到**\n", flush=True)


def t06_usage_fields(llm: LLMClient) -> None:
    """缓存与思考字段在哪一层 —— 写错位置会读到 0 而不报错。"""
    if skip_if_shape_mismatch("⑥ usage 字段"):
        return
    import httpx
    payload = {"model": MODEL, "max_tokens": 4096, "temperature": 0.0,
               "messages": [{"role": "user", "content": "说一句话。"}]}
    try:
        r = httpx.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization":
                     "Bearer " + os.environ[API_KEY_ENV]},
            json=payload, timeout=180.0)
        # ⚠️ 原来没判 status_code：`r.json()` 解析 404 的 HTML 正文
        # 会抛 JSONDecodeError（第一次跑就撞了这个），
        # 而报出来的错是「Extra data: line 1 column 5」，跟真实原因无关。
        if r.status_code != 200:
            report("⑥ usage 字段", "❌",
                   f"HTTP {r.status_code} {r.text[:120]}")
            return
        u = r.json().get("usage") or {}
        report("⑥ usage 字段", "✅", f"原始 usage = {u}")
    except Exception as e:
        report("⑥ usage 字段", "❌", f"{type(e).__name__}: {str(e)[:160]}")


def t07_max_tokens(llm: LLMClient) -> None:
    """认不认大预算 —— 不认就会在生产里变 400。"""
    if skip_if_shape_mismatch("⑦ 大预算接受度"):
        return
    import httpx
    res = []
    ok_count = 0
    for mt in (131072, 65536):
        payload = {"model": MODEL, "max_tokens": mt, "temperature": 0.0,
                   "messages": [{"role": "user", "content": "说一句话。"}]}
        try:
            r = httpx.post(
                f"{BASE_URL}/chat/completions",
                headers={"Authorization":
                         "Bearer " + os.environ[API_KEY_ENV]},
                json=payload, timeout=180.0)
            if r.status_code == 200:
                ok_count += 1
            res.append(f"{mt}: HTTP {r.status_code}"
                       + ("" if r.status_code == 200 else f" {r.text[:80]}"))
        except Exception as e:
            res.append(f"{mt}: {type(e).__name__}")
    # 同 ⑤：全挂 = 没测到 = 不通过
    report("⑦ 大预算接受度", "✅" if ok_count else "❌", " / ".join(res))


def main() -> int:
    global BASE_URL, PROVIDER, API_KEY_ENV
    llm = LLMClient()
    # ⚠️ 顺序要紧：**先定 provider**，再取 BASE_URL。
    # 原来先取默认 stage 的 base_url、后硬编码 provider=internai，
    # 两边各取一半 → 每个检查都 404，而输出看着像一次正经预检。
    resolved = _resolve_provider(llm)
    if not resolved:
        return 2
    PROVIDER, BASE_URL, API_KEY_ENV = resolved
    if not os.environ.get(API_KEY_ENV or ""):
        print(f"❌ 未设置 {API_KEY_ENV}")
        return 2
    print(f"模型：{MODEL}\n平台：{PROVIDER} @ {BASE_URL}\n"
          + "=" * 60, flush=True)
    try:
        for fn in (t01_basic, t02_stream, t03_tools, t04_forced_choice,
                   t05_effort_semantics, t06_usage_fields, t07_max_tokens):
            try:
                fn(llm)
            except Exception as e:      # 单项失败不许中断整轮预检
                report(fn.__name__, "❌", f"{type(e).__name__}: {str(e)[:160]}")
    finally:
        llm.close()
    print("=" * 60)
    bad = [r for r in RESULTS if r[1] == "❌"]
    na = [r for r in RESULTS if r[1] == "➖"]
    for tag, ok, _ in RESULTS:
        print(f"  {ok} {tag}")
    verdict = ("❌ 有 " + str(len(bad)) + " 项不通过" if bad
               else f"✅ 适用项全通过（{len(na)} 项因协议不适用跳过）")
    print(f"\n{verdict}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
