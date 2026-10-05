"""Agnes 3.0 能不能做 write_audit —— 修好三个致命缺陷后的版本。

前一版（`probe_agnes_write_quality.py`）的三个缺陷
------------------------------------------------
1. **喂的稿子是错的那份**。它读 `/tmp/ra_r5_e2e.db` 的报告，
   而 4 条标准答案来自 R6c（`/tmp/ra_r6_real.db`）——**两轮跑的是不同文本**。
2. **只喂了开头 26,000 字**，而 4 条问题分布在 sec2 / sec5 / sec6 / sec8-10，
   3 条根本不在范围内。
3. **从没验证标准答案是否真的存在于喂进去的稿子里**。
   这是最致命的——**拿一个没核对的前提当事实去评分**，
   「没找到」和「压根没这个问题」在输出里长得一模一样。

这一版怎么修
------------
· 读 R6c 那份报告（标准答案的来源，必须是同一份文本）
· 喂**全稿**（67,000 字 ≈ 40k token，Agnes 上下文 512K，装得下；
  这本来就是 write_audit 的真实形态）
· **评分前逐条验证**：该问题的关键词是否真的出现在喂进去的稿子里。
  关键词不在 → 标「题目不成立」并跳过，不计入分母。

这样得到的分数才有意义。

用法：.venv/bin/python tests/probes/probe_agnes_audit_quality.py
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
R6 = Path("/tmp/ra_r6_real.db")          # R6c 那轮的库
GAP = 8.0

# MiniMax 在 R6c 那轮找到的 4 个 high。keywords 用于「验证这道题是否成立」
GROUND_TRUTH = [
    {"name": "离子电导率单位未统一",
     "keywords": ["mS cm", "mS/cm", "10⁻³ S/cm"],
     "min_hits": 1,
     "why": "规约要求统一用 S/cm，正文混用其它单位"},
    {"name": "把日本项目写成国内产能",
     "keywords": ["Idemitsu", "四国", "Li₂S"],
     "min_hits": 2,      # 至少两个词同时出现，这道题才成立
     "why": "Idemitsu 与四国化学是日本项目"},
    {"name": "ARC 的 T3 定义错",
     "keywords": ["ARC", "T3"],
     "min_hits": 2,
     "why": "T3 是最大自升温速率，最高温度是 T4"},
    {"name": "自造引用写法",
     "keywords": ["; S", "实际出自"],
     "min_hits": 1,
     "why": "出现合并引用或越界引用的自创格式"},
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
    if not R6.exists():
        sys.exit(f"缺 {R6}——需要 R6c 那轮的库（标准答案和稿子必须同源）")
    c = sqlite3.connect(R6)
    row = c.execute("SELECT markdown FROM report WHERE session_id=?", (SID,)).fetchone()
    md = row[0] if row else ""
    tr = c.execute("SELECT spec_json, audit_json, audit_history_json FROM write_trace "
                   "WHERE session_id=?", (SID,)).fetchone()
    if not md or not tr:
        sys.exit("R6c 库里没有报告或 write_trace")
    spec = json.loads(tr[0] or "{}")
    audit = json.loads(tr[1] or "{}")
    hist = json.loads(tr[2] or "[]")

    print("=" * 78)
    print("前置：确认稿子与标准答案同源")
    print("=" * 78)
    print(f"  稿子 {len(md)} 字，来自 {R6}")
    print(f"  规约 {len(json.dumps(spec, ensure_ascii=False))} 字")
    print(f"  该库留下的审查轮数：{len(hist)}")
    mm = (audit.get("findings") or [])
    print(f"  最后一轮 findings {len(mm)} 条，"
          f"high {sum(1 for f in mm if f.get('severity') == 'high')} 条")

    print(f"\n{'='*78}\n逐题验证：这些题在这份稿子里真的成立吗")
    print("=" * 78)
    valid = []
    for g in GROUND_TRUTH:
        hits = {k: md.count(k) for k in g["keywords"] if k in md}
        ok = len(hits) >= g["min_hits"]
        print(f"  {'✓成立' if ok else '✗不成立'} {g['name']:<20} "
              f"命中 {len(hits)}/{g['min_hits']}  {hits}")
        if ok:
            valid.append(g)
    print(f"\n  ⇒ {len(valid)} / {len(GROUND_TRUTH)} 道题成立，"
          f"只有成立的题才计入分数")

    if not valid:
        print("\n  没有成立的题，这次测不出东西（换个库或换题目）")
        return

    print(f"\n{'='*78}\n喂全稿给 Agnes 3.0 审")
    print("=" * 78)
    payload = json.dumps({"研究问题": "固态电池的产业化进展，以及各家技术路线的分歧点",
                          "写作规约": spec, "草稿": md}, ensure_ascii=False)
    print(f"  输入 {len(payload)} 字（约 {len(payload)//2} token）")
    r = call(prompts.WRITE_AUDIT, payload, 32768)
    txt = r.get("text", "")

    print(f"\n{'='*78}\n评分")
    print("=" * 78)
    hit = 0
    for g in valid:
        # 判据：Agnes 的输出里出现了该问题的特征描述
        # 用「问题名 + 关键细节」的组合词，降低误判
        detail = {
            "离子电导率单位未统一": ["S/cm", "mS", "单位"],
            "把日本项目写成国内产能": ["Idemitsu", "四国", "日本"],
            "ARC 的 T3 定义错": ["T3", "T4", "自升温", "最高温度"],
            "自造引用写法": ["引用", "自创", "格式"],
        }[g["name"]]
        found = sum(1 for k in detail if k in txt)
        ok = found >= 2
        hit += 1 if ok else 0
        print(f"  {'✓找到' if ok else '✗没找到'} {g['name']:<20} "
              f"输出里命中 {found} 个特征词 {detail[:3]}")

    d = {}
    try:
        from research_agent.llm.provider import _strip_fence, _strip_json_slips
        d = json.loads(_strip_json_slips(_strip_fence(txt)))
    except Exception:
        pass
    if d:
        f = d.get("findings") or []
        print(f"\n  findings {len(f)} 条，high "
              f"{sum(1 for x in f if x.get('severity') == 'high')} 条，"
              f"need_rework={d.get('need_rework')}")
        for x in f[:10]:
            print(f"    [{x.get('severity'):<4}] {x.get('task')}/{x.get('section')}: "
                  f"{str(x.get('problem'))[:88]}")
        print(f"\n  spec_issue: {str(d.get('spec_issue'))[:180]}")
    else:
        print(f"\n  JSON 解析失败，输出开头：{txt[:300]!r}")

    print(f"\n{'='*78}\n结论")
    print("=" * 78)
    print(f"  命中 {hit} / {len(valid)}（分母只算成立的题）")
    print(f"  MiniMax 在同一份稿子上：{len(mm)} findings，"
          f"{sum(1 for f in mm if f.get('severity') == 'high')} high")
    print("  · 命中 ≥ 3/4 且 high 数接近 → 能力够，write_audit 可以接")
    print("  · 命中 1–2          → 边缘，审查留给 MiniMax，"
          "write_plan 也许还能用（只要 JSON 合规）")


if __name__ == "__main__":
    main()
