"""write_plan 任务分派：消融 + 每组重复，用来定位剩下的失败因素。

背景（2026-10-15）
-----------------
全量跑里这一步空返回 → tasks=0 → 整篇报告空。
抬 `max_output_tokens` 4096 → 16384 后，实测成功率：

  4,096   1/14  ≈ 7%
  16,384  3/9   ≈ 33%

⇒ 抬预算**有效但不够**，仍有约 2/3 的尝试失败。

⚠️ **每组必须重复多次。** 单次结果完全不可信：
同一配置、同一提示词，单次测到的"成功"和"失败"都出现过；
同一预算下思考量能差 4.9 倍。n=1 得出的一切"趋势"都是噪声
（见 HANDOVER 坑 34 —— 我已经栽在这上面一次）。

两组各跑 n 次，比较：
  A  effort=high + json_object   （当前生产配置）
  B  effort=high + 无 json_object
  C  无 effort + json_object

另记：失败里出现「reasoning 只有 ~3000 字就 finish=stop」——
**那不是没预算，是想得太浅就收尾不写正文。** 所以单纯加预算治不了它。

用法：
    python tests/probe_plan_tasks_ablation.py [每组次数]
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

for _line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        k, v = _line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import httpx  # noqa: E402
import yaml  # noqa: E402

from research_agent.graph import prompts  # noqa: E402
# ⚠️ 必须复用生产的 SSE 解析器。xj189 发的是 `data:{...}`（冒号后无空格），
# 手搓成 `startswith("data: ")` 会把每一行都跳过、content 恒为 0，
# 测出「必空」的假结论。见 HANDOVER 坑 33。
from research_agent.llm.provider import _sse_lines  # noqa: E402
from probe_plan_tasks_empty import build_prompt  # noqa: E402


def one(p: dict, st: dict, key: str, user: str, *,
        effort: bool, json_mode: bool) -> tuple[int, int, str, float]:
    body: dict = {
        "model": p["model"],
        "messages": [{"role": "system", "content": prompts.WRITE_PLAN_TASKS},
                     {"role": "user", "content": user}],
        "max_tokens": st["max_output_tokens"],
        "temperature": st["temperature"],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if effort:
        body["reasoning_effort"] = st["reasoning_effort"]
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    t0 = time.time()
    text = reason = fin = ""
    with httpx.stream("POST", f"{p['base_url'].rstrip('/')}/chat/completions",
                      headers={"Authorization": f"Bearer {key}",
                               "Content-Type": "application/json"},
                      json=body, timeout=300.0) as r:
        r.raise_for_status()
        for obj in _sse_lines(r):
            for ch in obj.get("choices") or []:
                d = ch.get("delta") or {}
                text += d.get("content") or ""
                reason += d.get("reasoning_content") or ""
                fin = ch.get("finish_reason") or fin
    return len(text), len(reason), fin, time.time() - t0


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    cfg = yaml.safe_load((ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))
    p, st = cfg["providers"]["xj189"], cfg["stages"]["write_plan"]
    key = os.environ.get(p["api_key_env"], "")
    user = build_prompt()
    print(f"提示词一次构造、三组共用（{len(user):,} 字）；"
          f"max_output_tokens={st['max_output_tokens']:,}；每组 {n} 次\n")

    groups = [
        ("A 生产配置 effort=high + json_object", dict(effort=True, json_mode=True)),
        ("B 去掉 json_object", dict(effort=True, json_mode=False)),
        ("C 去掉 reasoning_effort", dict(effort=False, json_mode=True)),
    ]
    tally: dict[str, list[int]] = {}
    for label, kw in groups:
        ok = 0
        print(f"── {label}")
        for i in range(n):
            try:
                cl, rl, fin, dt = one(p, st, key, user, **kw)
            except Exception as e:
                print(f"   {i+1}: 💥 {type(e).__name__}: {str(e)[:70]}")
                time.sleep(2)
                continue
            good = bool(cl)
            ok += good
            print(f"   {i+1}: {'✅' if good else '❌'} content {cl:>5} / "
                  f"reasoning {rl:>6} / finish={fin or '-':<7} / {dt:6.1f}s")
            time.sleep(2)
        tally[label] = [ok, n]
        print(f"   → {ok}/{n}\n")

    print("=" * 52)
    for label, (ok, tot) in tally.items():
        print(f"  {ok}/{tot}  {label}")
    best = max(tally.items(), key=lambda kv: kv[1][0])
    print(f"\n最高：{best[0]}（{best[1][0]}/{best[1][1]}）")
    if best[1][0] == best[1][1]:
        print("⚠️ 组内全成功也**不等于**结论成立——n 太小，看方差（坑 34）。"
              "\n   确认某组更好之前，把它再跑一轮。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
