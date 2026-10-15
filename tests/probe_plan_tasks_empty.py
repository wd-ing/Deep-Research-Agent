"""诊断 write_plan「任务分派」这一步为什么空返回。

背景（2026-10-15 全量跑 s_e68ea5bd047a）：
  node_plan_spec   成功 —— 出了完整规约（thesis/12 术语/9 注意事项）
  node_plan_tasks  失败 —— 3 次尝试全空，344 秒后放弃，tasks=0，整篇报告空

⚠️ 这两个节点**共用 stage 名 "write_plan"**，所以 llm_call 里只看到 2 行，
很容易误判成「第一次成功、第二次失败」。真相是：成功的是规约那次。

这个脚本用**真实数据**（该会话 82 段摘要 + 大纲）复现任务分派调用，
连打 N 次，观测：
  · 空返回率
  · finish_reason（length = 预算被 thinking 吃光）
  · 实际有多少 token 花在 thinking 上

用法：
    python tests/probe_plan_tasks_empty.py [次数]
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        k, v = _line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import httpx  # noqa: E402
import yaml  # noqa: E402

from research_agent.graph import prompts  # noqa: E402
# ⚠️ **必须复用生产的 SSE 解析器**，不要自己手搓。
# 我第一版写的是 `line.startswith("data: ")`（冒号后带空格），
# 而 xj189 实际发的是 `data:{...}`（**没有空格**）—— 于是每一行都被跳过，
# content 恒为空，测出「5/5 确定性失败」这种**完全捏造**的结论。
# 生产那份 `provider._sse_lines` 是对的（`startswith("data:")` + `line[5:]`）。
from research_agent.llm.provider import _sse_lines  # noqa: E402

DB = os.environ.get("PROBE_DB", "/tmp/ra_v6_full.db")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 4


def build_prompt() -> str:
    """照 `node_plan_tasks` 的口径拼：问题 + 规约 + 框架（段号 + 摘要）。

    ⚠️ 段号必须用 `EvidenceTools._build_index` 的同一套编号：
    来源按 rowid 排 S1、S2…，段按 (来源 rowid, 段 rowid) 排 S3·5。
    自己另编一套会让分派出来的段号对不上后面的工具读取。
    """
    c = sqlite3.connect(DB)
    question = c.execute("select question from session").fetchone()[0]
    outline = json.loads(c.execute("select outline_json from session").fetchone()[0])
    secs = outline if isinstance(outline, list) else list(outline.values())

    src_no = {sid: i for i, (sid,) in enumerate(
        c.execute("select id from source order by rowid").fetchall(), 1)}
    rows = c.execute(
        "select c.rowid, c.source_id, c.summary from chunk c "
        "where c.on_topic=1 and c.summary is not null and c.summary != '' "
        "order by (select rowid from source where source.id=c.source_id), c.rowid"
    ).fetchall()
    per: dict[str, int] = {}
    segs = []
    for _rowid, source_id, summary in rows:
        per[source_id] = per.get(source_id, 0) + 1
        segs.append({"段号": f"S{src_no[source_id]}·{per[source_id]}",
                     "概括": summary})

    payload = {
        "研究问题": question,
        "写作规约（你上一轮定的，必须遵守）": {
            "thesis": "固态电池产业化正处于从实验室到量产的关键阶段，"
                      "多条技术路线并行且分歧未定，固-固界面、成本与制造是核心瓶颈。",
            "glossary": [{"canonical": "全固态电池", "en": "ASSB",
                          "note": "离子传递完全通过固体电解质进行。"}],
            "cautions": ["不同来源的成本数据口径不一，引用时须带年份"],
        },
        "框架与轮廓": {"小节": secs, "证据段落": segs},
    }
    return json.dumps(payload, ensure_ascii=False)


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))
    p = cfg["providers"]["xj189"]
    st = cfg["stages"]["write_plan"]
    key = os.environ.get(p["api_key_env"], "")
    if not key:
        print(f"❌ 未设置 {p['api_key_env']}")
        return 1

    prompt = build_prompt()
    msgs = [{"role": "system", "content": prompts.WRITE_PLAN_TASKS},
            {"role": "user", "content": prompt}]
    n_seg = len(json.loads(prompt)["框架与轮廓"]["证据段落"])
    print(f"复现载荷：{len(prompt):,} 字（{len(prompt)//3:,} tok 粗估）"
          f"，{n_seg} 段摘要")

    empty = 0
    n_len = 0
    rows: list[tuple[str, int, int, int, float]] = []
    for i in range(N):
        body = {
            "model": p["model"], "messages": msgs,
            "max_tokens": st["max_output_tokens"],
            "temperature": st["temperature"],
            "reasoning_effort": st["reasoning_effort"],
            "response_format": {"type": "json_object"},
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        t0 = time.time()
        text, reasoning, finish, usage = "", "", "", {}
        with httpx.stream("POST", f"{p['base_url'].rstrip('/')}/chat/completions",
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"},
                          json=body, timeout=300.0) as r:
            r.raise_for_status()
            for obj in _sse_lines(r):
                usage = obj.get("usage") or usage
                for ch in obj.get("choices") or []:
                    d = ch.get("delta") or {}
                    text += d.get("content") or ""
                    reasoning += d.get("reasoning_content") or ""
                    finish = ch.get("finish_reason") or finish
        dt = time.time() - t0
        blank = not text.strip()
        empty += blank
        n_len += finish == "length"
        rows.append(("空" if blank else "有", len(text), len(reasoning), dt))
        print(f"  第 {i+1} 次：content {len(text):>6} 字 / reasoning "
              f"{len(reasoning):>6} 字 / finish={finish or '-':<8} / {dt:6.1f}s"
              f"{'   ← 空返回' if blank else ''}")

        if text.strip():
            try:
                json.loads(text)
            except json.JSONDecodeError:
                print("          ⚠️ 有内容但不是合法 JSON")
        if i < N - 1:
            time.sleep(3)

    print(f"\n空返回 {empty}/{N}；finish_reason=length（预算被思考吃光）{n_len}/{N}")
    if empty == N:
        print("⇒ **确定性失败**：同参数重发必然还空，重试纯属浪费"
              "（和 provider.py:844 记的 write_plan 历史故障同一形态）")
    elif empty:
        print("⇒ **间歇性失败**：部分尝试能出内容，重试有意义，但成功率偏低")
    else:
        print("⇒ 这次没复现：可能是载荷规模相关（本次比线上那次小/大）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
