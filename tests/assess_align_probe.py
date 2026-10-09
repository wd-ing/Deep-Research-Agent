"""assess 的框架校准专项测试：查「现场 0/14、隔离 4/4」到底差在哪。

现象（2026-10-08 现场跑）
------------------------
`probe_assess_run` 跑完 3 轮检索，14 节的框架**一节都没被校准**
（`outline_revision_log` 为空、outline 里 0 个「材料支撑」标记）。
但用同一批材料、同一份 brief 在隔离环境重跑 4 次，**4 次都给了
outline_review**（8 收窄 / 11 收窄 …），`_to_revision` 也都解析成功。

排除了：输出截断（2,942 vs 预算 8,192）、JSON 修复丢字段（纯 json.loads）、
brief 超 24,000 截断（5,437 字符）、代码未生效（同进程同代码）。

**剩下的变量只有一个没测过：`检索历史`。**
现场第 2、3 轮都带真实的「上一轮你报过的缺口」，
而隔离重跑我传的是「这是第一轮，没有历史」。

本脚本就是把那个变量单独拎出来测：
  A 组：带真实检索历史（复刻现场第 3 轮）
  B 组：写「没有历史」
  C 组：不带 检索历史 字段
每组跑 N 次，看 outline_review 的给出率与动作分布。

用法：
    .venv/bin/python tests/assess_align_probe.py /tmp/ra_r8_probe.db s_19430fd66d00 --n 3
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

SRC_DB = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ra_r8_probe.db"
SID = sys.argv[2] if len(sys.argv) > 2 else None
N = 3
if "--n" in sys.argv:
    N = int(sys.argv[sys.argv.index("--n") + 1])
OUT_DB = os.environ.get("RA_ASSESS_PROBE") or "/tmp/ra_assess_align_probe.db"

from research_agent.db.repo import get_repo  # noqa: E402
from research_agent.llm.provider import LLMClient  # noqa: E402
from research_agent.graph import prompts  # noqa: E402
from research_agent.graph.nodes import (  # noqa: E402
    _assess_history, _score_sections, _section_terms, _to_revision, _action_hist)


def build(repo, with_history: str, round_no: int) -> str:
    """复刻 node_assess 的 brief 组装。with_history: real / empty / none"""
    outline = json.loads(
        repo.q1("SELECT outline_json FROM session WHERE id=?", (SID,))["outline_json"]
    ).get("sections") or []
    src = {}
    for r in repo.q("SELECT id FROM source WHERE session_id=? ORDER BY rowid", (SID,)):
        src[r["id"]] = len(src) + 1
    segs = []
    for r in repo.q("SELECT id,source_id,summary,token_count,on_topic FROM chunk "
                    "WHERE session_id=? ORDER BY rowid", (SID,)):
        if not src.get(r["source_id"]):
            continue
        segs.append({"ref": f"S{src[r['source_id']]}", "summary": r["summary"],
                     "token_count": r["token_count"], "on_topic": r["on_topic"]})
    use = [s for s in segs if s["on_topic"]] or segs
    views = []
    for s in outline:
        sc = _score_sections(s, use)
        b = sc[0][0] if sc else 0
        views.append({"id": s.get("id"), "title": s.get("title"),
                      "question": (s.get("question") or "")[:120],
                      "题面词": _section_terms(s)[:10],
                      "沾边段数": len(sc), "最高吻合度": b,
                      "材料样例": [x[1][:80] for x in sc[:3]]})
    payload = {"框架状态": "尚未审批", "本轮可用段落数": len(use)}
    if with_history == "real":
        payload["检索历史"] = _assess_history(repo, SID, round_no) or "没有历史"
    elif with_history == "empty":
        payload["检索历史"] = "这是第一轮，没有历史"
    payload["小节"] = views
    return json.dumps(payload, ensure_ascii=False)[:24000]


def run(llm, brief, label):
    d = llm.complete_json("assess", brief, system=prompts.ASSESS)
    given = bool(d.get("outline_review"))
    acts = _action_hist(d.get("outline_review"))
    op = _to_revision(d.get("outline_review"))["op"]
    secs = len(((d.get("outline_review") or {}).get("sections") or []))
    return {"label": label, "keys": sorted(d.keys()), "given": given,
            "sections": secs, "actions": acts, "op": op,
            "narrow": len(_to_revision(d.get("outline_review")).get("narrow") or [])}


def main() -> None:
    global SID, OUT_DB
    if not Path(SRC_DB).exists():
        sys.exit(f"缺库 {SRC_DB}")
    for suf in ("", "-wal", "-shm"):
        p = Path(OUT_DB + suf)
        if p.exists():
            p.unlink()
    shutil.copy(SRC_DB, OUT_DB)
    os.environ["RA_DB_PATH"] = OUT_DB
    repo = get_repo()
    if not SID:
        SID = repo.q1("SELECT id FROM session LIMIT 1")["id"]

    ol = json.loads(repo.q1("SELECT outline_json FROM session WHERE id=?",
                            (SID,))["outline_json"]).get("sections") or []
    n_seg = repo.q1("SELECT COUNT(*) c FROM chunk WHERE session_id=? AND on_topic=1",
                    (SID,))["c"]
    rounds = repo.q1("SELECT COUNT(DISTINCT round) c FROM run_trace "
                     "WHERE session_id=? AND kind='assess'", (SID,))["c"]
    last = max(rounds, 1)

    print("=" * 74)
    print("assess 框架校准专项：现场 0/14，隔离 4/4，差异在哪")
    print("=" * 74)
    print(f"\n库 {SRC_DB}  session {SID}")
    print(f"大纲 {len(ol)} 节，过闸门段落 {n_seg}，已跑 {rounds} 轮 assess")

    real_hist = _assess_history(repo, SID, last - 1) if last > 1 else {}
    print(f"\n现场最后一轮的真实检索历史：{json.dumps(real_hist, ensure_ascii=False)[:200]}")

    briefs = {
        "real": build(repo, "real", last - 1),
        "empty": build(repo, "empty", last - 1),
        "none": build(repo, "none", last - 1),
    }
    for k, v in briefs.items():
        print(f"  {k:<6} brief {len(v):,} 字符")

    llm = LLMClient()
    results = []
    for variant in ("real", "empty", "none"):
        for i in range(N):
            r = run(llm, briefs[variant], f"{variant}#{i+1}")
            results.append(r)
            print(f"\n  ── {r['label']}  {'✓ 给了' if r['given'] else '✗ 没给'}")
            print(f"     keys = {r['keys']}")
            if r["given"]:
                print(f"     {r['sections']} 节  动作 {r['actions']}")
                print(f"     → _to_revision: op={r['op']}  收窄 {r['narrow']}")

    print(f"\n{'=' * 74}\n汇总（每组 {N} 次）\n{'=' * 74}")
    for variant in ("real", "empty", "none"):
        rs = [r for r in results if r["label"].startswith(variant)]
        hit = sum(1 for r in rs if r["given"])
        ops = [r["op"] for r in rs]
        print(f"  {variant:<6} 给出率 {hit}/{len(rs)}   op={ops}")

    print(f"\n判读：")
    print(f"  · real 组给出率若明显低于 empty → **检索历史是抑制因素**")
    print(f"  · 三组都高 → 现场那 3 次是随机波动，不是可复现的缺陷")
    print(f"  · 三组都低 → 14 节是压力，输出装不下这么多内容")


if __name__ == "__main__":
    main()