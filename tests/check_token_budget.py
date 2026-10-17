"""预算体检：每个环节的「实测最大输出」占配置上限多少。

为什么要有这个
--------------
2026-10-15 一轮全量跑里 `write_plan` 空返回，导致 `tasks=0`、整篇没正文。
回头查 `llm_call` 才发现 **v5 实测最大输出 7,654 = 当时上限 4,096 的 187%**
——这个环节**一直在靠「空返回翻倍重试」硬撑**，而且撑得并不稳
（4,096 下成功率只有 1/14）。

这个数字从跑完那天起就明明白白躺在库里，没人看。
本脚本做的就是把它**摆到台面上**：超 100% 说明已在靠兜底活着；
超 70% 说明贴墙，再涨一轮就会开始失败。

⚠️ **「实测最大」只统计成功的那几次**（`status='ok'`）。空返回那些行
`output_tokens=0`，混进来会把水位压低、���成假的安全感——
这正是这类体检脚本最容易骗自己的地方。

用法：
    python tests/check_token_budget.py [db路径 ...]
    python tests/check_token_budget.py --warn-only     # 只出报告，不因失败退出
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research_agent.llm.provider import ModelRegistry  # noqa: E402

# 贴墙到这个比例就开始警告（还没超，但余量不多了）
WARN_RATIO = 0.70


def collect(db: str, reg: ModelRegistry) -> dict[str, tuple[int, int, int]]:
    """stage -> (成功次数, 实测最大输出, 实测均值输出)"""
    c = sqlite3.connect(db)
    q = ("select stage, count(*), max(output_tokens), avg(output_tokens) "
         "from llm_call where status='ok' and output_tokens > 0 group by stage")
    return {st: (n, mx, av) for st, n, mx, av in c.execute(q)}


def main(argv: list[str]) -> int:
    warn_only = "--warn-only" in argv
    dbs = [a for a in argv if not a.startswith("--")] or [
        "/tmp/ra_xj189_v5.db", "/tmp/ra_v6_full.db"]
    reg = ModelRegistry()

    obs: dict[str, tuple[int, int, int]] = {}
    for db in dbs:
        if not Path(db).exists():
            print(f"（跳过 {db}：不存在）")
            continue
        for st, v in collect(db, reg).items():
            n, mx, av = v
            p = obs.get(st)
            # 合并多库：次数相加，最大值取大
            obs[st] = ((p[0] + n if p else n),
                       max(p[1] if p else 0, mx),
                       max(p[2] if p else 0, av))

    if not obs:
        print("没有可用数据")
        return 0

    print(f"{'环节':<18}{'effort':<7}{'上限':>8}{'兜底':>8}"
          f"{'实测最大':>9}{'占比':>7}{'成功次数':>9}")
    print("-" * 72)
    over: list[str] = []
    near: list[str] = []

    # ⚠️ 配置里没有的 stage **不能让它崩**（2026-10-17 修）。
    # 起因：`write_plan` 拆成了 `write_plan_spec` / `write_plan_tasks`，
    # 而历史数据里那些行的 stage 还写着 `write_plan` ——
    # 脚本直接 `reg.stage(st)` 就抛 ConfigError，整个体检跑不完。
    # 而「历史数据归不到新 stage」本身就是要报告的事实，不能被崩溃盖掉。
    orphan: list[tuple[str, int, int]] = []
    live: dict[str, tuple[int, int, int]] = {}
    for st, v in obs.items():
        try:
            reg.stage(st)
        except Exception:
            orphan.append((st, v[0], v[1]))
            continue
        live[st] = v

    rows = sorted(live.items(),
                  key=lambda kv: -kv[1][1] / max(reg.stage(kv[0]).max_output_tokens, 1))
    for st, (n, mx, av) in rows:
        cfg = reg.stage(st)
        cap = cfg.max_output_tokens
        ratio = mx / cap if cap else 0.0
        flag = ""
        if ratio > 1.0:
            flag = "🔴 超上限"
            over.append(st)
        elif ratio >= WARN_RATIO:
            flag = "⚠️ 贴墙"
            near.append(st)
        print(f"{st:<18}{cfg.reasoning_effort or '-':<7}{cap:>8,}"
              f"{cfg.guard.max_output_tokens:>8,}{mx:>9,}{ratio:>6.0%}{n:>9}  {flag}")

    if orphan:
        print(f"\n⚠️ 配置里已不存在的环节（{len(orphan)} 个），其历史数据无法归因：")
        for st, n, mx in sorted(orphan, key=lambda x: -x[2]):
            print(f"   {st:<18}{n:>4} 次  实测最大 {mx:,}")
        print("   —— 这不是错误，是**改名/拆分的代价**：这些调用的归属已经不可知。")

    print()
    if over:
        print(f"🔴 超上限 {len(over)} 个：{', '.join(over)}")
        print("   这些环节**已经在靠「空返回翻倍重试」活着**，平时看着没事。")
        print("   正确修法是抬基础预算，不是加重试——见 models.yaml 里的注释。")
    if near:
        print(f"⚠️ 贴墙 {len(near)} 个（≥{WARN_RATIO:.0%}）：{', '.join(near)}")
        print("   还没超，但余量不多。下次任务变复杂就可能失败。")
    if not over and not near:
        print("✅ 所有环节都在预算内，且离墙还有余量。")
    print()
    print(f"⚠️ 本脚本只统计**成功**的调用（status='ok'）。空返回的行输出为 0，")
    print(f"   混进来会把水位算低、看起来更安全——那正好是它要抓的东西。")

    if over and not warn_only:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
