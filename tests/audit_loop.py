"""审查板块的**独立** A/B 测试：只跑审查，不重跑写作。

要回答的问题只有一个：
**把「上一轮 findings 原文」喂回审查、把 `rework_effective` 从
模型自评改成逐条核对之后，模型的行为有没有变化？**

为什么这个测试不能省
--------------------
旧设计里 `rework_effective` 是模型**自己填的布尔值**，而它手上
从来没有上轮的 findings，所以只能笼统自评。实测两个独立 session
都出现了同一个形状：

    session s_afe93128dac0：round1 high 从 3 涨到 7，
        reason 写「首轮返修强化了证据分级和概念边界」，字段却填 false
    session s_a9f5bc761016：round1 high 仍是 4，
        reason 写「已有明显改善」，字段却填 false

代码采信了 false，于是**在 high 一个都没解决的情况下收工**。

这个 harness 用**同一份真实稿子 + 同一批上轮 findings**，
分别喂新旧两版提示词，直接对比模型的判定。

它能验证什么、不能验证什么
--------------------------
- ✅ 能验证：模型是否真的在逐条核对、blocked 是否被正确分类、
  判定是否从「笼统自评」变成「有依据」
- ❌ 不能验证：最终报告质量是否变好——那要完整跑一轮 write，贵得多

前提条件（踩过坑 #11，必须逐条核对）
--------------------------------------
1. 稿子、规约、上轮 findings **三者同源**（同一 session 读出来）
2. 上轮 findings 要**真的在这份稿子里**——所以必须先自己验证，
   否则「模型说还在」和「压根没这个问题」在输出里长得一模一样
3. 旧版提示词用**原样**的字段名，不能改写——否则不是 A/B

用法：
    .venv/bin/python tests/audit_loop.py /tmp/ra_c1d2.db.write s_afe93128dac0
    .venv/bin/python tests/audit_loop.py /tmp/ra_r6_real.db s_a9f5bc761016 --repeat 3
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
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

SRC_DB = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ra_c1d2.db.write"
SID = sys.argv[2] if len(sys.argv) > 2 else None
REPEAT = 3
if "--repeat" in sys.argv:
    REPEAT = int(sys.argv[sys.argv.index("--repeat") + 1])
OUT_DB = os.environ.get("RA_AUDIT_OUT") or (str(SRC_DB) + ".audit")
os.environ["RA_DB_PATH"] = OUT_DB

from research_agent.db.repo import get_repo  # noqa: E402
from research_agent.llm.provider import LLMClient  # noqa: E402
from research_agent.graph import prompts  # noqa: E402

# ---- 旧版提示词：从 git 里取，保证是**原样**而不是我凭记忆重写 ----
# 坑 #11：对比测试的前提是「标准答案」真的来自被测的那一版。
# 凭记忆抄一遍几乎一定会抄错，所以直接取 git 里的原文。
def _old_audit_prompt() -> str:
    import subprocess
    prev = "HEAD"
    try:
        txt = subprocess.run(
            ["git", "show", f"{prev}:research_agent/graph/prompts.py"],
            cwd=ROOT, capture_output=True, text=True, timeout=30).stdout
    except Exception as e:
        print(f"⚠️ 取不到旧版提示词（{e}），无法做 A/B")
        sys.exit(1)
    marker = 'WRITE_AUDIT = """'
    i = txt.find(marker)
    if i < 0:
        sys.exit("旧版 prompts.py 里找不到 WRITE_AUDIT")
    j = txt.find('"""', i + len(marker))
    return txt[i + len(marker):j]


OLD_PROMPT = _old_audit_prompt()
NEW_PROMPT = prompts.WRITE_AUDIT


def D(o) -> str:
    return json.dumps(o, ensure_ascii=False, indent=1)


def _find_draft(session_id: str, repo) -> tuple[str, str]:
    """找到这个 session 的稿子，并**验证同源**。

    坑 #11：稿子、规约、上轮 findings 必须来自同一轮。
    harness 把报告写文件不写 DB，所以 `report` 表从 R5 起就没更新过——
    拿它当稿子会读到一个完全无关的 session。

    同源判据用**规约 thesis 的特征片段**，不是研究问题：
    同一个问题跑过多轮（固态电池那个题目跑了十几轮），
    只比对问题会匹配到一堆不相干的稿子——那正是坑 #11 的第二次形态。
    """
    spec = {}
    try:
        import sqlite3 as _s
        c = _s.connect(str(SRC_DB))
        row = c.execute("SELECT spec_json FROM write_trace WHERE session_id=?",
                        (session_id,)).fetchone()
        if row:
            spec = json.loads(row[0] or "{}")
    except Exception:
        pass
    thumb = str(spec.get("thesis") or "")[20:60]

    try:
        c = sqlite3.connect(str(SRC_DB))
        row = c.execute("SELECT markdown FROM report WHERE session_id=?",
                        (session_id,)).fetchone()
        if row and row[0] and len(row[0]) > 1000:
            return row[0], f"{SRC_DB} 的 report 表"
    except Exception:
        pass
    for f in sorted(Path("/tmp").glob("*report*.md")):
        try:
            t = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if not thumb:
            continue
        if thumb in t:                      # thesis 指纹命中 = 同源
            return t, str(f)
    return "", ""


def load_case(session_id: str) -> dict:
    """从库里把这轮审查的**全部输入**读出来（三者必须同源）。"""
    import sqlite3
    c = sqlite3.connect(str(SRC_DB))
    c.row_factory = sqlite3.Row
    wt = c.execute("SELECT spec_json, tasks_json, audit_json, audit_history_json "
                   "FROM write_trace WHERE session_id=?", (session_id,)).fetchone()
    sess = c.execute("SELECT question FROM session WHERE id=?", (session_id,)).fetchone()
    if not wt:
        sys.exit(f"库里没有 {session_id} 的 write_trace")
    hist = json.loads(wt["audit_history_json"] or "[]")
    if len(hist) < 2:
        sys.exit(f"这轮审查只有 {len(hist)} 轮，没有「上轮 findings」可核对。"
                 "要测 round≥1 的行为，得挑跑过至少一轮返修的 session。")
    spec = json.loads(wt["spec_json"] or "{}")
    tasks = json.loads(wt["tasks_json"] or "[]")
    audit_prev = json.loads(wt["audit_json"] or "{}")

    # ⚠️ 上轮要求返修的那几条（就是模型当时写的 problem/fix 原文）
    prev_targets = []
    for rec in reversed(hist):
        if rec.get("targets"):
            # targets 只有 "t2→sec3" 这种标签；原文要从 audit_json 里捞
            break
    # audit_json 是**最后一轮**的；上轮原文要从历史里找带 problem 的记录
    for rec in reversed(hist):
        if rec.get("target_detail"):
            prev_targets = rec["target_detail"]
            break
    if not prev_targets:
        # 退化：从最后一轮的 findings 里取 high（那就是它上轮要求改的东西）
        prev_targets = [{"task": f.get("task"), "section": f.get("section"),
                         "problem": f.get("problem"), "fix": f.get("fix")}
                        for f in (audit_prev.get("findings") or [])
                        if f.get("severity") == "high"]
    return {
        "question": sess["question"] if sess else "",
        "spec": spec, "tasks": tasks,
        "hist": hist,
        "prev_targets": prev_targets,
        "draft": _find_draft(session_id, get_repo())[0],
        "draft_from": _find_draft(session_id, get_repo())[1],
    }


def build_payload(case: dict, with_prev: bool) -> str:
    """组装审查输入。

    ⚠️ 草稿是**必须**的：没有草稿，模型无法判断任何问题是否「还在」，
    逐条核对就退化成瞎猜——那正是我们要修的东西。
    """
    p = {"研究问题": case["question"], "写作规约": case["spec"]}
    if case["tasks"]:
        p["任务分派（每个任务分到了几段证据）"] = [
            {"task": t.get("id"), "covers": t.get("covers"),
             "证据段数": len(t.get("segments") or []),
             "要求写到": len(t.get("must_cover") or [])}
            for t in case["tasks"]]
    draft = case.get("draft") or ""
    if not draft:
        print("❌ 拿不到草稿，测不了：没有稿子就无法核对任何问题。")
        print("   稿子必须与这个 session 同源（坑 #11）。")
        sys.exit(1)
    p["草稿"] = draft
    if with_prev and case["prev_targets"]:
        p["这是第几轮返修后的草稿"] = 1
        p["上一轮你提出的问题（逐条核对，现在还在吗）"] = case["prev_targets"]
    return D(p)


def run_one(system: str, payload: str, label: str) -> dict:
    llm = LLMClient()
    t0 = time.time()
    try:
        d = llm.complete_json("write_audit", payload[:120000], system=system)
        err = None
    except Exception as e:
        d, err = {}, f"{type(e).__name__}: {str(e)[:160]}"
    dt = round(time.time() - t0, 1)
    findings = [f for f in (d.get("findings") or []) if isinstance(f, dict)]
    high = [f for f in findings if f.get("severity") == "high"]
    out = {
        "label": label, "error": err, "sec": dt,
        "findings": len(findings), "high": len(high),
        "need_rework": d.get("need_rework"),
        "rework_effective": d.get("rework_effective"),
        "reason": str(d.get("reason") or "")[:220],
        "high_items": [{"task": f.get("task"), "section": f.get("section"),
                        "problem": str(f.get("problem") or "")[:110]}
                       for f in high],
    }
    if "blocked" in d:
        bl = d.get("blocked") or []
        out["blocked"] = len(bl)
        out["blocked_items"] = [{"task": b.get("task") if isinstance(b, dict) else None,
                                 "kind": b.get("kind") if isinstance(b, dict) else "?",
                                 "issue": str((b.get("issue") if isinstance(b, dict)
                                               else b) or "")[:110]}
                                for b in bl]
    if "rework_check" in d:
        ck = d.get("rework_check") or []
        out["check"] = [{"task": x.get("task"), "section": x.get("section"),
                         "status": x.get("status")} for x in ck if isinstance(x, dict)]
        out["still"] = sum(1 for x in ck if isinstance(x, dict)
                           and x.get("status") in ("still_there", "changed_form"))
        out["checked"] = len(ck)
    return out


def show(r: dict) -> None:
    print(f"\n  ── {r['label']}  ({r['sec']}s)")
    if r["error"]:
        print(f"     ❌ {r['error']}")
        return
    print(f"     findings={r['findings']}  high={r['high']}  "
          f"need_rework={r['need_rework']}  rework_effective={r['rework_effective']}")
    if "blocked" in r:
        print(f"     blocked={r['blocked']}")
        for b in r.get("blocked_items") or []:
            print(f"        · [{b['kind']}] {b['task']} — {b['issue']}")
    if "check" in r:
        print(f"     逐条核对 {r['checked']} 条，其中仍在/变形 {r['still']} 条")
        for x in r["check"]:
            print(f"        · {x['task']}/{x['section']}: {x['status']}")
    print(f"     reason: {r['reason']}")


def main() -> None:
    global SID
    if not Path(SRC_DB).exists():
        sys.exit(f"缺库 {SRC_DB}")
    if not SID:
        c = sqlite3.connect(SRC_DB)
        rows = c.execute("SELECT session_id FROM write_trace "
                         "WHERE audit_history_json IS NOT NULL "
                         "ORDER BY session_id").fetchall()
        done = [r[0] for r in rows
                if len(json.loads(sqlite3.connect(SRC_DB).execute(
                    "SELECT audit_history_json FROM write_trace WHERE session_id=?",
                    (r[0],)).fetchone()[0] or "[]")) >= 2]
        if not done:
            sys.exit("库里没有跑过 ≥2 轮审查的 session")
        SID = done[-1]

    for suf in ("", "-wal", "-shm"):
        p = Path(OUT_DB + suf)
        if p.exists():
            p.unlink()
    shutil.copy(SRC_DB, OUT_DB)     # LLMClient 记账要 session 外键

    case = load_case(SID)
    print("=" * 74)
    print(f"审查板块 A/B  |  库 {SRC_DB}  session {SID}")
    print("=" * 74)
    print(f"\n问题：{case['question'][:50]}")
    print(f"上轮要求返修 {len(case['prev_targets'])} 条：")
    for t in case["prev_targets"]:
        print(f"  · {t.get('task')}/{t.get('section')}："
              f"{str(t.get('problem') or '')[:80]}")
    print(f"\n【0】先验证前提：稿子与 findings 同源，且问题真的在稿子里")
    draft = case.get("draft") or ""
    if not draft:
        print("  ❌ 找不到同源稿子，测试无法进行")
        print("     （稿子不对源，「还在」和「没这问题」在输出里长得一样——坑 #11）")
        sys.exit(1)
    print(f"  稿子来源：{case.get('draft_from')}")
    print(f"  长度 {len(draft):,} 字符，二级标题 {draft.count(chr(10)+'## ')} 个")
    hit = miss = 0
    titles = {}
    try:
        outline = json.loads(
            (sqlite3.connect(str(SRC_DB)).execute(
                "SELECT outline_json FROM session WHERE session_id=?",
                (SID,)).fetchone() or ["{}"])[0] or "{}").get("sections") or []
        titles = {s.get("id"): (s.get("title") or "") for s in outline}
    except Exception:
        pass
    for t in case["prev_targets"]:
        sec = str(t.get("section") or "")
        # 稿子里的标题是中文小节名，不含 secN —— 用小节标题的前缀做关键词
        kw = (titles.get(sec) or "")[:8]
        present = (kw in draft) if kw else (sec in draft)
        hit += 1 if present else 0
        miss += 0 if present else 1
        mark = "在稿子里" if present else "不在稿子里 ⚠️"
        print(f"    · {t.get('task')}/{sec}"
              f"{'（'+kw+'）' if kw else ''}: {mark}")
    print(f"  → {hit} 条命中、{miss} 条落空。"
          f"{'⚠️ 落空的条目不该拿去核对（那不是模型判断力问题）' if miss else '全部命中，核对有意义。'}")

    old_payload = build_payload(case, with_prev=False)   # 旧版：只说第几轮
    new_payload = build_payload(case, with_prev=True)    # 新版：附上轮 findings

    print(f"\n{'='*74}\n【A】旧版（模型自评 rework_effective，无上轮 findings）\n{'='*74}")
    olds = []
    for i in range(REPEAT):
        r = run_one(OLD_PROMPT, old_payload, f"旧 #{i+1}")
        show(r); olds.append(r)

    print(f"\n{'='*74}\n【B】新版（逐条核对 rework_check + blocked 分类）\n{'='*74}")
    news = []
    for i in range(REPEAT):
        r = run_one(NEW_PROMPT, new_payload, f"新 #{i+1}")
        show(r); news.append(r)

    # ---- 汇总 ----
    def summarize(rs, tag):
        ok = [r for r in rs if not r["error"]]
        if not ok:
            print(f"  {tag}: 全部失败")
            return
        eff = [r["rework_effective"] for r in ok]
        hi = [r["high"] for r in ok]
        print(f"  {tag}: high 均值 {sum(hi)/len(hi):.1f}  "
              f"rework_effective={eff}")
        if "check" in ok[0]:
            ck = [r.get("checked", 0) for r in ok]
            st = [r.get("still", 0) for r in ok]
            print(f"      逐条核对 {ck} 条，仍在 {st} 条")
        if "blocked" in ok[0]:
            bl = [r.get("blocked", 0) for r in ok]
            print(f"      blocked 条数 {bl}")

    print(f"\n{'='*74}\n汇总（各 {REPEAT} 次）\n{'='*74}")
    summarize(olds, "旧版")
    summarize(news, "新版")
    print(f"\n判读要点：")
    print(f"  1. 新版是否真的在逐条核对（checked > 0）——没有就说明提示词没起作用")
    print(f"  2. 旧版是否在 reason 说「有改善」的同时填 false——自相矛盾复现即坐实")
    print(f"  3. blocked 是否被用起来了——为 0 说明模型还在把它们塞进 findings")


if __name__ == "__main__":
    main()