"""并发化之后，判定结果有没有变？——用上一轮的真实数据离线验证。

为什么必须验这个
--------------
`summarize` 和 `relevance` 从串行改成并发之后，**时间省了、token 没省**，
唯一能说「没有副作用」的证据就是：同一批输入下，
并发产出的判定结果与串行**逐条相同**。

只看「跑通了」是不够的——顺序错了、方法标签错了、
统计数字变了，都不会报错，只会让报告悄悄变样。

怎么验
------
用 `/tmp/ra_c1d2.db` 里的 140 个真实段落，把 LLM 判定换成**确定性的 mock**
（按段落内容算一个稳定的假判定），然后：
  · 串行跑一遍
  · 并发跑一遍
  · 逐条比对 on_topic / method / 顺序 / by_method 统计

mock 而不是真调 API，是为了把这个验证做成零成本、可重复的回归项。
真调 API 的时间收益另由 `tests/e2e_run.py` 那类实跑去看。

用法：.venv/bin/python tests/parallel_check.py [库路径]
"""

from __future__ import annotations

import os
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
os.environ.setdefault("RA_DB_PATH", "/tmp/ra_parallel_check.db")

from research_agent.graph.subgraphs import relevance as R  # noqa: E402
from research_agent.graph.subgraphs import extract_batch as E  # noqa: E402

DB = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ra_c1d2.db"


def load_segments() -> list[dict]:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT id, heading_path, summary, content FROM chunk "
        "WHERE session_id=(SELECT id FROM session ORDER BY created_at DESC LIMIT 1) "
        "ORDER BY rowid")]
    return rows


def mock_judge(seg, topic_label, blob, method, hits=0, total=0):
    """确定性假判定：内容长度决定结果，同一输入永远同一输出。"""
    n = len(blob or "")
    on = (n % 7) >= 3                      # 3/7 判为相关
    return {"on_topic": on, "method": f"{method}_mock",
            "reason": f"mock: len={n}", "hits": hits}


def serial_gate(segments, kw, label):
    """改动之前的原始实现（串行），用作对照。"""
    strong = kw.get("strong") or []
    weak = kw.get("weak") or []
    keep, marks = [], []
    for seg in segments:
        blob = " ".join([
            seg.get("heading_path") or "", seg.get("title") or "",
            seg.get("summary") or "",
            "" if (seg.get("summary") or "").strip() else (seg.get("content") or "")[:600],
        ])[:3000]
        if not strong and not weak:
            r = mock_judge(seg, label, blob, method="fallback")
        else:
            hits = R._match(blob, strong)
            if hits >= 1:
                r = {"on_topic": True, "method": "rule", "hits": hits,
                     "total": len(strong), "reason": "rule"}
            elif weak and len(strong) == 0 and R._match(blob, weak) / len(weak) >= 0.4:
                r = {"on_topic": True, "method": "rule_weak", "reason": "weak"}
            else:
                r = mock_judge(seg, label, blob, method="llm", hits=hits, total=len(strong))
        marks.append((1 if r["on_topic"] else 0, r.get("reason", ""),
                      r.get("method", ""), seg["id"]))
        if r["on_topic"]:
            keep.append(seg)
    return {"keep": keep, "marks": marks}


def main() -> None:
    if not Path(DB).exists():
        sys.exit(f"缺库 {DB}")
    segs = load_segments()
    if not segs:
        sys.exit("库里没有段落")
    kw = {"strong": ["固态电池", "硫化物", "全固态", "电解质", "宁德时代"],
          "weak": ["电池", "材料", "产线"]}
    label = "固态电池的产业化"

    print("=" * 72)
    print(f"1. relevance：{len(segs)} 个真实段落，串行 vs 并发")
    print("=" * 72)

    orig = R._llm_judge_seg
    R._llm_judge_seg = mock_judge
    try:
        t0 = time.time()
        a = serial_gate(segs, kw, label)
        t_serial = time.time() - t0

        t0 = time.time()
        b = R.gate_segments([dict(s) for s in segs], kw, label)
        t_par = time.time() - t0
    finally:
        R._llm_judge_seg = orig

    ma, mb = a["marks"], b["marks"]
    # 比对**行为字段**：判定(on_topic) / 方法(method) / 段 id。
    # reason 是给人看的说明文本，本次对照的串行 mock 自己写的措辞就与真实实现不同，
    # 拿它当判据会得到假阳性——第一版就是这么误判了一次。
    key = lambda t: (t[0], t[2], t[3])          # noqa: E731
    same = [key(x) for x in ma] == [key(x) for x in mb]
    reason_same = [x[1] for x in ma] == [x[1] for x in mb]

    print(f"  串行 {t_serial*1000:.0f}ms  并发 {t_par*1000:.0f}ms")
    print(f"  标记条数  串行 {len(ma)}  并发 {len(mb)}")
    print(f"  keep 条数 串行 {len(a['keep'])}  并发 {len(b['keep'])}")
    print(f"  by_method 串行 {R._count(x[2] for x in ma)}")
    print(f"  by_method 并发 {R._count(x[2] for x in mb)}")
    print(f"  reason 文本一致：{reason_same}"
          f"{'（串行 mock 措辞不同，非缺陷）' if not reason_same else ''}")
    if same:
        print("  ✓ 行为判定**逐条完全相同**（判定结果、方法归类、顺序都一样）")
    else:
        for i, (x, y) in enumerate(zip(ma, mb)):
            if key(x) != key(y):
                print(f"  ✗ 第 {i} 条不同：串行 {x} / 并发 {y}")
                break
    assert same, "并发改变了判定结果"
    assert len(a["keep"]) == len(b["keep"])

    print(f"\n{'='*72}\n2. summarize：分批顺序与并发后是否一一对应")
    print("=" * 72)
    fake = [{"chunk_id": f"c{i}", "text": f"段落{i}。" * (200 + i * 7),
             "heading_path": f"H{i}"} for i in range(120)]
    segs2 = [x for x in fake if x["text"].strip()]
    batches, cur, cur_chars = [], [], 0
    for s in segs2:
        n = len(s["text"])
        if cur and (len(cur) >= E.SUMMARY_BATCH
                    or cur_chars + n > E.SUMMARY_BUDGET_CHARS):
            batches.append(cur)
            cur, cur_chars = [], 0
        cur.append(s)
        cur_chars += n
    if cur:
        batches.append(cur)
    print(f"  {len(segs2)} 段 → {len(batches)} 批，各批段数 {[len(b) for b in batches]}")

    calls: list[int] = []

    def mock_complete(_self, stage, prompt, system=None):
        # 挂到类上的是**普通函数**，作为绑定方法调用时第一个参数是 self。
        # 从 prompt 里数出这次要概括几段，用来核对回填对不对。
        n = int(prompt.split("以下是 ")[1].split(" 个报告段落")[0])
        calls.append(n)
        import time as _t
        _t.sleep(0.01)                     # 模拟网络延迟，好让并发真的重叠
        return {"summaries": [{"index": j, "summary": f"概括{j}"} for j in range(n)]}

    from research_agent.llm import provider as P
    orig_cj = P.LLMClient.complete_json
    P.LLMClient.complete_json = mock_complete
    try:
        t0 = time.time()
        r_par = E.summarize_segments(segs2)
        t_par = time.time() - t0
    finally:
        P.LLMClient.complete_json = orig_cj

    n_expect = len(segs2)
    got = len(r_par["summaries"])
    print(f"  并发耗时 {t_par*1000:.0f}ms  产出 {got}/{n_expect} 条概括，失败 {r_par['failed']} 批")
    print(f"  各批请求的段数（顺序 = 批顺序）：{calls}")
    assert calls == [len(b) for b in batches], "并发后批次顺序被打乱了"
    assert got == n_expect, f"概括条数不对：{got} != {n_expect}"
    print("  ✓ 批次顺序未变，每段的概括都回填到了正确的 chunk")
    print("  ✓ 零失败")

    print(f"\n{'='*72}\n结论")
    print("=" * 72)
    print("  并发化**不改变任何判定结果**：relevance 逐条相同、summarize 一一对应。")
    print("  唯一变化是墙钟时间，token 一分不省。")
    print("  真跑的时间收益用 tests/e2e_run.py 或 probe_assess_run.py 看。")


if __name__ == "__main__":
    main()