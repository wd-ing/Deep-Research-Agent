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
—— ⚠️ **精确的丢弃点不是 `SSEAccum`**：它其实把 tool_calls 攒进了 `_calls`，
`blocks()` 能完整取到（这一点 2026-10-16 的 `smoke_intern_ai.py` 已实测证过）。
真正丢的是**下一行**：`_opencode` 的流式分支 `return txt, usage` 只交文本，
于是只发了 tool_call 没发正文的那些块，在这里被丢掉 →
`if not txt:` 判成「空返回」→ 翻倍重试 → 再调工具 → 再丢 → 放弃。

⇒ 这能一次解释「想了一会儿就停、正文一个字没有」。

⚠️ **这个假设至今没有任何证据支持过**（2026-10-16 写下时就说「未验证」，
一直因为 xj189 不可达没能验）。本脚本是第一次真去验它。
**本轮平台换成信通院 discovery-api**（`defaults.provider` 读取，跟得上）。

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
    # 平台从 `defaults.provider` 读，别写死 —— 换平台后这个探针要还能用
    prov_name = sys.argv[2] if len(sys.argv) > 2 else cfg["defaults"]["provider"]
    p, st = cfg["providers"][prov_name], cfg["stages"]["write_plan"]
    key = os.environ.get(p["api_key_env"], "")
    msgs = [{"role": "system", "content": prompts.WRITE_PLAN_TASKS},
            {"role": "user", "content": build_prompt()}]

    print(f"=== 平台 {prov_name}（{p['model']}）===")
    print(f"base_url: {p['base_url']}")

    # ⚠️ 这里必须先把换行去掉再查子串（2026-10-17 修）：
    # 提示词原文是「…看不准的时候可以调用\n工具取原文——取完再继续判断。」，
    # **那句被换行劈开了**，直接 `in` 原文会得到 False，
    # 于是打出「提示词里承诺工具：False」——差点被当成提示词没承诺过工具。
    # 同类坑：读代码看到的是 `prompt.py:377-378` 两行，肉眼扫一遍容易漏。
    flat = prompts.WRITE_PLAN_TASKS.replace("\n", "")
    print(f"提示词里承诺工具：{'可以调用工具取原文' in flat}")
    print(f"实际传给模型的工具：0 个（complete_json 不带 tools）\n")

    saw_calls = 0
    ok = 0
    fields = collections.Counter()
    # 形态 A 计数：`finish=stop` **且**正文 0 字 = 模型自己停了却没写正文
    # （形态 B 是 `finish=length`，另一个病因，不能混着统计）
    stop_no_content = [0, []]
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
        if fin == "stop" and not good:
            stop_no_content[0] += 1
            stop_no_content[1].append(len(reason))
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

    stops = stop_no_content[0]
    lengths = stop_no_content[1]
    if saw_calls:
        print("\n⇒ **模型确实在调工具**，而我们没给工具定义 —— 假设成立：")
        print("  那些 tool_call 被 `_opencode` 的 `return txt` 丢掉（"
              "`SSEAccum` 本身没丢，它攒在 `_calls` 里，`blocks()` 能取到），")
        print("  于是 `if not txt:` 判成「空返回」→ 翻倍重试 → 再调 → 再丢。")
        print("  修法二选一：① 把工具接上（保留提示词的承诺）"
              "\n             ② 把提示词里「可以调用工具」那句删掉")
    elif stops:
        print(f"\n⚠️ **结论必须限定**：这次确实抓到了形态 A（`finish=stop` 且正文 0 字）"
              f"{stops} 次，\n   而它一次工具调用都没发 ⇒ **对形态 A 而言，这个假设被证伪**。")
        print("   但要注意：这**只覆盖形态 A**。形态 B（finish=length、思考烧光预算）"
              "是另一个病因，")
        print("   拿形态 B 的样本来否定形态 A 的假设是无效推理。")
    else:
        print("\n⚠️ **不能下任何结论** —— 这批样本里形态 A 一次都没出现")
        print(f"   （finish=stop + 正文 0 字 出现 {stops} 次；"
              f"思考字数分布 {sorted(lengths)}）。")
        print("   形态 A 没复现 ⇒ **既没证实也没证伪**这个假设。")
        print("   拿形态 B（finish=length）的样本来否定形态 A 的假设是**无效推理** ——")
        print("   这正是我第一版脚本犯的错，它会直接打印"
              "「这个假设不成立」，那是过度断言。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
