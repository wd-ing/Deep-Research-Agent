"""`config/models.yaml` 环节→模型钉死关系的守门测试（2026-10-20）。

背景：2026-11-03 MiniMax 订阅到期，迁火山方舟（Ark）。
难环节（assess / write_plan_spec / write_plan_tasks / write_audit /
outline / probe_outline）走 ark_glm = glm-5-3-flash-260828 +
`reasoning_effort: low`（glm 关不掉思考，缺省=max 会 120s+ 超时，
validate() 拦死）；其余 12 个环节走默认 ark_mini =
doubao-seed-2-0-mini-260428，`reasoning_effort: disabled` 经档案
effort_off:none 翻译成发 `none`。write_* 的 MiniMax 专属键
（forced_thinking / round_max_tokens）必须删掉——validate() 对
非 minimax 会拦。

有人把环节挪回 MiniMax 档案、给 glm 环节删了 reasoning_effort、
或把 MiniMax 专属键留在 ark 环节上时，这里会当场指出来。

用法：
    .venv/bin/python tests/test_stage_migration.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _isolate  # noqa: F401, E402

os.environ.setdefault("MINIMAX_API_KEY", "stub-not-used")
os.environ.setdefault("ARK_API_KEY", "stub-not-used")

from research_agent.llm.provider import ModelRegistry  # noqa: E402

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}"
          + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def main() -> int:
    print("=== stages → ark_mini/ark_glm 钉死校验 ===")
    reg = ModelRegistry()

    for stage in ("assess", "write_plan_spec", "write_plan_tasks",
                  "write_audit", "outline", "probe_outline"):
        c = reg.stage(stage)
        check(f"{stage}: ark_glm glm-flash + high + 无 forced_thinking",
              c.provider == "ark"
              and c.model == "glm-5-3-flash-260828"
              and c.reasoning_effort == "high"
              and c.thinking_effort is None
              and c.forced_thinking is None,
              f"{c.provider}/{c.model} "
              f"{c.reasoning_effort}/{c.thinking_effort} "
              f"forced={c.forced_thinking}")

    for stage in ("write_section", "write_revise", "write_patch"):
        c = reg.stage(stage)
        check(f"{stage}: ark_glm + low 无 MiniMax 专属键",
              c.provider == "ark"
              and c.model == "glm-5-3-flash-260828"
              and c.reasoning_effort == "low"
              and c.forced_thinking is None
              and c.round_max_tokens is None,
              f"{c.provider}/{c.model} effort={c.reasoning_effort} "
              f"forced={c.forced_thinking} round={c.round_max_tokens}")

    # 迁移三环节的具体额度/超时（M3.1 思考与正文共享 max_tokens，
    # 按 A/B 实测峰值 2~4 倍富余给；timeout 与 write_audit 同款 420）。
    for stage, want_max, want_to in (
            ("assess", 16384, 300.0),
            ("outline", 16384, None),
            ("probe_outline", 16384, None),
            ("write_plan_spec", 24576, 420.0),
            ("write_plan_tasks", 32768, 420.0)):
        c = reg.stage(stage)
        check(f"{stage}: max_output_tokens={want_max} "
              f"timeout={want_to or '默认'} guard 同额",
              c.max_output_tokens == want_max
              and (want_to is None or c.timeout_s == want_to)
              and c.guard.max_output_tokens == want_max,
              f"max={c.max_output_tokens} to={c.timeout_s} "
              f"guard={c.guard.max_output_tokens}")

    # write_section 强制交卷轮预算 16384（2026-10-27 批 4：v21 预加载
    # 一次写完整节，t14 的 submit_section 入参在 8192 顶断白烧 55s）。
    # round_max_tokens 2048 同样是 P2 压上限设计，不许跟着涨；
    # timeout_s 继承 defaults 300s：16384 ÷ 最慢 62 tok/s ≈ 264s 够。
    # fast_answer 是纯文本流式：forced_thinking 只作用于工具轮、
    # 对它无效；M3 关思考必须走 reasoning_effort: disabled
    # （语义值 → profile effort_off → thinking.type=disabled）。
    # 2026-10-27 冒烟里 `low` 曾让思考吃光 16384 预算两次重试——
    # 钉 yaml 原文而不是装载后的 cfg（'disabled' 会被翻译掉）。
    import yaml
    yml = yaml.safe_load(open(reg.path, encoding="utf-8"))
    fa = yml["stages"]["fast_answer"]
    c = reg.stage("fast_answer")
    check("fast_answer: ark_glm + low + 无 forced_thinking",
          (fa.get("provider") or "") == "ark_glm"
          and fa.get("reasoning_effort") == "low"
          and "forced_thinking" not in fa
          and c.stream is True
          and c.max_output_tokens == 12288
          and c.guard.max_output_tokens == 16384,
          f"yaml prov={fa.get('provider')} effort={fa.get('reasoning_effort')} "
          f"forced={fa.get('forced_thinking')} "
          f"max={c.max_output_tokens}")

    c = reg.stage("write_section")
    check("write_section: max_output_tokens=16384 且无 round_max_tokens",
          c.max_output_tokens == 16384
          and c.round_max_tokens is None
          and c.guard.max_output_tokens == 16384
          and c.timeout_s == 300.0,
          f"max={c.max_output_tokens} round={c.round_max_tokens} "
          f"guard={c.guard.max_output_tokens} to={c.timeout_s}")

    # write_revise 强制交卷轮预算 8192（2026-10-25 批 3：v20 里
    # submit_section 入参两次被 4096 顶断）；round_max_tokens 2048 是
    # P2 的读证据轮压上限设计，不许跟着涨。
    c = reg.stage("write_revise")
    check("write_revise: max_output_tokens=8192 且无 round_max_tokens",
          c.max_output_tokens == 8192
          and c.round_max_tokens is None
          and c.guard.max_output_tokens == 8192,
          f"max={c.max_output_tokens} round={c.round_max_tokens} "
          f"guard={c.guard.max_output_tokens}")

    # outline/probe_outline 也在上面 M3.1 表里钉死了（2026-10-25 迁移）。
    # 这里只补一条兜底：它们都不许带 forced_thinking（M3.1 关不掉思考）。
    for stage in ("outline", "probe_outline"):
        c = reg.stage(stage)
        check(f"{stage}: 无 forced_thinking",
              c.forced_thinking is None, f"forced={c.forced_thinking}")

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败：")
        for f in FAILED:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
