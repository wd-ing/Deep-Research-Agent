"""`complete_json_forced` 的 **provider 分派**（2026-10-18）。

## 背景：全量 e2e 里 `summarize` 24 次全部 404

`complete_json_forced` 原来**无条件**调 `_openai_compat_forced`，
于是 provider=minimax 的环节会拿 OpenAI 形状拼 URL：

    https://api.minimaxi.com/anthropic/chat/completions  → 404 page not found

`summarize` 是唯一用它的地方（`extract_batch`），
而之前所有验证都打在 write_plan* / relevance 上，够不到它。
**报错前缀是小写的 `minimax`（`_openai_compat` 格式）而不是大写的
`MiniMax`（`_minimax` 格式）——前缀不一致就是认这条路的线索。**

## 本文件守三件事

① minimax 走 `_minimax_tools`（Anthropic 形状），不发 OpenAI 套壳
② 「关思考 + 发工具」在 minimax 上**必须报错**（护栏现在两条路共用）
③ internai/xj189 那些 OpenAI 兼容平台**不受影响**

桩接在 `self._client.post` / `.stream` 上（两条都接，记录走的是哪条），
provider **钉死**，不跟着 `defaults` 漂。

用法：.venv/bin/python tests/test_forced_dispatch.py
"""

from __future__ import annotations

import copy
import json as json_module
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
os.environ.setdefault("MINIMAX_API_KEY", "test-key-not-real")
os.environ.setdefault("INTERN_AI_API_KEY", "test-key-not-real")
os.environ["RA_DB_PATH"] = str(Path(tempfile.gettempdir()) / "ra_forced_disp.db")

import research_agent.db.repo as _repo_mod  # noqa: E402
_r = _repo_mod.get_repo()
if not _r.q1("SELECT id FROM session WHERE id=?", ("s_fd",)):
    _r.x("INSERT INTO session (id,title,question,status,created_at,updated_at) "
         "VALUES (?,?,?,?,?,?)", ("s_fd", "分派自检", "固态电池", "done",
                                  "2026-01-01T00:00:00+00:00",
                                  "2026-01-01T00:00:00+00:00"))

from research_agent.llm.provider import LLMClient  # noqa: E402

fails: list[str] = []


def check(tag: str, ok: bool, detail: str = "") -> None:
    print(f"{'✅' if ok else '❌'} {tag}" + (f"\n   {detail}" if detail else ""))
    if not ok:
        fails.append(tag)


SCHEMA = {"type": "object", "properties": {"summaries": {
    "type": "array", "items": {"type": "object", "properties": {
        "index": {"type": "integer"}, "summary": {"type": "string"}},
        "required": ["index", "summary"]}}}}


class FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.text = json_module.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._p


class FakeStreamCtx:
    def __init__(self, lines, status=200, body=""):
        self.lines = lines
        self.status_code = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self):
        yield from self.lines

    def read(self):
        return self._body.encode()


class FakeClient:
    def __init__(self):
        self.sent: list[dict] = []
        self.urls: list[str] = []
        self.modes: list[str] = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.urls.append(url)
        self.modes.append("post")
        self.sent.append(json_module.loads(json_module.dumps(json)))
        return FakeResp({"content": [{"type": "tool_use", "id": "t1",
                                      "name": "emit_summaries",
                                      "input": {"summaries": [{"index": 0}]}}],
                         "stop_reason": "tool_use",
                         "usage": {"input_tokens": 10, "output_tokens": 20}})

    def stream(self, method, url, headers=None, json=None, timeout=None):
        self.urls.append(url)
        self.modes.append("stream")
        self.sent.append(json_module.loads(json_module.dumps(json)))
        ev = [{"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
              {"type": "content_block_start", "index": 0,
               "content_block": {"type": "tool_use", "id": "t1",
                                 "name": "emit_summaries"}},
              {"type": "content_block_delta", "index": 0,
               "delta": {"type": "input_json_delta",
                         "partial_json": '{"summaries": [{"index": 0}]}'}},
              {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
               "usage": {"output_tokens": 20}}]
        lines = []
        for e in ev:
            lines.append(f"event: {e['type']}")
            lines.append(f"data: {json_module.dumps(e)}")
        return FakeStreamCtx(lines)


def pin(llm, stage, provider, base_url, key_env, effort, stream=False):
    cfg = copy.copy(llm.registry.stage(stage))
    cfg.provider = provider
    cfg.base_url = base_url
    cfg.api_key_env = key_env
    cfg.reasoning_effort = effort
    cfg.stream = stream
    llm.registry._cache[stage] = cfg
    return cfg


# ── ① minimax 走 Anthropic 形状 ──
print("=== ① minimax 必须走 `_minimax_tools`，不能拼 OpenAI 端点 ===")
llm = LLMClient()
pin(llm, "summarize", "minimax", "https://api.minimaxi.com/anthropic",
    "MINIMAX_API_KEY", "low")
llm._client = FakeClient()
out = llm.complete_json_forced("summarize", "概括这三段", name="emit_summaries",
                               schema=SCHEMA)
check("① 拿到了工具的 input", out == {"summaries": [{"index": 0}]}, str(out))
check("① URL 是 Anthropic 的 /v1/messages（不是 /chat/completions）",
      llm._client.urls == ["https://api.minimaxi.com/anthropic/v1/messages"],
      str(llm._client.urls))
sent = llm._client.sent[0]
check("① 工具是 Anthropic 形状（有 input_schema，没有 type:function 套壳）",
      sent["tools"] == [{"name": "emit_summaries",
                         "description": "以结构化形式返回 summarize 的结果",
                         "input_schema": SCHEMA}],
      str(sent.get("tools"))[:200])
check("① 强制形状正确（Anthropic 的 tool_choice）",
      sent.get("tool_choice") == {"type": "tool", "name": "emit_summaries"},
      str(sent.get("tool_choice")))
check("① 只发那一个工具", len(sent.get("tools") or []) == 1,
      str(len(sent.get("tools") or [])))

# ── ② 关思考 + 发工具 = 必须响 ──
print("\n=== ② minimax 关思考 + 发工具：必须报错而不是发出请求 ===")
# ⚠️ 「关思考」在配置里写的是 `reasoning_effort: disabled`，但
# `ModelRegistry.stage()` 会把它翻译成 `providers.<名>.effort_off or None`，
# 而 **minimax 档案故意不写 `effort_off`** ⇒ 解析出来是 `None`。
# 直接给 `None` 才与生产一致；喂 `"disabled"` 会造出一个 registry
# 永远不会产生的状态，测的就不是线上那条路了。
llm1b = LLMClient()
_off_stage = next((n for n in llm1b.registry.raw.get("stages") or {}
                   if (llm1b.registry.raw["stages"][n] or {}).get("reasoning_effort")
                   == "disabled"), None)
if _off_stage:
    # 期望值**从档案算**，不写死某个 provider 的行为：
    #   minimax 档案故意不声明 `effort_off` ⇒ `disabled` → None
    #   internai / xj189 声明了 ⇒ `disabled` → none / None
    # 写死 minimax 的话，默认一切回 internai 这条就假失败 ——
    # 失败原因是**测试自己过期**，不是代码坏了（同一个陷阱第三次）。
    _prof_name = ((llm1b.registry.raw["stages"][_off_stage] or {}).get("provider")
                  or (llm1b.registry.raw.get("defaults") or {}).get("provider"))
    _effort_off = ((llm1b.registry.raw.get("providers") or {}).get(_prof_name)
                   or {}).get("effort_off")
    _want = _effort_off or None
    _got = llm1b.registry.stage(_off_stage).reasoning_effort
    check(f"② registry 按档案把 `disabled` 翻译成 {_want!r}（provider={_prof_name}）",
          _got == _want,
          f"{_off_stage}: 配置写 'disabled' → 解析成 {_got!r}"
          f"（providers.{_prof_name}.effort_off={_effort_off!r}）")
else:
    check("② registry 按档案把 `disabled` 翻译成 effort_off", False,
          "配置里已经没有环节写 disabled 了，这条机制没法验")

llm2 = LLMClient()
pin(llm2, "summarize", "minimax", "https://api.minimaxi.com/anthropic",
    "MINIMAX_API_KEY", None)
llm2._client = FakeClient()
try:
    llm2.complete_json_forced("summarize", "概括这三段", name="emit_summaries",
                              schema=SCHEMA)
    check("② 关思考 + 发工具必须报错", False, "居然成功了")
    check("② 请求根本没发出去", False, "")
except RuntimeError as e:
    check("② 关思考 + 发工具必须报错", "关思考" in str(e) and "工具" in str(e),
          str(e)[:130])
    check("② 请求根本没发出去", llm2._client.sent == [],
          f"实际发出 {len(llm2._client.sent)} 次")
except Exception as e:
    check("② 关思考 + 发工具必须报错", False, f"{type(e).__name__}: {str(e)[:90]}")
    check("② 请求根本没发出去", False, "")

# 护栏只对 minimax 生效：OpenAI 兼容平台关思考照常
print("\n=== ③ OpenAI 兼容平台不受影响 ===")
llm3 = LLMClient()
pin(llm3, "summarize", "internai", "https://intern-ai.example/api/v1",
    "INTERN_AI_API_KEY", "disabled")
llm3._client = FakeClient()


class OAIResp(FakeResp):
    pass


def _oai_post(self, url, headers=None, json=None, timeout=None):
    self.urls.append(url)
    self.modes.append("post")
    self.sent.append(json_module.loads(json_module.dumps(json)))
    return FakeResp({"choices": [{"message": {"role": "assistant", "content": None,
                                              "tool_calls": [{"id": "c1",
                                                              "function": {
                                                                  "name": "emit_summaries",
                                                                  "arguments": '{"summaries": []}'}}]},
                                 "finish_reason": "tool_calls"}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


FakeClient.post = _oai_post
out3 = llm3.complete_json_forced("summarize", "概括这三段", name="emit_summaries",
                                 schema=SCHEMA)
check("③ internai + 关思考照常跑（护栏不该误伤）",
      out3 == {"summaries": []}, str(out3))
check("③ internai 走 OpenAI 端点",
      llm3._client.urls == ["https://intern-ai.example/api/v1/chat/completions"],
      str(llm3._client.urls))
check("③ internai 发的是 OpenAI 形状工具",
      (llm3._client.sent[0].get("tools") or [{}])[0].get("type") == "function",
      str(llm3._client.sent[0].get("tools"))[:150])

print("\n" + "=" * 60)
print(f"失败 {len(fails)} 项" + ("：" + "、".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
