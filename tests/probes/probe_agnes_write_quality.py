"""Agnes 3.0 能不能做 write_plan / write_audit——质量对比，不是配额问题。

为什么是这个
------------
配额已经算清了：write 阶段 43 次调用，Agnes 按 8 次/分钟只要 5.4 分钟，
比 MiniMax 的 32.1 分钟快 6 倍。**这是最后一道关**：
能力不够的话，配额再宽也没用。

怎么测才有说服力
----------------
不评「写得像不像」——那是主观的。本探针用**有标准答案的对比**：

R6c 那轮 MiniMax 的审查在真实稿子上找出了 4 个具体问题（见 `GROUND_TRUTH`），
都是可核验的事实错误，不是风格问题：
  1. 离子电导率单位没按规约统一成 S/cm（t2、t3 两个任务都犯）
  2. 把日本项目 Idemitsu / 四国化学写成了「国内 Li₂S 产能」
  3. ARC 的 T3 定义错了（应是最大自升温速率，不是最高温度）
  4. 正文自造引用写法（程序也抓到了 4 处）

**同一份稿子、同一份规约**，看 Agnes 能不能找到这些。找到几条就是几分。
这比让人打分硬得多。

另外单独测 `write_plan` 形态：给它真实的轮廓与段落概括，看能否产出
结构合规的 spec JSON（thesis / glossary / cautions / sections 都在不在）。

用法：.venv/bin/python tests/probes/probe_agnes_write_quality.py
"""

from __future__ import annotations

import json
import os
import re
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

sys.path.insert(0, str(ROOT))
from research_agent.graph import prompts  # noqa: E402

BASE = "https://apihub.agnes-ai.com/v1"
AUTH = {"Authorization": f"Bearer {os.environ['AGNES_API_KEY']}",
        "Content-Type": "application/json"}
MODEL = "agnes-3.0-flash"
GAP = 8.0

# MiniMax 在 R6c 那轮真实稿子上找到的问题（有标准答案）
GROUND_TRUTH = [
    ("单位未统一 S/cm",
     ["S/cm", "mS cm", "mS/cm", "10⁻³ S/cm"],
     "规约要求离子电导率统一用 S/cm，正文混用 mS cm⁻¹、mS/cm"),
    ("把日本项目写成国内产能",
     ["Idemitsu", "四国", "国内", "Li₂S"],
     "Idemitsu 和四国化学是日本项目，正文却把其产能归为「国内」"),
    ("ARC 的 T3 定义错",
     ["ARC", "T3", "T4", "最高温度", "自升温"],
     "ARC 里 T3 是最大自升温速率，最高温度是 T4"),
    ("自造引用写法",
     ["[S3·1; S19·1]", "S19·4 的引言", "; "],
     "正文出现合并引用与越界引用的自创写法"),
]


def call(system: str, user: str, max_tokens: int, thinking: bool = True) -> dict:
    payload = {"model": MODEL, "max_tokens": max_tokens, "temperature": 0.0,
               "chat_template_kwargs": {"enable_thinking": thinking},
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    t0 = time.time()
    try:
        r = httpx.post(f"{BASE}/chat/completions", json=payload,
                       headers=AUTH, timeout=300)
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
    u = d.get("usage") or {}
    det = u.get("completion_tokens_details") or {}
    print(f"    {dt:>6}s  输出 {len(txt):>5} 字  "
          f"reason {det.get('reasoning_tokens','?'):>5}  "
          f"text {det.get('text_tokens','?'):>5}  finish {ch.get('finish_reason')}")
    return {"text": txt, "finish": ch.get("finish_reason"),
            "out": det.get("text_tokens", 0), "reason": det.get("reasoning_tokens", 0)}


def try_json(txt: str) -> dict:
    """用项目自己的修复逻辑解析。"""
    try:
        from research_agent.llm.provider import _strip_fence, _strip_json_slips
        import json as _j
        t = _strip_json_slips(_strip_fence(txt))
        return _j.loads(t)
    except Exception:
        return {}


def main() -> None:
    db = Path("/tmp/ra_r5_e2e.db")
    if not db.exists():
        sys.exit(f"缺 {db}")
    import sqlite3
    c = sqlite3.connect(db)
    md = (c.execute("SELECT markdown FROM report WHERE session_id='s_a9f5bc761016'")
          .fetchone() or [""])[0]
    spec_raw = c.execute(
        "SELECT spec_json FROM write_trace WHERE session_id='s_a9f5bc761016'").fetchone()
    outline_raw = c.execute(
        "SELECT outline_json FROM session WHERE id='s_a9f5bc761016'").fetchone()
    # R6c 那轮的库在 /tmp/ra_r6_real.db，优先用它（有最新的 spec 与 findings）
    r6 = Path("/tmp/ra_r6_real.db")
    if r6.exists():
        c6 = sqlite3.connect(r6)
        row = c6.execute("SELECT spec_json, tasks_json, audit_history_json "
                         "FROM write_trace WHERE session_id='s_a9f5bc761016'").fetchone()
        if row and row[0]:
            spec_raw = (row[0],)
    spec = json.loads((spec_raw[0] if spec_raw else "") or "{}")
    outline = json.loads((outline_raw[0] if outline_raw else "{}") or "{}").get("sections") or []

    # 只喂前 4 节，控制输入量（R6c 的 review 全稿 286k token 太大）
    head = md[:26000]

    print("=" * 76)
    print("1. write_audit 形态：拿真实稿子审，看能不能找到 MiniMax 找到的那 4 个问题")
    print("=" * 76)
    print(f"  输入：规约 {len(json.dumps(spec, ensure_ascii=False))} 字 + 稿子开头 {len(head)} 字")
    payload = json.dumps({"研究问题": "固态电池的产业化进展，以及各家技术路线的分歧点",
                          "写作规约": spec, "草稿": head}, ensure_ascii=False)
    r = call(prompts.WRITE_AUDIT, payload, 16384, thinking=True)
    findings_txt = r.get("text", "")
    d = try_json(findings_txt)
    if not d:
        # JSON 拉不出来也照样能测：直接对原文做关键词命中
        print("    （JSON 解析失败，改用原文关键词命中评估）")
    ftext = findings_txt
    hit = 0
    for name, kws, why in GROUND_TRUTH:
        # 判据：发现描述里出现了这个问题的特征词
        found = any(k in ftext for k in kws[:2])
        hit += 1 if found else 0
        print(f"    {'✓' if found else '✗'} {name:<22} {'找到' if found else '没找到'}")
        if not found:
            print(f"        （{why[:70]}）")
    print(f"\n  ⇒ 命中 {hit} / 4")
    if d:
        f = d.get("findings") or []
        print(f"  findings {len(f)} 条，high {sum(1 for x in f if x.get('severity')=='high')} 条")
        for x in f[:6]:
            print(f"    [{x.get('severity')}] {x.get('task')}/{x.get('section')}: "
                  f"{str(x.get('problem'))[:90]}")
        print(f"  need_rework={d.get('need_rework')}  spec_issue="
              f"{str(d.get('spec_issue'))[:100]}")

    print(f"\n{'='*76}\n2. write_plan 形态：真实轮廓 → 合规的 spec JSON")
    print("=" * 76)
    import sqlite3 as _s
    c2 = _s.connect(r6 if r6.exists() else db)
    segs = [r[0] for r in c2.execute(
        "SELECT summary FROM chunk WHERE session_id='s_a9f5bc761016' "
        "AND summary IS NOT NULL AND summary!='' ORDER BY rowid LIMIT 80")]
    srcs = [r[0] for r in c2.execute(
        "SELECT title FROM source WHERE session_id='s_a9f5bc761016' LIMIT 32")]
    brief = {
        "研究问题": "固态电池的产业化进展，以及各家技术路线的分歧点",
        "已批准的框架": [{"id": s.get("id"), "title": s.get("title"),
                        "question": s.get("question"), "kind": s.get("kind")}
                       for s in outline],
        "来源清单": srcs,
        "证据段落": [{"段号": f"S{i+1}·{i//6+1}", "这段在讲": s} for i, s in enumerate(segs)],
    }
    r2 = call(prompts.WRITE_PLAN_SPEC,
              json.dumps(brief, ensure_ascii=False)[:40000], 16384, thinking=True)
    d2 = try_json(r2.get("text", ""))
    if d2:
        print(f"    键：{sorted(d2)}")
        for k in ("thesis", "glossary", "cautions", "sections", "spec_uncertainty"):
            v = d2.get(k)
            n = len(v) if isinstance(v, (list, dict)) else (1 if v else 0)
            print(f"    {k:<18} {'✓' if n else '✗'}  {n}")
        print(f"    论点：{str(d2.get('thesis'))[:150]}")
    else:
        print(f"    JSON 解析失败，输出开头：{r2.get('text','')[:200]!r}")

    print(f"\n{'='*76}\n判读")
    print("=" * 76)
    print("  · 审查命中 3 条以上 → 能力够，write 阶段可以接")
    print("  · 命中 1–2 条      → 边缘，要靠 MiniMax 兜审查")
    print("  · 命中 0 条        → 能力不够，write_audit 留给 MiniMax，")
    print("                       但 write_plan（只要 JSON 合规）可能还能用")
    print("  · JSON 结构完整性另看：项目有 json_repair 兜，但字段缺失没法修")


if __name__ == "__main__":
    main()
