"""离线验证 `node_plan_tasks` 的工具循环接线（不需要网络）。

2026-10-15 把工具循环加回来时，xj189 的 443 端口正好不可达，真实验不了。
但「接线对不对」这件事**不需要 API 就能验**：把 `run_tool_loop` 换成桩，
检查 ① 桩的返回有没有被正确解析 ② 工具调用次数有没有记进 write_trace
③ 工具循环抛异常时有没有正确退回单次调用。

⚠️ 验的是**接线**，不是「加了工具之后空返回率会不会降」——后者必须打真平台。

用法：
    python tests/probe_plan_tasks_toolloop.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research_agent.graph.subgraphs import write as W  # noqa: E402

OK_TASKS = {
    "tasks": [
        {"id": "t1", "covers": ["sec1"], "mandate": "比较四条路线",
         "segments": ["S1·1", "S2·3"], "must_cover": ["电导率"], "not_yours": []},
        {"id": "t2", "covers": ["sec2", "sec3"], "mandate": "讲界面",
         "segments": ["S3·1"], "must_cover": ["接触缝隙"], "not_yours": ["路线对比"]},
        # 故意给一条**编造的段号** S9·9 —— 应被 _normalize_tasks 丢掉
        {"id": "t3", "covers": ["sec9"], "mandate": "不存在的小节",
         "segments": ["S9·9"], "must_cover": [], "not_yours": []},
    ]
}


def make_state() -> dict:
    return {
        "session_id": "",                      # 空 → 不进工具循环，走单次路径
        "question": "固态电池的产业化进展",
        "spec": {"thesis": "T", "glossary": [], "cautions": []},
        "outline": [{"id": "sec1"}, {"id": "sec2"}, {"id": "sec3"}],
        "on_topic": [{"ref": "S1·1", "summary": "a"}, {"ref": "S2·3", "summary": "b"},
                     {"ref": "S3·1", "summary": "c"}],
    }


def run(label: str, *, sid: str, stub, expect_ok: bool) -> None:
    st = make_state()
    st["session_id"] = sid
    orig_loop, orig_json = W.run_tool_loop, W.LLM.complete_json
    orig_tools = W.LLM.complete_json
    calls: dict = {"tool": 0, "single": 0}

    def fake_loop(llm, stage, system, messages, provider, max_rounds=3):
        calls["tool"] += 1
        calls["max_rounds"] = max_rounds
        specs = provider.specs()
        calls["tool_names"] = [s["name"] for s in specs]
        return stub(specs)

    def fake_json(stage, prompt, system=None):
        calls["single"] += 1
        return OK_TASKS

    W.run_tool_loop = fake_loop
    W.LLM.complete_json = fake_json
    try:
        out = W.node_plan_tasks(st)
    finally:
        W.run_tool_loop, W.LLM.complete_json = orig_loop, orig_json

    tasks = out.get("tasks") or []
    ev = (out.get("events") or [{}])[0]
    ok = (ev.get("kind") == "write_plan_tasks") == expect_ok
    print(f"{'✅' if ok else '❌'} {label}")
    print(f"    工具循环调用 {calls['tool']} 次 / 单次调用 {calls['single']} 次"
          f"  max_rounds={calls.get('max_rounds')}")
    if calls["tool"]:
        print(f"    传进去的工具：{calls.get('tool_names')}")
    print(f"    事件：{ev.get('kind')}  tasks={len(tasks)} "
          f"raw={ev.get('raw_tasks')} tool_reads={ev.get('tool_reads')}")
    if ev.get("kind") == "write_plan_tasks":
        ids = [t["id"] for t in tasks]
        segs = [s for t in tasks for s in t["segments"]]
        print(f"    保留的任务 {ids}（t3 的 covers=sec9 不存在，应被丢弃）")
        print(f"    保留的段号 {segs}（S9·9 是编造的，应被丢弃）")
    else:
        print(f"    plan_failed={out.get('plan_failed')}")


def main() -> int:
    print("=== ① 工具循环正常返回（桩）===")
    run("走工具循环、解析它的 JSON", sid="s_stub",
        stub=lambda specs: (json.dumps(OK_TASKS, ensure_ascii=False), [], [[{"name": "read_segments"}]]),
        expect_ok=True)

    print("\n=== ② 工具循环抛异常 → 必须退回单次调用，不能整步失败 ===")
    def boom(specs):
        raise RuntimeError("工具循环炸了")
    run("退回单次调用", sid="s_stub2", stub=boom, expect_ok=True)

    print("\n=== ③ 无 session_id → 直接走单次路径 ===")
    run("单次路径", sid="", stub=lambda specs: ("{}", [], []), expect_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
