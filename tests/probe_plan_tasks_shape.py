"""为什么偏偏是这个环节总挂？——把「输出负担」拆成两个维度分别压。

背景（2026-10-15）
-----------------
`node_plan_tasks` 空返回率随 reasoning 档位变化很大：
  effort=high            4/13 ≈ 31%
  effort=low             3/5  ≈ 60%
  不带 reasoning_effort  3/4  ≈ 75%（且 13–20s，另一次是 504）

失败形态高度规律，**双峰**：
  · 思考 ~2,900–3,200 字就 `finish=stop`，正文 0 字
  · 思考 ~22,000–42,000 字，`finish=length`，预算烧光
而成功都落在思考 8,500–19,500 字之间（或完全不思考）。

⚠️ 所以它**不是「随机不好使」**，是有明确结构的。

这个脚本验证一个假设：**触发条件是「精确枚举 + 说明性文字」两件事叠加**，
而不是提示词难、也不是预算不够（v5 实测这个环节的输出只有 ~1,347 token）。

三个变体，同提示词同预算同 effort=high，只改「要模型产出什么」：

  S 小输出     2 个任务、每个 ≤5 个段号、不写说明
  E 只枚举     7 个任务 + 全部 117 个段号，但**不写任何说明文字**
  F 全量       规约原样（对照，已知 ≈31%）

判读：
  S 高 + E 高 → 两件事叠加才是触发条件
  S 高 + E 低 → 光「精确枚举」就够触发
  S 低 + E 高 → 光「写说明」就够触发

用法：
    python tests/probe_plan_tasks_shape.py [每组次数]
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
from research_agent.llm.provider import _sse_lines  # noqa: E402
from probe_plan_tasks_empty import build_prompt  # noqa: E402

SYSTEM = prompts.WRITE_PLAN_TASKS

SMALL = """
**严格输出 JSON**。只给 2 个任务，每个最多列 5 个段号，不写任何说明文字：
{"tasks":[{"id":"t1","covers":["sec1"],"mandate":"一句话","segments":["S1·1"]}]}
"""


def once(p: dict, st: dict, key: str, user: str, system: str) -> tuple:
    body = {
        "model": p["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": st["max_output_tokens"],
        "temperature": st["temperature"],
        "reasoning_effort": st["reasoning_effort"],
        "response_format": {"type": "json_object"},
        "stream": True,
        "stream_options": {"include_usage": True},
    }
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
    return len(text), len(reason), fin, time.time() - t0, text


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    cfg = yaml.safe_load((ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))
    p, st = cfg["providers"]["xj189"], cfg["stages"]["write_plan"]
    key = os.environ.get(p["api_key_env"], "")
    base = build_prompt()
    payload = json.loads(base)

    # 剥掉说明文字，只留段号枚举
    enum_only = {
        "研究问题": payload["研究问题"],
        "写作规约（你上一轮定的，必须遵守）": payload["写作规约（你上一轮定的，必须遵守）"],
        "框架与轮廓": payload["框架与轮廓"],
    }
    enum_sys = SYSTEM + (
        "\n\n**这次只列归属，不写说明**：mandate / must_cover / not_yours "
        "一律填空字符串，covers 照常填。把 7 个任务都列全。\n")

    groups = [
        ("S 小输出（2 任务 / ≤5 段号 / 无说明）", SYSTEM, base + SMALL),
        ("E 只枚举（全量段号 / 说明留空）", enum_sys,
         json.dumps(enum_only, ensure_ascii=False)),
        ("F 全量（对照）", SYSTEM, base),
    ]

    print(f"effort={st['reasoning_effort']}  max_tokens={st['max_output_tokens']:,}  "
          f"每组 {n} 次\n")
    tally = {}
    for label, system, user in groups:
        ok = 0
        print(f"── {label}")
        for i in range(n):
            try:
                cl, rl, fin, dt, txt = once(p, st, key, user, system)
            except Exception as e:
                print(f"   {i+1}: 💥 {type(e).__name__}")
                time.sleep(2)
                continue
            good = bool(txt.strip())
            ok += good
            print(f"   {i+1}: {'✅' if good else '❌'} content {cl:>5} / "
                  f"reasoning {rl:>6} / finish={fin or '-':<7} / {dt:6.1f}s")
            time.sleep(2)
        tally[label.split()[0]] = (ok, n)
        print(f"   → {ok}/{n}\n")

    print("=" * 46)
    for k2, (ok, tot) in tally.items():
        print(f"  {ok}/{tot}  {k2}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
