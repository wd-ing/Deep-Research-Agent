"""子图共用的重试判据与工具入参归一化（2026-10-20 从 write.py 抽出）。

为什么抽出来：`probe_outline` 改走 `complete_json_forced` 后要复用同一套
判据（429/额度不重试、5xx/529 等 `_TRANSIENT_WAIT_S` 再打、工具入参
`{"item":[...]}` 漂移归一），在两个子图里各抄一份迟早分叉。
write.py 通过 `from ._retry import ...` 引回，原有 `W.xxx` 引用不变。
"""

from __future__ import annotations

import json
import re
import time

from ...llm.provider import ConfigError

# MiniMax 5xx/529 是「当前服务集群负载较高，请稍后重试」的瞬时过载——
# 服务端自己要求重试，等 _TRANSIENT_WAIT_S 秒再打一次是有意义的；
# 429/其他 4xx 是额度或参数问题，重发一个死一个，不在此列。
# （2026-10-19 真实跑：二轮审查撞 529 被判不重试，返修稿没复审就收工。）
# 任务级等待。⚠️ 叠加口径（2026-10-20）：provider 层 `_minimax_send`
# 对过载类故障另有独立额度（默认 5 次，退避 10/20/40/60/60s，单次
# 调用最坏 +190s 等待）。本层的 20s×3 叠在它上面——一次「任务」
# 最坏 ≈ 3 × (timeout_s + 190s) + 40s，别把两层当成同一个重试。
_TRANSIENT_WAIT_S = 20
# 流式连接中途收到的 `api_error`（如 `unknown error, 722`）是服务端内部故障，
# 与 5xx 同属瞬时：2026-10-20 v17 的 write_audit 因此整轮审查被跳过。
_TRANSIENT_RE = re.compile(
    r"MiniMax (500|502|503|504|529)\b|MiniMax 流式错误[^\n]*api_error")


def _transient_http(e_or_reason) -> bool:
    """异常对象或原因字符串里是否带 MiniMax 5xx/529 瞬时过载。"""
    return bool(_TRANSIENT_RE.search(str(e_or_reason or "")))


def _audit_retryable(e: Exception) -> bool:
    """哪些异常值得再打一次。

    · MiniMax 5xx/529——瞬时过载（服务端明说「请稍后重试」），重试；
    · EmptyOutput（空返回、工具参数被 `_reject_broken_args` 判截断）及
      其他非 HTTP 异常——随机失败，重试；
    · `MiniMax 4xx`（429 额度/限流、参数错）与 ConfigError——重发没意义。
    """
    if isinstance(e, ConfigError):
        return False
    if _transient_http(e):
        return True
    return not str(e).startswith("MiniMax ")


def _wait_transient(stage: str, e) -> None:
    """因过载重试前先等一会儿，并打一行可被日志看见的提示。"""
    code = _TRANSIENT_RE.search(str(e))
    print(f"⚠️  [{stage}] 服务过载（MiniMax {(code.group(1) if code and code.group(1) else 'api_error')}），"
          f"{_TRANSIENT_WAIT_S}s 后重试", flush=True)
    time.sleep(_TRANSIENT_WAIT_S)


def _norm_audit_list(v) -> list:
    """M3.1 实测会把数组包成 `{"item": [...]}` 或回成字符串——统一收成 list。

    单 dict 的 `item` 值包成一项；字符串按 JSON 试解；其余非 list 归 []。
    """
    if isinstance(v, dict):
        v = v.get("item")            # {"item": X} 包装（真实样本出现过）
    if isinstance(v, dict):
        v = [v]                      # X 本身是单个 dict → 一项
    elif isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = []
    return v if isinstance(v, list) else []
