"""框架优化循环的**真实数据回放**：用已有 session 的 140 段材料跑一次 assess。

要回答的问题只有一个：
**当材料真的摆在 assess 面前时，它会不会把 sec6 那个
「逐家核验四家企业完整台账」的要求指出来、并建议收窄？**

为什么必须用真实材料测
--------------------
离线测试只能证明「四种动作都能正确执行」，
证明不了「模型会不会真的去做」。而这正是本次改动要验证的东西。

**核心验证点**（来自审查 A/B 的 `blocked`）：
sec6 要求逐家核验广汽/长安/欣旺达/宁德时代的研发、中试、装车、量产节点，
而分到的 12 段材料全是媒体转述、无企业公告——
审查时模型已经判了 `bad_assignment`。**assess 在同一批材料上
应该得出接近的结论，否则说明它和审查的判断标准不一致。**

⚠️ 前提校验（照搬审查 A/B 的教训，坑 #11）：
1. **同源** —— 材料、大纲、规约必须来自同一 session
2. **问题真在材料里** —— 建议「并入 secX」时，secX 必须真的存在且相关，
   否则「建议并入」和「凭空捏造」在输出里长得一模一样

用法：
    .venv/bin/python tests/align_replay.py /tmp/ra_c1d2.db.write s_afe93128dac0
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

SRC_DB = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ra_c1d2.db.write"
SID = sys.argv[2] if len(sys.argv) > 2 else None
OUT_DB = os.environ.get("RA_ALIGN_OUT") or (str(SRC_DB) + ".align")
os.environ["RA_DB_PATH"] = OUT_DB          # 必须在 get_repo 之前设好

from research_agent.db.repo import get_repo  # noqa: E402
from research_agent.llm.provider import LLMClient  # noqa: E402
from research_agent.graph import prompts  # noqa: E402
from research_agent.graph.nodes import _to_revision, _score_sections  # noqa: E402


def load(session_id: str) -> dict:
    """读出这一轮 assess 会看到的全部输入（三者同源）。"""
    repo = get_repo()
    sess = repo.q1("SELECT question, outline_json, probe_json FROM session WHERE id=?",
                   (session_id,))
    if not sess:
        sys.exit(f"库里没有 session {session_id}")
    outline = json.loads(sess["outline_json"] or "{}").get("sections") or []
    signals = (json.loads(sess["probe_json"] or "{}") or {}).get("signals") or {}

    segs = []
    src_no: dict = {}
    for s in repo.q("SELECT id FROM source WHERE session_id=? ORDER BY rowid",
                    (session_id,)):
        src_no[s["id"]] = len(src_no) + 1
    for r in repo.q("SELECT id, source_id, heading_path, summary, token_count, on_topic "
                    "FROM chunk WHERE session_id=? ORDER BY rowid", (session_id,)):
        if not src_no.get(r["source_id"]):
            continue
        segs.append({**dict(r), "ref": f"S{src_no[r['source_id']]}"})

    return {"question": sess["question"], "outline": outline, "signals": signals,
            "segs": segs, "n_src": len(src_no)}


def build_brief(d: dict) -> str:
    """复刻 node_assess 的输入组装。"""
    segs = d["segs"]
    on_topic = [s for s in segs if s.get("on_topic")]
    use = on_topic or segs
    section_views = []
    for s in d["outline"]:
        scored = _score_sections(s, use)
        best = scored[0][0] if scored else 0
        section_views.append({
            "id": s.get("id"), "title": s.get("title"),
            "question": (s.get("question") or "")[:120],
            "沾边段数": len(scored), "最高吻合度": best,
            "材料样例": [x[1][:80] for x in scored[:3]],
        })
    return json.dumps({"框架状态": "尚未审批",
                       "本轮可用段落数": len(use),
                       "检索历史": "这是第一轮，没有历史",
                       "小节": section_views}, ensure_ascii=False)[:24000]


def show_review(rev: dict, outline: list[dict]) -> None:
    secs = (rev or {}).get("sections") or []
    if not secs:
        print("  ⚠️ 模型没有给 outline_review")
        return
    ids = {s.get("id") for s in outline}
    print(f"  给出 {len(secs)} 个小节的评估：\n")
    for s in secs:
        sid = str(s.get("id") or "")
        title = next((x.get("title") for x in outline if x.get("id") == sid), "?")
        print(f"    {sid}（{str(title)[:18]}）")
        print(f"      材料支撑={s.get('材料支撑')}  可达粒度={s.get('可达粒度')}")
        print(f"      动作={s.get('动作')}"
              + (f" → {s.get('收窄后question', '')[:50]}"
                 if s.get("动作") == "收窄题面" else "")
              + (f" → 并入 {s.get('并入')}" if s.get("动作") == "并入" else ""))
        reason = str(s.get("理由") or "")[:90]
        if reason:
            print(f"      理由={reason}")
        # 前提校验：并入目标必须真实存在
        if s.get("动作") == "并入":
            into = str(s.get("并入") or "")
            ok = into in ids and into != sid
            print(f"      {'✓' if ok else '✗'} 并入目标校验：{into} "
                  f"{'存在' if ok else '不存在 —— 可能是凭空捏造'}")
        print()


def main() -> None:
    global SID
    if not Path(SRC_DB).exists():
        sys.exit(f"缺库 {SRC_DB}")
    if not SID:
        import sqlite3
        c = sqlite3.connect(SRC_DB)
        r = c.execute("SELECT id FROM session "
                      "WHERE id IN (SELECT session_id FROM chunk) "
                      "ORDER BY created_at DESC").fetchall()
        if not r:
            sys.exit("库里没有可用 session")
        SID = r[0][0]

    for suf in ("", "-wal", "-shm"):
        p = Path(OUT_DB + suf)
        if p.exists():
            p.unlink()
    shutil.copy(SRC_DB, OUT_DB)        # LLMClient 记账要 session 外键

    d = load(SID)
    print("=" * 74)
    print(f"框架优化循环 · 真实数据回放")
    print(f"库 {SRC_DB}   session {SID}")
    print("=" * 74)
    print(f"\n问题：{d['question'][:46]}")
    print(f"材料：{d['n_src']} 个来源、{len(d['segs'])} 段")
    print(f"大纲：{len(d['outline'])} 节")

    brief = build_brief(d)
    print(f"\nassess 输入 {len(brief):,} 字符（框架状态=尚未审批）")

    print(f"\n【核心验证点】sec6 要求逐家核验四家企业完整台账，")
    print(f"而材料全是媒体转述——审查阶段已判为 bad_assignment。")
    print(f"assess 在同一批材料上应该得出接近的结论。")

    print(f"\n{'=' * 74}\n运行 assess（1 次调用）\n{'=' * 74}")
    llm = LLMClient()
    data = llm.complete_json("assess", brief, system=prompts.ASSESS)

    print(f"\n  has_enough={data.get('has_enough')}  "
          f"gaps={len(data.get('gaps') or [])}")
    rev = data.get("outline_review")
    show_review(rev, d["outline"])

    print(f"{'=' * 74}\n判读\n{'=' * 74}")
    secs = (rev or {}).get("sections") or []
    acts = {}
    for s in secs:
        a = str(s.get("动作") or "?")
        acts[a] = acts.get(a, 0) + 1
    print(f"  动作分布：{acts}")
    print(f"  材料支撑分布：{_dist(secs, '材料支撑')}")

    # 逐节明细：重点看 sec6
    print(f"\n  逐节结论：")
    for s in secs:
        sid = str(s.get("id") or "")
        print(f"    {sid:<6} {str(s.get('材料支撑')):<6} "
              f"{str(s.get('动作')):<6} {str(s.get('可达粒度'))[:28]}")

    sec6 = next((s for s in secs if str(s.get("id")) == "sec6"), None)
    print(f"\n  === 核心验证：sec6 ===")
    if not sec6:
        print("  ⚠️ 模型没有评估 sec6")
    else:
        thin = str(sec6.get("材料支撑")) in ("偏薄", "几乎无")
        acted = str(sec6.get("动作")) != "保持"
        print(f"    材料支撑 = {sec6.get('材料支撑')}  "
              f"{'✓ 识别为偏薄' if thin else '✗ 没识别出材料不足'}")
        print(f"    动作     = {sec6.get('动作')}  "
              f"{'✓ 给了动作' if acted else '✗ 判为保持（没发现问题）'}")
        if thin and acted:
            print(f"    ⇒ 核心验证通过：assess 在材料面前认出了这个要求撑不住")
        else:
            print(f"    ⇒ 核心验证未通过：assess 的判断与审查阶段不一致")

    print(f"\n  下一步：看这份 outline_review 转成 pending_revision 后的样子")
    pr = _to_revision(rev)
    print(f"    op={pr['op']}  收窄{len(pr.get('narrow') or [])} "
          f"并入{len(pr.get('merge') or [])} 删去{len(pr.get('remove') or [])}")
    print(f"\n  .venv/bin/python tests/cost_report.py {OUT_DB} {SID}")


def _dist(secs: list, key: str) -> dict:
    out: dict = {}
    for s in secs:
        v = str(s.get(key) or "?")
        out[v] = out.get(v, 0) + 1
    return out


if __name__ == "__main__":
    main()