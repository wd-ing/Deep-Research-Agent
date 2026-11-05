"""B-4 下游检查（真实 API）：F spec vs S spec 各跑 node_dispatch n=2。

用 v22 库证据 + v22 write_trace 的 tasks，spec 分别替换为
/tmp/exp_plan_spec/{F0,S0}.json 里的产物。预加载默认开（生产路径）。
记录 violations/越界/ground_bad/chars/failed（exp_write_preload 同口径）。

用法：set -a && source .env && set +a && .venv/bin/python tests/experiments/exp_slim_dispatch.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

REAL_SID = "s_f29d6edbcbe9"
SRC_DB = ROOT / "data/refs/ra_v22_final.db"
DB = "/tmp/ra_v22_slim_dispatch.db"
OUT = Path("/tmp/exp_slim_dispatch")
OUT.mkdir(parents=True, exist_ok=True)

if not Path(DB).exists():
    shutil.copy(SRC_DB, DB)
os.environ["RA_DB_PATH"] = DB

from tests.experiments.exp_plan_spec import build_state                  # noqa: E402
from tests.experiments.exp_write_preload import ground_count            # noqa: E402
from research_agent.db.repo import get_repo                  # noqa: E402
from research_agent.graph.subgraphs import write as W        # noqa: E402
from research_agent.llm.provider import session_scope        # noqa: E402


def run_one(tag: str, spec: dict, state: dict, repo) -> dict:
    if not repo.q1("SELECT id FROM session WHERE id=?", (tag,)):
        repo.x("INSERT INTO session (id, title, question, status, "
               "created_at, updated_at) VALUES (?,?,?,?,"
               "datetime('now'),datetime('now'))",
               (tag, f"exp {tag}", f"exp {tag}", "done"))
    st = dict(state)
    st["spec"] = spec
    st.update({"drafts": {}, "violations": {}, "failed": [],
               "rework_rounds": 0, "rework_effective": True,
               "revise_unresolved": [], "session_id": REAL_SID})
    row0 = repo.q1("SELECT COALESCE(MAX(rowid),0) m FROM llm_call")["m"]
    t0 = time.time()
    try:
        with session_scope(tag):
            out = W.node_dispatch(st)
        err = ""
    except Exception as e:
        out, err = {}, f"{type(e).__name__}: {e}"
    wall = round(time.time() - t0, 1)
    drafts = out.get("drafts") or {}
    violations = out.get("violations") or {}
    kinds: dict[str, int] = {}
    for bads in violations.values():
        for b in bads or []:
            k = b.get("kind") or "?"
            kinds[k] = kinds.get(k, 0) + 1
    ground = ground_count(drafts, st.get("tasks") or [], st)
    chars = sum(len(str(v)) for secs in drafts.values()
                for v in secs.values())
    res = {"tag": tag, "wall_s": wall, "err": err[:200],
           "failed": len(out.get("failed") or []),
           "violations_total": sum(len(v or []) for v in violations.values()),
           "viol_by_kind": kinds, "ground_bad": ground["bad_dedup"],
           "chars": chars,
           "empty": [t for t, secs in drafts.items()
                     if not any(str(v).strip() for v in secs.values())]}
    (OUT / f"{tag}.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def main() -> None:
    # exp_* 模块的顶置代码会把 RA_DB_PATH 改成它们自己的库——
    # get_repo() 首次调用前必须重设回本脚本的副本。
    os.environ["RA_DB_PATH"] = DB
    repo = get_repo()
    state = build_state(repo)
    tr = repo.write_trace(REAL_SID)
    state["tasks"] = tr.get("tasks") or []
    specs = {m: json.loads((Path("/tmp/exp_plan_spec") / f"{m}0.json")
                           .read_text())["spec"] for m in ("F", "S")}
    results = []
    for mode in ("F", "S"):
        for i in range(2):
            tag = f"{mode}{i}"
            print(f"[{time.strftime('%H:%M:%S')}] {tag} spec={mode}",
                  flush=True)
            r = run_one(tag, specs[mode], state, repo)
            results.append(r)
            print(" ", r, flush=True)
    (OUT / "summary.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
