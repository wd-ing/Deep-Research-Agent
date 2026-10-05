"""write 各环节的单次负载体检。

已知 write_plan 在 effort=high 下必然断连。现在要查的是**还有哪些环节
也有同样的风险**——上一轮 audit 之所以没爆，是因为 plan 失败后喂进去的
是 4,647 字的占位稿，不是 3 万字的真报告。那是假阴性。

本脚本按真实规模逐个测，并且顺带验证一个想法：
**把 plan 拆成「规约」与「任务列表」两次调用后，第二次调用的
稳定前缀可以与子 agent 的前缀完全一致**——于是 plan 的第二次调用
顺手就把子 agent 扇出要的缓存预热了，不需要额外的预热调用，
也不会多给子 agent 任何它不该看的东西。

顺序：
  1. plan 第一次（只出规约）@ high
  2. plan 第二次（只出任务列表，前缀与子 agent 相同）@ high
  3. 一个子 agent 调用（同前缀）——看第二次是否已经把缓存热好
  4. audit（真报告规模）@ high
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from research_agent.graph import prompts  # noqa: E402
from research_agent.llm.provider import _parse_json  # noqa: E402

KEY = os.environ["MINIMAX_API_KEY"]
URL = "https://api.minimaxi.com/anthropic/v1/messages"
M31 = "MiniMax-M3.1-Flash-Preview"
QUESTION = "固态电池的产业化进展，以及各家技术路线的分歧点"


# ---------------------------------------------------------------- 真实数据

def load() -> dict:
    c = sqlite3.connect("/tmp/ra_e2e7.db")
    c.row_factory = sqlite3.Row
    plan = json.loads(c.execute("SELECT outline_json FROM session LIMIT 1")
                      .fetchone()[0] or "{}")
    outline = plan.get("sections") or []
    rows = c.execute(
        "SELECT c.content, s.url, s.title, s.domain, s.source_type, s.source_date, "
        "       s.char_count FROM chunk c LEFT JOIN source s ON s.id=c.source_id "
        "WHERE c.session_id IS NOT NULL LIMIT 400").fetchall()
    sources, seen = [], set()
    for r in rows:
        if r["url"] and r["url"] not in seen:
            seen.add(r["url"])
            sources.append(dict(r))
    src_lines = [f"[S{i+1}] {s['title'] or s['domain']} | {s['domain']} "
                 f"| {s['source_type']} | {s['source_date'] or '日期未知'} "
                 f"| 正文 {s['char_count'] or 0} 字"
                 for i, s in enumerate(sources)]
    url2n = {s["url"]: i + 1 for i, s in enumerate(sources)}
    ev = []
    for r in c.execute(
            "SELECT c.content, s.url FROM chunk c LEFT JOIN source s ON s.id=c.source_id "
            "WHERE c.session_id IS NOT NULL ORDER BY c.ingested_at LIMIT 260"):
        t = (r["content"] or "").strip()
        if len(t) < 40:
            continue
        for s in [x for x in re.split(r"(?<=[。！？])", t) if len(x) > 12][:2]:
            n = url2n.get(r["url"])
            if n:
                ev.append({"i": len(ev), "text": s.strip()[:300], "entities": [],
                           "source": n, "nature": "unknown",
                           "confidence": "medium", "date": None})
    report = sorted(Path("/tmp").glob("e2e_report_s_*.md"),
                    key=lambda p: p.stat().st_mtime)[-1].read_text(encoding="utf-8")
    return {"outline": outline, "src_lines": src_lines, "evidence": ev,
            "report": report}


D = load()
BRIEF = {
    "研究问题": QUESTION,
    "已批准的框架": [{"id": s.get("id"), "title": s.get("title"),
                    "question": s.get("question"), "kind": s.get("kind")}
                   for s in D["outline"]],
    "探针轮廓": {},
    "来源清单": D["src_lines"],
    "断言摘要": [{"i": e["i"], "摘要": e["text"][:70], "来源": e["source"],
                 "性质": e["nature"], "置信": e["confidence"], "日期": e["date"]}
                for e in D["evidence"]],
}
print(f"真实规模：大纲 {len(D['outline'])} 节 | 来源 {len(D['src_lines'])} | "
      f"断言 {len(D['evidence'])} | 旧报告 {len(D['report'])} 字\n")


# ---------------------------------------------------------------- 调用

def call(label: str, system: str, user: str, effort: str,
         mt: int = 16384) -> dict:
    payload = {"model": M31, "max_tokens": mt, "temperature": 0.0,
               "system": system,
               "messages": [{"role": "user", "content":
                             user + "\n\n只输出 JSON，不要任何解释文字或代码围栏。"}]}
    if effort:
        payload["output_config"] = {"effort": effort}
    t0 = time.time()
    try:
        r = httpx.post(URL, headers={"x-api-key": KEY,
                                    "Authorization": f"Bearer {KEY}",
                                    "Content-Type": "application/json",
                                    "anthropic-version": "2023-06-01"},
                       json=payload, timeout=300.0)
        dt = round(time.time() - t0, 1)
    except Exception as e:
        print(f"{label:<34} ✗ {type(e).__name__} {round(time.time()-t0,1)}s")
        return {"ok": False}
    if r.status_code != 200:
        print(f"{label:<34} ✗ HTTP {r.status_code}")
        return {"ok": False}
    blocks = r.json().get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    think = "".join(b.get("thinking", "") for b in blocks if b.get("type") == "thinking")
    u = r.json().get("usage") or {}
    ok = False
    if text.strip():
        try:
            ok = bool(_parse_json(text))
        except Exception:
            ok = False
    print(f"{label:<34} {'✓' if ok else '✗'} {dt:6.1f}s "
          f"入{u.get('input_tokens') or 0:>6} 思考{len(think):>6} "
          f"正文{len(text):>6} 出{u.get('output_tokens') or 0:>6} "
          f"缓存读{u.get('cache_read_input_tokens') or 0:>6}")
    return {"ok": ok, "text": text, "spec": None, "usage": u}


# ---- 1. plan 第一次：只出规约 ----
SPEC_SYS = """你在为一份深度研究报告制定写作规约。

给定：研究问题、已批准的小节框架、全部事实的摘要、来源清单。

产出（严格输出 JSON），只包含以下键：
{
  "thesis": "一句话说清这份报告要论证什么",
  "glossary": [{"canonical": "规范名称", "en": "英文或 null",
                "aliases": ["别名"], "note": "使用注意或禁忌"}],
  "units": ["单位与口径要求"],
  "cautions": ["对特定来源或内容的提醒"],
  "spec_uncertainty": "规约里哪一条你自己没把握、可能影响哪些小节"
}
`spec_uncertainty` 必须填。

**这一轮不要产出任务列表。** 只出规约。"""
r1 = call("1. plan-规约 @high", SPEC_SYS,
          json.dumps(BRIEF, ensure_ascii=False, indent=1)[:40000], "high")
if not r1["ok"]:
    sys.exit("\n规约这步就断了，后面几步无法测。")
spec = _parse_json(r1["text"])
print(f"     → 规约 {len(r1['text'])} 字符，术语 "
      f"{len(spec.get('glossary') or [])} 条")

# ---- 2. plan 第二次：只出任务列表，前缀与子 agent 一致 ----
SHARED = (f"研究问题：{QUESTION}\n\n【写作规约】\n"
          + json.dumps(spec, ensure_ascii=False, indent=1) + "\n\n")
TASK_SYS = prompts.WRITE_PLAN
r2 = call("2. plan-任务列表 @high", TASK_SYS,
          SHARED + json.dumps(BRIEF, ensure_ascii=False, indent=1)[:40000], "high")
n_tasks = 0
if r2["ok"]:
    d = _parse_json(r2["text"])
    n_tasks = len(d.get("tasks") or [])
    print(f"     → 任务 {n_tasks} 个，need_full "
          f"{len(d.get('need_full') or [])} 条")

# ---- 3. 子 agent 调用：同前缀，验证缓存是否已被 plan 第二次热好 ----
OTHERS = [{"id": "t1", "covers": ["sec1"], "职责": "占位职责描述"}] * n_tasks or [{"id": "t1"}]
SEC_PREFIX = (f"研究问题：{QUESTION}\n\n【写作规约】\n"
              + json.dumps(spec, ensure_ascii=False, indent=1)
              + f"\n\n【其他任务的职责（不要替它们写）】\n"
              + json.dumps(OTHERS, ensure_ascii=False, indent=1) + "\n\n")
r3 = call("3. 子 agent @low（同前缀）", prompts.WRITE_RULES,
          SEC_PREFIX + "【你的任务】\nid=t1  对应小节=['sec1']\n"
          "要回答的问题：测试\n\n【你的来源】\n" + "\n".join(D["src_lines"][:5])
          + "\n\n【你的证据】\n" + json.dumps(
              [{"编号": i, "内容": e["text"], "来源": e["source"]}
               for i, e in enumerate(D["evidence"][:15])],
              ensure_ascii=False)[:12000], "low")

# ---- 4. 第二次子 agent，确认缓存确实命中 ----
r4 = call("4. 子 agent @low（应命中缓存）", prompts.WRITE_RULES,
          SEC_PREFIX.replace("id=t1", "id=t2") + "【你的任务】\nid=t2\n"
          "【你的证据】\n" + json.dumps(
              [{"编号": i, "内容": e["text"]}
               for i, e in enumerate(D["evidence"][15:30])],
              ensure_ascii=False)[:12000], "low")

# ---- 5. audit：真报告规模 ----
AUDIT_PAYLOAD = {
    "研究问题": QUESTION,
    "写作规约": spec,
    "草稿": D["report"][:30000],
}
r5 = call("5. audit @high（真报告 3 万字）", prompts.WRITE_AUDIT,
          json.dumps(AUDIT_PAYLOAD, ensure_ascii=False, indent=1)[:120000], "high")

print(f"\n{'='*70}")
print(f"总结：规约 {'通' if r1['ok'] else '断'} / "
      f"任务列表 {'通' if r2['ok'] else '断'} / "
      f"子agent {'通' if r3['ok'] else '断'} / "
      f"audit {'通' if r5['ok'] else '断'}")
if r3["ok"] and r4["ok"]:
    a = r3["usage"].get("cache_read_input_tokens") or 0
    b = r4["usage"].get("cache_read_input_tokens") or 0
    print(f"缓存：第 3 次读 {a}，第 4 次读 {b} —— "
          f"{'第二次明显更高，说明 plan-任务列表那次确实预热了' if b > a else '没有明显预热效果'}")
