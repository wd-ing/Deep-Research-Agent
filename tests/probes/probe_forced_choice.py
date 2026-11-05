"""直接打真平台验「`tool_choice` 强制」这条保底路径（线上零覆盖的那一段）。

2026-10-17 加。起因：A/B 探针里 B 组两次都是模型**自己**调 `submit_tasks`，
Phase B（`tool_choice` 强制）一次都没执行，于是
「模型回文本时能不能被强制拉回工具」这件事**只有离线测试背书**。

## 为什么不靠「等模型回文本」来验

那要碰运气。与其等，不如**直接调那一个函数**——

    LLM.complete_with_tools(stage, system, messages, [submit_tasks],
                            tool_choice="submit_tasks")

走的就是 `_openai_compat_forced` 的流式分支，和 Phase B 发出去的报文**逐字相同**
（同 tool_choice 形状、同流式、同 `reasoning_effort`、同预算）。
**唯一没验的链接就是「平台认不认 tool_choice」，而这正是本脚本要打的那一下。**

## 要看的四件事

1. 平台认不认 `tool_choice`（有没有回 `tool_calls`）
2. `arguments` 是不是合法 JSON、能不能解析出任务
3. 段号有没有编造的
4. `finish_reason` 会不会又撞 `length`

用法：
    .venv/bin/python tests/probes/probe_forced_choice.py [次数] [模型]
    # 指定模型（换模型对比时用，注入 registry，走同一条生产路径）：
    .venv/bin/python tests/probes/probe_forced_choice.py 2 Atria-Dawn
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

for _line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        k, v = _line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

SRC_DB = Path(os.environ.get("PROBE_SRC_DB", str(ROOT / "data" / "refs" / "ra_internai_v1.db")))
# Phase B 用的 stage。别在下面散着写字面量——上一版就是因为
# 从另一个文件抄了 `BASE_STAGE` 这个名字，两处 NameError。
BASE_STAGE = "write_plan_tasks"
WORK_DB = Path("/tmp/ra_forced_choice.db")


def _load_probe_module():
    """复用 `probe_plan_tasks_forced.build_state` —— 载荷口径不许各写一套。

    段号编号规则（先编号再过滤）、`outline["sections"]` 的取法都在那儿，
    抄一遍就会抄错（第一版就把顺序抄反了，见 DECISIONS）。
    """
    p = ROOT / "tests" / "probes" / "probe_plan_tasks_forced.py"
    spec = importlib.util.spec_from_file_location("_ppf", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    if not SRC_DB.exists():
        print(f"❌ 找不到 {SRC_DB}")
        return 1
    shutil.copy(SRC_DB, WORK_DB)
    os.environ["RA_DB_PATH"] = str(WORK_DB)

    pf = _load_probe_module()
    state = pf.build_state(str(WORK_DB))
    known = {d["ref"] for d in state["on_topic"]}

    from research_agent.graph import prompts
    from research_agent.graph.subgraphs import write as W
    from research_agent.llm.provider import LLMClient, _parse_json

    prompt = W._dumps({"研究问题": state["question"],
                       "写作规约（你上一轮定的，必须遵守）": state["spec"],
                       "框架与轮廓": W._brief(state)})
    messages = [{"role": "user", "content": W._clip(prompt, 60000)}]

    llm = LLMClient()
    # 换模型对比时只换 `model`，**其余配置与生产一字不改**
    # （预算 / effort / stream / timeout 都照旧）——这样比的才是模型本身。
    if len(sys.argv) > 2 and sys.argv[2]:
        import copy
        alt = copy.copy(llm.registry.stage("write_plan_tasks"))
        alt.model = sys.argv[2]
        llm.registry._cache[BASE_STAGE] = alt
        print(f"⚠️  模型换成 {sys.argv[2]}（其余配置照旧）")
    cfg = llm.registry.stage("write_plan_tasks")
    print(f"载荷 {len(state['on_topic'])} 段 / {len(state['outline'])} 小节")
    print(f"{cfg.provider} / {cfg.model} / effort={cfg.reasoning_effort} / "
          f"max_tokens={cfg.max_output_tokens} / timeout={cfg.timeout_s:.0f} / "
          f"stream={cfg.stream}\n")

    ok_forced = 0
    dropped = 0
    for i in range(n):
        t0 = time.time()
        # ⚠️ 这一行就是 Phase B 发的那个调用，不是「差不多的调用」。
        # 单次失败**不许中断整轮** —— 第一次跑就是被 RemoteProtocolError
        # 直接带崩的，整轮数据全丢。失败也要算数（它本身就是结论）。
        try:
            r = llm.complete_with_tools(
                "write_plan_tasks", prompts.WRITE_PLAN_TASKS, messages,
                [W.SUBMIT_TASKS], tool_choice="submit_tasks")
        except Exception as e:
            dropped += 1
            print(f"── 第{i+1}次 ──")
            print(f"  ❌ {type(e).__name__} after {(time.time()-t0):.1f}s")
            print(f"     {str(e)[:180]}")
            continue
        blocks = r.get("blocks") or []
        u = r.get("usage") or {}
        calls = [b for b in blocks if b.get("type") == "tool_use"]
        text = "".join(b.get("text") or "" for b in blocks
                       if b.get("type") == "text")

        print(f"── 第{i+1}次 ──")
        print(f"  耗时 {(time.time()-t0):.1f}s / completion {u.get('completion_tokens')}")
        print(f"  tool_calls: {[c.get('name') for c in calls] or '[]（⚠️ 没调工具）'}")
        print(f"  回退文本长度: {len(text)}")

        if not calls:
            print("  ❌ 平台无视了 tool_choice —— 保底路径不成立")
            continue
        if calls[0].get("_args_broken"):
            print("  ❌ arguments 被截断（_args_broken）")
            continue
        ok_forced += 1
        # 生产解析函数必须被真正跑到，不能自己 json.loads 糊弄过去
        data = _parse_json(json.dumps(calls[0].get("input"), ensure_ascii=False))
        tasks = W._normalize_tasks([t for t in (data.get("tasks") or [])
                                   if isinstance(t, dict)],
                                  state["outline"], known)
        segs = sorted({s for t in tasks for s in t["segments"]})
        fake = [s for s in segs if s not in known]
        print(f"  ✅ 强制成功 → 解析出 {len(tasks)} 个任务、{len(segs)} 个段号"
              + (f"，❌ 编造 {len(fake)}: {fake[:5]}" if fake else "，无编造"))
        print(f"  covers: {[t.get('covers') for t in tasks]}")

    print(f"\n强制成功 {ok_forced}/{n}（连接中断 {dropped} 次）")
    llm.close()
    return 0 if ok_forced == n else 1


if __name__ == "__main__":
    raise SystemExit(main())
