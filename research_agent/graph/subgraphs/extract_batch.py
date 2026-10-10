"""抽取子图：把本轮所有来源的正文批量压成结构化断言。

存在的理由是结构性的，不是优化：
  1. 逐源调用会让「一个源一次 LLM」的次数等于来源数，成本失控
  2. 批量调用后，claims 与 source_nature 可以在**同一次**调用里产出，
     不额外花钱
  3. 批次之间可并发，失败只影响一批
  4. 子图有自己的状态，主循环看不到「批」的细节
"""

from __future__ import annotations

import asyncio
from typing import Any

from langgraph.graph import END, START, StateGraph

from ...llm.provider import EmptyOutput, LLMClient, parallel_labelled
from .. import prompts
from ..state import ExtractBatchState

# 批次参数：按字符预算切，不按条数切。
# 上一版按「每批 4 条 + 32,000 字符」切，遇到 arxiv 那种 7 万字长文时
# 会各自成批，短文被挤散——实测 17 次调用、输入 127,023 token，
# 比逐源调用的 30 次、62,762 token 反而更贵。改为纯预算驱动。
#
# 曾试过给长文做「头尾采样」压到 8,000 字符，实测断言回收率只有 55%，
# 丢的全是实验章节里的数值断言（阈值、准确率、成本对比）。文档假设错了：
# 论文的信息密度不是头尾密中间疏，而是摘要、方法、实验、结论都重要。
# 现在长文全量进，只保留下限过滤和一道防病态输入的硬上限。
BATCH_BUDGET_CHARS = 60000     # 单批正文字符预算
BATCH_MAX_SOURCES = 8           # 单批最多几个源，防止碎批
PROMPT_OVERHEAD = 1800          # 提示词骨架开销，按每批重复计入
MIN_SOURCE_CHARS = 800          # 正文短于此的源不进抽取（实测 <3千字档平均只产出 2.2 段）
HARD_CAP_CHARS = 150000         # 防病态输入的硬上限，超过则截断（正常语料不会触发）
SUMMARY_BUDGET_CHARS = 24000    # 一次概括调用的段落正文字符预算
SUMMARY_BATCH = 12            # 一次最多概括几段
SUMM_WORKERS = 4              # 概括批次的并发度。缓存要预热、别打太满（2026-10-07）

CONCURRENCY = 5

LLM = LLMClient()


def _clip(text: str, cap: int = HARD_CAP_CHARS) -> str:
    """只做病态输入保护。正常语料不触发，不做主动压缩。"""
    if not text or len(text) <= cap:
        return text or ""
    elided = len(text) - cap
    return (text[: int(cap * 0.7)]
            + f"\n\n【中间省略 {elided} 字符：超长输入保护】\n\n"
            + text[-int(cap * 0.3):])


NATURE_BATCH = 8            # 一次判几个来源
NATURE_HEAD_CHARS = 2500    # 每个来源给判定看的开头字数


def _make_nature_items(items: list[dict]) -> list[dict]:
    """构造来源性质判定的输入。

    **不再需要整篇正文。** 判定依据是「第一人称我们发布」「有 abstract 和
    methodology」「通篇转述无一手链接」「署名记者」这些**出处信号**，它们
    集中在标题、开头和署名处。所以只给：
      标题 + URL + 域名 + 开头 2500 字 + **各段概括**

    段落概括直接告诉你这篇在讲什么、是不是罗列。

    ⚠️ `summaries` 取的是 `s["summary"]`（概括，约 38 字/段），
    **不是 `s["text"]`（段落原文，约 2000 字/段）**。
    这一点曾经错得很离谱：字段名和注释都写着「概括」，代码取的却是原文，
    而且渲染时不截断——`extract_batch.py` 的 docstring 还写着
    「判定不需要整篇正文」。实测那个字段贡献了 extract 输入的 **77%**
    （327,109 / 427,005 字符），而这批原文同一次调用里已经被
    `summarize_segments` 读过一遍。详见 `COST_C1.md` 的 C1-b。

    要兜住两种退化：概括失败的段落、整篇没被切成段的来源——
    所以开头 2500 字始终保留，它是判定信号的主要来源。
    """
    out = []
    for i, it in enumerate(items):
        segs = it.get("segments") or []
        head = (it.get("text") or "")[:NATURE_HEAD_CHARS]
        out.append({
            "i": i,
            "source_id": it["source_id"],
            "title": it.get("title") or "",
            "url": it.get("url") or "",
            "head": head,
            "summaries": [(s.get("summary") or "").strip()
                          for s in segs if (s.get("summary") or "").strip()][:12],
        })
    return out


def _render_nature(batch: list[dict]) -> str:
    parts = []
    for it in batch:
        summ = "\n".join(f"  - {s}" for s in it["summaries"] if s)
        parts.append(
            f"===== 来源 {it['i']} =====\n"
            f"标题：{it['title']}\n"
            f"URL：{it['url']}\n"
            f"各段概括：\n{summ or '（无）'}\n"
            f"开头：\n{it['head']}"
        )
    return "\n\n".join(parts)


def _run_nature_batch(batch: list[dict]) -> list[dict]:
    """一次调用判一批来源的性质。失败抛异常交给上层重试。"""
    blob = _render_nature(batch)
    data = LLM.complete_json("extract",
                             f"以下是 {len(batch)} 个来源的元信息与开头：\n\n{blob}",
                             system=prompts.SOURCE_NATURE)
    out = []
    for it in (data.get("sources") or []):
        i = it.get("source_index")
        if not isinstance(i, int) or not (0 <= i < len(batch)):
            continue          # 模型给错下标就跳过，不猜
        src = batch[i]
        out.append({
            "kind": "claims",              # 保持同一形状，node_extract 不用改
            "source_id": src["source_id"],
            "title": src.get("title", ""),
            "url": src.get("url", ""),
            "claims": [],
            "source_nature": it.get("source_nature", "unknown"),
            "nature_reason": it.get("nature_reason", ""),
        })
    return out


def make_batches(items: list[dict]) -> list[list[dict]]:
    """按条数切批——来源性质判定不再吃整篇正文，不需要字符预算了。"""
    return [items[i:i + NATURE_BATCH] for i in range(0, len(items), NATURE_BATCH)]


def _node_extract(state: ExtractBatchState) -> dict:
    batches = state.get("batches") or []
    if not batches:
        return {"results": [], "failed_batches": 0, "events": []}

    async def run():
        sem = asyncio.Semaphore(CONCURRENCY)

        async def one(b):
            async with sem:
                return await asyncio.to_thread(_run_nature_batch, b)

        return await asyncio.gather(*(one(b) for b in batches),
                                    return_exceptions=True)

    raw = asyncio.run(run())

    results, failed = [], 0
    for i, r in enumerate(raw):
        if isinstance(r, Exception):
            failed += 1
            results.append({"kind": "extract_error", "batch": i,
                            "error": f"{type(r).__name__}: {r}"[:200]})
        else:
            results.extend(r)

    return {
        "results": results,
        "failed_batches": failed,
        "events": [{
            "kind": "extract_batch", "batches": len(batches),
            "sources": sum(len(b) for b in batches),
            "results": len([r for r in results if r.get("kind") == "claims"]),
            "failed": failed,
        }],
    }


def build_extract_graph():
    g = StateGraph(ExtractBatchState)
    g.add_node("extract", _node_extract)
    g.add_edge(START, "extract")
    g.add_edge("extract", END)
    return g


def extract_all(items: list[dict]) -> dict:
    """批量抽取入口。

    items: 每项一个来源，形如
      {"source_id","title","url","published","text",           # 抽取断言用整篇
       "segments": [{"chunk_id","text","heading_path"}, ...]}  # 概括用段落
    返回 {"results": [...], "summaries": {chunk_id: str},
          "failed_batches": int, "events": [...]}

    **先概括、后判来源性质**（2026-10-06 改，依据见 `COST_C1.md` C1-b）：
    原来反过来，于是 `_make_nature_items` 那个叫「各段概括」的字段
    只能退而取段落**原文**、且渲染时不截断——实测它贡献了 extract 输入的 77%
    （327,109 字符 / 427,005），而这批原文 `summarize_segments` 当场就读过一遍。
    extract 的任务只是判「这个来源是什么性质」，依据集中在标题与开头 2500 字，
    白读整篇是纯浪费。调换顺序后它读的是真正的概括（每段约 38 字）。
    """
    if not items:
        return {"results": [], "summaries": {}, "failed_batches": 0, "events": []}

    # 1) 先概括，并挂回 segments，后面的 nature 才有「概括」可用
    sum_out = summarize_segments(
        [s for it in items for s in (it.get("segments") or [])])
    for it in items:
        for s in (it.get("segments") or []):
            s["summary"] = (sum_out["summaries"] or {}).get(s["chunk_id"], "")

    # 2) 再判来源性质
    batches = make_batches(_make_nature_items(items))
    g = build_extract_graph().compile()
    out = g.invoke({"batches": batches})
    out.update(sum_out)
    return out
def summarize_segments(items: list[dict]) -> dict:
    """为每个段落写一句话概括。

    **从 node_finalize 搬到这里。** 原先它在报告写完之后才跑，产出只进
    跨会话召回的检索索引，写作阶段根本用不上——于是写作只能看抽取出来的
    原子断言，而断言是从整篇里挑出来的一句，父级是几万字的文章，
    没法展开回去看上下文。

    段落是写作取材的最小单位：约 700 token，一个完整论述单元，
    概括完随时能展开回原文。
    """
    segs = [{"chunk_id": it["chunk_id"], "text": it.get("text") or "",
             "heading": it.get("heading_path") or ""}
            for it in items if (it.get("text") or "").strip()]
    if not segs:
        return {"summaries": {}, "failed": 0, "events": []}

    batches: list[list[dict]] = []
    cur: list[dict] = []
    cur_chars = 0
    for s in segs:
        n = len(s["text"])
        if cur and (len(cur) >= SUMMARY_BATCH or cur_chars + n > SUMMARY_BUDGET_CHARS):
            batches.append(cur)
            cur, cur_chars = [], 0
        cur.append(s)
        cur_chars += n
    if cur:
        batches.append(cur)

    out: dict[str, str] = {}
    failed = 0
    # 批次之间完全独立（各读各的段落、写各自的 summary），所以并发跑。
    # 实测串行时 18 批 × 7.9 秒 ≈ 142 秒，占 extract 节点的八成。
    # 第一个批次仍串行：冷缓存下并发会一起打空，命中率反而掉
    # （write 阶段实测过：冷缓存 8 并发只 3 命中，预热后 7/8）。
    def _one(item) -> dict | None:
        bi, batch = item
        blob = "\n\n".join(
            f"[{j}] {s['heading']}\n{s['text'][:3000]}" for j, s in enumerate(batch))
        # **强制工具调用输出**（2026-10-09）。原来是纯提示词让模型手写 JSON，
        # 实测 xj189 上 **9% 的批次崩在 JSON 中途**（`Expecting ',' delimiter:
        # line 1 column 630`——第 630 字符，**中间位置不是结尾截断**），
        # 崩掉的批次整批丢掉，那些段落在写作阶段**根本看不到**（静默丢证据）。
        # 改成工具之后平台在解码层面保证 arguments 是合法 JSON，
        # 同一组数据实测 3/3 通过、5.2~8.8 秒。
        data = LLM.complete_json_forced(
            "summarize",
            f"以下是 {len(batch)} 个报告段落，请为每段写一句话概括：\n\n{blob}",
            name="emit_summaries",
            schema={"type": "object", "properties": {"summaries": {
                "type": "array", "description": "与输入段落一一对应的概括",
                "items": {"type": "object", "properties": {
                    "index": {"type": "integer",
                             "description": "段落下标，对应输入里的 [n]"},
                    "summary": {"type": "string", "description": "一句话概括"}},
                    "required": ["index", "summary"]}}},
                "required": ["summaries"]},
            system=prompts.SUMMARIZE,
        )
        got: dict[str, str] = {}
        for it in ((data or {}).get("summaries") or []):
            idx = it.get("index")
            # 模型给错下标就跳过，不猜
            if isinstance(idx, int) and 0 <= idx < len(batch):
                txt = (it.get("summary") or "").strip()
                if txt:
                    got[batch[idx]["chunk_id"]] = txt
        return got

    for bi, got in enumerate(parallel_labelled(
            list(enumerate(batches)), _one, workers=SUMM_WORKERS,
            label="段落概括", first_serial=True)):
        if got is None:
            failed += 1
            print(f"⚠️  段落概括批次 {bi} 失败", flush=True)
            continue
        out.update(got)

    return {"summaries": out, "failed": failed,
            "events": [{"kind": "segment_summary", "segments": len(segs),
                        "batches": len(batches), "done": len(out),
                        "failed": failed}]}
