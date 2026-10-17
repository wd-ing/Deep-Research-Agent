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
  3. 返回空内容时自动重试并翻倍预算——意外情况的兜底

⚠️ **第 1 条和第 3 条曾经被搞反了因果（2026-10-15 实测纠正）。**
当时的理解是「预算不够 → 多给」，于是把翻倍当成第 3 条的补丁。
实测（`tests/probe_plan_tasks_empty.py`，真实 82 段摘要载荷）：

  · write_plan 的 max_output_tokens=4,096 时，成功率 **1/14**
  · 提到 16,384 时，成功率 **2/3**

**抬预算是对的，而且效果接近 10 倍。** 但机制不是「给它空间它就多想」——
同一预算下思考量能差 4.9 倍（16,384 连打三次：8,593 / 11,401 /
41,771 字）。`max_tokens` 是**上限**，不是思考档位；真正管思考深度的是
`reasoning_effort`（那个模型才感知得到）。

⚠️ 反过来的推论同样重要：**翻倍重试是兜底，不该是常态路径。**
v5 实测 write_plan 最大输出 7,654 = 旧上限 4,096 的 **187%** ——
它一直在靠翻倍硬撑。既然次次都要触发，就该一开始设够。
`tests/check_token_budget.py` 现在会盯这件事。

所以 validate 从「必须有 thinking_budget: 0」改成「抽取类环节必须有 guard 策略」。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections import deque
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
    "relevance", "assess",
    # ⚠️ `write_plan` 在 2026-10-17 拆成了两个 stage（见 write.py:73/126/138）。
    # 拆之前两节点共用一个 `max_output_tokens`，而它们的需求差一个量级：
    # 分派要看 99 段证据的概括，规约只看框架 + 术语。
    # 更糟的是三处调用点在 DB 里全记成 `write_plan`，**历史数据无法归因到节点**。
    "write_plan_spec", "write_plan_tasks",
    "write_audit",
    "write_section", "write_revise", "summarize",
    # dedupe：配置里有、图里没有调用点，是**记着的待办**不是废代码。
    # 真要接入时它已经在白名单和 guard 里了。查证：grep 调用点为 0。
    # （`assign` 原来也在这个名单里，2026-10-13 连配置一起删了——
    #  它要映射的「断言」已被 superseded_by 取代，环节不存在了。）
    "dedupe",
}

# MiniMax 的思考深度档位。省略 output_config.effort 时默认 max。
# 注意 none 不是关闭思考——那个值会返回 400。
THINKING_EFFORTS = {"low", "medium", "high", "xhigh", "max"}

# `reasoning_effort` 的合法取值（OpenAI 兼容平台）。
# ⚠️ 里面**没有** `off`：YAML 1.1 会把裸 `off` 解析成布尔 false，
# 而 `False` 在 `if cfg.reasoning_effort:` 眼里是「没配」⇒ 字段不发
# ⇒ 平台按默认**开**思考，且全程不报错。关思考请写 `disabled`
# （平台中性的语义值，由 providers 档案翻译成 `none` 或「不发字段」）。
REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "disabled"}

# 走 `/chat/completions` 的 OpenAI 兼容平台，共用同一份实现。
#
# **模型不写死在代码里**：具体模型名、base_url、api_key_env、思考档位
# 全部从 `config/models.yaml` 读。换平台只改配置，不动这里。
#   · opencode —— 已有，接过 write 板块
#   · xj189    —— 2026-10-09 加，DeepSeek 系（deepseek-v4-flash-0731）
OPENAI_COMPAT = {"opencode", "xj189", "internai"}


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


def _fire_delta(stage: str, kind: str, chunk: str) -> None:
    fn = _DELTA_HOOK.get()
    if fn:
        try:
            fn(current_session(), stage, kind, chunk)
        except Exception:
            # 展示是锦上添花，绝不能因为展示失败拖垮研究
            pass


# ================================================================ 主动限流

class SendRateLimiter:
    """按**发送时刻**限速的滑动窗口（每分钟 N 次）。

    为什么不是并发闸门：`tests/probes/probe_agnes_rpm_window.py` 实测过，
    平台限的是**发送时刻的滚动窗口**，慢请求不占后续窗口的槽位。
    写成「最多 N 个在途」会错——那会把 100~300 秒的长请求算成占用，
    于是实际发送速率被压到远低于平台允许值，白等。

    为什么用滑动窗口而不是「按自然分钟」：自然分钟边界会让跨界的两批
    在两秒内各发 N 次。滑动窗口保守，而保守正是这里要的。

    ⚠️ **线程安全是硬要求**：`parallel_labelled` / `node_dispatch` 用的是
    真 `ThreadPoolExecutor`，不共享锁的话每个 worker 各有一个窗口，
    等于限流器没装。
    """

    def __init__(self, per_minute: int, window_s: float = 60.0) -> None:
        self.per_minute = max(0, int(per_minute))
        # 窗口长度可注入，**只为了测试**。写死 60 秒的话，
        # 守门测试会真的睡满一分钟——测不出来就只能不测，或者让测试变慢到没人跑。
        # 生产路径永远用默认值。
        self.window_s = float(window_s)
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()
        # 可观测：限流是「让人等」，等太久必须看得见，不能变成静默变慢
        self.waits = 0
        self.waited_s = 0.0
        self.max_wait_s = 0.0

    def acquire(self) -> float:
        """必要时阻塞，返回本次等了多久（秒）。"""
        if self.per_minute <= 0:
            return 0.0
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                while self._stamps and now - self._stamps[0] >= self.window_s:
                    self._stamps.popleft()
                if len(self._stamps) < self.per_minute:
                    self._stamps.append(now)
                    break
                # 睡在锁外面：否则一个线程的等待会把其余全锁死。
                nap = self.window_s - (now - self._stamps[0]) + 0.02
            time.sleep(nap)
            waited += nap
        if waited > 0:
            with self._lock:
                self.waits += 1
                self.waited_s += waited
                self.max_wait_s = max(self.max_wait_s, waited)
        return waited

    def stats(self) -> dict:
        with self._lock:
            return {"per_minute": self.per_minute, "waits": self.waits,
                    "waited_s": round(self.waited_s, 1),
                    "max_wait_s": round(self.max_wait_s, 1),
                    "in_window": len(self._stamps)}


def _limiter_key(base_url: str) -> str:
    """端点标识：**只取 host[:port]**，scheme 和路径一律忽略。

    ⚠️ 这行是限流能不能工作的要害（2026-10-17，`test_rate_limiter` 抓出来的）。

    注册侧拿到的是 `registry` 里的 `base_url`（带 `https://` 和 `/v1`），
    发送侧拿到的只有 `httpx.URL` 对象。**两边各算各的 key 就会永远对不上**，
    于是 `_LIMITERS.get(host)` 永远是 `None` —— 限流器装上了、一行日志都没有、
    一次请求都没限住，**而且什么都不报错**。

    所以两边必须走同一个规范化函数，不许各自实现。
    """
    from urllib.parse import urlsplit
    raw = (base_url or "").strip()
    if "//" not in raw:
        raw = "//" + raw          # 传进来可能只是 host
    s = urlsplit(raw)
    host = (s.hostname or "").lower()
    if s.port:
        host = f"{host}:{s.port}"
    return host or raw.lower()


# 按端点共享：`LLMClient` 会被多处 `import` 成不同实例，
# 而限额是**按账号/端点**算的，不是按 client 实例算的。
_LIMITERS: dict[str, SendRateLimiter] = {}
_LIMITERS_LOCK = threading.Lock()


def get_limiter(base_url: str, per_minute: int) -> SendRateLimiter:
    key = _limiter_key(base_url)
    with _LIMITERS_LOCK:
        lim = _LIMITERS.get(key)
        if lim is None:
            lim = SendRateLimiter(per_minute)
            _LIMITERS[key] = lim
        elif per_minute > 0 and lim.per_minute != per_minute:
            # 配置改了要跟着改，否则改了配置没生效还不报错
            lim.per_minute = per_minute
        return lim


def reset_limiters() -> None:
    """只给测试用。"""
    with _LIMITERS_LOCK:
        _LIMITERS.clear()


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
        # ⚠️ 2026-10-17 **撤销了原来这条拦截**。
        #
        # 它写于 2026-10-09，当时 MiniMax 确实完全忽略 `reasoning_effort`，
        # 配上去会被静默忽略。现在 `_minimax` / `_minimax_tools` **会把它翻译成
        # `thinking.type`**（实测：M3 的 `disabled` 关、`adaptive` 开），
        # 于是这条规则变成**挡住合法配置** —— 把 minimax 设为默认时
        # 15 个 stage 全部起不来。
        #
        # 「静默忽略」这个风险已经不在了：翻译发生在一个地方，
        # 且翻译失败会 400（响的，不是静默的）。
        #
        # 真正需要拦的是另一个，见 `run_tool_loop`：
        # **关思考 + 发工具会让 MiniMax 假装调用工具并编造结果。**
        #
        # if self.reasoning_effort is not None and self.provider == "minimax":
        #     raise ConfigError(...
        # 形状校验（2026-10-17 加）：**挡住 YAML 布尔值这个坑**。
        #
        # YAML 1.1 把裸 `off` / `on` / `yes` / `no` 全解析成布尔。
        # 有人写 `reasoning_effort: off` 想表达「关思考」时，
        # 拿到的是 Python 的 `False` —— 于是
        #   `== "disabled"` 判不成立 → 翻译不发生
        #   → 翻译后的值仍是 False → `if cfg.reasoning_effort:` 是假
        #   → payload 里**根本不带这个字段** → 平台按默认开思考。
        # 全程**不报错**，只是安静地多烧 token。必须在这里拦住。
        if self.reasoning_effort is not None:
            if not isinstance(self.reasoning_effort, str):
                raise ConfigError(
                    f"环节 {self.stage} 的 reasoning_effort="
                    f"{self.reasoning_effort!r} 不是字符串。"
                    "多半是 YAML 把 off/on/yes/no 解析成了布尔 —— "
                    "请写 `disabled`（关思考）或 `none`/`low`/`high`（字面量档位）。"
                )
            if self.reasoning_effort not in REASONING_EFFORTS:
                raise ConfigError(
                    f"环节 {self.stage} 的 reasoning_effort="
                    f"{self.reasoning_effort!r} 非法，"
                    f"只接受 {sorted(REASONING_EFFORTS)}。"
                    "「关思考」请写 `disabled`（平台中立，由档案翻译成 none 或不发）。"
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

        # `reasoning_effort: disabled` 是**平台中性的语义值**，翻译在这里收口
        # （2026-10-17 加）。三个 payload 现场（_opencode 流式/非流式、
        # _opencode_tools）都只认最终值，不参与翻译 —— 收在一处才不会漏。
        #
        # 为什么需要它：两个平台的「关思考」写法**正好相反**。
        #   internai：省略 = 开思考 ⇒ 要关就得显式发 `none`
        #   xj189  ：省略 = 关思考 ⇒ 要关就干脆不发这个字段
        # 环节若写死字面量 `none`，切回 xj189 就会让 7 个本该关思考的环节
        # （占全部调用的 68%）**反向打开思考** —— 不报错，只是静默多烧 token。
        #
        # 档案没声明 `effort_off` 就翻译成 None（不发该字段）。
        #
        # ⚠️ **这个值刻意不叫 `off`**：YAML 1.1 把裸 `off` 解析成布尔 false，
        # 于是 `== "off"` 判不成立、翻译不发生、字段不发 ⇒ 思考反而默认打开。
        # 真踩过一次（`tests/test_platform_switch.py` 抓出来的）。
        if s.get("reasoning_effort") == "disabled":
            s["reasoning_effort"] = prof.get("effort_off") or None

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
        # 限流限额挂在**建 stage 的时候**登记，而不是每次发送时读配置：
        # 读配置会让「限流行为」依赖调用顺序，而且多一次字典查找在热路径上。
        #
        # ⚠️ 限额是**按端点**的，取各档案里的最大值：同一个端点上不同阶段
        # 可能写不同的值，取 max 才是这个端点真正的约束。
        rpm = max(int((self.raw.get("providers") or {}).get(prov_name, {})
                      .get("rate_limit_per_min", 0) or 0),
                  int(s.get("rate_limit_per_min", 0) or 0))
        if rpm > 0 and cfg.base_url:
            get_limiter(cfg.base_url, rpm)
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
        # finish_reason（2026-10-17 加）：截断检测的唯一可靠信号。
        # `length` = 模型自己撞到 max_tokens 上限被切断，
        # **内容非空但残缺**，比空返回更难发现。
        self.finish_reason: str | None = None
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
            if ch.get("finish_reason"):
                self.finish_reason = ch["finish_reason"]
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
                # 拼出来的 JSON 坏了（最常见的原因是**被 max_tokens 截断**）。
                # 工具循环里宁可空参数也不能崩，所以这里照旧给 `{}`，
                # **但必须打上标记** —— 强制工具调用那条路（`_openai_compat_forced`）
                # 会调 `_reject_broken_args()` 把它变成一次显式失败。
                # 2026-10-17：原来这个 `{}` 是完全静默的，而一旦改用强制输出，
                # 被截断的 arguments 会伪装成「模型返回了空结果」正常往下走，
                # 整篇报告静默少一块 —— 正是本项目反复消灭的那类静默失败。
                args = {}
                out.append({"type": "tool_use", "id": slot["id"],
                            "name": slot["name"], "input": args,
                            "_args_broken": True})
                continue
            out.append({"type": "tool_use", "id": slot["id"],
                        "name": slot["name"], "input": args})
        return out

    def text_only(self) -> str:
        return "".join(self.text)


class AnthropicSSEAccum:
    """把 **Anthropic Messages 形状**的 SSE 增量攒成一次完整响应。

    为什么需要它（2026-10-17）：`_minimax` 打的是
    `https://api.minimaxi.com/anthropic/v1/messages`，**协议是 Anthropic**，
    而上面的 `SSEAccum` 是 OpenAI 形状（`choices[].delta`）。
    以前 `_minimax` 直接走非流式，于是 **8 个声明 `stream: true` 的环节
    全部静默降级** —— 配置说有流式，实际没有，不报错、不告警。

    ## 实测的事件序列（`stream: true`，MiniMax-M3）

        message_start        → message.usage（input_tokens 等）
        ping                 → 忽略
        content_block_start  → index + {type: text|thinking|tool_use, id, name}
        content_block_delta  → delta.type = text_delta | thinking_delta
                                        | input_json_delta（partial_json 分片）
        content_block_stop   → 结束该块
        message_delta        → delta.stop_reason（end_turn / max_tokens / tool_use）
                               usage.output_tokens
                               usage.output_tokens_details.thinking_tokens
        message_stop

    ⚠️ **最容易错的地方：thinking 和 text 不在同一个 `index` 上。**
    实测三条对照：

        关思考         → index 0 = text
        开思考         → index 0 = thinking
        开思考 + 工具  → index 0 = thinking，**index 1 = tool_use**

    所以「取 index 0 的 text」在这里会拿到空字符串，而且**不报错**。

    ## 输出形状

    `blocks()` 返回的**就是 Anthropic 形状的块**（`text` / `thinking` /
    `tool_use` + `input`），和非流式路径的返回值完全一致 ——
    调用方不必分支。工具参数拼不成合法 JSON 时打 `_args_broken`，
    与 `SSEAccum` 同一套约定，`_reject_broken_args` 照样接得住。
    """

    def __init__(self, on_delta: Optional[Callable[[str, str], None]] = None,
                 stage: str = "") -> None:
        # index -> {"type","id","name","text","args"}
        # `_order` 保留到达顺序：Anthropic 不保证 index 单调递增，
        # 靠 sorted(index) 排序会在「先 thinking 后 text」时被反过来。
        self._blocks: dict[int, dict] = {}
        self._order: list[int] = []
        self.usage: dict = {}
        self.stop_reason: str | None = None
        self.error: dict | None = None
        self._on_delta = on_delta
        self.stage = stage

    def _fire(self, kind: str, chunk: str) -> None:
        if self._on_delta:
            try:
                self._on_delta(kind, chunk)
            except Exception:
                # 展示是锦上添花，不能因为展示失败拖垮调用
                pass

    def _slot(self, index: int) -> dict:
        slot = self._blocks.get(index)
        if slot is None:
            slot = {"type": "text", "id": None, "name": None,
                    "text": "", "args": []}
            self._blocks[index] = slot
            self._order.append(index)
        return slot

    def feed(self, obj: dict) -> None:
        t = obj.get("type")
        if t == "message_start":
            u = (obj.get("message") or {}).get("usage") or {}
            self.usage.update({k: v for k, v in u.items() if v is not None})
        elif t == "content_block_start":
            i = int(obj.get("index") or 0)
            cb = obj.get("content_block") or {}
            slot = self._slot(i)
            slot["type"] = cb.get("type") or "text"
            slot["id"] = cb.get("id")
            slot["name"] = cb.get("name")
            # `text` 允许非空（少数平台第一块就带内容）
            if cb.get("text"):
                slot["text"] += cb["text"]
            if cb.get("thinking"):
                slot["text"] += cb["thinking"]
        elif t == "content_block_delta":
            slot = self._slot(int(obj.get("index") or 0))
            d = obj.get("delta") or {}
            dt = d.get("type")
            if dt == "text_delta" and d.get("text"):
                slot["text"] += d["text"]
                self._fire("text", d["text"])
            elif dt == "thinking_delta" and d.get("thinking"):
                slot["text"] += d["thinking"]
                self._fire("reasoning", d["thinking"])
            elif dt == "input_json_delta" and d.get("partial_json"):
                # 工具参数是**分片到达**的（与 OpenAI 形状同理），
                # 必须按块累积再拼，一次 json.loads 必然炸
                slot["args"].append(d["partial_json"])
        elif t == "message_delta":
            stop = (obj.get("delta") or {}).get("stop_reason")
            if stop:
                self.stop_reason = stop
            u = obj.get("usage") or {}
            # 后到的覆盖先到的：message_start 里 output_tokens 是个小数
            self.usage.update({k: v for k, v in u.items() if v is not None})
        elif t == "error":
            # `event: error` + data。**不能吞** —— 吞了就变成
            # 「流到一半没下文」，而空返回是下面能查到的失败模式
            self.error = obj.get("error") or {"raw": obj}

    def blocks(self) -> list[dict]:
        """攒成 Anthropic 形状的块，和非流式路径的返回保持一致。"""
        out: list[dict] = []
        for i in self._order:
            slot = self._blocks[i]
            kind = slot["type"]
            if kind == "tool_use":
                raw = "".join(slot["args"])
                try:
                    args = json.loads(raw) if raw.strip() else {}
                except Exception:
                    # 同 `SSEAccum`：给 `{}` 但**必须打标记**，
                    # 让 `_reject_broken_args` 把它变成一次显式失败
                    out.append({"type": "tool_use", "id": slot["id"],
                                "name": slot["name"], "input": {},
                                "_args_broken": True})
                    continue
                out.append({"type": "tool_use", "id": slot["id"],
                            "name": slot["name"], "input": args})
            elif kind == "thinking":
                if slot["text"]:
                    out.append({"type": "thinking", "thinking": slot["text"]})
            elif slot["text"]:
                out.append({"type": "text", "text": slot["text"]})
        return out

    def text_only(self) -> str:
        return "".join(b["text"] for b in self.blocks()
                       if b.get("type") == "text")


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


# 空返回/截断重试时的 **effort 降档梯子**（2026-10-17 加）。
#
# 为什么是降 effort 而不是加预算：
# `max_tokens` 是思考和正文的**共享天花板**，思考吃满 ⇒ 正文必然 0
# （实测 write_plan @16,384：reasoning_tokens 恰好 16,384、正文 0 字）。
# 所以「失败就翻倍预算」是把墙抬高，让模型想得更远再撞一次。
#
# ⚠️ **梯子最底是 `low`，不是 `disabled`**：
# `none` 在这个平台上**不是可靠开关** —— 实测（`probe_plan_tasks_modeB.py` E 格）
# `disabled` 的思考中位仍有 11,109 token，还跑飞过一次 16,384。
# 之前说「`none` 是干净开关、n=3 全 0、方差为零」是**用简单题测出来的假象**，
# 换成 write_plan 的真实载荷就不成立了，已撤回。
_EFFORT_LADDER = ("high", "medium", "low")


def _lower_effort(current: str | None) -> str | None:
    """给一档更低。没有更低档就返回 None（调用方据此停止重试）。"""
    if not current or current not in _EFFORT_LADDER:
        return None
    i = _EFFORT_LADDER.index(current)
    return _EFFORT_LADDER[i + 1] if i + 1 < len(_EFFORT_LADDER) else None


def _is_truncated(usage: dict | None, finish_reason: str | None,
                  budget: int) -> bool:
    """输出是不是被 `max_tokens` 上限**切断**了（2026-10-17 加）。

    为什么必须单独判 —— **截断比空返回隐蔽得多**：

      · 空返回：`text` 为空 → 现有 guard 的 `if not txt` 直接抓到，翻倍重试
      · 截断：`text` 非空但**只写了一半**，看起来是成功返回

    而 JSON 环节会把截断的结果**抢救成合法 JSON** ——
    `_parse_json` 会「截到最后一个字符层面完整的位置，再补齐未闭合的括号」。
    于是模型少交了几组字段，代码照样拿到一份能 `json.loads` 的结果，
    **不报错、不降级、看不出异常**。

    实测（2026-10-17，信通院 discovery-api，`probe_outline`）：
    输出 8,192 token = 上限 8,192，提示词要的四组信号只交了三组 ——
    `subtopics` 5 条正常，`entities` / `jargon` / `controversies` **全是 0**，
    而 `degraded: false`、`quality: substantive`。
    在 xj189 上被掩盖了：那边这个环节最大输出 3,966，从没撞过上限。

    判定用两个信号，主次分明：
      1. `finish_reason == "length"` —— 平台明说被截断，**最可靠**
      2. `completion_tokens >= budget` —— 没用 `stream_options.include_usage`
         或平台不报 finish_reason 时的兜底

    ⚠️ 第 2 条是**推断**不是事实：理论上存在平台把用量算得比实际多的可能。
    所以它只在「正好顶到上限」时触发（`>=` 而非接近即触发），
    宁可多重试一次，也不要把残缺结果当成功放过去。

    ⚠️ **2026-10-17 补：Anthropic 形状的 usage 与 stop_reason。**
    换到 minimax 后发现这两个信号**一个都读不到**：
      · OpenAI 形状：截断 = `finish_reason == "length"`，用量 `completion_tokens`
      · Anthropic 形状：截断 = `stop_reason == "max_tokens"`，用量 `output_tokens`
    而本函数原来只认前者 ⇒ 在 MiniMax 上**恒返回 False**，
    截断的正文会被当成成功返回（`write_section` / `write_audit` 少一截，
    `degraded: false`，不报错）。两个字段名一起补上。
    """
    # 两套形状的截断标记都收：OpenAI "length" / Anthropic "max_tokens"
    if finish_reason and str(finish_reason).lower() in ("length", "max_tokens"):
        return True
    used = (usage or {}).get("completion_tokens")
    if used is None:
        # Anthropic 形状用 `output_tokens`（`_record` 早就两个都读了，
        # 只有这里漏了 —— 记账对、截断判断错，是最难发现的那种不一致）
        used = (usage or {}).get("output_tokens")
    try:
        used_i = int(used)
    except (TypeError, ValueError):
        return False
    return budget > 0 and used_i >= budget


def _anthropic_msgs_to_openai(messages: list[dict]) -> list[dict]:
    """把工具循环攒下的 **Anthropic 形状**对话转成 OpenAI 形状。

    ⚠️ **这是 2026-10-17 在信通院 discovery-api 上实测暴露的 bug。**

    `run_tool_loop` 按 Anthropic Messages 协议攒对话：
        assistant: [{"type":"tool_use","id":...,"name":...,"input":{…}}]
        user     : [{"type":"tool_result","tool_use_id":...,"content":"…"}]
    而 `_opencode_tools` 之前是**原样透传**给 OpenAI 兼容端点的。

    为什么以前没炸：xj189 **不校验**内容块形状，照单全收。
    信通院会严格校验，第二轮直接 400：

        internai 400 upstream_request_rejected
        "4 validation errors for ValidatorIterator
         0.ChatCompletionContentPartTextParam.text  Field required
         [input_value={'id': 'chatcmpl-tool-…', 'type': 'tool_use'}]"

    **症状是静默的**：400 被 `_run_task` 的兜底吃掉，降级成单次调用、
    不报错 —— 于是「子 agent 拿不到证据全文」这件事完全没人知道。
    实测那一轮 write_section **10 次里 4 次**走的都是降级路径。

    对应关系（OpenAI 把「工具结果」拆成**独立消息**，不是一个内容块数组）：
        tool_use 块   → assistant 消息上的 `tool_calls[]`
                        `input`（对象）要序列化成 **JSON 字符串**
        tool_result 块 → **独立的** `{"role":"tool","tool_call_id":…}` 消息
        user 轮里的 text 块 → 跟在所有 tool 消息**之后**的普通 user 消息
                        （顺序不能提前：tool 消息必须紧邻 assistant）

    ⚠️ assistant 只有 tool_use 没有正文时，`content` 必须是 `None`
    而不是 `""` —— 空串会被当成「有正文但是空的」。
    """
    out: list[dict] = []
    for m in messages or []:
        role = m.get("role")
        content = m.get("content")
        # 纯字符串的轮次两边协议一致，原样带走
        if isinstance(content, str) or content is None:
            out.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            out.append({"role": role, "content": str(content)})
            continue

        if role == "assistant":
            texts = [b.get("text", "") for b in content
                     if b.get("type") == "text"]
            calls = []
            for b in content:
                if b.get("type") != "tool_use":
                    continue
                inp = b.get("input")
                calls.append({
                    "id": b.get("id"),
                    "type": "function",
                    "function": {
                        "name": b.get("name") or "",
                        "arguments": inp if isinstance(inp, str)
                        else json.dumps(inp or {}, ensure_ascii=False),
                    },
                })
            joined = "".join(texts)
            msg = {"role": "assistant",
                   # 只有 tool_use 没有正文时必须给 None，给 "" 会被当成空正文
                   "content": joined if (joined or not calls) else None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue

        # user 轮：每个 tool_result 拆成一条独立消息，正文块排在它们之后
        trailing_text: list[str] = []
        for b in content:
            t = b.get("type")
            if t == "tool_result":
                c = b.get("content")
                if isinstance(c, list):          # Anthropic 允许内容块数组
                    c = "".join(x.get("text", "") for x in c
                                if isinstance(x, dict))
                out.append({"role": "tool",
                            "tool_call_id": b.get("tool_use_id"),
                            "content": c if isinstance(c, str)
                            else json.dumps(c, ensure_ascii=False)})
            elif t == "text":
                trailing_text.append(b.get("text", ""))
            else:
                trailing_text.append(json.dumps(b, ensure_ascii=False))
        if trailing_text:
            out.append({"role": "user", "content": "\n".join(trailing_text)})
    return out


def _reject_broken_args(blocks: list[dict], stage: str,
                        finish_reason: str | None = None) -> None:
    """工具参数拼不成合法 JSON 时**显式失败**，不许它悄悄变成 `{}`。

    2026-10-17 加。强制工具调用（`complete_json_forced`）把「模型只填
    `arguments` 对象」当稳定性保证，但那个保证有一个前提：
    **生成过程要没被 `max_tokens` 切断**。一旦截断，
    `arguments` 必然是半截 JSON，`SSEAccum.blocks()` 按老规矩给 `{}`。

    如果就这么返回，调用方拿到的是「成功的空对象」——
    分派会变成 `tasks: []`，报告静默变成空。
    而 `write_plan_tasks` 那个曾经的病根就是这个（`tasks=0`、整篇没正文）。
    """
    bad = [b for b in blocks if b.get("_args_broken")]
    if not bad:
        return
    names = ", ".join(str(b.get("name")) for b in bad)
    raise EmptyOutput(
        f"环节 {stage} 的工具参数拼不成合法 JSON（工具：{names}，"
        f"finish_reason={finish_reason}）。"
        f"最可能的原因是生成被 max_tokens 截断——**这就是「预算不够」而不是"
        f"「模型返回了空结果」**，所以这里必须失败，不许把 `{{}}` 当结果交出去。")


class LLMClient:
    """统一调用入口。厂商差异收敛在这里。"""

    def __init__(self, registry: ModelRegistry | None = None) -> None:
        self.registry = registry or ModelRegistry()
        # 限流挂在 httpx 的 **request event hook** 上（2026-10-17 加）。
        #
        # 为什么是它而不是在 `_opencode` / `_opencode_tools` /
        # `_openai_compat_forced` 各埋一次：
        #   · 那是 6~8 个发送点，改一处漏一处就会**静默失效**
        #     （限流器最坏的失败模式就是「装上了但不生效」——它只会让一切变慢，
        #      不报错，没有人看得出它没在工作）
        #   · `_opencode` 内部还有自己的重试循环（空返回 / 断连 / 429），
        #     在外层埋**数不到重试**，一撞限流就被连着打
        #   · event hook 是**每一次真实发送**，包括重试和流式，一处覆盖全部
        self._limit_notice_at = 0.0
        self._client = httpx.Client(timeout=180.0,
                                    event_hooks={"request": [self._throttle]})
        self.warnings: list[str] = []
        # 排队超时的累计次数。**不是重试能解决的问题**——并发降不下来它还会来，
        # 所以单独记一个计数：`cost_report.py` 读它来判断该不该调 GATE_WORKERS。
        # （2026-10-10 补：之前只写不读，是个死变量，等于白记。）
        self._queue_pressure = 0

    def _throttle(self, request) -> None:
        """每发一个请求前过一次滑动窗口。

        `request` 是 httpx.Request，能拿到 URL —— 于是**按端点**而不是按
        client 实例取限额（限额是按账号算的，多个 `LLMClient` 共用一个桶）。
        """
        lim = _LIMITERS.get(_limiter_key(str(request.url)))
        if lim is None or lim.per_minute <= 0:
            return
        waited = lim.acquire()
        if waited > 0.5:
            # 等超过半秒才说一句，且一分钟最多一句 —— 限流是常态，
            # 每等一次就打印会把日志淹掉，而没人看的告警等于没有。
            now = time.monotonic()
            if now - self._limit_notice_at > 60.0:
                self._limit_notice_at = now
                msg = (f"[限流] {request.url.host} {lim.per_minute}/分钟，"
                       f"本次等待 {waited:.1f}s"
                       f"（累计 {lim.waits} 次 / {lim.waited_s:.0f}s）")
                self.warnings.append(msg)
                print(f"⚠️  {msg}", flush=True)

    def limiter_stats(self) -> dict:
        """限流统计。**限流是主动引入的等待，必须能被查**，否则
        「一轮变慢了」永远查不出是不是它。"""
        return {k: v.stats() for k, v in _LIMITERS.items()}

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
        tool_choice: str | None = None,
    ) -> dict:
        """带工具的一次调用。返回原始 content 块与 usage。

        `tool_choice`（2026-10-17 加）传工具名 = **必须**调它，只发那一个工具。
        不传 = 老规矩，`tools` 里随便挑或直接回文本。

        `tool_choice` 管的是**输出形状**：`run_tool_loop` 原来最后一轮
        不带工具，于是「最终答案」是让模型手写一整份 JSON 文本——
        分派环节实测输出 7,135 token，而纯提示词路径的长 JSON 会中途崩
        （见 `complete_json_forced` 的实测记录）。有了 `tool_choice`，
        最后一轮改成填 `arguments`，平台在解码层面保证它是合法 JSON。

        ⚠️ **只有 OPENAI_COMPAT 走通了**。MiniMax 分支显式报错而不是
        悄悄降级成非强制——降级等于把「保证」变成「也许」，而调用方
        （`run_tool_loop`）会把这次失败退成走上一轮的文本，
        行为仍然正确，但**不能假装约束生效过**。

        至于**为什么需要工具调用**本身：
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
        forced = None
        if tool_choice:
            # `isinstance` 这道防御跟 `to_openai_tools` 保持一致：
            # `tools` 里混进非 dict 时，抛在这里比在后面某个 `t.get` 上炸好。
            forced = next((t for t in tools or []
                           if isinstance(t, dict) and t.get("name") == tool_choice),
                          None)
            if forced is None:
                raise ConfigError(
                    f"环节 {stage} 要求强制调用工具 {tool_choice}，"
                    f"但 tools 里没有它（现有："
                    f"{[t.get('name') for t in tools or []]}）")
        try:
            if cfg.provider in OPENAI_COMPAT:
                if forced is not None:
                    blocks, usage = self._openai_compat_forced(
                        cfg, system, "", to_openai_tools([forced])[0],
                        messages=messages)
                else:
                    blocks, usage = self._opencode_tools(
                        cfg, system, messages, tools)
            elif cfg.provider == "minimax":
                # 🔴 **不能降级成非强制**（2026-10-17 撤销了原来那条报错）。
                # 当时的理由是「MiniMax 没接 tool_choice」，不成立 ——
                # 它认这个字段。
                #
                # ⚠️ **但「认」不等于「保证」**（同日 n=10 分变量复测）：
                #   提示词点名工具 + 开思考 + 强制 → 10/10
                #   提示词点名工具 + 关思考 + 强制 → 6/10
                #   提示词点名工具 + 关思考 + 不强制 → 5/10（其中 1 次真编造）
                #   中性提示 + 开思考 + 强制 → 1/3
                # ⇒ 强制是**暗示**：真正让 Phase B 成立的是「提示词想要这个工具」
                #   +「开着思考」，不是 `tool_choice` 本身。
                #
                # 仍然不降级：降级等于把「保证」变成「也许」——
                # Phase B 失败会静默退回手写 JSON，
                # 而那正是 2026-10-17 那次失败的原因（真实载荷实测 1/2）。
                # 兜底靠另外两层（Phase A 自愿交卷 + `_parse_json`），不是靠降级。
                blocks, usage = self._minimax_tools(
                    cfg, system, messages, tools,
                    tool_choice=(forced or {}).get("name"))
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

    # ---------- 可观测性 ----------

    def _warn_reasoning_ratio(self, cfg: StageConfig, usage: dict,
                              reasoning_text: str = "") -> None:
        """防线二：思考占比超阈值就记一条告警。流式与非流式共用。

        ⚠️ **这道防线以前等于没有**（2026-10-15 修），两个独立的原因叠加：
          ① 它只写在**非流式**分支里，而所有需要真思考的环节
             （write_plan / assess / write_section / probe_outline /
             write_audit / outline）**全是 `stream: true`** —— 一次都没跑过；
          ② 就算跑到，它读 `usage.completion_tokens_details.reasoning_tokens`，
             而 xj189 这个字段是 `{}`（实测报文），于是 reasoning 恒为 0，
             `0 > 0.7` 恒不成立。

        两层都修了：抽成公共入口（①），数值改走 `_reasoning_tokens`
        并用流式攒下来的思考正文兜底（②）。

        ⚠️ **2026-10-17 再补一处**：读的是 `completion_tokens`，而 Anthropic
        形状（minimax）报的是 `output_tokens` ⇒ 这里恒为 0 直接 return。
        `_minimax` 自己有一套按字符算的占比判断，所以那个环节没漏；
        但统一收口更省心，免得下次换 provider 又错在这里。
        """
        u = usage or {}
        completion = int(u.get("completion_tokens") or 0) \
            or int(u.get("output_tokens") or 0)
        if not completion:
            return
        reasoning = _reasoning_tokens(usage, reasoning_text)
        if reasoning / completion <= cfg.guard.max_reasoning_ratio:
            return
        # 数字是实测还是估算，必须说清楚——告警本身也会被当证据引用。
        src = "平台报数" if _has_reasoning_field(usage) else "按思考正文估算"
        msg_w = (
            f"[{cfg.stage}] reasoning 占比 {reasoning}/{completion}（{src}）"
            f"超过阈值 {cfg.guard.max_reasoning_ratio:.0%}，"
            f"该环节可能不适合 {cfg.model}"
        )
        self.warnings.append(msg_w)
        print(f"⚠️  {msg_w}", flush=True)

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
        # 基础预算**一次给够，不在重试里翻倍**（2026-10-17 改）。
        # `max_tokens` 是思考和正文的共享天花板（实测 write_plan @16,384 时
        # reasoning_tokens 恰好 16,384、正文 0），所以「撞空就加钱」方向是反的 ——
        # 加钱只会让它想得更远再撞一次。空/截断的正确动作是**降 effort**。
        # `effort` 在重试里逐档下调；预算全程不动。
        effort = cfg.reasoning_effort
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
            if effort:
                payload["reasoning_effort"] = effort
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
                self._warn_reasoning_ratio(
                    cfg, usage, "".join(acc.reasoning))
                # 截断与空返回**共用额度与降档逻辑**（2026-10-17 加、改）。
                # ⚠️ **动作从「翻倍预算」改成「降 effort」** —— 方向是反的，
                # 而且原来那个方向从来就不对：
                #   `max_tokens` 是思考和正文的**共享天花板**，思考吃满 ⇒ 正文必然 0
                #   （实测 write_plan @16,384：思考 token 16,384/16,384，正文 0）
                # 所以「预算不够」的正确解法是**一次给够**（见 models.yaml 的定标），
                # 而不是失败了临时加钱 —— 加钱只会让它想得更远再撞一次。
                # 真撞了天花板还出不来正文，说明这个任务上**想得太多**，
                # 该做的是压思考量。
                if not txt or _is_truncated(usage, acc.finish_reason, budget):
                    truncated = bool(txt)
                    last_reason = "truncated" if truncated else "empty"
                    if empty_left <= 0:
                        break
                    nxt = _lower_effort(effort)
                    if nxt is None:
                        # 档位已经到底（通常是 disabled），再试也是同一份行为
                        break
                    empty_left -= 1
                    effort = nxt
                    self.warnings.append(
                        f"[{cfg.stage}] 流式{'输出被上限截断' if truncated else '返回空内容'}"
                        f"（finish_reason={acc.finish_reason}），"
                        f"**不抬预算**、改降 reasoning_effort → {effort!r} 重试"
                        f"（额度还剩 {empty_left}）")
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
            choice0 = (data.get("choices") or [{}])[0]
            msg = choice0.get("message") or {}
            text = (msg.get("content") or "").strip()
            usage = data.get("usage") or {}
            usage["latency_ms"] = int((time.time() - t0) * 1000)

            # 防线二：思考占比告警（流式与非流式走同一个入口）
            self._warn_reasoning_ratio(
                cfg, usage, msg.get("reasoning_content") or "")

            # 防线一：内容为空**或被上限截断**则重试。
            # ⚠️ **动作是降 effort，不是加预算**（理由见上面 `effort` 那段注释）。
            if not text or _is_truncated(usage, choice0.get("finish_reason"), budget):
                truncated = bool(text)
                last_reason = "truncated" if truncated else "empty"
                if empty_left <= 0:
                    break
                nxt = _lower_effort(effort)
                if nxt is None:
                    break
                empty_left -= 1
                effort = nxt
                self.warnings.append(
                    f"[{cfg.stage}] 第 {attempt+1} 次"
                    f"{'输出被上限截断' if truncated else '返回空内容'}"
                    f"（finish_reason={choice0.get('finish_reason')}），"
                    f"**不抬预算**、改降 reasoning_effort → {effort!r} 重试"
                    f"（额度还剩 {empty_left}）"
                )
                continue
            return text, usage

        # ⚠️ **只陈述观测到的事实，不下诊断。**
        # 原来这里写死了一句「这正是 M3.1 Flash 那种 thinking 吃光预算的失效模式」——
        # 2026-10-15 那轮失败的是 `deepseek-v4-flash-0731`，这句话指错了模型，
        # 而且断言了一个代码并不知道成因的因果。
        # （同一类错误在别处也犯过：告警只说「该环节可能不适合 X」，
        #  证据只够说「相关」，不够说「不适合」。）
        #
        # ⚠️ 这里**不能**再建议「抬 max_output_tokens」了（2026-10-17 改）：
        # 重试的动作已经换成**降 reasoning_effort**，预算是按实测定死、
        # 重试里一动不动。梯子走完还失败，说明「压思考量」这条也不管用，
        # 再说「加钱」就是让模型想得更远再撞一次。
        raise EmptyOutput(
            f"环节 {cfg.stage} 的重试额度用尽仍未拿到内容"
            f"（模型 {cfg.model}，空返回 {cfg.guard.retry_on_empty} 次、"
            f"连接中断 {drops} 次，最后原因 {last_reason}）。"
            f"**已经试过的**：reasoning_effort 沿 high→medium→low 降档到 "
            f"{effort or '未设'}，预算全程未抬（{budget}）。"
            f"这一条只是排查建议，本次并未观测到成因。"
        )

    # ---------- MiniMax（Anthropic Messages 格式） ----------

    def _minimax_once(self, cfg: StageConfig, url: str, headers: dict,
                      payload: dict) -> tuple[int, str, dict | None]:
        """发**一次** MiniMax 请求（流式或非流式），返回 `(状态码, 错误正文, 解析结果)`。

        ## 为什么要这一层

        `payload["stream"]` 为真时走 SSE，否则走普通 POST。两种返回的
        **原始形状完全不同**（SSE 是一串事件，非流式是一个 JSON 对象），
        但只要在这里把它**归一成非流式那套**：

            {"content": [Anthropic 形状的块], "stop_reason": ..., "usage": {...}}

        上层的重试、截断判断、思考占比就**一个字都不用分支**。
        分支集中在这里一处 —— 与 `_minimax_tools` 也共用，
        两边不会走岔（它们以前就各写各的，很容易一边改了另一边没改）。

        ## 为什么要真的走流式

        1. **绕开空闲超时**：nginx / 网关测的是「两次读到数据之间的间隔」，
           流式持续推块就永不空闲。xj189 那边实测同参数流式跑过 245.5 秒、
           非流式 61.0 秒就 504。MiniMax 侧**没测过超时阈值**，
           但 `write_plan_tasks` 非流式实测跑到过 228.5 秒 ——
           已经和 xj189 那堵墙同一量级，不能靠运气。
        2. **拿到 `stop_reason`**：截断判断要靠它（`_is_truncated`）。
           非流式也报，但流式能**在流到一半时就看出被切断**。
        3. 中途有进度可展示（`_hook_for` 那条增量推送链）。
        """
        if not payload.get("stream"):
            r = self._client.post(url, headers=headers, json=payload,
                                  timeout=cfg.timeout_s)
            if r.status_code != 200:
                return r.status_code, r.text, None
            return 200, "", r.json()

        with self._client.stream("POST", url, headers=headers, json=payload,
                                 timeout=cfg.timeout_s) as resp:
            if resp.status_code != 200:
                return resp.status_code, resp.read().decode("utf-8", "replace"), None
            acc = AnthropicSSEAccum(_hook_for(cfg.stage), cfg.stage)
            for obj in _sse_lines(resp):
                acc.feed(obj)
        if acc.error:
            # `event: error` 必须响：吞掉就变成「流到一半没下文」，
            # 而下面能查到的失败模式（空 / 截断）都不是它
            raise RuntimeError(f"MiniMax 流式错误: {str(acc.error)[:300]}")
        return 200, "", {"content": acc.blocks(),
                         "stop_reason": acc.stop_reason,
                         "usage": acc.usage}

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
        # 思考深度：`output_config.effort` 与 `thinking.type` 是**两套互不相干**的
        # 开关，M3 与 M3.1-Flash 的行为还正好相反。2026-10-17 逐项实测：
        #
        #   设置                        MiniMax-M3          M3.1-Flash-Preview
        #   不传 thinking            思考   0  ✅          思考 0
        #   output_config.effort=low 思考   0（被忽略）    思考 240
        #   output_config.effort=high思考   0（被忽略）    思考 0
        #   effort=none              200（被忽略）        **400**
        #   thinking.type=disabled   思考   0  ✅          **400**
        #   thinking.type=adaptive   思考 435  ✅          思考 369
        #
        # ⇒ **M3 的真开关是 `thinking.type`，不是 `effort`。**
        #   省 effort 字段或传任何值，M3 都当没听见。
        # ⇒ M3.1-Flash **关不掉思考**（disabled 直接 400），
        #   而本项目 7 个环节靠关思考省 68% 的 token —— 所以选 M3。
        #
        # ⚠️ 这段注释以前写的是「省略即 max / 该模型要求 adaptive」，与实测相反。
        # 那是基于 M3.1-Flash 记的，换到 M3 就不成立。**结论必须跟着实测的模型走。**
        if cfg.reasoning_effort:
            payload["thinking"] = {"type": "adaptive"}
            # effort 对 M3 无效，但 M3.1-Flash / M2.x 认它；留着无害，
            # 且换模型时这一行立刻起作用。
            if cfg.thinking_effort:
                payload["output_config"] = {"effort": cfg.thinking_effort}
        else:
            # 显式写 disabled 而不是省略：省略在 M3 上是「关」，
            # 但**换个模型语义就可能翻**（M3.1-Flash 上省略也是关、adaptive 才开，
            # 但 disabled 会 400）。写出来至少失败是响的。
            payload["thinking"] = {"type": "disabled"}
        if json_mode:
            # Anthropic 协议没有 response_format，用提示词约束
            payload["messages"] = [{
                "role": "user",
                "content": prompt + "\n\n只输出 JSON，不要任何解释文字或代码围栏。",
            }]

        budget = cfg.max_output_tokens
        headers = {"Authorization": f"Bearer {key}",
                   "Content-Type": "application/json",
                   "x-api-key": key}
        if cfg.stream:
            # ⚠️ **这里以前是静默降级的**（2026-10-17 修）：
            # 配置里 8 个环节写着 `stream: true`，而 `_minimax` 只实现 `.post`
            # ⇒ 全部走非流式，**不报错、不告警**。与坑 47（限流器 key 不匹配）
            # 是同一形态：装上了、看着在用、其实从没跑过。
            payload["stream"] = True
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
                status, body, parsed = self._minimax_once(
                    cfg, url, headers, payload)
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
            if status != 200:
                retry, why = _retryable_http(status, body)
                if retry and net_left > 0:
                    net_left -= 1
                    self.warnings.append(
                        f"[{cfg.stage}] HTTP {status} {why}，退避重试（额度剩 {net_left}）")
                    print(f"⚠️  [{cfg.stage}] HTTP {status} {why}，退避重试（额度剩 {net_left}）", flush=True)
                    time.sleep(3.0 * (attempt + 1))
                    continue
                raise RuntimeError(f"MiniMax {status}: {body[:300]}")
            # `parsed` 的形状**与非流式完全一致**（`content` / `stop_reason` / `usage`），
            # 所以下面这些判断一个字都没改 —— 这是把流式接进来的关键：
            # 分支只留在 `_minimax_once` 一层。
            blocks = parsed.get("content") or []
            stop_reason = parsed.get("stop_reason")
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
            think = "".join(b.get("thinking", "") for b in blocks if b.get("type") == "thinking")
            usage = parsed.get("usage") or {}
            out_tok = usage.get("output_tokens") or 0
            usage["latency_ms"] = int((time.time() - t0) * 1000)

            if think and out_tok:
                ratio = len(think) / max(1, len(text) + len(think))
                if ratio > cfg.guard.max_reasoning_ratio:
                    msg_w = (f"[{cfg.stage}] 思考占比 {len(think)} 字符偏高，"
                             f"模型 {cfg.model} 可能不匹配该环节")
                    self.warnings.append(msg_w)
                    print(f"⚠️  {msg_w}", flush=True)

            # 🔴 **截断检测（2026-10-17 补，之前这里完全没有）。**
            # 原来只判 `if not text`，于是被 `max_tokens` 切断的正文
            # （`write_section` / `write_audit` 最典型：写到一半断在句子中间）
            # **被当成成功返回** —— `degraded: false`，不报错，不重试。
            # 换 provider 之前它在 `_openai_compat` 两个分支里是有的，
            # 这次是切 MiniMax 时漏了：`_is_truncated` 只读 OpenAI 形状的
            # `completion_tokens` / `finish_reason`，MiniMax 给的是
            # `output_tokens` / `stop_reason`。字段名已一并补进 `_is_truncated`。
            if not text or _is_truncated(usage, stop_reason, budget):
                truncated = bool(text)
                last_reason = "truncated" if truncated else "empty"
                if empty_left <= 0:
                    break
                empty_left -= 1
                what = (f"被 max_tokens={budget} 截断（有 {len(text)} 字符正文）"
                        if truncated else f"无可见输出（思考 {len(think)} 字符）")
                self.warnings.append(
                    f"[{cfg.stage}] 第 {attempt+1} 次{what}，"
                    f"预算翻倍至 {min(cfg.guard.max_output_tokens, budget*2)} 后重试"
                    f"（重试额度还剩 {empty_left}）"
                )
                budget = min(cfg.guard.max_output_tokens, budget * 2)
                continue
            return text, usage

        raise EmptyOutput(
            f"环节 {cfg.stage} 的重试额度用尽仍未拿到内容"
            f"（模型 {cfg.model}，空返回/截断重试 {cfg.guard.retry_on_empty} 次、"
            f"连接中断 {drops} 次，最后原因 {last_reason}）。"
            f"`empty` = 想得太多把预算吃光；`truncated` = 顶到 max_tokens 被切断。"
        )

    # ---------- 工具调用的两个后端 ----------

    def _minimax_tools(self, cfg: StageConfig, system: str | None,
                       messages: list[dict], tools: list[dict],
                       tool_choice: str | None = None) -> tuple[list, dict]:
        key = os.environ.get(cfg.api_key_env or "MINIMAX_API_KEY", "")
        if not key:
            raise ConfigError(f"未设置 {cfg.api_key_env or 'MINIMAX_API_KEY'}")
        base = cfg.base_url or "https://api.minimaxi.com/anthropic"
        url = f"{base.rstrip('/')}/v1/messages"
        headers = {"Authorization": f"Bearer {key}",
                   "Content-Type": "application/json",
                   "x-api-key": key}
        payload: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": cfg.max_output_tokens,
            "temperature": cfg.temperature,
            "messages": messages,
        }
        if tool_choice:
            # Anthropic 形状的强制：**只发那一个工具** + 指名。
            #
            # ⚠️ **这里原来写着「MiniMax-M3 认这个，4/4 全部正确」——已订正。**
            # 那 4/4 是在**提示词本身就点名要调工具**时测的，混淆了自变量。
            # 2026-10-17 分变量复测（每格 n=10，`tool_choice` 一律指名单工具）：
            #
            #   提示词点名工具 + 开思考 + 强制   → 真调 10/10，零编造
            #   提示词点名工具 + 关思考 + 强制   → 真调  6/10，回文本 4/10
            #   提示词点名工具 + 关思考 + 不强制 → 真调  5/10，**真·编造 1/10**
            #   只说「查一下 X」不点名 + 关思考 → 真调  3/3
            #   中性提示 + 开思考 + 强制         → 真调  1/3
            #
            # ⇒ **`tool_choice` 在 M3 上是暗示，不是保证**：强制 6/10 对不强制
            #   5/10，差异在噪声内。真正决定成败的是**提示词想不想要这个工具**。
            # ⇒ 所以 MiniMax 上的「保证」只能来自三层里的另外两层
            #   （Phase A 自愿交卷 + `_parse_json` 兜底），Phase B 是加分项。
            #   平台层面（internai/xj189）的强制仍是硬的，两边行为不同，别混记。
            pick = next((t for t in tools or []
                         if isinstance(t, dict) and t.get("name") == tool_choice),
                        None)
            if pick is None:
                raise ConfigError(
                    f"环节 {cfg.stage} 要求强制调用 {tool_choice}，tools 里没有它")
            payload["tools"] = [pick]
            payload["tool_choice"] = {"type": "tool", "name": tool_choice}
        elif tools:
            # 同 `_opencode_tools`：空数组要整个省略（见那里的实测记录）。
            payload["tools"] = tools
        if system:
            payload["system"] = system
        # 思考开关与 `_minimax` 同一套（那里的实测对照表是依据）。
        #
        # ⚠️ **这里比 `_minimax` 更要紧**。2026-10-17 首测 n=1：
        # `thinking.type=disabled` + 工具（且提示词点名要调）时，MiniMax-M3
        # 回一段文本声称「已经按你的要求调用 lookup」并编造查询结果 ——
        # 比不调用危险得多，因为它把「没查」伪装成「查过了」。
        # 后面 n=10 复现把自变量分开了（表见上面 `tool_choice` 那段）：
        # 危险格是「**提示词点名工具** + 关思考」，命中率掉到 5~6/10 并出现真编造；
        # 而「只说『查一下 X』」不点名时，关思考照样 3/3 正常调用。
        # 由 `run_tool_loop` 在发之前拦住（只有真要发工具的那次调用才知道）。
        if cfg.reasoning_effort:
            payload["thinking"] = {"type": "adaptive"}
            if cfg.thinking_effort:
                payload["output_config"] = {"effort": cfg.thinking_effort}
        else:
            payload["thinking"] = {"type": "disabled"}
        # tools 参与缓存前缀且排在 system 之前，所以循环里 tools 必须始终一致
        if cfg.stream:
            payload["stream"] = True
        t0 = time.time()
        # 与 `_minimax` 共用同一个发送入口 —— 这两条路以前各写各的，
        # 流式支持只补一边的话，工具那一半会继续静默走非流式
        status, body, parsed = self._minimax_once(cfg, url, headers, payload)
        if status != 200:
            raise RuntimeError(f"MiniMax {status}: {body[:300]}")
        usage = (parsed or {}).get("usage") or {}
        usage["latency_ms"] = int((time.time() - t0) * 1000)
        blocks_out = (parsed or {}).get("content") or []
        # ⚠️ **工具参数被截断，这里只告警不失败**（2026-10-17）。
        # `_openai_compat_forced` 那条路靠 `_reject_broken_args` 硬失败，
        # 因为 SSEAccum 拼出来的是**字符串**，坏了就是坏了。
        # Anthropic 形状返回的是**已解析的对象**——JSON 永远合法，
        # 截断只表现为「少几个字段」，下游 `_normalize_tasks` 照常补齐。
        # 那时抛异常会把一份可用的任务表变成整环节失败，
        # 代价大于收益，所以这里选择**说出来**而不是**炸掉**。
        # ⚠️ 这个缺口是真实存在的：工具侧截断目前只有告警，没有硬防线。
        stop_reason = (parsed or {}).get("stop_reason")
        # 流式下 `input_json` 是**拼出来的字符串**，拼坏了就是坏了 ——
        # 与 `_openai_compat_forced` 同一套处理，不许 `{}` 蒙混过关。
        # （非流式下平台返回的已经是解析好的对象，这一层不会触发。）
        _reject_broken_args(blocks_out, cfg.stage, stop_reason)
        if str(stop_reason or "") == "max_tokens" and any(
                b.get("type") == "tool_use" for b in blocks_out):
            msg_w = (f"[{cfg.stage}] 工具参数被 max_tokens={cfg.max_output_tokens} "
                     f"截断，任务表可能不完整（stop_reason=max_tokens）")
            self.warnings.append(msg_w)
            print(f"⚠️  {msg_w}", flush=True)
        return blocks_out, usage

    def _opencode_tools(self, cfg: StageConfig, system: str | None,
                        messages: list[dict], tools: list[dict]) -> tuple[list, dict]:
        """OpenAI 形状。返回 Anthropic 形状的块，好让调用方不必分支。"""
        key = os.environ.get(cfg.api_key_env or "", "")
        if not key:
            raise ConfigError(
                f"未设置 {cfg.api_key_env or '(该阶段未配置 api_key_env)'}")
        # 工具循环第二轮起，对话里会带上 tool_use / tool_result **内容块**，
        # 那是 Anthropic 形状 —— OpenAI 端点要的是 tool_calls + 独立的
        # role:"tool" 消息。不转的话信通院直接 400（见
        # `_anthropic_msgs_to_openai` 的实测记录）。第一轮全是纯字符串，
        # 转换是恒等的。
        msgs = _anthropic_msgs_to_openai(messages)
        if system:
            msgs = [{"role": "system", "content": system}] + msgs
        payload: dict[str, Any] = {
            "model": cfg.model, "messages": msgs,
            "max_tokens": cfg.max_output_tokens, "temperature": cfg.temperature,
        }
        # ⚠️ **空 tools 必须整个省略，不能发 `[]`**（2026-10-17 实测撞到）。
        # `run_tool_loop` 的最后一轮按设计就是「不带工具」，于是这里会发空数组，
        # 而信通院严格校验：
        #     400 upstream_request_rejected
        #     "`tools` must not be an empty array. Either provide at least
        #      one tool or omit the field entirely."
        # xj189 不校验块形状，这个洞从 2026-10-15 工具循环加回来起就一直藏着
        # —— 与坑 41（第二轮 400）同一形态：**平台的宽松把协议错误盖住了**。
        if tools:
            payload["tools"] = to_openai_tools(tools)
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
                # 同流式分支：解析失败原来完全静默，这里给 `{}` 并打标记，
                # 由 `complete_json_forced` 转成显式失败。
                args = {}
                blocks.append({"type": "tool_use", "id": tc.get("id"),
                               "name": fn.get("name"), "input": args,
                               "_args_broken": True})
                continue
            blocks.append({"type": "tool_use", "id": tc.get("id"),
                           "name": fn.get("name"), "input": args})
        if msg.get("content"):
            blocks.append({"type": "text", "text": msg["content"]})
        u = j.get("usage") or {}
        u["latency_ms"] = int((time.time() - t0) * 1000)
        return blocks, u

    def _openai_compat_forced(self, cfg: StageConfig, system: str | None,
                              prompt: str, tool: dict,
                              messages: list[dict] | None = None) -> tuple[list, dict]:
        """带 `tool_choice` 的强制调用。返回 Anthropic 形状的块。

        与 `_opencode_tools` 的区别只有一个：多发 `tool_choice`，
        让平台**强制**模型走工具而不是回文本。这是
        `complete_json_forced` 的后端，见那里的说明。

        ⚠️ **2026-10-17 加了流式分支和 `messages` 参数。**
        之前只有非流式，于是「用工具调用约束长输出」这条路对
        `write_plan_tasks`（`stream: true`）**一直没法用** ——
        我上一轮草草记成「暂不采用」就再没管，那其实是把主线丢了。
        两个环节拆开之后它是独立 stage，可以单独挂。
        """
        key = os.environ.get(cfg.api_key_env or "", "")
        if not key:
            raise ConfigError(
                f"未设置 {cfg.api_key_env or '(该阶段未配置 api_key_env)'}")
        if messages is None:
            msgs = ([{"role": "system", "content": system}] if system else []) + [
                {"role": "user", "content": prompt}]
        else:
            # 工具循环续接：对话里已经带了 tool_use / tool_result，
            # 必须走同一套协议转换（Anthropic 形状 → OpenAI 形状）
            msgs = _anthropic_msgs_to_openai(messages)
            if system:
                msgs = [{"role": "system", "content": system}] + msgs
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

        # ---- 流式分支（2026-10-17 加）----
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
            blocks = acc.blocks()
            # ⚠️ 参数被截断 → `{}` 会伪装成「成功的空结果」。这里必须响。
            _reject_broken_args(blocks, cfg.stage, acc.finish_reason)
            return blocks, usage

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
                # 同上：这个分支以前和流式不对称——流式有标记有拒绝，
                # 非流式没有，于是「拼不成 JSON」在这条路上是静默的。
                args = {}
                blocks.append({"type": "tool_use", "id": tc.get("id"),
                               "name": fn.get("name"), "input": args,
                               "_args_broken": True})
                continue
            blocks.append({"type": "tool_use", "id": tc.get("id"),
                           "name": fn.get("name"), "input": args})
        if msg.get("content"):
            blocks.append({"type": "text", "text": msg["content"]})
        u = j.get("usage") or {}
        u["latency_ms"] = int((time.time() - t0) * 1000)
        _reject_broken_args(blocks, cfg.stage,
                            (j["choices"][0].get("finish_reason") or None))
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


def _has_reasoning_field(usage: dict) -> bool:
    """平台有没有**自己报**思考 token。

    没有就只能靠我们自己攒的思考正文估算——告警里必须如实标出来，
    否则一个粗估值会被当成实测值引用。
    """
    u = usage or {}
    cur: object = u
    for k in ("completion_tokens_details", "reasoning_tokens"):
        if not isinstance(cur, dict):
            return False
        cur = cur.get(k)
        if cur:
            return True
    return False


def _reasoning_tokens(usage: dict, reasoning_text: str = "") -> int:
    """从各家 usage 里取出「有多少输出 token 花在了思考上」。

    和 `_cache_read` 同一个坑：**同一个概念放在不同位置，甚至根本不报。**
      · OpenAI 标准：`completion_tokens_details.reasoning_tokens`
      · 顶层 `reasoning_tokens`（部分聚合平台）
      · **xj189 实测两者都没有**——`completion_tokens_details` 是 `{}`，
        报文里也没有 `reasoning_content`

    ⚠️ 最后一这条意味着**光读 usage 永远读到 0**，
    于是「思考占比超过阈值」那道告警在 xj189 上恒不触发——
    而它本来正是为抓「thinking 吃光预算」写的。
    （见 `2026-10-15` 那轮全量跑：write_plan 空返回、告警一条没响。）

    ⇒ 兜底改用**我们自己流式攒下来的思考正文**估算。
    宁可给一个近似值，也不要让这道防线静默失效。
    中英混排 3 字符/token 是粗估，重点是**量级**对不对，
    不是小数点后几位。
    """
    u = usage or {}
    for path in (("completion_tokens_details", "reasoning_tokens"),
                 ("reasoning_tokens",)):
        cur: object = u
        for k in path:
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(k)
        if cur:
            try:
                return int(cur)
            except (TypeError, ValueError):
                pass
    return (len(reasoning_text or "") + 2) // 3 if reasoning_text else 0


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
