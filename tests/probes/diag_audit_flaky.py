"""两件事的复测。

A. audit 配极大的 max_output_tokens 能不能吃下真实报告规模
   （之前 16384 在 29,363 字真报告 + effort=high 下断连）

B. 子 agent 的断连到底是服务端抖动还是我上次的构造问题
   —— 这次两个 prompt **只差任务 ID 与证据内容**，
   system、规约、来源块、字段顺序全部逐字一致
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


def load():
    c = sqlite3.connect("/tmp/ra_e2e7.db")
    c.row_factory = sqlite3.Row
    outline = (json.loads(c.execute("SELECT outline_json FROM session LIMIT 1")
                   .fetchone()[0] or "{}")).get("sections") or []
    rows = c.execute(
        "SELECT c.content, s.url, s.title, s.domain, s.source_type, "
        "s.source_date, s.char_count FROM chunk c LEFT JOIN source s "
        "ON s.id=c.source_id WHERE c.session_id IS NOT NULL LIMIT 400").fetchall()
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
    return outline, src_lines, ev, report


OUTLINE, SRC, EV, REPORT = load()
SPEC = {"thesis": "全固态仍在中试，半固态先行，路线分歧集中在电解质体系与界面工程",
        "glossary": [{"canonical": f"术语{i}", "en": None, "aliases": []}
                     for i in range(6)],
        "units": ["能量密度统一 Wh/kg"], "cautions": ["S3 为厂商自述"],
        "spec_uncertainty": "部分术语归属材料内无定论"}


def call(label, system, user, effort, mt):
    payload = {"model": M31, "max_tokens": mt, "temperature": 0.0,
               "system": system, "messages": [{"role": "user", "content": user}]}
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
        print(f"  {label:<40} ✗ {type(e).__name__} {round(time.time()-t0,1)}s")
        return None
    if r.status_code != 200:
        print(f"  {label:<40} ✗ HTTP {r.status_code}")
        return None
    blocks = r.json().get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    think = "".join(b.get("thinking", "") for b in blocks if b.get("type") == "thinking")
    u = r.json().get("usage") or {}
    ok = False
    if text.strip():
        try:
            ok = bool(_parse_json(text))
        except Exception:
            pass
    print(f"  {label:<40} {'✓' if ok else '✗'} {dt:6.1f}s "
          f"思考{len(think):>6} 正文{len(text):>6} 出{u.get('output_tokens') or 0:>6}"
          f"/{mt}")
    return {"ok": ok, "text": text, "usage": u}


# ================= A. audit 极大预算 =================
print("=== A. audit @high + 极大 max_output_tokens ===")
print(f"（真报告 {len(REPORT)} 字）\n")
AUD = json.dumps({"研究问题": QUESTION, "写作规约": SPEC,
                  "草稿": REPORT[:30000]}, ensure_ascii=False, indent=1)
for mt in (16384, 49152, 131072):
    call(f"audit 预算 {mt}", prompts.WRITE_AUDIT, AUD[:120000], "high", mt)


# ================= B. 子 agent 干净对照 =================
print(f"\n=== B. 子 agent 断连复测（两个 prompt 只差任务 ID 与证据）===")

def section_prompt(tid: str, sec: str, evs: list) -> str:
    """与 write.py 的 _run_task 同构。"""
    others = [{"id": f"t{i}", "covers": [f"sec{i}"], "职责": f"第 {i} 块的职责描述"}
              for i in range(1, 9) if f"t{i}" != tid]
    head = (f"研究问题：{QUESTION}\n\n"
            f"【写作规约】\n{json.dumps(SPEC, ensure_ascii=False, indent=1)}\n\n"
            f"【其他任务的职责（不要替它们写）】\n"
            f"{json.dumps(others, ensure_ascii=False, indent=1)}\n\n"
            f"────────  以上为规约，以下为你的任务  ────────\n\n"
            f"【你的任务】\nid={tid}  对应小节=[{sec}]\n"
            f"要回答的问题：{sec} 这一块要回答什么\n"
            f"必须写到：{json.dumps(['要点甲', '要点乙'], ensure_ascii=False)}\n"
            f"不归你管：{json.dumps(['别人的内容'], ensure_ascii=False)}\n\n")
    used = sorted({e["source"] for e in evs})
    legend = [l for l in SRC
              if (m := re.match(r"\[S(\d+)\]", l)) and m.group(1) in {str(u) for u in used}]
    ev_view = [{"编号": i, "内容": e["text"], "实体": e["entities"],
                "来源": e["source"], "来源性质": e["nature"],
                "置信": e["confidence"], "日期": e["date"]}
               for i, e in enumerate(evs)]
    tail = (f"【你的来源（只能引用这些编号）】\n" + ("\n".join(legend) or "（无）")
            + "\n\n【你的证据】\n" + json.dumps(ev_view, ensure_ascii=False, indent=1) + "\n")
    return head + tail


P1 = section_prompt("t1", "sec1", EV[:15])
P2 = section_prompt("t2", "sec2", EV[15:30])
# 除任务 ID/小节/证据外，两份 prompt 的固定部分应完全一致
print(f"  prompt 长度 {len(P1)} / {len(P2)}；"
      f"规约块相同: {P1[P1.index('【写作规约】'):P1.index('【其他任务')] == P2[P2.index('【写作规约】'):P2.index('【其他任务')]}")
print()
ok_n = 0
for i in range(4):
    r = call(f"子 agent 第 {i+1} 次 @low", prompts.WRITE_RULES,
             P1 if i % 2 == 0 else P2, "low", 16384)
    if r and r["ok"]:
        ok_n += 1
print(f"\n  4 次里成功 {ok_n} 次")
