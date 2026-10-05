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
from typing import Any

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


# ---------- 当前会话 ----------
# LLMClient 是全局单例，调用点遍布主图与子图，逐个传 session_id 不现实。
# 用 ContextVar 把「这次调用属于哪场研究」作为环境传下去：主图节点进入时设一次，
# 子图与并发协程在同一个 Task 上下文里自然继承。
_CURRENT_SESSION: ContextVar[str | None] = ContextVar(
    "ra_current_session", default=None
)


def current_session() -> str | None:
    return _CURRENT_SESSION.get()


@contextmanager
def session_scope(session_id: str | None):
    """在一场研究的范围内记账。子图、并发协程自动继承。"""
    tok = _CURRENT_SESSION.set(session_id)
    try:
        yield
    finally:
        _CURRENT_SESSION.reset(tok)


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

    @classmethod
    def from_dict(cls, d: dict | None) -> "GuardPolicy":
        d = d or {}
        return cls(
            max_reasoning_ratio=float(d.get("max_reasoning_ratio", 0.5)),
            retry_on_empty=int(d.get("retry_on_empty", 2)),
            max_output_tokens=int(d.get("max_output_tokens", 4096)),
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
            api_key_env=s.get("api_key_env", d.get("api_key_env", "OPENCODE_API_KEY")),
            batch_size=int(s.get("batch_size", 20)),
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


class LLMClient:
    """统一调用入口。厂商差异收敛在这里。"""

    def __init__(self, registry: ModelRegistry | None = None) -> None:
        self.registry = registry or ModelRegistry()
        self._client = httpx.Client(timeout=180.0)
        self.warnings: list[str] = []

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
        if cfg.provider == "opencode":
            text, usage = self._opencode(cfg, prompt, system, json_mode)
        elif cfg.provider == "minimax":
            text, usage = self._minimax(cfg, prompt, system, json_mode)
        elif cfg.provider == "gemini":
            text, usage = self._gemini(cfg, prompt, system, json_mode)
        else:
            raise ConfigError(f"未知 provider: {cfg.provider}")
        self._record(cfg, prompt, text, usage)
        return text

    def complete_json(self, stage: str, prompt: str, system: str | None = None) -> Any:
        raw = self.complete(stage, prompt, system=system, json_mode=True)
        return _parse_json(raw)

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
        if cfg.provider == "minimax":
            blocks, usage = self._minimax_tools(cfg, system, messages, tools)
        elif cfg.provider == "opencode":
            blocks, usage = self._opencode_tools(cfg, system, messages, tools)
        else:
            raise ConfigError(
                f"环节 {stage} 的 provider {cfg.provider} 暂不支持工具调用")
        text = "".join(b.get("text", "") for b in blocks
                       if b.get("type") == "text")
        self._record(cfg, _msgs_chars(messages), text, usage)
        return {"blocks": blocks, "usage": usage}

    # ---------- 记账（P7：只记不告警） ----------

    def _record(self, cfg: StageConfig, prompt: str, out: str,
                usage: dict | None = None) -> None:
        """记一次调用。usage 是 API 报上来的真实值，缺失时退回本地估算。

        真实值里最要紧的是 cache_read_input_tokens：write 环节 12 个子 agent
        共享一段固定前缀，它决定我们到底省没省钱。只记本地估算的话，
        这个数永远看不到。
        """
        try:
            from ..db.repo import get_repo

            u = usage or {}
            get_repo().log_llm_call(
                session_id=_CURRENT_SESSION.get(),
                stage=cfg.stage,
                model=f"{cfg.provider}/{cfg.model}",
                input_tokens=int(u.get("input_tokens") or 0) or _rough_tokens(prompt),
                output_tokens=int(u.get("output_tokens") or 0) or _rough_tokens(out),
                latency_ms=int(u.get("latency_ms") or 0) or None,
                cache_read_tokens=int(u.get("cache_read_input_tokens") or 0),
                cache_creation_tokens=int(u.get("cache_creation_input_tokens") or 0),
                est_input_tokens=_rough_tokens(prompt),
            )
        except Exception:
            pass  # 记账失败不影响主流程

    # ---------- OpenCode ----------

    def _opencode(
        self, cfg: StageConfig, prompt: str, system: str | None, json_mode: bool
    ) -> tuple[str, dict]:
        key = os.environ.get(cfg.api_key_env or "OPENCODE_API_KEY", "")
        if not key:
            raise ConfigError(f"未设置 {cfg.api_key_env or 'OPENCODE_API_KEY'}")

        msgs = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        budget = cfg.max_output_tokens
        attempts = cfg.guard.retry_on_empty + 1 if cfg.stage in NEEDS_GUARD else 1
        last_reason = ""
        drops = 0          # 因连接中断而重试的次数，用于在恢复时明确告知

        for attempt in range(attempts):
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

            t0 = time.time()
            try:
                r = self._client.post(
                    f"{cfg.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                        "x-opencode-session": self.registry.session_header(),
                    },
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
                #
                # 真正有效的控制手段在别处：压低思考（output_config.effort）、
                # 切小单次输入、把过重的调用拆开。连续失败会抛上去，
                # 由上层降级路径接手，不静默。
                last_reason = f"transport: {type(e).__name__}"
                drops += 1
                if attempt + 1 >= attempts:
                    self.warnings.append(
                        f"[{cfg.stage}] 连续 {attempts} 次连接中断"
                        f"（{type(e).__name__}），该环节判定不可用")
                    print(f"❌ [{cfg.stage}] 连续 {attempts} 次连接中断，放弃", flush=True)
                    raise
                self.warnings.append(
                    f"[{cfg.stage}] 第 {attempt+1}/{attempts} 次连接中断"
                    f"（{type(e).__name__}），退避重试")
                print(f"⚠️  [{cfg.stage}] 连接中断（{attempt+1}/{attempts}），退避重试",
                      flush=True)
                time.sleep(2.0 * (attempt + 1))
                continue
            if drops:
                print(f"✅ [{cfg.stage}] 第 {attempt+1} 次重试成功"
                      f"（此前中断 {drops} 次）", flush=True)
            if r.status_code != 200:
                raise RuntimeError(f"OpenCode {r.status_code}: {r.text[:300]}")
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

            # 防线一：内容为空则重试并翻倍预算
            if not text:
                last_reason = "empty"
                budget = min(cfg.guard.max_output_tokens, budget * 2)
                self.warnings.append(
                    f"[{cfg.stage}] 第 {attempt+1} 次返回空内容，"
                    f"预算翻倍至 {budget} 后重试"
                )
                continue
            return text, usage

        raise EmptyOutput(
            f"环节 {cfg.stage} 连续 {attempts} 次返回空内容"
            f"（模型 {cfg.model}，最后原因 {last_reason}）。"
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
        attempts = cfg.guard.retry_on_empty + 1 if cfg.stage in NEEDS_GUARD else 1
        drops = 0          # 因连接中断而重试的次数，用于在恢复时明确告知
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
                if attempt + 1 >= attempts:
                    self.warnings.append(
                        f"[{cfg.stage}] 连续 {attempts} 次连接中断"
                        f"（{type(e).__name__}），该环节判定不可用")
                    print(f"❌ [{cfg.stage}] 连续 {attempts} 次连接中断，放弃", flush=True)
                    raise
                self.warnings.append(
                    f"[{cfg.stage}] 第 {attempt+1}/{attempts} 次连接中断"
                    f"（{type(e).__name__}），退避重试")
                print(f"⚠️  [{cfg.stage}] 连接中断（{attempt+1}/{attempts}），退避重试",
                      flush=True)
                time.sleep(2.0 * (attempt + 1))
                continue
            if drops:
                print(f"✅ [{cfg.stage}] 第 {attempt+1} 次重试成功"
                      f"（此前中断 {drops} 次）", flush=True)
            if r.status_code != 200:
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
                self.warnings.append(
                    f"[{cfg.stage}] 第 {attempt+1} 次无可见输出（思考 {len(think)} 字符），"
                    f"预算翻倍至 {min(cfg.guard.max_output_tokens, budget*2)} 后重试"
                )
                budget = min(cfg.guard.max_output_tokens, budget * 2)
                continue
            return text, usage

        raise EmptyOutput(
            f"环节 {cfg.stage} 连续 {attempts} 次无可见输出"
            f"（模型 {cfg.model}）。这是 reasoning 模型思考吃光预算的典型失效模式。"
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
        key = os.environ.get(cfg.api_key_env or "OPENCODE_API_KEY", "")
        if not key:
            raise ConfigError(f"未设置 {cfg.api_key_env or 'OPENCODE_API_KEY'}")
        msgs = ([{"role": "system", "content": system}] if system else []) + [
            {"role": m["role"], "content": m["content"]} for m in messages]
        payload: dict[str, Any] = {
            "model": cfg.model, "messages": msgs, "tools": tools,
            "max_tokens": cfg.max_output_tokens, "temperature": cfg.temperature,
        }
        if cfg.reasoning_effort:
            payload["reasoning_effort"] = cfg.reasoning_effort
        t0 = time.time()
        r = self._client.post(
            f"{cfg.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "x-opencode-session": self.registry.session_header()},
            json=payload, timeout=cfg.timeout_s,
        )
        if r.status_code != 200:
            raise RuntimeError(f"OpenCode {r.status_code}: {r.text[:300]}")
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
