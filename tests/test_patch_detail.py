"""补丁明细落事件 + /api/sessions/{sid}/review 端点守门测试（离线）。

2026-10-20 加。前端「朱批修订视图」需要逐条补丁明细：
`_apply_patches` 把实际应用的 old/new/before 收进 applied_out，
`node_revise` 的 write_revise 事件带 `patch.applied_items`（截 60），
review 端点从 sse_event/run_trace 聚合读出（去重、跳过损坏行）。

用法：
    .venv/bin/python tests/test_patch_detail.py
"""

from __future__ import annotations

import json
import os
import _isolate  # noqa: F401
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("MINIMAX_API_KEY", "stub-not-used")
os.environ["RA_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "t.db")

from research_agent.db.repo import get_repo                        # noqa: E402
from research_agent.graph.subgraphs import write as W              # noqa: E402

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}"
          + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


# ------------------------------------------------------------- ① _apply_patches

def _state(**kw):
    st = {"session_id": None, "question": "q",
          "outline": [{"id": "sec1", "title": "第一节"}],
          "tasks": [{"id": "t1", "covers": ["sec1"],
                     "segments": ["S3·1"], "must_cover": [],
                     "not_yours": []}],
          "drafts": {}, "violations": {}}
    st.update(kw)
    return st


def _apply(body, items, patches, unresolved=None, **kw):
    st = _state(drafts={"t1": {"sec1": body}}, **kw)
    out: list[dict] = []
    n = W._apply_patches(st, "t1", items,
                         {"patches": patches, "unresolved": unresolved or []},
                         {"t1": {"sec1": body}}, [], [], set(), out)
    return n, out


def t_apply_patches_detail():
    body = "前面四十字左右的上下文内容就在这。乙句要改。丙句不动。"
    items = [{"section": "sec1", "problem": "乙句有问题", "action": "edit",
              "quote": "乙句要改。"}]
    n, out = _apply(body, items,
                    [{"item": 0, "old": "乙句要改。", "new": "乙句改好了。"}])
    check("① edit 成功：返回 1 且明细一条",
          n == 1 and len(out) == 1, f"n={n} out={out}")
    it = out[0]
    check("① 明细字段：old/new 是实际替换值",
          it["old"] == "乙句要改。" and it["new"] == "乙句改好了。", str(it))
    check("① 明细字段：task/section/title/action/problem",
          it["task"] == "t1" and it["section"] == "sec1"
          and it["section_title"] == "第一节" and it["action"] == "edit"
          and it["problem"] == "乙句有问题", str(it))
    check("① before 是替换前 40 字上下文",
          it["before"] == "前面四十字左右的上下文内容就在这。", repr(it["before"]))

    # delete：new="" 合法，action 保留 'delete'
    items2 = [{"section": "sec1", "problem": "整句删除", "action": "delete",
               "quote": "乙句要改。"}]
    n, out = _apply(body, items2,
                    [{"item": 0, "old": "乙句要改。", "new": ""}])
    check("① delete 成功：new=='' 且 action 保留",
          n == 1 and out[0]["action"] == "delete" and out[0]["new"] == "",
          str(out))

    # 被拒（old 未找到）与 unresolved 都不进明细，返回值仍只数成功的
    n, out = _apply(body, items,
                    [{"item": 0, "old": "不存在的句子", "new": "x"}])
    check("① old 未找到：不收明细、返回 0",
          n == 0 and out == [], f"n={n} out={out}")
    n, out = _apply(body, items, [], unresolved=[{"item": 0, "reason": "r"}])
    check("① unresolved：不收明细、返回 0", n == 0 and out == [])

    # 越界引用被拒也不进明细
    n, out = _apply(body, items,
                    [{"item": 0, "old": "乙句要改。", "new": "乙句带 [S9·9]。"}])
    check("① 越界引用被拒：不收明细", n == 0 and out == [])

    # 老调用形态（不传 applied_out）不炸
    st = _state(drafts={"t1": {"sec1": body}})
    n = W._apply_patches(st, "t1", items,
                         {"patches": [{"item": 0, "old": "乙句要改。",
                                       "new": "x。"}],
                          "unresolved": []},
                         {"t1": {"sec1": body}}, [], [], set())
    check("① 不传 applied_out 兼容", n == 1)


# ------------------------------------------------------------- ② node_revise

class PatchLLM:
    """write_patch 脚本桩（同 test_patch_revise 的 PatchLLM）。"""

    def __init__(self, script=None):
        self.script = script or []
        self.calls: list[dict] = []

    def complete_json_forced(self, stage, prompt, name, schema, system=None):
        self.calls.append({"stage": stage, "prompt": prompt, "name": name})
        i = len([c for c in self.calls if c["stage"] == stage]) - 1
        seq = self.script if stage == "write_patch" else []
        r = seq[min(i, len(seq) - 1)] if seq else Exception("unexpected")
        if isinstance(r, Exception):
            raise r
        return r


def _run_revise(state, patch_script, run_script=None):
    llm = PatchLLM(patch_script)
    o_llm, o_run = W.LLM.complete_json_forced, W._run_task
    calls: list = []

    def fake_run(state_, task, revise=None):
        calls.append(task["id"])
        seq = (run_script or {}).get(task["id"], [({}, "x")])
        return seq[min(len(calls) - 1, len(seq) - 1)]

    W.LLM.complete_json_forced = llm.complete_json_forced
    W._run_task = fake_run
    try:
        return llm, calls, W.node_revise(state)
    finally:
        W.LLM.complete_json_forced = o_llm
        W._run_task = o_run


def _revise_state(drafts, targets, **kw):
    st = _state(**kw)
    st.update({"drafts": drafts, "failed": [], "rework_rounds": 0,
               "segments": [], "on_topic": [], "src_lines": [],
               "audit": {"targets": targets}, "spec": {"thesis": "T"}})
    return st


def t_revise_event_items():
    body = "甲句保持不变。乙句要改。丙句也不动。"
    tgt = {"task": "t1", "section": "sec1", "problem": "乙句要重写",
           "fix": "f", "quote": "乙句要改。", "action": "edit"}
    st = _revise_state({"t1": {"sec1": body}}, [tgt])
    llm, calls, out = _run_revise(
        st, [{"patches": [{"item": 0, "old": "乙句要改。",
                           "new": "乙句改好了。"}],
              "unresolved": []}])
    patch = (out["events"][0]).get("patch") or {}
    items = patch.get("applied_items")
    check("② write_revise 事件带 applied_items",
          isinstance(items, list) and len(items) == 1, str(patch)[:160])
    if items:
        check("② 明细字段齐全且 round 在事件外层",
              items[0]["old"] == "乙句要改。"
              and items[0]["new"] == "乙句改好了。"
              and items[0]["section_title"] == "第一节"
              and out["events"][0].get("round") == 1, str(items[0]))

    # 70 条全部应用 → 事件截到 60 且记 truncated=10。
    # 每句要唯一——「第N句」形式天然互不重复。
    body70 = "".join(f"第{i}句是原稿。" for i in range(70))
    targets = [{"task": "t1", "section": "sec1", "problem": "p", "fix": "f",
                "quote": f"第{i}句是原稿。", "action": "edit"}
               for i in range(70)]
    patches = [{"item": i, "old": f"第{i}句是原稿。",
                "new": f"第{i}句改好了。"} for i in range(70)]
    st = _revise_state({"t1": {"sec1": body70}}, targets)
    llm, calls, out = _run_revise(
        st, [{"patches": patches, "unresolved": []}])
    patch = (out["events"][0]).get("patch") or {}
    check("② 70 条应用 → applied_items=60、applied_truncated=10",
          len(patch.get("applied_items") or []) == 60
          and patch.get("applied_truncated") == 10
          and patch.get("applied") == 70,
          f"len={len(patch.get('applied_items') or [])} "
          f"trunc={patch.get('applied_truncated')} applied={patch.get('applied')}")


# ------------------------------------------------------------- ③ review 端点

def _revise_envelope(items, rnd=1):
    return {"kind": "progress", "node": "write",
            "event": {"kind": "write_revise", "round": rnd,
                      "patch": {"applied": len(items),
                                "applied_items": items}}}


def t_review_endpoint():
    repo = get_repo()
    sid = repo.create_session("t-review", "q")
    it = {"task": "t1", "section": "sec1", "section_title": "第一节",
          "action": "edit", "problem": "p", "old": "旧句", "new": "新句",
          "before": "前文"}
    repo.save_sse_event(sid, _revise_envelope([it], rnd=1))
    repo.save_sse_event(sid, _revise_envelope([it], rnd=2))   # 同内容第二轮
    repo.save_sse_event(sid, {"kind": "progress", "node": "write",
                              "event": {"kind": "write_task"}})   # 无关事件
    repo.x("INSERT INTO sse_event(session_id, at, kind, node, event_json) "
           "VALUES (?,?,?,?,?)",
           (sid, "2026-10-20T00:00:00.000+00:00", "progress", "write",
            "{不是合法json"))
    repo.save_run_trace(sid, 0, "run_health",
                        {"kind": "run_health", "by_stage": {},
                         "unsummarized_on_topic_chunks":
                             {"missing": 0, "total": 3, "sources": 0},
                         "gate_degraded_chunks": 1, "unjudged_sources": 2,
                         "failed_tasks": 0})

    sid_empty = repo.create_session("t-empty", "q")

    try:
        from fastapi.testclient import TestClient
        from research_agent.server import app
        c = TestClient(app)

        r = c.get(f"/api/sessions/{sid}/review")
        check("③ 端点 200", r.status_code == 200, str(r.status_code))
        d = r.json()
        check("③ patches 去重后 1 条且带 round",
              len(d["patches"]) == 1 and d["patches"][0]["round"] == 1
              and d["patches"][0]["old"] == "旧句", str(d)[:200])
        check("③ health 非 null 且原样字段在",
              isinstance(d["health"], dict)
              and d["health"].get("by_stage") == {}
              and d["health"].get("unjudged_sources") == 2,
              str(d.get("health"))[:160])

        d2 = c.get(f"/api/sessions/{sid_empty}/review").json()
        check("③ 无事件会话：patches=[]、health=null",
              d2 == {"patches": [], "health": None}, str(d2))

        r404 = c.get("/api/sessions/s_no_such/review")
        check("③ 不存在的 sid → 404", r404.status_code == 404)
    except ImportError:
        # TestClient 依赖缺失时直接调函数（lead 允许，注明即可）
        from research_agent import server as SV
        try:
            d = SV.session_review(sid)
            check("③ 直调 session_review：patches 去重 1 条",
                  len(d["patches"]) == 1 and d["health"] is not None)
        except Exception as e:
            check("③ 直调 session_review", False, repr(e))
        check("③ TestClient 缺失，404/空会话用例未跑", True)


def main() -> int:
    print("=== ① _apply_patches 明细 ===")
    t_apply_patches_detail()
    print("\n=== ② node_revise 事件 ===")
    t_revise_event_items()
    print("\n=== ③ review 端点 ===")
    t_review_endpoint()
    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
