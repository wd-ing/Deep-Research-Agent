"""`config/models.yaml` 环节→模型钉死关系的守门测试（2026-10-20）。

背景：assess / write_plan_spec / write_plan_tasks 迁到 M3.1-Flash low
（A/B 实测：分派输出 16~38k→6~10k token、耗时 140~424s→34~62s，
依据 /tmp/exp_model_ab/summary.txt）；write_section / write_revise /
write_patch **必须留 M3 + forced_thinking: disabled**（它们靠关思考
+ round_max_tokens 吃饭，M3.1 关不掉思考，disabled → 400）。

有人把这三个环节改回 defaults（M3）或把 write_* 挪到 minimax31 时，
这里会当场指出来。

用法：
    .venv/bin/python tests/test_stage_migration.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("MINIMAX_API_KEY", "stub-not-used")

from research_agent.llm.provider import ModelRegistry  # noqa: E402

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}"
          + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def main() -> int:
    print("=== stages → M3.1/M3 钉死校验 ===")
    reg = ModelRegistry()

    for stage in ("assess", "write_plan_spec", "write_plan_tasks",
                  "write_audit", "outline", "probe_outline"):
        c = reg.stage(stage)
        check(f"{stage}: M3.1-Flash + low/low + 无 forced_thinking",
              c.provider == "minimax"
              and c.model == "MiniMax-M3.1-Flash"
              and c.reasoning_effort == "low"
              and c.thinking_effort == "low"
              and c.forced_thinking is None,
              f"{c.provider}/{c.model} "
              f"{c.reasoning_effort}/{c.thinking_effort} "
              f"forced={c.forced_thinking}")

    for stage in ("write_section", "write_revise", "write_patch"):
        c = reg.stage(stage)
        check(f"{stage}: 仍 M3 + forced_thinking=disabled",
              c.model == "MiniMax-M3"
              and c.forced_thinking == "disabled",
              f"{c.model} forced={c.forced_thinking}")

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
