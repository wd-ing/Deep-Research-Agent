"""v22 write_section EmptyOutput 复现实验（真实 API，B 组 preload n=2）。

复用 tests/experiments/exp_write_preload.py 的重建逻辑（v20 证据、13 任务）；
只跑预加载组，看能否复现 v22 的 ~48s/无 stop_reason 三连失败。
每次跑完把该会话的 llm_call error 行（含新插桩诊断）原样打出。

用法：
    set -a && source .env && set +a
    .venv/bin/python tests/experiments/exp_preload_repro.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.experiments.exp_write_preload import (build_state, run_one, line, OUT)  # noqa: E402
from research_agent.db.repo import get_repo                            # noqa: E402
from research_agent.llm.provider import STREAM_TIMELINES               # noqa: E402

N = 3

# 成功调用的流时间线（内存收集）：first_delta_s / max_gap_s 分布。
STREAM_OK: list[dict] = []


def main() -> None:
    repo = get_repo()
    state = build_state(repo)
    print(f"session={state['session_id']} tasks={len(state['tasks'])} "
          f"on_topic={len(state['on_topic'])}", flush=True)
    for i in range(N):
        tag = f"repro_B{i}"
        print(f"[{time.strftime('%H:%M:%S')}] {tag} 起跑", flush=True)
        t_start = time.time()
        try:
            r = run_one(tag, True, build_state(repo), repo)
        except Exception as e:
            print(f"⚠️  {tag} 崩了：{type(e).__name__}: {e}", flush=True)
            (OUT / f"{tag}.json").write_text(
                json.dumps({"tag": tag, "error": repr(e)}), encoding="utf-8")
            continue
        print(line(r), flush=True)
        # 本次窗口内的流时间线（成功 write_section 也收）
        for s in list(STREAM_TIMELINES):
            if s.get("at", 0) >= t_start and s.get("stage") == "write_section":
                STREAM_OK.append(s)
        errs = repo.q(
            "SELECT stage, status, substr(error,1,1600) e, latency_ms "
            "FROM llm_call WHERE session_id=? AND status!='ok' "
            "ORDER BY rowid", (tag,))
        if not errs:
            print(f"  {tag}: 0 条非 ok 调用", flush=True)
        for row in errs:
            print(f"  ERR {row['stage']} {row['latency_ms']}ms "
                  f"status={row['status']}", flush=True)
            print(f"    {row['e']}", flush=True)
        (OUT / f"{tag}_errors.json").write_text(
            json.dumps([dict(e) for e in errs], ensure_ascii=False,
                       indent=1), encoding="utf-8")
    # ---- 成功调用的间隔分布 ----
    ok = [s for s in STREAM_OK if not s.get("broken")]
    fd = sorted(s["first_delta_s"] for s in ok
                if s.get("first_delta_s") is not None)
    mg = sorted(s["max_gap_s"] for s in ok if s.get("max_gap_s") is not None)

    def dist(xs):
        if not xs:
            return {}
        import statistics as st
        p95 = xs[min(len(xs) - 1, int(len(xs) * 0.95))]
        return {"n": len(xs), "min": xs[0], "med": st.median(xs),
                "p95": p95, "max": xs[-1]}
    dist_out = {"ok_calls": len(ok), "broken_calls":
                len(STREAM_OK) - len(ok),
                "first_delta_s": dist(fd), "max_gap_s": dist(mg),
                "timelines": STREAM_OK}
    (OUT / "stream_stats.json").write_text(
        json.dumps(dist_out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("== stream stats ==", json.dumps(
        {k: v for k, v in dist_out.items() if k != "timelines"},
        ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
