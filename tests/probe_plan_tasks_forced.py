"""在线验收：分派环节改走「强制工具交卷」之后到底发生了什么。

2026-10-17 加。**只跑生产代码，不打桩**：`node_plan_tasks` → `EvidenceTools`
→ `run_tool_loop` → `LLMClient` → 信通院 discovery-api。载荷是真那一轮的
99 段 / 3 小节（从 internai 全量复跑的库回放）。

为什么必须打真平台（这已经是第四次同一条教训）：
离线守门测试只证明「该强制的时候强制了」，证明不了**平台认不认这个
`tool_choice`、认了以后 arguments 是不是真的合法、思考会不会被
工具约束压掉**。预检 9/9 全过而生产照样挂过一次。

要看的五件事：
  1. 提交轮是否真的返回了 `tool_use`（平台有没有无视 `tool_choice`）
  2. `arguments` 解析出来的任务数、以及段号是不是编的
  3. `finish_reason`：有没有又撞 `length`（撞了就是 65,536 仍不够）
  4. **多花了多少** —— 改造前一轮调用，现在典型两轮，token 与耗时对比
  5. 证据工具还读不读（`plan_tool_reads`）—— 参考轮实测是 0

用法：
    .venv/bin/python tests/probe_plan_tasks_forced.py [每种路径的次数]
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        k, v = _line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

SRC_DB = Path(os.environ.get("PROBE_SRC_DB", "/tmp/ra_internai_v1.db"))
SID = os.environ.get("PROBE_SID", "s_efb71e9cbf66")
# 拷一份再连：探针会写 llm_call / write_trace，不许污染那轮参考数据。
WORK_DB = Path("/tmp/ra_forced_probe.db")

# ④ 之前的提示词片段（`git show HEAD:research_agent/graph/prompts.py`）。
# ⚠️ **对照组必须连提示词一起还原**，只摘 `final_tool` 是不够的。
# 第一版探针漏了这一步：模型看到提示词写着「调用 submit_tasks 交卷」，
# 手上却没有这个工具，于是**凭空编了一个同名 tool_call**（A 组实测
# `tool_calls=['submit_tasks']`）。那不是对照组，是混合体。
OLD_PROMPT_BLOCK = '''**严格输出 JSON**：

{
  "tasks": [{
     "id": "t1",
     "covers": ["sec1"],
     "mandate": "这一块要回答什么问题",
     "segments": ["S3·5", "S3·6", "S7·1"],
     "must_cover": ["必须写到的点"],
     "not_yours": ["不要碰的东西"]
  }]
}'''


def old_prompt(new: str) -> str:
    """把提示词里「调用 submit_tasks 交卷」那段换回「严格输出 JSON」。"""
    i = new.index("**判断够了就调用 `submit_tasks` 交卷**")
    j = new.index("不必为了凑数去读原文。")
    j = new.index("\n", j) + 1
    return new[:i] + OLD_PROMPT_BLOCK + "\n" + new[j:]


def build_state(db: str) -> dict:
    """照 `nodes.py:1227-1238` 的口径重建 `node_plan_tasks` 需要的 state。

    段号必须跟 `EvidenceTools` 一致（来源按 rowid 编号 S1、S2…，
    段按来源内顺序编号），否则分派出来的段号在后续工具读取时对不上——
    这正是 `probe_plan_tasks_empty.build_prompt` 记过的坑，这里重算一遍
    而不是复用它，因为它只返回拼好的字符串、不返回 state。
    """
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    question = c.execute("select question from session where id=?", (SID,)).fetchone()[0]
    outline = json.loads(c.execute(
        "select outline_json from session where id=?", (SID,)).fetchone()[0])
    secs = outline.get("sections") if isinstance(outline, dict) else outline
    secs = secs if isinstance(secs, list) else list((secs or {}).values())
    row = c.execute("select spec_json from write_trace where session_id=?",
                    (SID,)).fetchone()
    spec = json.loads(row[0]) if row and row[0] else {}

    src_no = {sid: i for i, (sid,) in enumerate(
        c.execute("select id from source where session_id=? order by rowid",
                  (SID,)).fetchall(), 1)}
    src_lines = [{"id": i, "title": t or "", "url": u}
                 for i, (t, u) in enumerate(c.execute(
                     "select title, url from source where session_id=? order by rowid",
                     (SID,)).fetchall(), 1)]
    segs = []
    seg_no: dict[str, int] = {}
    for r in c.execute(
            "select id, source_id, heading_path, summary, token_count, on_topic "
            "from chunk where session_id=? order by rowid", (SID,)).fetchall():
        d = dict(r)
        # ⚠️ **必须先编号再过滤**（`nodes.py:1231-1238` 就是这个顺序）。
        # 先过滤会让编号从 1 重排，被闸门刷掉的段在生产里占着号，
        # 于是探针里的 S3·5 指向另一段——分派出来的段号在后续工具读取时对不上。
        n = seg_no.get(d["source_id"], 0) + 1
        seg_no[d["source_id"]] = n
        d["ref"] = f"S{src_no.get(d['source_id'], 0)}·{n}"
        if not d.get("on_topic") or not (d.get("summary") or "").strip():
            continue
        segs.append(d)
    return {"session_id": SID, "question": question, "spec": spec,
            "outline": secs, "on_topic": segs, "src_lines": src_lines,
            "signals": {}}


_ORIG = None


def install_probe() -> list[dict]:
    """包一层 `complete_with_tools`，记录每次调用的形状与用量。

    ⚠️ 只能包一次。重复包装会让第 2 次之后把调用记进**两个** log，
    于是「这一轮调用了几次」凭空翻倍——那是个只会在结论里出现的假象。
    """
    global _ORIG
    from research_agent.llm.provider import LLMClient
    log: list[dict] = []
    if _ORIG is None:
        _ORIG = LLMClient.complete_with_tools

    def wrapped(self, stage, system, messages, tools, tool_choice=None):
        t0 = time.time()
        r = _ORIG(self, stage, system, messages, tools, tool_choice=tool_choice)
        blocks = r.get("blocks") or []
        u = r.get("usage") or {}
        log.append({
            "tool_choice": tool_choice,
            "n_tools": len(tools or []),
            "tool_calls": [b.get("name") for b in blocks
                           if b.get("type") == "tool_use"],
            "text_len": sum(len(b.get("text") or "") for b in blocks
                            if b.get("type") == "text"),
            "broken": any(b.get("_args_broken") for b in blocks),
            "completion": u.get("completion_tokens"),
            "latency": int((time.time() - t0) * 1000),
        })
        return r

    LLMClient.complete_with_tools = wrapped
    return log


def run_once(label: str, state: dict, forced: bool) -> dict:
    from research_agent.graph import prompts
    from research_agent.graph.subgraphs import write as W
    log = install_probe()
    orig = W.run_tool_loop
    orig_prompt = prompts.WRITE_PLAN_TASKS

    def plain(llm, stage, system, messages, provider, max_rounds=3,
              final_tool=None):
        # 对照组：把 final_tool 摘掉，就是改造前的行为（形参仍要留着，
        # 否则调用点 TypeError → 退回单次调用，A 组会测到退路而不是基线）。
        return orig(llm, stage, system, messages, provider,
                    max_rounds=max_rounds)

    W.run_tool_loop = orig if forced else plain
    # 对照组还要把提示词换回去（见 `old_prompt` 的说明）
    prompts.WRITE_PLAN_TASKS = (orig_prompt if forced
                                else old_prompt(orig_prompt))
    W.prompts.WRITE_PLAN_TASKS = prompts.WRITE_PLAN_TASKS
    t0 = time.time()
    try:
        out = W.node_plan_tasks(dict(state))
    finally:
        W.run_tool_loop = orig
        prompts.WRITE_PLAN_TASKS = orig_prompt
    ev = (out.get("events") or [{}])[0]
    tasks = out.get("tasks") or []
    total_ms = int((time.time() - t0) * 1000)
    tok = sum((c["completion"] or 0) for c in log)

    print(f"\n── {label} ──")
    print(f"  结果：kind={ev.get('kind')}  kept={len(tasks)}  "
          f"raw={ev.get('raw_tasks')}  tool_reads={ev.get('tool_reads')}")
    if out.get("plan_failed"):
        print(f"  ❌ plan_failed={out['plan_failed'][:160]}")
    for i, c in enumerate(log):
        print(f"  轮{i+1}: tool_choice={str(c['tool_choice']):<16} "
              f"tools={c['n_tools']}  tool_calls={c['tool_calls'] or '[]'} "
              f"text={c['text_len']}字 "
              f"completion={c['completion']} {c['latency']/1000:.1f}s"
              + ("  ⚠️破参数" if c["broken"] else ""))
    print(f"  合计：{len(log)} 次调用 / completion {tok} / {total_ms/1000:.1f}s")
    segs = sorted({s for t in tasks for s in t.get("segments") or []})
    known = {d["ref"] for d in state["on_topic"]}
    fake = [s for s in segs if s not in known]
    print(f"  段号 {len(segs)} 个"
          + (f"，❌ 编造 {len(fake)} 个: {fake[:5]}" if fake else "，无编造 ✅"))
    print(f"  covers：{[t.get('covers') for t in tasks]}")
    return {"kind": ev.get("kind"), "kept": len(tasks), "calls": len(log),
            "tok": tok, "ms": total_ms, "fake": len(fake),
            "forced_used": any(c["tool_choice"] for c in log),
            "tool_reads": ev.get("tool_reads")}


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    if not SRC_DB.exists():
        print(f"❌ 找不到 {SRC_DB}")
        return 1
    shutil.copy(SRC_DB, WORK_DB)
    os.environ["RA_DB_PATH"] = str(WORK_DB)

    state = build_state(str(WORK_DB))
    print(f"载荷：{len(state['on_topic'])} 段 / {len(state['outline'])} 小节 / "
          f"规约 {len(state['spec'])} 字段")
    print(f"平台：{os.environ.get('INTERN_AI_BASE_URL', '(走 models.yaml)')}")

    from research_agent.llm.provider import LLMClient
    cfg = LLMClient().registry.stage("write_plan_tasks")
    print(f"环节：{cfg.provider} / {cfg.model} / effort={cfg.reasoning_effort} "
          f"/ max_tokens={cfg.max_output_tokens} / timeout={cfg.timeout_s} "
          f"/ stream={cfg.stream}\n")

    print("=" * 64)
    print("对照组 A：不挂 final_tool（= 改造前的手写 JSON 路径）")
    print("=" * 64)
    a = [run_once(f"A 第{i+1}次", state, forced=False) for i in range(n)]

    print("\n" + "=" * 64)
    print("实验组 B：挂 submit_tasks 强制交卷（= 改造后）")
    print("=" * 64)
    b = [run_once(f"B 第{i+1}次", state, forced=True) for i in range(n)]

    def med(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2]

    print("\n" + "=" * 64)
    print(f"{'指标':<14}{'A 手写JSON':>14}{'B 强制交卷':>14}")
    print("-" * 64)
    for key, label in (("kept", "存活任务"), ("calls", "调用次数"),
                       ("tok", "completion"), ("ms", "耗时ms")):
        print(f"{label:<14}{med([x[key] for x in a]):>14}{med([x[key] for x in b]):>14}")
    print(f"{'失败':<14}"
          f"{sum(1 for x in a if x['kind'] != 'write_plan_tasks'):>14}"
          f"{sum(1 for x in b if x['kind'] != 'write_plan_tasks'):>14}")
    print(f"{'编造段号':<14}{sum(x['fake'] for x in a):>14}{sum(x['fake'] for x in b):>14}")
    print(f"\n强制路径命中（B 组用到 tool_choice 的次数）："
          f"{sum(1 for x in b if x['forced_used'])}/{len(b)}")
    print(f"证据读取 A={sum(x['tool_reads'] or 0 for x in a)} / "
          f"B={sum(x['tool_reads'] or 0 for x in b)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
