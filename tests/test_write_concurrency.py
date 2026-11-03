"""write 全并发（WRITE_WORKERS）的守门测试（离线，桩 _run_task）。

2026-10-25 时间优化：dispatch 从「4 一批 + 每批串行预热」改成
一次全并发（workers=min(len, WRITE_WORKERS), first_serial=False）。
v18 基线：缓存命中 8.3%，预热不回本；dispatch 540s vs 单任务 ~120s。

验：①12 任务同时在飞峰值==12（不再分批）；②结果顺序与 todo 一致；
③失败任务仍走重试批（同一并发度）。

用法：.venv/bin/python tests/test_write_concurrency.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _isolate  # noqa: F401, E402

from research_agent.graph.subgraphs import write as W   # noqa: E402

FAILED: list[str] = []
BODY = "正文 [S1·1]。" + "按材料口径展开。" * 30


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}" + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def _task(tid: str, covers=None) -> dict:
    return {"id": tid, "covers": covers or ["sec1"], "mandate": "m",
            "segments": ["S1·1"], "must_cover": [], "not_yours": []}


def _state(tasks, **kw) -> dict:
    st = {"session_id": None, "question": "q", "spec": {"thesis": "T"},
          "outline": [{"id": f"sec{i}"} for i in range(1, 13)],
          "tasks": tasks, "drafts": {}, "failed": [],
          "segments": [], "on_topic": [], "src_lines": []}
    st.update(kw)
    return st


class PeakMeter:
    """在飞的 _run_task 计数：记录峰值与每个任务被调次数。"""

    def __init__(self, script=None, delay=0.05):
        self.script = script or {}
        self.calls: dict[str, int] = {}
        self.delay = delay
        self._lock = threading.Lock()
        self.inflight = 0
        self.peak = 0
        self.order: list[str] = []

    def run(self, state, task, revise=None):
        tid = task["id"]
        with self._lock:
            self.calls[tid] = self.calls.get(tid, 0) + 1
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            self.order.append(tid)
        try:
            time.sleep(self.delay)
            seq = self.script.get(tid)
            if seq is None:
                return ({"sec1": BODY}, None)
            return seq[min(self.calls[tid] - 1, len(seq) - 1)]
        finally:
            with self._lock:
                self.inflight -= 1


def _with_meter(script, fn, delay=0.05):
    m = PeakMeter(script, delay)
    orig = W._run_task
    W._run_task = m.run
    try:
        out = fn()
    finally:
        W._run_task = orig
    return m, out


def t1_all_parallel_12():
    tasks = [_task(f"t{i}") for i in range(1, 13)]
    m, out = _with_meter(None, lambda: W.node_dispatch(_state(tasks)))
    check("① 峰值并发 == 12（不分批、无串行头）", m.peak == 12,
          f"peak={m.peak}")
    check("① 每任务恰好 1 次", all(v == 1 for v in m.calls.values())
          and len(m.calls) == 12, str(m.calls))
    check("① 全部进 drafts", len(out["drafts"]) == 12)
    ev = (out.get("events") or [{}])[0]
    check("① first_serial_s 字段在、值为 0",
          ev.get("first_serial_s") == 0.0, str(ev))


def t2_order_preserved():
    """快慢不一也要按 todo 顺序合并进 drafts。"""
    # t1 最慢、其它秒回；drafts 键序必须仍是 t1..t8
    class Slow(PeakMeter):
        def run(self, state, task, revise=None):
            with self._lock:
                self.calls[task["id"]] = self.calls.get(task["id"], 0) + 1
                self.inflight += 1
                self.peak = max(self.peak, self.inflight)
            try:
                time.sleep(0.3 if task["id"] == "t1" else self.delay)
                return ({"sec1": BODY}, None)
            finally:
                with self._lock:
                    self.inflight -= 1

    m = Slow(delay=0.01)
    orig = W._run_task
    W._run_task = m.run
    try:
        out = W.node_dispatch(_state([_task(f"t{i}") for i in range(1, 9)]))
    finally:
        W._run_task = orig
    check("② 峰值 == 8", m.peak == 8, f"peak={m.peak}")
    check("② drafts 键序 == todo 顺序",
          list(out["drafts"].keys()) == [f"t{i}" for i in range(1, 9)],
          str(list(out["drafts"].keys())))


def t3_retry_still_works():
    """失败任务仍走重试批，且重试批也全并发。"""
    script = {
        "t1": [({}, "交卷但正文全空"), ({"sec1": BODY}, None)],
        "t2": [({}, "交卷但正文全空"), ({"sec1": BODY}, None)],
    }
    m, out = _with_meter(
        script, lambda: W.node_dispatch(_state([_task(f"t{i}") for i in range(1, 7)])))
    ev = (out.get("events") or [{}])[0]
    check("③ 6 任务首跑全并发", m.peak >= 6, f"peak={m.peak}")
    check("③ 两败俱救：retried=2 recovered=2",
          ev.get("retried") == 2 and ev.get("recovered") == 2, str(ev))
    check("③ t1/t2 各调 2 次", m.calls["t1"] == 2 and m.calls["t2"] == 2)


if __name__ == "__main__":
    t1_all_parallel_12()
    t2_order_preserved()
    t3_retry_still_works()
    print()
    if FAILED:
        print("失败：" + "、".join(FAILED))
        sys.exit(1)
    print("全过 ✅")
