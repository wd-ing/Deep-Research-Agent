"""LLM provider：按环节分发到配置的模型。

关于 thinking 的设计变更（2026-10-04）
--------------------------------------
原设计是「抽取与去重环节必须显式设 thinking_budget: 0，缺失则拒绝启动」。
那是针对 Gemini 写的硬约束，动机来自 MiniMax M3.1 Flash 的实测失效模式：
在 4096 预算下，五次里有三次把预算全用在 thinking 上而没吐出任何内容。

换到 space-bunny-free 后实测发现：
  - reasoning_effort=none / 0  → 400，不支持
  - reasoning_effort=minimal    → reasoning 压到 28 到 73 tokens，但不为零
  - reasoning_effort=low        → 0 到 57 tokens，方差大
即**该模型无法真正关闭思考**。硬约束如果照搬会直接让服务起不来，
而它真正需要的防线是另外三条：

  1. 给足输出预算，让思考吃掉的那部分不会挤掉内容
  2. 监控 reasoning 占比，超阈值记一条告警（占比过高说明任务不匹配该模型）
  3. 返回空内容时自动重试并翻倍预算——这才是 M3.1 那个失效模式的直接补丁

所以 validate 从「必须有 thinking_budget: 0」改成「抽取类环节必须有 guard 策略」。
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]

# 所有环节都要走 guard。曾经只覆盖抽取类环节，结果 write 阶段
# 配了 retry_on_empty 却不生效——思考 16,928 字符耗尽预算后直接崩掉。
NEEDS_GUARD = {
    "probe_light", "probe_outline", "probe_degraded", "outline", "extract",
    "relevance", "assess", "write_plan", "write_audit",
    "write_section", "write_revise", "summarize",
    # assign / dedupe：配置里有、图里没有调用点，是**记着的待办**不是废代码。
    # 真要接入时它们已经在白名单和 guard 里了。查证：grep 调用点为 0。
    "assign", "dedupe",
}

# MiniMax 的思考深度档位。省略 output_config.effort 时默认 max。
# 注意 none 不是关闭思考——那个值会返回 400。
THINKING_EFFORTS = {"low", "medium", "high", "xhigh", "max"}

# 走 `/chat/completions` 的 OpenAI 兼容平台，共用同一份实现。
#
# **模型不写死在代码里**：具体模型名、base_url、api_key_env、思考档位
# 全部从 `config/models.yaml` 读。换平台只改配置，不动这里。
#   · opencode —— 已有，接过 write 板块
#   · xj189    —— 2026-10-09 加，DeepSeek 系（deepseek-v4-flash-0731）
OPENAI_COMPAT = {"opencode", "xj189"}


# ---------- 当前会话 ----------
# LLMClient 是全局单例，调用点遍布主图与子图，逐个传 session_id 不现实。
# 用 ContextVar 把「这次调用属于哪场研究」作为环境传下去：主图节点进入时设一次，
# 子图与并发协程在同一个 Task 上下文里自然继承。
_CURRENT_SESSION: ContextVar[str | None] = ContextVar(
    "ra_current_session", default=None
)


def current_session() -> str | None:
    return _CURRENT_SESSION.get()


# 思考/正文增量的回调。形如 fn(session_id, stage, kind, chunk)。
# **默认 None = 完全不启用**，所以现有调用点行为一个字都不变。
#
# 为什么用 contextvar 而不是逐层传参：`complete()` 有几十个调用点
# （节点、子图、工具循环、并发子 agent），串参数会污染整条调用链。
# 而并发子 agent 的 ThreadPoolExecutor 不继承 contextvar——
# 所以要用必须连 `submit_run_in_context` 一起用，
# 否则增量会归到空 session（同一个坑 C1-j 已经踩过一次）。
_DELTA_HOOK: ContextVar[Optional[Callable[[str | None, str, str, str], None]]] = \
    ContextVar("_DELTA_HOOK", default=None)

# 关思考的环节：不推增量（没有 reasoning，正文碎片对前端只是噪音）
_NO_DELTA_STAGES = {"relevance", "summarize", "extract",
                   "probe_light", "probe_degraded"}

# 这些环节的输出是 **JSON**（框架 / 任务分派 / 审查结论），
# 边流边推给用户只会看到一坨没闭合的括号。它们的 thinking 照常推（有用），
# text 不推。真正给读者看的正文是 write_section / write_revise。
_NO_TEXT_DELTA_STAGES = {"outline", "write_plan", "write_audit"}


def set_delta_hook(fn) -> Any:
    """注册思考增量回调，返回 token（用 reset 还原）。"""
    return _DELTA_HOOK.set(fn)


def reset_delta_hook(token) -> None:
    _DELTA_HOOK.reset(token)


def _fire_delta(stage: str, kind: str, chunk: str) -> None:
    fn = _DELTA_HOOK.get()
    if fn:
        try:
            fn(current_session(), stage, kind, chunk)
        except Exception:
            # 展示是锦上添花，绝不能因为展示失败拖垮研究
            pass


@contextmanager
def delta_scope(fn):
    """在作用域内启用思考增量回调。传 None 等于关掉。"""
    token = _DELTA_HOOK.set(fn)
    try:
        yield
    finally:
        _DELTA_HOOK.reset(token)


@contextmanager
def session_scope(session_id: str | None):
    """在一场研究的范围内记账。子图、并发协程自动继承。

    ⚠️ **线程不会自动继承 contextvar**（2026-10-07 修）。
    `ThreadPoolExecutor.submit`/`map` 起的是**新线程**，`ContextVar`
    在那里是空的——于是 write 阶段 11 个并发子 agent 的调用
    `session_id` 全记成了 NULL。

    后果不只是难看：`session_cost()` 少算一半，而它正是
    `max_tokens` 硬条件的判据。实测漏掉 238,901 输入 token。

    用 `run_in_context()` 包一层：复制当前 context 再在线程里跑。
    并发协程（`asyncio.to_thread`）本来就传播 contextvar，不用管。
    """
    tok = _CURRENT_SESSION.set(session_id)
    try:
        yield
    finally:
        _CURRENT_SESSION.reset(tok)


def run_in_context(fn, *args, **kwargs):
    """把 fn 绑成「在线程里也能看见当前 contextvar」的**零参**可调用对象。

    ⚠️⚠️ **必须在提交任务之前、在主线程里调用它。**（2026-10-07 踩过）

    `copy_context()` 复制的是「调用 run_in_context 那一刻」的 context。
    如果写成 `pool.map(lambda t: run_in_context(f, t), items)`，
    那个 lambda 是**在 worker 线程里跑**的，复制到的是 worker 的空 context，
    于是修复看起来写了、实际一点用没有（而且不报错，静默继续漏记账）。

    正确用法：先造好一批零参 wrapper，再 submit。

        jobs = [run_in_context(_run_task, state, t, None) for t in tasks]
        with ThreadPoolExecutor(4) as pool:
            results = [f.result() for f in [pool.submit(j) for j in jobs]]

    或者用 `submit_run_in_context(pool, fn, *args, **kwargs)` 直接拿 future。
    """
    if not args and not kwargs:
        raise TypeError(
            "run_in_context 必须显式绑定参数。它返回的是零参函数，"
            "配合并发时记得**在主线程里先造好**再提交。")
    import contextvars as _cv
    ctx = _cv.copy_context()

    def _wrapped():
        return ctx.run(lambda: fn(*args, **kwargs))

    return _wrapped


def submit_run_in_context(pool, fn, *args, **kwargs):
    """在**当前线程**复制 context 后提交任务——这才是线程池要用的入口。

    存在的理由：`pool.map(lambda ...)` 会让包装在 worker 线程里才执行，
    context 复制到的是空值，而且**不报错**。见 `run_in_context` 的警告。
    """
    return pool.submit(run_in_context(fn, *args, **kwargs))


def parallel_labelled(items: list, fn, workers: int = 4, label: str = "",
                      first_serial: bool = True) -> list:
    """对 items 并发跑 `fn(item)`，**按原顺序**返回结果。

    `first_serial=True` 时**第一个任务串行跑**，其余再并发。
    原因是缓存惊群：并发请求会同时打到一个冷缓存，命中率反而下降。
    write 阶段早就踩过这个（实测冷缓存 8 并发只 3 命中，预热后 7/8），
    这里沿用同样的做法。

    `fn` 抛异常时，这一项记为 `None`，不中断整批——
    调用方按各自的语义处理（判失败、放行、还是保留旧值）。
    """
    from concurrent.futures import ThreadPoolExecutor

    if not items:
        return []
    out: list = [None] * len(items)

    def _safe(i: int, it):
        try:
            out[i] = fn(it)
        except Exception as e:
            print(f"⚠️  [{label}] 第 {i} 项失败: {type(e).__name__} {str(e)[:80]}",
                  flush=True)
            out[i] = None

    head = items[:1] if first_serial else []
    tail = items[1:] if first_serial else items
    if head:
        _safe(0, head[0])
    if tail:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [submit_run_in_context(pool, _safe, i, it)
                    for i, it in enumerate(tail, start=1 if first_serial else 0)]
            for f in futs:
                f.result()
    return out


class ConfigError(RuntimeError):
    pass


class EmptyOutput(RuntimeError):
    """模型连续返回空内容，重试耗尽。"""


@dataclass
class GuardPolicy:
    """抽取类环节的三道防线。"""

    max_reasoning_ratio: float = 0.5
    retry_on_empty: int = 2
    max_output_tokens: int = 4096
    # 传输断连的独立额度。**不与 retry_on_empty 共用**（2026-10-06 拆开）。
    #
    # 拆开的原因：这两件事根本不是一回事。
    #   · 空返回 = reasoning 思考吃光了输出预算，模型的失败模式，要翻倍预算重来
    #   · 连接中断 = 传输层抖动，参数没变，重发多半就成了
    # 共用预算时，一次抖动 + 一次空返回就把 3 次用光，
    # 于是 write_audit 这种本来能成的环节被判「该环节不可用」——
    # 实测 R6c 那轮就是 D(1/3) D(2/3) 成功，三次刚好耗尽。
    retry_on_transport: int = 2

    @classmethod
    def from_dict(cls, d: dict | None) -> "GuardPolicy":
        d = d or {}
        return cls(
            max_reasoning_ratio=float(d.get("max_reasoning_ratio", 0.5)),
            retry_on_empty=int(d.get("retry_on_empty", 2)),
            max_output_tokens=int(d.get("max_output_tokens", 4096)),
            retry_on_transport=int(d.get("retry_on_transport", 2)),
        )


@dataclass
class StageConfig:
    stage: str
    provider: str
    model: str
    max_output_tokens: int
    temperature: float
    reasoning_effort: str | None
    thinking_effort: str | None
    timeout_s: float
    base_url: str = ""
    api_key_env: str = ""
    batch_size: int = 20
    # 该环节是否用流式调用。
    #
    # **只为绕开 xj189 前面 nginx 的 60 秒读超时**（2026-10-10 实测：
    # `write_audit` 连撞三次 504、环节判定不可用；同参数流式跑过 245.5 秒）。
    # nginx 测的是「两次读到数据之间的间隔」，流式持续推块就永不空闲。
    #
    # 只给离 60 秒近的环节开——`relevance` 2.3s、`summarize` 6.8s
    # 离得远，全量流式属于为看不见的收益买单。
    stream: bool = False
    guard: GuardPolicy = field(default_factory=GuardPolicy)

    def validate(self) -> None:
        if self.stage in NEEDS_GUARD and self.guard.retry_on_empty < 1:
            raise ConfigError(
                f"环节 {self.stage} 必须配置 guard.retry_on_empty >= 1。"
                "空返回是真实存在的失败模式，没有重试就没有防线。"
            )
        if self.thinking_effort is not None:
            if self.thinking_effort not in THINKING_EFFORTS:
                raise ConfigError(
                    f"环节 {self.stage} 的 thinking_effort={self.thinking_effort!r} 非法，"
                    f"只接受 {sorted(THINKING_EFFORTS)}。"
                )
            if self.provider != "minimax":
                raise ConfigError(
                    f"环节 {self.stage} 配了 thinking_effort，但 provider 是 "
                    f"{self.provider}。output_config.effort 是 MiniMax 的参数，"
                    "换 provider 时必须一并删掉，否则会被静默忽略——"
                    "那正是上一轮踩过的坑。"
                )
        # 反向也要拦（2026-10-09 补）：MiniMax 不认 reasoning_effort，
        # 配上去同样被静默忽略，症状和上面一模一样。
        # 有了 `providers:` 档案之后这种错配更容易发生——阶段从 xj189 换回
        # minimax 时忘了删 reasoning_effort，就正好踩上。
        if self.reasoning_effort is not None and self.provider == "minimax":
            raise ConfigError(
                f"环节 {self.stage} 配了 reasoning_effort，但 provider 是 minimax。"
                "reasoning_effort 是 OpenAI 兼容平台的参数，MiniMax 用 "
                "output_config.effort —— 配错会被静默忽略。"
            )


class ModelRegistry:
    """读 config/models.yaml。"""

    def __init__(self, path: str | Path | None = None) -> None:
        import yaml

        self.path = Path(path or REPO_ROOT / "config" / "models.yaml")
        self.raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        self._cache: dict[str, StageConfig] = {}

    def stage(self, name: str) -> StageConfig:
        if name in self._cache:
            return self._cache[name]
        s = (self.raw.get("stages") or {}).get(name)
        if s is None:
            raise ConfigError(f"models.yaml 里没有环节 {name}")
        d = self.raw.get("defaults", {})

        # 阶段可以只写 `provider: xj189`，其余（模型名 / 地址 / 密钥 / 思考档位）
        # 从 `providers:` 档案里继承。**这是「模型不写死在代码里」的关键**：
        # 换模型、换平台只改配置的一行，不必把 7 行抄到每个阶段上。
        prov_name = s.get("provider") or d.get("provider")
        prof = (self.raw.get("providers") or {}).get(prov_name) or {}
        if not s.get("model") and prof.get("model"):
            s = {**prof, **{k: v for k, v in s.items() if k != "provider"}}
        elif prof:
            # 档案补齐缺失项，阶段自己写的优先
            s = {**{k: v for k, v in prof.items() if k != "provider"}, **s}
            s["provider"] = prov_name

        if name in NEEDS_GUARD and s.get("guard") is None:
            # 之前这里默默用 GuardPolicy 的默认值，配置被删或拼错都不报错，
            # 防线没了却看不出来。护栏是显式决策，不该有隐式兜底。
            raise ConfigError(
                f"环节 {name} 必须显式配置 guard"
                "（max_reasoning_ratio / retry_on_empty / max_output_tokens）。"
                "这三个是 P6 定的三道防线，缺一项都意味着该环节裸奔。"
            )
        cfg = StageConfig(
            stage=name,
            provider=s.get("provider", d.get("provider", "opencode")),
            model=s.get("model", d.get("model", "")),
            max_output_tokens=int(
                s.get("max_output_tokens", d.get("max_output_tokens", 4096))),
            temperature=float(s.get("temperature", d.get("temperature", 0.0))),
            reasoning_effort=s.get("reasoning_effort"),
            thinking_effort=s.get("thinking_effort"),
            timeout_s=float(s.get("timeout_s", d.get("timeout_s", 180))),
            base_url=s.get("base_url", d.get("base_url", "")).rstrip("/"),
            api_key_env=s.get("api_key_env") or d.get("api_key_env"),
            batch_size=int(s.get("batch_size", 20)),
            stream=bool(s.get("stream", d.get("stream", False))),
            guard=GuardPolicy.from_dict(s.get("guard")),
        )
        cfg.validate()
        self._cache[name] = cfg
        return cfg

    def alternatives(self) -> list[dict]:
        return self.raw.get("alternatives", []) or []

    def session_header(self) -> str:
        """OpenCode 要求带 session 头把同一会话路由到同一上游以保住 prompt 缓存。"""
        return uuid.uuid4().hex


def to_openai_tools(tools: list[dict]) -> list[dict]:
    """Anthropic 形状的工具定义 → OpenAI 形状。

    ⚠️ **这个转换曾经不存在**，2026-10-09 首次切到 OpenAI 兼容平台时
    整场 write 的工具循环全挂：

        xj189 400 MODEL_INVALID_REQUEST:
        "The request failed because it is missing `tools.function` parameter"

    `EvidenceTools.specs()` 返回的是 Anthropic 形状
    （`{name, description, input_schema}`，R5 时为 MiniMax 写的），
    而 OpenAI 系要求 `{type:"function", function:{name, parameters}}`。

    更该记的是**预检为什么没发现**：`tests/preflight_xj189.py` 里
    我是**手搓标准格式**去测的，测通了就写「工具调用 ✓」——
    **测的不是项目真正会发的那种**。坑 #1 的第四次形态。
    """
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        # 已经是 OpenAI 形状就别动（自己写的 fixture 可能就是这个形状）
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            out.append(t)
            continue
        name = t.get("name")
        if not name:
            continue
        out.append({"type": "function", "function": {
            "name": name,
            "description": t.get("description") or "",
            "parameters": t.get("input_schema")
                         or t.get("parameters")
                         or {"type": "object", "properties": {}},
        }})
    return out


class SSEAccum:
    """把 OpenAI 形状的 SSE 增量攒成一次完整响应。

    **为什么需要它**：xj189 前面有 nginx 的 `proxy_read_timeout 60s`
    （实测 `write_audit` 连撞三次 504），而流式输出能绕过——
    nginx 测的是「两次读到数据之间的间隔」，流式持续推块就永不空闲。
    实测同参数流式跑过 245.5 秒，非流式 61.0 秒就 504。

    **最容易错的地方**：工具调用的 `function.arguments` 是**分片到达**的
    （实测 570 片拼成 2,430 字符），必须按 `index` 累积再拼，
    一次 `json.loads` 会直接炸。而工具失效是**静默的**——降级成单次调用、
    不报错，所以这里拼错很难发现。

    顺带把 `reasoning_content` 也攒起来（实测 174 块增量），
    前端要做「展示思考」时直接用。
    """

    def __init__(self, on_delta: Optional[Callable[[str, str], None]] = None,
                 stage: str = "") -> None:
        self.text: list[str] = []
        self.reasoning: list[str] = []
        self.usage: dict = {}
        # index -> {"id", "name", "args": [片段…]}
        self._calls: dict[int, dict] = {}
        self.role: str | None = None
        # 增量回调（2026-10-11 加，给前端展示思考过程用）。
        # **默认 None = 完全保持原行为**——现有 7 个开流式的环节都没传。
        self._on_delta = on_delta
        self.stage = stage

    def _fire(self, kind: str, chunk: str) -> None:
        if self._on_delta:
            try:
                self._on_delta(kind, chunk)
            except Exception:
                # 展示是锦上添花，不能因为展示失败拖垮调用
                pass

    def feed(self, obj: dict) -> None:
        if obj.get("usage"):
            self.usage = obj["usage"]
        for ch in obj.get("choices") or []:
            d = ch.get("delta") or {}
            if d.get("role"):
                self.role = d["role"]
            if d.get("content"):
                self.text.append(d["content"])
                self._fire("text", d["content"])
            if d.get("reasoning_content"):
                self.reasoning.append(d["reasoning_content"])
                self._fire("reasoning", d["reasoning_content"])
            for tc in (d.get("tool_calls") or []):
                # index 缺省时退化成 0——单工具调用很常见
                i = tc.get("index")
                i = 0 if i is None else int(i)
                slot = self._calls.setdefault(
                    i, {"id": None, "name": None, "args": []})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["args"].append(fn["arguments"])

    def blocks(self) -> list[dict]:
        """攒成 Anthropic 形状的块，和非流式路径的返回保持一致。"""
        out: list[dict] = []
        txt = "".join(self.text)
        if txt:
            out.append({"type": "text", "text": txt})
        for i in sorted(self._calls):
            slot = self._calls[i]
            raw = "".join(slot["args"])
            try:
                args = json.loads(raw) if raw else {}
            except Exception:
                # 拼出来的 JSON 坏了：宁可空参数，也不能让整个工具循环崩掉
                args = {}
            out.append({"type": "tool_use", "id": slot["id"],
                        "name": slot["name"], "input": args})
        return out

    def text_only(self) -> str:
        return "".join(self.text)


def _hook_for(stage: str) -> Optional[Callable[[str, str], None]]:
    """当前阶段要不要开增量推送。

    **只给值得看的环节开**：关思考的环节（relevance / summarize / extract）
    没有 reasoning，增量全是正文碎片，推给前端只是噪音。
    开思考的（assess / outline / write_* / probe_outline）才有东西可看。
    """
    if _DELTA_HOOK.get() is None:
        return None
    if stage in _NO_DELTA_STAGES:
        return None
    def _cb(kind: str, chunk: str) -> None:
        if kind == "text" and stage in _NO_TEXT_DELTA_STAGES:
            return          # JSON 环节的正文不给用户看
        _fire_delta(stage, kind, chunk)
    return _cb


def _sse_lines(resp) -> Iterator[dict]:
    """把 SSE 响应流解析成一个个 JSON 对象（`[DONE]` 与 keep-alive 丢掉）。"""
    import json as _json
    for line in resp.iter_lines():
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            yield _json.loads(data)
        except Exception:
            continue


class LLMClient:
    """统一调用入口。厂商差异收敛在这里。"""

    def __init__(self, registry: ModelRegistry | None = None) -> None:
        self.registry = registry or ModelRegistry()
        self._client = httpx.Client(timeout=180.0)
        self.warnings: list[str] = []
        # 排队超时的累计次数。**不是重试能解决的问题**——并发降不下来它还会来，
        # 所以单独记一个计数：`cost_report.py` 读它来判断该不该调 GATE_WORKERS。
        # （2026-10-10 补：之前只写不读，是个死变量，等于白记。）
        self._queue_pressure = 0

    def close(self) -> None:
        self._client.close()

    # ---------- 对外 ----------

    def complete(
        self,
        stage: str,
        prompt: str,
        system: str | None = None,
        json_mode: bool = False,
    ) -> str:
        cfg = self.registry.stage(stage)
        t0 = time.time()
        try:
            if cfg.provider in OPENAI_COMPAT:
                text, usage = self._opencode(cfg, prompt, system, json_mode)
            elif cfg.provider == "minimax":
                text, usage = self._minimax(cfg, prompt, system, json_mode)
            elif cfg.provider == "gemini":
                text, usage = self._gemini(cfg, prompt, system, json_mode)
            else:
                raise ConfigError(f"未知 provider: {cfg.provider}")
        except Exception as e:
            # 抛出去之前先把这次失败记下来——重试是调用方的事，
            # 记账是这里的事，两者不能因为一个挂掉另一个。
            self._record_error(cfg, prompt, e,
                               int((time.time() - t0) * 1000))
            raise
        self._record(cfg, prompt, text, usage)
        return text

    def complete_json(self, stage: str, prompt: str, system: str | None = None) -> Any:
        raw = self.complete(stage, prompt, system=system, json_mode=True)
        try:
            return _parse_json(raw)
        except Exception as e:
            # `complete()` 已经记了一行 ok——token 确实花了，不该抹掉。
            # 这里补记一行 error，表示这次尝试**业务上没成**。
            # 两行的 token 不重复（这行记 0），所以 `session_cost()`
            # 的花费统计不受影响；只有「调用次数」要按 status 过滤。
            self._record_error(self.registry.stage(stage), prompt, e)
            raise

    def complete_json_forced(self, stage: str, prompt: str,
                             name: str, schema: dict,
                             system: str | None = None) -> Any:
        """**用工具调用把输出约束成 JSON**（约束解码的可用替代）。

        为什么不用纯提示词 + `response_format`：
        纯提示词是让模型「手写」一整段 JSON 文本，实测在 OpenAI 兼容平台上
        **长 JSON 会在中途崩**（2026-10-09：xj189 段落概括 9% 失败率，
        报 `Expecting ',' delimiter: line 1 column 630`——第 630 字符，
        **中间位置而非结尾截断**，就是写崩了）。

        改成工具之后，模型只填 `arguments` 对象，**平台在解码层面就保证
        它是合法 JSON**，不需要模型自己维持引号/逗号/括号的配平。
        同一组数据实测 3/3 通过、5.2~8.8 秒（纯提示词是 9% 失败）。

        ⚠️ **必须用 `tool_choice` 强制指定**。只把工具列进 `tools` 的话，
        模型可能选择直接回文本而不调工具——那就退化成纯提示词了。
        """
        tool = {"type": "function", "function": {
            "name": name, "description": f"以结构化形式返回 {stage} 的结果",
            "parameters": schema,
        }}
        cfg = self.registry.stage(stage)
        t0 = time.time()
        try:
            blocks, usage = self._openai_compat_forced(cfg, system, prompt, tool)
        except Exception as e:
            self._record_error(cfg, prompt, e, int((time.time() - t0) * 1000))
            raise
        for b in blocks:
            if b.get("type") == "tool_use":
                # 先确认拿到了工具调用再记账——下面那个 EmptyOutput 是
                # 业务失败不是调用失败，记成 ok 会把失败藏起来。
                self._record(cfg, prompt, "", usage)
                return b.get("input")
        self._record_error(cfg, prompt, EmptyOutput(
            f"环节 {stage} 要求调用 {name}，但模型返回了文本而不是工具调用。"
            "检查是不是漏传 tool_choice。"), int((time.time() - t0) * 1000))
        raise EmptyOutput(
            f"环节 {stage} 要求调用 {name}，但模型返回了文本而不是工具调用。"
            "检查是不是漏传 tool_choice。")

    # ---------- 工具调用 ----------

    def complete_with_tools(
        self,
        stage: str,
        system: str | None,
        messages: list[dict],
        tools: list[dict],
    ) -> dict:
        """带工具的一次调用。返回原始 content 块与 usage。

        为什么需要它：一次调用解决不了「我得先看看才决定」的问题。
        以前的做法是让模型在回复里点名要哪几条的全文（need_full），
        但那要求它在**还没看到全文时**就预测自己需要什么，
        漏了它自己不知道。改成工具后可以边看边决定。

        协议上两个坑（都实测踩过）：
          · 一轮可能发**多个** tool_use，每个都要回一个 tool_result，
            少一个或顺序不对就 400 `tool call and result not match`
          · tool_use 后面不能直接跟普通文本轮次，必须先 tool_result
        """
        cfg = self.registry.stage(stage)
        t0 = time.time()
        try:
            if cfg.provider in OPENAI_COMPAT:
                blocks, usage = self._opencode_tools(cfg, system, messages, tools)
            elif cfg.provider == "minimax":
                blocks, usage = self._minimax_tools(cfg, system, messages, tools)
            else:
                raise ConfigError(
                    f"环节 {stage} 的 provider {cfg.provider} 暂不支持工具调用")
        except Exception as e:
            self._record_error(cfg, _msgs_chars(messages), e,
                               int((time.time() - t0) * 1000))
            raise
        text = "".join(b.get("text", "") for b in blocks
                       if b.get("type") == "text")
        self._record(cfg, _msgs_chars(messages), text, usage)
        return {"blocks": blocks, "usage": usage}

    # ---------- 记账（P7：只记不告警） ----------

    def _record_error(self, cfg: StageConfig, prompt: str, exc: BaseException,
                      ms: int | None = None) -> None:
        """记一次**失败的**调用（2026-10-13 加）。

        为什么必须有：`_record` 只在成功路径上调用，所以 504 / 429 /
        连接超时 / JSON 解析失败这些情况，`llm_call` 里**查不到任何痕迹**。
        真出问题时，能看到的只有「这一轮安静地过去了」，无从下手。

        token 记 0，但留 `est_input_tokens`——这次尝试的规模还是值得知道，
        它能回答「是不是长 prompt 更容易崩」这类问题。
        """
        try:
            from ..db.repo import get_repo

            get_repo().log_llm_call(
                session_id=_CURRENT_SESSION.get(),
                stage=cfg.stage,
                model=f"{cfg.provider}/{cfg.model}",
                input_tokens=0,
                output_tokens=0,
                latency_ms=ms,
                est_input_tokens=_rough_tokens(prompt),
                status="error",
                error=f"{type(exc).__name__}: {exc}"[:500],
            )
        except Exception:
            pass  # 记账失败不影响主流程

    def _record(self, cfg: StageConfig, prompt: str, out: str,
                usage: dict | None = None) -> None:
        """记一次调用。usage 是 API 报上来的真实值，缺失时退回本地估算。

        真实值里最要紧的是缓存命中：write 环节 12 个子 agent
        共享一段固定前缀，它决定我们到底省没省钱。只记本地估算的话，
        这个数永远看不到。各家 usage 字段名不同，统一走 `_cache_read` 收口。
        """
        try:
            from ..db.repo import get_repo

            u = usage or {}
            get_repo().log_llm_call(
                session_id=_CURRENT_SESSION.get(),
                stage=cfg.stage,
                model=f"{cfg.provider}/{cfg.model}",
                input_tokens=(int(u.get("input_tokens") or 0)
                              or int(u.get("prompt_tokens") or 0)
                              or _rough_tokens(prompt)),
                output_tokens=(int(u.get("output_tokens") or 0)
                               or int(u.get("completion_tokens") or 0)
                               or _rough_tokens(out)),
                latency_ms=int(u.get("latency_ms") or 0) or None,
                cache_read_tokens=_cache_read(u),
                cache_creation_tokens=int(u.get("cache_creation_input_tokens") or 0),
                est_input_tokens=_rough_tokens(prompt),
            )
        except Exception:
            pass  # 记账失败不影响主流程

    # ---------- OpenCode ----------

    def _headers(self, cfg: StageConfig) -> dict:
        """请求头。平台特有头在这里按 provider 挂，**不进代码逻辑**。

        opencode 要一个会话头做归因；xj189 不需要（它的鉴权只有 Bearer）。
        """
        h = {"Authorization": f"Bearer {os.environ.get(cfg.api_key_env or '', '')}",
             "Content-Type": "application/json"}
        if cfg.provider == "opencode":
            h["x-opencode-session"] = self.registry.session_header()
        return h

    def _opencode(
        self, cfg: StageConfig, prompt: str, system: str | None, json_mode: bool
    ) -> tuple[str, dict]:
        key = os.environ.get(cfg.api_key_env or "", "")
        if not key:
            raise ConfigError(
                f"未设置 {cfg.api_key_env or '(该阶段未配置 api_key_env)'}")

        msgs = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        budget = cfg.max_output_tokens
        # 两种失败的额度**分开算**，不互相抢。理由见 GuardPolicy.retry_on_transport。
        guarded = cfg.stage in NEEDS_GUARD
        empty_left = cfg.guard.retry_on_empty if guarded else 0
        net_left = cfg.guard.retry_on_transport if guarded else 0
        attempts = 1 + empty_left + net_left
        last_reason = ""
        drops = 0          # 因连接中断而重试的次数，用于在恢复时明确告知
        # 限流有**独立的额度**：它不是抖动，是服务端要求放慢，混进传输额度
        # 会让一次限流就把重试机会耗光（2026-10-08 relevance 连撞 429 实测）。
        limit_left = 3
        # 限流重试不占 attempts（那里面是空返回和断连的额度），
        # 所以循环上限要多放行几次，否则限流额度根本用不到。
        max_tries = attempts + limit_left

        for attempt in range(max_tries):
            payload: dict[str, Any] = {
                "model": cfg.model,
                "messages": msgs,
                "max_tokens": budget,
                "temperature": cfg.temperature,
            }
            # 该模型只接受 minimal / low 这类档位，none 与 0 会被 400 拒绝
            if cfg.reasoning_effort:
                payload["reasoning_effort"] = cfg.reasoning_effort
            if json_mode:
                payload["response_format"] = {"type": "json_object"}

            # ---- 流式分支：只为绕开 xj189 前面 nginx 的 60 秒读超时 ----
            # 见 StageConfig.stream 的说明与 SSEAccum。
            if cfg.stream:
                payload["stream"] = True
                payload["stream_options"] = {"include_usage": True}
                t0 = time.time()
                try:
                    with self._client.stream(
                            "POST", f"{cfg.base_url}/chat/completions",
                            headers=self._headers(cfg), json=payload,
                            timeout=cfg.timeout_s) as resp:
                        if resp.status_code != 200:
                            body = resp.read().decode("utf-8", "replace")
                            raise RuntimeError(
                                f"{cfg.provider} {resp.status_code}: {body[:300]}")
                        acc = SSEAccum(_hook_for(cfg.stage), cfg.stage)
                        for obj in _sse_lines(resp):
                            acc.feed(obj)
                except httpx.TransportError as e:
                    last_reason = f"transport: {type(e).__name__}"
                    if net_left <= 0:
                        self.warnings.append(
                            f"[{cfg.stage}] 流式连接中断额度用尽"
                            f"（{type(e).__name__}）")
                        raise
                    net_left -= 1
                    time.sleep(2.0 * (attempt + 1))
                    continue
                txt = acc.text_only()
                usage = dict(acc.usage or {})
                usage["latency_ms"] = int((time.time() - t0) * 1000)
                if not txt:
                    # 流式下空返回同样是真实失效模式，走同一套翻倍预算
                    last_reason = "empty"
                    if empty_left <= 0:
                        break
                    empty_left -= 1
                    budget = min(cfg.guard.max_output_tokens, budget * 2)
                    self.warnings.append(
                        f"[{cfg.stage}] 流式返回空内容，预算翻倍至 {budget} "
                        f"后重试（空返回额度还剩 {empty_left}）")
                    continue
                return txt, usage

            t0 = time.time()
            try:
                r = self._client.post(
                    f"{cfg.base_url}/chat/completions",
                    headers=self._headers(cfg),
                    json=payload,
                    timeout=cfg.timeout_s,
                )
            except httpx.TransportError as e:
                # 这个重试是**吸收偶发抖动**，不是解决长生成失败。
                #
                # 实测两类断连要分开看（2026-10-05）：
                #   · 偶发：子 agent 干净对照连跑 4 次全通（18 到 25 秒），
                #     但整场跑下来仍偶发。重试在这里有用，得留。
                #   · 确定性：write_plan 在 effort=high + 2.4 万输入下
                #     连续三次全断，参数完全相同重发必然还断。重试纯属浪费。
                #     根因查出来了：是 timeout 180 秒太贴（write_audit 均 132 秒、
                #     最长 170 秒），已提到 300。
                #
                # 真正有效的控制手段在别处：压低思考（output_config.effort）、
                # 切小单次输入、把过重的调用拆开。额度用尽会抛上去，
                # 由上层降级路径接手，不静默。
                last_reason = f"transport: {type(e).__name__}"
                drops += 1
                if net_left <= 0:
                    self.warnings.append(
                        f"[{cfg.stage}] 连接中断额度用尽（共 {drops + net_left} 次，"
                        f"最后 {type(e).__name__}），该环节判定不可用")
                    print(f"❌ [{cfg.stage}] 连接中断额度用尽，放弃", flush=True)
                    raise
                net_left -= 1
                self.warnings.append(
                    f"[{cfg.stage}] 第 {attempt+1} 次连接中断"
                    f"（{type(e).__name__}，传输额度还剩 {net_left}），退避重试")
                print(f"⚠️  [{cfg.stage}] 连接中断（传输额度剩 {net_left}），退避重试",
                      flush=True)
                time.sleep(2.0 * (attempt + 1))
                continue
            if drops:
                print(f"✅ [{cfg.stage}] 第 {attempt+1} 次重试成功"
                      f"（此前中断 {drops} 次）", flush=True)
            if r.status_code != 200:
                retry, why = _retryable_http(r.status_code, r.text)
                # 限流**不占传输额度**：它不是抖动，是服务端要求放慢。
                # 混进 retry_on_transport 的话，一次限流就把重试机会耗光
                # （2026-10-08 实测 relevance 连撞 429，退避到额度剩 0 还在撞）。
                kind, wait = classify_limit(r.text, dict(r.headers))
                if retry and limit_left > 0 and kind != "seat_quota":
                    limit_left -= 1
                    # 排队超时不能等——并发的根源没变，等多久它还会排不下。
                    # 短等一下（让队列自己泄掉一点）就重试，
                    # 并且**明确告诉调用方该降并发**，别让人以为重试能解决。
                    if kind == "queue_timeout":
                        sleep_s = min(wait, 5.0)
                        self._queue_pressure += 1
                        note = "（排队超时：重试无用，**该降并发**）"
                    else:
                        sleep_s = min(max(wait, 3.0 * (attempt + 1)), 60.0)
                        note = ""
                    self.warnings.append(
                        f"[{cfg.stage}] HTTP {r.status_code} {why}，"
                        f"等待 {sleep_s:.0f}s 后重试"
                        f"（限流额度剩 {limit_left}）{note}")
                    print(f"⚠️  [{cfg.stage}] HTTP {r.status_code} {why}，"
                          f"等待 {sleep_s:.0f}s 重试（限流额度剩 {limit_left}）{note}",
                          flush=True)
                    time.sleep(sleep_s)
                    continue
                raise RuntimeError(f"{cfg.provider} {r.status_code}: {r.text[:300]}")
            data = r.json()
            msg = data["choices"][0]["message"]
            text = (msg.get("content") or "").strip()
            usage = data.get("usage") or {}
            reasoning = (
                (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
            )
            completion = usage.get("completion_tokens") or 0
            usage["latency_ms"] = int((time.time() - t0) * 1000)

            # 防线二：思考占比告警
            if completion and reasoning / completion > cfg.guard.max_reasoning_ratio:
                msg_w = (
                    f"[{cfg.stage}] reasoning 占比 {reasoning}/{completion} "
                    f"超过阈值 {cfg.guard.max_reasoning_ratio:.0%}，"
                    f"该环节可能不适合 {cfg.model}"
                )
                self.warnings.append(msg_w)
                print(f"⚠️  {msg_w}", flush=True)

            # 防线一：内容为空则重试并翻倍预算（额度独立于传输断连）
            if not text:
                last_reason = "empty"
                if empty_left <= 0:
                    break
                empty_left -= 1
                budget = min(cfg.guard.max_output_tokens, budget * 2)
                self.warnings.append(
                    f"[{cfg.stage}] 第 {attempt+1} 次返回空内容，"
                    f"预算翻倍至 {budget} 后重试（空返回额度还剩 {empty_left}）"
                )
                continue
            return text, usage

        raise EmptyOutput(
            f"环节 {cfg.stage} 的重试额度用尽仍未拿到内容"
            f"（模型 {cfg.model}，空返回 {cfg.guard.retry_on_empty} 次、"
            f"连接中断 {drops} 次，最后原因 {last_reason}）。"
            f"这正是 M3.1 Flash 那种 thinking 吃光预算的失效模式。"
        )

    # ---------- MiniMax（Anthropic Messages 格式） ----------

    def _minimax(
        self, cfg: StageConfig, prompt: str, system: str | None, json_mode: bool
    ) -> tuple[str, dict]:
        key = os.environ.get(cfg.api_key_env or "MINIMAX_API_KEY", "")
        if not key:
            raise ConfigError(f"未设置 {cfg.api_key_env or 'MINIMAX_API_KEY'}")
        base = cfg.base_url or "https://api.minimaxi.com/anthropic"
        url = f"{base.rstrip('/')}/v1/messages"
        # Anthropic 协议用顶层 system 字段，不要伪装成对话消息
        payload: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": cfg.max_output_tokens,
            "temperature": cfg.temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system
        # 思考深度走 output_config.effort，不走 Anthropic 的 thinking.budget_tokens。
        #
        # 这里曾经写的是 thinking={"type":"enabled","budget_tokens":N}，但 MiniMax
        # 的 thinking.type 合法值只有 adaptive 与 disabled，没有 enabled。
        # 那个字段不报 400，被静默忽略，于是「0 / 1024 / 4096 / 不传」四组设置
        # 实际是同一组（都等于省略 → adaptive + 默认 effort=max），思考长度清一色
        # ~35,000 字符。当时据此得出「无法控制思考，只能换模型」——结论的来源
        # 是一次测错了参数名的黑盒实验。
        #
        # 官方文档：M3.1-Flash-Preview 的 effort 取 low/medium/high/xhigh/max，
        # 省略即 max，none 与 disabled 都会 400（该模型要求 adaptive）。
        # MiniMax-M3 则相反——省略 thinking 即关闭思考，且不接受 effort。
        if cfg.thinking_effort:
            payload["output_config"] = {"effort": cfg.thinking_effort}
        if json_mode:
            # Anthropic 协议没有 response_format，用提示词约束
            payload["messages"] = [{
                "role": "user",
                "content": prompt + "\n\n只输出 JSON，不要任何解释文字或代码围栏。",
            }]

        budget = cfg.max_output_tokens
        # 传输断连与空返回的额度分开算，理由见 GuardPolicy.retry_on_transport。
        guarded = cfg.stage in NEEDS_GUARD
        empty_left = cfg.guard.retry_on_empty if guarded else 0
        net_left = cfg.guard.retry_on_transport if guarded else 0
        attempts = 1 + empty_left + net_left
        drops = 0          # 因连接中断而重试的次数，用于在恢复时明确告知
        last_reason = ""
        for attempt in range(attempts):
            if attempt > 0:
                payload["max_tokens"] = budget

            t0 = time.time()
            try:
                r = self._client.post(
                    url,
                    headers={"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json",
                             "x-api-key": key},
                    json=payload,
                    timeout=cfg.timeout_s,
                )
            except httpx.TransportError as e:
                # 吸收偶发抖动，不解决长生成的确定性失败。理由见 _opencode 同一处。
                drops += 1
                last_reason = f"transport: {type(e).__name__}"
                if net_left <= 0:
                    self.warnings.append(
                        f"[{cfg.stage}] 连接中断额度用尽（共 {drops + net_left} 次，"
                        f"最后 {type(e).__name__}），该环节判定不可用")
                    print(f"❌ [{cfg.stage}] 连接中断额度用尽，放弃", flush=True)
                    raise
                net_left -= 1
                self.warnings.append(
                    f"[{cfg.stage}] 第 {attempt+1} 次连接中断"
                    f"（{type(e).__name__}，传输额度还剩 {net_left}），退避重试")
                print(f"⚠️  [{cfg.stage}] 连接中断（传输额度剩 {net_left}），退避重试",
                      flush=True)
                time.sleep(2.0 * (attempt + 1))
                continue
            if drops:
                print(f"✅ [{cfg.stage}] 第 {attempt+1} 次重试成功"
                      f"（此前中断 {drops} 次）", flush=True)
            if r.status_code != 200:
                retry, why = _retryable_http(r.status_code, r.text)
                if retry and net_left > 0:
                    net_left -= 1
                    self.warnings.append(
                        f"[{cfg.stage}] HTTP {r.status_code} {why}，退避重试（额度剩 {net_left}）")
                    print(f"⚠️  [{cfg.stage}] HTTP {r.status_code} {why}，退避重试（额度剩 {net_left}）", flush=True)
                    time.sleep(3.0 * (attempt + 1))
                    continue
                raise RuntimeError(f"MiniMax {r.status_code}: {r.text[:300]}")
            blocks = r.json().get("content") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
            think = "".join(b.get("thinking", "") for b in blocks if b.get("type") == "thinking")
            usage = r.json().get("usage") or {}
            out_tok = usage.get("output_tokens") or 0
            usage["latency_ms"] = int((time.time() - t0) * 1000)

            if think and out_tok:
                ratio = len(think) / max(1, len(text) + len(think))
                if ratio > cfg.guard.max_reasoning_ratio:
                    msg_w = (f"[{cfg.stage}] 思考占比 {len(think)} 字符偏高，"
                             f"模型 {cfg.model} 可能不匹配该环节")
                    self.warnings.append(msg_w)
                    print(f"⚠️  {msg_w}", flush=True)

            if not text:
                last_reason = "empty"
                if empty_left <= 0:
                    break
                empty_left -= 1
                self.warnings.append(
                    f"[{cfg.stage}] 第 {attempt+1} 次无可见输出（思考 {len(think)} 字符），"
                    f"预算翻倍至 {min(cfg.guard.max_output_tokens, budget*2)} 后重试"
                    f"（空返回额度还剩 {empty_left}）"
                )
                budget = min(cfg.guard.max_output_tokens, budget * 2)
                continue
            return text, usage

        raise EmptyOutput(
            f"环节 {cfg.stage} 的重试额度用尽仍未拿到内容"
            f"（模型 {cfg.model}，空返回 {cfg.guard.retry_on_empty} 次、"
            f"连接中断 {drops} 次，最后原因 {last_reason}）。"
            f"这是 reasoning 模型思考吃光预算的典型失效模式。"
        )

    # ---------- 工具调用的两个后端 ----------

    def _minimax_tools(self, cfg: StageConfig, system: str | None,
                       messages: list[dict], tools: list[dict]) -> tuple[list, dict]:
        key = os.environ.get(cfg.api_key_env or "MINIMAX_API_KEY", "")
        if not key:
            raise ConfigError(f"未设置 {cfg.api_key_env or 'MINIMAX_API_KEY'}")
        base = cfg.base_url or "https://api.minimaxi.com/anthropic"
        url = f"{base.rstrip('/')}/v1/messages"
        payload: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": cfg.max_output_tokens,
            "temperature": cfg.temperature,
            "tools": tools,
            "messages": messages,
        }
        if system:
            payload["system"] = system
        if cfg.thinking_effort:
            payload["output_config"] = {"effort": cfg.thinking_effort}
        # tools 参与缓存前缀且排在 system 之前，所以循环里 tools 必须始终一致
        t0 = time.time()
        r = self._client.post(
            url,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "x-api-key": key},
            json=payload,
            timeout=cfg.timeout_s,
        )
        if r.status_code != 200:
            raise RuntimeError(f"MiniMax {r.status_code}: {r.text[:300]}")
        j = r.json()
        usage = j.get("usage") or {}
        usage["latency_ms"] = int((time.time() - t0) * 1000)
        return j.get("content") or [], usage

    def _opencode_tools(self, cfg: StageConfig, system: str | None,
                        messages: list[dict], tools: list[dict]) -> tuple[list, dict]:
        """OpenAI 形状。返回 Anthropic 形状的块，好让调用方不必分支。"""
        key = os.environ.get(cfg.api_key_env or "", "")
        if not key:
            raise ConfigError(
                f"未设置 {cfg.api_key_env or '(该阶段未配置 api_key_env)'}")
        msgs = ([{"role": "system", "content": system}] if system else []) + [
            {"role": m["role"], "content": m["content"]} for m in messages]
        payload: dict[str, Any] = {
            "model": cfg.model, "messages": msgs,
            "tools": to_openai_tools(tools),
            "max_tokens": cfg.max_output_tokens, "temperature": cfg.temperature,
        }
        if cfg.reasoning_effort:
            payload["reasoning_effort"] = cfg.reasoning_effort
        t0 = time.time()

        # ---- 流式分支 ----
        # 工具循环是 write_section 的主战场（实测 30 次调用 / 33 次工具读取），
        # 而它**每轮都带上一轮的工具结果**，输入比单次调用大得多。
        # 实测平均 14.5s、最长 48.9s —— 工具那一轮就撞 60 秒墙，
        # 降级成单次调用（子 agent 拿不到证据全文，**而且不报错**）。
        if cfg.stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
            with self._client.stream(
                    "POST", f"{cfg.base_url}/chat/completions",
                    headers=self._headers(cfg), json=payload,
                    timeout=cfg.timeout_s) as resp:
                if resp.status_code != 200:
                    body = resp.read().decode("utf-8", "replace")
                    raise RuntimeError(
                        f"{cfg.provider} {resp.status_code}: {body[:300]}")
                acc = SSEAccum(_hook_for(cfg.stage), cfg.stage)
                for obj in _sse_lines(resp):
                    acc.feed(obj)
            usage = dict(acc.usage or {})
            usage["latency_ms"] = int((time.time() - t0) * 1000)
            return acc.blocks(), usage

        r = self._client.post(
            f"{cfg.base_url}/chat/completions",
            headers=self._headers(cfg),
            json=payload, timeout=cfg.timeout_s,
        )
        if r.status_code != 200:
            raise RuntimeError(f"{cfg.provider} {r.status_code}: {r.text[:300]}")
        j = r.json()
        msg = j["choices"][0]["message"]
        blocks: list[dict] = []
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            blocks.append({"type": "tool_use", "id": tc.get("id"),
                           "name": fn.get("name"), "input": args})
        if msg.get("content"):
            blocks.append({"type": "text", "text": msg["content"]})
        u = j.get("usage") or {}
        u["latency_ms"] = int((time.time() - t0) * 1000)
        return blocks, u

    def _openai_compat_forced(self, cfg: StageConfig, system: str | None,
                              prompt: str, tool: dict) -> tuple[list, dict]:
        """带 `tool_choice` 的强制调用。返回 Anthropic 形状的块。

        与 `_opencode_tools` 的区别只有一个：多发 `tool_choice`，
        让平台**强制**模型走工具而不是回文本。这是
        `complete_json_forced` 的后端，见那里的说明。
        """
        key = os.environ.get(cfg.api_key_env or "", "")
        if not key:
            raise ConfigError(
                f"未设置 {cfg.api_key_env or '(该阶段未配置 api_key_env)'}")
        msgs = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}]
        payload: dict[str, Any] = {
            "model": cfg.model, "messages": msgs,
            "tools": [tool],
            "tool_choice": {"type": "function",
                            "function": {"name": tool["function"]["name"]}},
            "max_tokens": cfg.max_output_tokens, "temperature": cfg.temperature,
        }
        if cfg.reasoning_effort:
            payload["reasoning_effort"] = cfg.reasoning_effort
        t0 = time.time()
        r = self._client.post(
            f"{cfg.base_url}/chat/completions",
            headers=self._headers(cfg),
            json=payload, timeout=cfg.timeout_s,
        )
        if r.status_code != 200:
            raise RuntimeError(f"{cfg.provider} {r.status_code}: {r.text[:300]}")
        j = r.json()
        msg = j["choices"][0]["message"]
        blocks: list[dict] = []
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            blocks.append({"type": "tool_use", "id": tc.get("id"),
                           "name": fn.get("name"), "input": args})
        if msg.get("content"):
            blocks.append({"type": "text", "text": msg["content"]})
        u = j.get("usage") or {}
        u["latency_ms"] = int((time.time() - t0) * 1000)
        return blocks, u

    # ---------- Gemini（保留，便于将来切回） ----------

    def _gemini(
        self, cfg: StageConfig, prompt: str, system: str | None, json_mode: bool
    ) -> tuple[str, dict]:
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise ConfigError("未设置 GEMINI_API_KEY")
        gc: dict[str, Any] = {
            "maxOutputTokens": cfg.max_output_tokens,
            "temperature": cfg.temperature,
        }
        if json_mode:
            gc["responseMimeType"] = "application/json"
        payload: dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": gc,
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        t0 = time.time()
        r = self._client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{cfg.model}:generateContent",
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json=payload,
            timeout=cfg.timeout_s,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Gemini {r.status_code}: {r.text[:300]}")
        j = r.json()
        cand = j.get("candidates") or [{}]
        parts = (cand[0].get("content") or {}).get("parts") or []
        u = j.get("usageMetadata") or {}
        return "".join(p.get("text", "") for p in parts), {
            "input_tokens": u.get("promptTokenCount") or 0,
            "output_tokens": u.get("candidatesTokenCount") or 0,
            "cache_read_input_tokens": u.get("cachedContentTokenCount") or 0,
            "latency_ms": int((time.time() - t0) * 1000),
        }


# ---------- 工具 ----------

def _cache_read(usage: dict) -> int:
    """从各家 usage 里取出「缓存命中了多少输入 token」。

    **三个口径都要收**——实测同名概念放在三个不同位置，谁也不规范：
      · Anthropic 系（MiniMax）：`cache_read_input_tokens`
      · OpenAI 标准（DeepSeek 官方）：`prompt_tokens_details.cached_tokens`
      · 聚合平台实测（xj189 2026-10-09）：**顶层** `cached_tokens`

    ⚠️ 最后这条是踩坑 #1 的又一次：先按 OpenAI 标准去 `prompt_tokens_details` 里找，
    读到 0，就得出「平台不支持缓存」的结论；打出来才发现它在**最外层**。
    **字段名对不上不代表字段不存在**——先看原始报文。

    这里只取缓存命中数，**不去改写输入总量的口径**：
    各平台 `prompt_tokens` 到底是「总量」还是「未命中部分」并不统一，
    猜错就是假的命中率。
    """
    u = usage or {}
    for path in (("cache_read_input_tokens",),
                 ("prompt_tokens_details", "cached_tokens"),
                 ("cached_tokens",)):
        cur: object = u
        for k in path:
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(k)
        if cur is not None:
            try:
                return int(cur)
            except (TypeError, ValueError):
                return 0
    return 0


def classify_limit(body: str, headers: dict | None = None) -> tuple[str, float]:
    """平台限流的几种情形**处置完全不同**（2026-10-09 实测）。

    返回 `(kind, retry_after_seconds)`：

    - `"seat_quota"` —— `MODEL_SEAT_QUOTA_EXHAUSTED`。
      **重试无效**，额度得管理员调。再试就是白烧一轮时间和配额。
    - `"queue_timeout"` —— `MODEL_QUEUE_TIMEOUT`「模型请求**排队超时**」。
      ⚠️ **这不是限流，是服务端队列排不下。**实测并发 8 全过、16 就开始报它，
      而平台文档写的是「同一 IP 每分钟 300 次」——**两者不是一回事**：
      窗口没满，但瞬时并发把队列压爆了。
      **处置是降并发，不是等**——等 Retry-After 对它没用，因为并发的
      来源没变（原来会被归到 `rate`，白等 5 秒然后再排一次队）。
    - `"account_concurrency"` —— 「当前账户并发已达上限」。
      等在途调用腾出位置，退避要**更长**（并发槽位释放比频率窗口慢）。
    - `"rate"` —— 「服务组/模型调用频率已达上限」`MODEL_RATE_LIMITED`。
      窗口真满了，按 Retry-After 等。

    `retry_after` 取响应头 `Retry-After`，缺失则按 kind 给保守默认值。
    """
    low = (body or "").lower()
    hdr = {k.lower(): v for k, v in (headers or {}).items()}

    ra = 0.0
    try:
        ra = float(hdr.get("retry-after") or 0)
    except (TypeError, ValueError):
        ra = 0.0

    if "model_seat_quota_exhausted" in low:
        return "seat_quota", max(ra, 60.0)
    # 排队超时要在「rate_limit_error」之前判——响应体的 type 字段就是它，
    # 先按 rate 判会把「等 5 秒」当成处置，而正确做法是降并发。
    if "model_queue_timeout" in low or "排队超时" in (body or ""):
        return "queue_timeout", max(ra, 2.0)
    if "并发已达上限" in (body or "") or "concurrency" in low:
        return "account_concurrency", max(ra, 15.0)
    if "调用频率已达上限" in (body or "") or "model_rate_limited" in low \
            or "rate limit" in low:
        return "rate", max(ra, 5.0)
    return "", ra


def _retryable_http(status: int, body: str,
                    headers: dict | None = None) -> tuple[bool, str]:
    """5xx 该不该重试？**同一个 5xx 码的答案可能相反**（2026-10-06 实测）。

    - `529 overloaded_error` / `rate_limit_error` → **重试**。
      实测一轮研究里 `probe_light` 连续 4 次撞 529，两次尝试全挂，
      探针整个作废 → 轮廓为空 → 大纲退化成 1 节 → 报告只有 19,261 字。
      而传输层当时**完全没重试**：`httpx.TransportError` 才走退避，
      5xx 是直接 `raise RuntimeError`。过载是典型的可重试错误，退避后通常能过。
    - `500 api_error` 且消息里有 `new_sensitive` / `content_filter` → **不重试**。
      实测也撞过同样的 500 码，但那是内容过滤，重发同样内容必然还是被拦，
      重试只是白白烧额度。
    - `MODEL_SEAT_QUOTA_EXHAUSTED` → **不重试**（见 `classify_limit`）。
      这是坑 #13 的第三次形态：同一个 429 码，可能是频率（该退避）
      也可能是额度耗尽（重发必然还撞）。

    判据取响应体里的 `error.type` 与消息文本，认不出来就按「不重试」处理——
    猜错成「重试」最坏是浪费，重试错成「不重试」只是少一次机会。
    """
    # 429 必须先判：它是限流，**该退避重试**，
    # 而它在 4xx 段里，顺序放错就会被下面的 `status < 500` 挡掉。
    if status == 429:
        kind, _ = classify_limit(body, headers)
        return kind != "seat_quota", kind or "限流"
    if status < 500:
        return False, ""
    low = (body or "").lower()
    if classify_limit(body, headers)[0] == "seat_quota":
        return False, "额度耗尽（重试无效，需管理员调额度）"
    if "overloaded" in low or "rate_limit" in low or "too_many_request" in low:
        return True, "过载/限流"
    if "new_sensitive" in low or "content_filter" in low or "sensitive" in low:
        return False, "内容过滤（重发无意义）"
    # 5xx 但认不出来：502/503/504 这类网关抖动确实可重试
    return status in (502, 503, 504), "网关抖动" if status in (502, 503, 504) else ""


def _msgs_chars(messages: list[dict]) -> str:
    """把多轮消息摊平成一段文本，只为本地估算 token 与记账。

    真实用量一律取 API 报上来的，这个估算只用于对比偏差。
    """
    out: list[str] = []
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            for b in c:
                if not isinstance(b, dict):
                    out.append(str(b))
                elif b.get("type") == "text":
                    out.append(str(b.get("text") or ""))
                else:
                    out.append(json.dumps(b, ensure_ascii=False))
        else:
            out.append(json.dumps(m, ensure_ascii=False, default=str))
    return "\n".join(out)


def _rough_tokens(text: str) -> int:
    from ..ingest.split import count_tokens

    return count_tokens(text or "")


def _scan(text: str):
    """扫描 JSON 状态。返回 (未闭合括号栈, 是否在字符串内, 最后一个完整结构结束处)。"""
    stack: list[str] = []
    in_str = False
    esc = False
    last_clean = 0
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
                last_clean = i + 1
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
            last_clean = i + 1
        elif ch in "0123456789truefalsn":
            last_clean = i + 1
    return stack, in_str, last_clean


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.startswith("```json"):
            t = t[7:]
        elif t.startswith("```JSON"):
            t = t[7:]
        t = t.rstrip()
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def _strip_json_slips(t: str) -> str:
    """去掉两类模型常见的 JSON 失误：`//` 行注释与尾逗号。

    这两种和「被 max_tokens 截断」是不同的问题，但报出来的错长得几乎一样
    （"Expecting property name enclosed in double quotes"），所以以前一起
    被当成截断处理，结果救不回来，探针两次判断全丢，大纲塌成一节。

    必须字符串感知：正文里出现 `//`（比如 URL）不能当注释删掉，
    中文全角标点也不能受影响。所以走一遍扫描而不是正则硬替换。
    """
    out: list[str] = []
    in_str = False
    esc = False
    i, n = 0, len(t)
    while i < n:
        ch = t[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        # 字符串外的 // 起到行尾为止
        if ch == "/" and i + 1 < n and t[i + 1] == "/":
            while i < n and t[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1

    # 逗号后面只隔着空白就撞上 } 或 ] 的，删掉这个逗号
    s = "".join(out)
    return re.sub(r",(\s*[}\]])", r"\1", s)


def _repair_truncated_json(text: str) -> Any:
    """把被 max_tokens 截断的 JSON 尽量救回来。

    模型输出到一半撞上预算上限时，括号或引号不闭合。救不回返回 None，
    让上层走重试，而不是静默吞掉一个残缺结果。
    同时也收拾尾逗号与 `//` 注释这两种非截断失误。
    逻辑与用例见 tests/json_repair.py。
    """
    t = _strip_json_slips(_strip_fence(text))
    starts = [i for i in (t.find("{"), t.find("[")) if i >= 0]
    if not starts:
        return None
    t = t[min(starts):]

    # 情况一：本身就能解析
    try:
        return json.loads(t)
    except Exception:
        pass

    # 截到最后一个「字符层面完整」的位置，再补齐未闭合的括号
    stack, in_str, cut = _scan(t)
    if in_str:
        # 末尾落在未闭合的字符串里：cut 会正好停在某个键名之后，
        # 需要把那个没有值的键整个删掉，下面统一处理
        t = t[:cut]
    elif stack:
        t = t[:cut] if cut else t

    # 清掉尾部悬空的逗号、冒号
    t = t.rstrip()
    t = re.sub(r'[,:]+$', "", t).rstrip()

    # 末尾是 `"key"` 这种没跟值的键，削掉键但保留它前面的容器括号
    # （用捕获组，否则会把 `{` 一起吃掉，最后补不出合法的 JSON）
    prev = None
    while prev != t:
        prev = t
        t = re.sub(r'([,{])\s*"[^"\\]*"\s*$', r'\1', t).rstrip()
        t = re.sub(r'[,:]+$', "", t).rstrip()

    stack, in_str, _ = _scan(t)
    for ch in reversed(stack):
        t += "}" if ch == "{" else "]"
    if in_str:
        t += '"'
    t = re.sub(r'[,:]+$', "", t.rstrip())

    try:
        return json.loads(t)
    except Exception:
        return None


def _parse_json(raw: str) -> Any:
    if not raw or not raw.strip():
        raise EmptyOutput("模型返回空内容，无法解析 JSON")
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.startswith("```json"):
            text = text[7:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as first_err:
        starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
        if starts:
            s = min(starts)
            e = max(text.rfind("}"), text.rfind("]"))
            if e > s:
                try:
                    return json.loads(text[s : e + 1])
                except json.JSONDecodeError:
                    pass
        # 收尾兜底：截断、尾逗号、// 注释都在这里处理。
        # 早先只在 e > s 时才尝试，于是「开头就写坏、末尾没闭合」这种
        # 直接把异常抛给上层——探针因此连续两次拿不到判断。
        repaired = _repair_truncated_json(text)
        if repaired is not None:
            print(f"⚠️  JSON 格式有误（{str(first_err)[:80]}），"
                  f"已修复后解析成功", flush=True)
            return repaired
        raise
