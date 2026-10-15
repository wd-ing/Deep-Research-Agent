"""抓 write_plan 任务分派的**完整原始流**，看失败那几次到底发生了什么。

背景（2026-10-15）
------------------
`node_plan_tasks` 空返回率很高（effort=high 下 4/13）。失败形态双峰：
思考 ~2,900–3,200 字就 `finish=stop`、正文 0 字；或者思考 >22,000 字烧光预算。

**新怀疑（这次要验的）**：
`WRITE_PLAN_TASKS` 提示词第 378 行明确写着「看不准的时候**可以调用工具取原文**
——取完再继续判断」，但 `node_plan_tasks` 调的是 `LLM.complete_json()`，
**一个工具定义都没传**。

而 `SSEAccum` 把 `content` 和 `tool_calls` **分流**，`text_only()` 只返回 content
—— 如果模型真的按提示词去调工具，那个 tool_call 会被**静默丢掉**，
流式路径 `if not txt:` 就把它判成「空返回」→ 重试 → 再调工具 → 再丢 → 放弃。

⇒ 这能一次解释「想了一会儿就停、正文一个字没有」。

本脚本不只收 content/reasoning，而是**打印每一块 delta 的全部字段名**，
并在最后报告：本次是否出现过 tool_calls。

用法：
    python tests/probe_plan_tasks_raw.py [次数]
"""

from __future__ import annotations

import collections
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


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    cfg = yaml.safe_load((ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))
    p, st = cfg["providers"]["xj189"], cfg["stages"]["write_plan"]
    key = os.environ.get(p["api_key_env"], "")
    msgs = [{"role": "system", "content": prompts.WRITE_PLAN_TASKS},
            {"role": "user", "content": build_prompt()}]

    print(f"提示词里承诺工具：{'可以调用工具取原文' in prompts.WRITE_PLAN_TASKS}")
    print(f"实际传给模型的工具：0 个（complete_json 不带 tools）\n")

    saw_calls = 0
    ok = 0
    fields = collections.Counter()
    for i in range(n):
        body = {
            "model": p["model"], "messages": msgs,
            "max_tokens": st["max_output_tokens"],
            "temperature": st["temperature"],
            "reasoning_effort": st["reasoning_effort"],
            "response_format": {"type": "json_object"},
            "stream": True, "stream_options": {"include_usage": True},
        }
        t0 = time.time()
        text = reason = fin = ""
        calls: list[dict] = []
        raw_keys: set[str] = set()
        try:
            with httpx.stream("POST", f"{p['base_url'].rstrip('/')}/chat/completions",
                              headers={"Authorization": f"Bearer {key}",
                                       "Content-Type": "application/json"},
                              json=body, timeout=300.0) as r:
                r.raise_for_status()
                for obj in _sse_lines(r):
                    for ch in obj.get("choices") or []:
                        raw_keys |= set(ch.keys())
                        d = ch.get("delta") or {}
                        fields.update(d.keys())
                        text += d.get("content") or ""
                        reason += d.get("reasoning_content") or ""
                        fin = ch.get("finish_reason") or fin
                        for tc in (d.get("tool_calls") or []):
                            calls.append(tc)
        except Exception as e:
            # ⚠️ 平台限流/抖动会直接抛。**一次网络错误不能当成"空返回"**——
            #    混进统计就会把基础设施故障算成模型问题（坑 33 的同款）。
            print(f"  {i+1}: ⚠️  {type(e).__name__}: {str(e)[:60]}"
                  f"（网络/平台故障，不计入成功率）")
            time.sleep(5)
            continue
        good = bool(text.strip())
        ok += good
        saw_calls += bool(calls)
        names = [((c.get("function") or {}).get("name") or "?") for c in calls]
        print(f"  {i+1}: {'✅' if good else '❌'} content {len(text):>5} / "
              f"reasoning {len(reason):>6} / finish={fin or '-':<7} / "
              f"{time.time()-t0:6.1f}s"
              + (f" / **tool_calls×{len(calls)} {names}**" if calls else ""))
        time.sleep(2)

    print(f"\n成功 {ok}/{n}；**出现 tool_calls 的次数：{saw_calls}/{n}**")
    print(f"delta 里出现过的字段：{dict(fields)}")
    print(f"chunk 顶层出现过的键：{sorted(raw_keys)}")
    if saw_calls:
        print("\n⇒ **假设成立**：模型按提示词去调工具，而工具调用被 `text_only()`"
              "\n  丢弃，于是这一轮被判成「空返回」。")
        print("  修法二选一：① 真的把工具接上（保留提示词的承诺）"
              "\n             ② 把提示词里「可以调用工具」那句删掉（承认它没有工具）")
    else:
        print("\n⇒ 没看到 tool_calls，这个假设**不成立**——"
              "\n  「想一会儿就停」另有原因，继续查。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
