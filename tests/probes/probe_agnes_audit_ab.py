"""Agnes 3.0 的审查能力 vs MiniMax —— 一次干净的 A/B。

这一版之前踩了三个坑，全是"前提没核对"
------------------------------------------------
1. 稿子读的是 `ra_r6_real.db` 的 `report` 表，但 **`tests/write_real.py`
   把报告写到 `/tmp/write_real_report.md`、不写 DB**（`write_real.py:123`）。
   所以那张表从 R5 起就没更新过——**喂进去的是 R5 的稿子**，
   而规约是 R6c 的。不同源。
2. 只喂了开头 26,000 字，4 道题有 3 道在范围外。
3. 「标准答案」其实混了 R5 和 R6c 两轮的 findings。

**最讽刺的是第 1 条造成的结果反而是个好消息**：
喂 R5 稿子时，Agnes 独立发现了 R5 的装配 bug（sec7 空 + sec8 重复），
诊断和 MiniMax 几乎一致，还多找出了标题层级与越界引用。
那说明它的审查能力是够的——只是那一轮的"评分"方式有问题。

这一版怎么保证干净
------------------
· 稿子 = `/tmp/write_real_report.md`（R6c 的真实产出，67,223 字）
· 规约 = `ra_r6_real.db` 的 `write_trace.spec_json`（R6c）
· **两者同源**，且评分前逐题验证关键词真的在这份稿子里
· 评分**双轨**：既看有没有点出指定的 4 个 high，
  也统计它实际找出了多少条 finding / high（不能只看前者）

标准答案：R6c 那轮 MiniMax 报的 4 个 high
  1. t2/sec2 离子电导率单位没按规约统一成 S/cm
  2. t4/sec3 8 家企业只在一句「公开证据不足」里合并列出
  3. t5/sec4 撤回了尺度放大的结构性因果链，改列材料级数据
  4. t10/sec8 对 604 Wh/kg / 450—550 Wh/kg 只重复「未找到独立信源」

用法：.venv/bin/python tests/probes/probe_agnes_audit_ab.py
"""

from __future__ import annotations

import json
import os
import sqlite3
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

import httpx  # noqa: E402

from research_agent.graph import prompts  # noqa: E402

BASE = "https://apihub.agnes-ai.com/v1"
AUTH = {"Authorization": f"Bearer {os.environ['AGNES_API_KEY']}",
        "Content-Type": "application/json"}
MODEL = "agnes-3.0-flash"
SID = "s_a9f5bc761016"
DRAFT = Path("/tmp/write_real_report.md")     # R6c 的产出，harness 写在这里
TRACE_DB = Path("/tmp/ra_r6_real.db")         # R6c 的规约/审查留痕在这里
GAP = 8.0

# R6c 那轮 MiniMax 报的 4 个 high。proof 是「这道题在这份稿子里成立」的关键词。
GT = [
    {"name": "t2/sec2 电导率单位未统一",
     "proof": {"mS cm": 3, "mS/cm": 2},
     "tell": ["S/cm", "mS", "单位", "口径"]},
    {"name": "t4/sec3 逐家核验只给一句合并结论",
     "proof": {"比亚迪": 1, "国轩": 1},
     "tell": ["逐家", "逐一", "比亚迪", "国轩", "登记"]},
    {"name": "t5/sec4 撤回尺度放大因果链",
     "proof": {"堆压": 2},
     "tell": ["尺度", "放大", "因果", "撤回"]},
    {"name": "t10/sec8 604 Wh/kg 只写「未找到独立信源」",
     "proof": {"604": 3},
     "tell": ["604", "独立信源", "回查", "证据审计"]},
]


def call(system: str, user: str, max_tokens: int) -> dict:
    payload = {"model": MODEL, "max_tokens": max_tokens, "temperature": 0.0,
               "chat_template_kwargs": {"enable_thinking": True},
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    t0 = time.time()
    try:
        r = httpx.post(f"{BASE}/chat/completions", json=payload,
                       headers=AUTH, timeout=420)
    except Exception as e:
        print(f"    ❌ {type(e).__name__} {str(e)[:60]}")
        return {}
    dt = round(time.time() - t0, 1)
    time.sleep(GAP)
    if r.status_code != 200:
        print(f"    ❌ HTTP {r.status_code} {dt}s  "
              f"{(r.json().get('error') or {}).get('message','')[:90]}")
        return {}
    d = r.json()
    ch = (d.get("choices") or [{}])[0]
    txt = (ch.get("message") or {}).get("content") or ""
    det = (d.get("usage") or {}).get("completion_tokens_details") or {}
    print(f"    {dt:>6}s  输出 {len(txt):>5} 字  "
          f"reason {det.get('reasoning_tokens','?'):>5}  "
          f"text {det.get('text_tokens','?'):>5}  finish {ch.get('finish_reason')}")
    return {"text": txt, "finish": ch.get("finish_reason")}


def main() -> None:
    for p in (DRAFT, TRACE_DB):
        if not p.exists():
            sys.exit(f"缺 {p}")
    md = DRAFT.read_text(encoding="utf-8")
    c = sqlite3.connect(TRACE_DB)
    tr = c.execute("SELECT spec_json, audit_json, audit_history_json FROM write_trace "
                   "WHERE session_id=?", (SID,)).fetchone()
    if not tr:
        sys.exit("trace 库里没有 write_trace")
    spec = json.loads(tr[0] or "{}")
    mm = (json.loads(tr[1] or "{}").get("findings") or [])
    hist = json.loads(tr[2] or "[]")

    print("=" * 78)
    print("前置：同源核对")
    print("=" * 78)
    print(f"  稿子  {len(md):>6} 字  ← {DRAFT}")
    print(f"  规约  {len(json.dumps(spec, ensure_ascii=False)):>6} 字  ← {TRACE_DB} write_trace")
    print(f"  留痕轮数 {len(hist)}，最后一轮 findings {len(mm)} 条、"
          f"high {sum(1 for f in mm if f.get('severity')=='high')} 条（MiniMax 的成绩）")
    print(f"  规约里的 sections {[s.get('id') for s in (spec.get('sections') or [])][:6]}…")

    print(f"\n{'='*78}\n逐题验证：4 道题在**这份**稿子里成立吗")
    print("=" * 78)
    valid = []
    for g in GT:
        hits = {k: md.count(k) for k, m in g["proof"].items() if md.count(k) >= m}
        ok = len(hits) == len(g["proof"])
        print(f"  {'✓成立' if ok else '✗不成立'} {g['name']:<38} {hits}")
        if ok:
            valid.append(g)
    print(f"\n  ⇒ {len(valid)}/4 成立")

    print(f"\n{'='*78}\n喂全稿给 Agnes 3.0 审（{len(md)} 字）")
    print("=" * 78)
    payload = json.dumps({"研究问题": "固态电池的产业化进展，以及各家技术路线的分歧点",
                          "写作规约": spec, "草稿": md}, ensure_ascii=False)
    r = call(prompts.WRITE_AUDIT, payload, 32768)
    txt = r.get("text", "")

    d = {}
    try:
        from research_agent.llm.provider import _strip_fence, _strip_json_slips
        d = json.loads(_strip_json_slips(_strip_fence(txt)))
    except Exception:
        pass

    print(f"\n{'='*78}\n双轨评分")
    print("=" * 78)
    print("  轨道 A：有没有点出 MiniMax 报的 4 个 high")
    hit = 0
    for g in valid:
        n = sum(1 for k in g["tell"] if k in txt)
        ok = n >= 2
        hit += 1 if ok else 0
        print(f"    {'✓' if ok else '✗'} {g['name']:<38} 特征词 {n} 个")
    print(f"\n  轨道 B：它实际报了多少（不能只看 A，A 会漏掉它找到的真问题）")
    if d:
        f = d.get("findings") or []
        hi = sum(1 for x in f if x.get("severity") == "high")
        print(f"    findings {len(f)} 条、high {hi} 条、need_rework={d.get('need_rework')}")
        for x in f[:12]:
            print(f"      [{x.get('severity'):<4}] {x.get('task')}/{x.get('section')}: "
                  f"{str(x.get('problem'))[:82]}")
        print(f"\n    spec_issue: {str(d.get('spec_issue'))[:200]}")
        for x in (d.get("remaining_issues") or [])[:4]:
            print(f"      改不动: {str(x)[:110]}")
    else:
        print(f"    JSON 解析失败，开头：{txt[:200]!r}")

    print(f"\n{'='*78}\n对照")
    print("=" * 78)
    print(f"  MiniMax：{len(mm)} findings、"
          f"{sum(1 for f in mm if f.get('severity')=='high')} high")
    if d:
        print(f"  Agnes  ：{len(d.get('findings') or [])} findings、"
              f"{sum(1 for x in (d.get('findings') or []) if x.get('severity')=='high')} high"
              f"，轨道 A 命中 {hit}/{len(valid)}")
    print("\n  判读：")
    print("   · 轨道 A 命中 ≥2 且轨道 B 的量级接近 → 能力相当，write_audit 可接")
    print("   · 轨道 A 命中 0–1 但轨道 B 的量级接近 → 漏检具体问题但有洞察，")
    print("     可作**第二意见**而不是主审")
    print("   · 两轨都低 → 能力不够，审查留给 MiniMax")


if __name__ == "__main__":
    main()
