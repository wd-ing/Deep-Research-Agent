"""真实载荷跑 MiniMax（把 minimax 当默认 provider），走完整生产路径。

2026-10-17 加。起因：DeepSeek 额度接近上限，MiniMax 限时可用，
用户决定**把 minimax 设为默认、后续测试全跑它**。这个脚本回答
「切过去到底有没有问题」。

跑的是**真的 `node_plan_tasks`**：工具循环 → Phase A 证据轮 →
Phase B 强制交卷 → `_normalize_tasks`，一个环节都不省。

⚠️ 探针结论只覆盖**这一个环节**。其余环节（尤其 7 个靠关思考省
token 的）在 MiniMax 上是另一套风险 —— 尤其「关思考 + 工具调用」会让
模型**假装调用并编造结果**，见 `_minimax` 的实测表。

用法：
    .venv/bin/python tests/probes/probe_minimax_plan.py [次数]
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

for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

SRC_DB = Path(os.environ.get("PROBE_SRC_DB", str(ROOT / "data" / "refs" / "ra_internai_v1.db")))
SID = os.environ.get("PROBE_SID", "s_efb71e9cbf66")
WORK_DB = Path("/tmp/ra_minimax_probe.db")


def _load(name: str):
    p = ROOT / "tests" / "probes" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"__{name}", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    if not SRC_DB.exists():
        print(f"❌ 找不到 {SRC_DB}")
        return 1
    shutil.copy(SRC_DB, WORK_DB)
    os.environ["RA_DB_PATH"] = str(WORK_DB)

    from research_agent.graph.subgraphs import write as W
    from research_agent.llm.provider import LLMClient

    # 把默认 provider 换成 minimax —— 走的是和改 `defaults.provider`
    # 完全一样的机制（阶段不写 provider，从 defaults 继承）
    # 默认 provider 现在**就是** minimax（配置已切）；这里再显式钉一次，
    # 让探针在别人把 defaults 改回去时仍然测的是 MiniMax。
    llm = W.LLM
    prof = (llm.registry.raw["providers"])["minimax"]
    llm.registry.raw.setdefault("defaults", {})["provider"] = "minimax"
    llm.registry._cache.clear()

    state = _load("probe_plan_tasks_forced").build_state(str(WORK_DB))
    known = {d["ref"] for d in state["on_topic"]}

    cfg = llm.registry.stage("write_plan_tasks")
    print(f"provider={cfg.provider}  model={cfg.model}  effort={cfg.reasoning_effort}")
    print(f"stream={cfg.stream}（⚠️ `_minimax` 不实现流式，会静默走非流式）")
    print(f"budget={cfg.max_output_tokens}  timeout={cfg.timeout_s:.0f}s")
    print(f"载荷 {len(state['on_topic'])} 段 / {len(state['outline'])} 小节\n")

    ok = 0
    for i in range(n):
        t0 = time.time()
        try:
            out = W.node_plan_tasks(dict(state))
        except Exception as e:
            print(f"── 第{i+1}次 ── ❌ {type(e).__name__}: {str(e)[:200]}")
            continue
        ev = (out.get("events") or [{}])[0]
        tasks = out.get("tasks") or []
        covered = sorted({c for t in tasks for c in t.get("covers") or []})
        all_secs = sorted({s.get("id") for s in state["outline"]})
        missing = [s for s in all_secs if s not in covered]
        segs = sorted({s for t in tasks for s in t.get("segments") or []})
        fake = [s for s in segs if s not in known]
        dt = time.time() - t0
        good = ev.get("kind") == "write_plan_tasks" and not missing and not fake
        ok += 1 if good else 0
        print(f"── 第{i+1}次 ── {'✅' if good else '⚠️ '} {dt:>6.1f}s")
        print(f"   kind={ev.get('kind')}  tasks={len(tasks)}  "
              f"raw={ev.get('raw_tasks')}  tool_reads={ev.get('tool_reads')}")
        print(f"   covers={covered}")
        print(f"   未覆盖小节: {missing or '无 ✅'}")
        print(f"   编造段号: {fake[:5] or '无 ✅'}   总段号 {len(segs)}")
        if out.get("plan_failed"):
            print(f"   plan_failed={out['plan_failed'][:160]}")
        llm.registry._cache.clear()
    print(f"\n达标 {ok}/{n}")
    llm.close()
    return 0 if ok == n else 1


if __name__ == "__main__":
    raise SystemExit(main())
