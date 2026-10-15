"""主图状态定义。

只带三样进 LLM 上下文（P6 确认）：大纲进度、本轮新证据、上轮压缩摘要。
完整历史永远不进上下文——这是防上下文膨胀的关键。
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

import operator


def merge_dict(a: dict, b: dict) -> dict:
    out = dict(a or {})
    out.update(b or {})
    return out


def append_unique(a: list, b: list) -> list:
    """累积列表，按 url 或 id 去重。"""
    seen = set()
    out = []
    for item in list(a or []) + list(b or []):
        if isinstance(item, dict):
            key = item.get("url") or item.get("id") or repr(item)[:80]
        else:
            key = item
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


class ResearchState(TypedDict, total=False):
    # ---------- 输入 ----------
    session_id: str
    question: str
    title: str

    # ---------- 时间敏感 ----------
    time_sensitive: bool

    # ---------- 探针（子图产出） ----------
    probe: dict                 # 子图出口契约，见 subgraphs/probe.py
    probe_degraded: bool        # 探针判定信息稀薄，报告需标注
    probe_headline: str

    # ---------- 框架 ----------
    outline: list                # [{id,title,question,grounded_in,kind}]
    outline_revision_log: Annotated[list, operator.add]
    # assess 算出的框架修订，等 apply_revision 去落地。
    #
    # ⚠️ **这一行曾经不存在**，于是整条校准链路是断的（2026-10-09 修）：
    # LangGraph **只保留 TypedDict 里声明过的字段**，未声明的返回值会被静默
    # 丢弃。node_assess 把 {"op":"revise",...} 写进返回值、node_apply_revision
    # 从 state 读它，中间这一格是空的——于是「算出来了但没落地」。
    #
    # 症状长得和「模型没给 outline_review」**一模一样**：
    # run_trace 里 op=revise（模型确实给了），outline_revision_log 却是空的。
    # 排查时我在「模型没给」和「代码丢了」之间猜了好几轮——
    # **教训：数据通道（state 声明）出问题，症状会伪装成上游行为问题。**
    pending_revision: dict       # {"op":"none"} 或 {"op":"revise", narrow/merge/remove}
    # assess 记下「我这一轮算出了修订」，apply_revision 拿它对账。
    # 两者对不上就说明中间丢了（历史上就是这么静默空转的）。
    _expect_revision: bool
    plan_queries: dict           # {"zh": [...], "en": [...]}
    plan_approved: bool

    # ---------- 循环 ----------
    round: int
    visited_urls: list
    url_count: int
    consecutive_no_new: int
    stop_reason: str
    # 「够了」(prompt) 还是「撞顶了」(mechanism)。
    # 审批时必须让用户知道他批的是一份材料撑得住的框架，
    # 还是一份尽力而为的框架——两者可信度完全不同。
    stop_layer: str
    # 已经发出过的检索词。assess 靠它避免第 2 轮原样重发——
    # 重复检索烧配额、产重复段落，还会把 consecutive_no_new 推高导致提前停。
    used_queries: list
    last_round_segments: int      # 上轮累计的可用段落数，用来算「本轮有无新增」

    # ---------- 证据 ----------
    pending_extract: list        # 本轮待批量抽取的来源
    evidence: list               # 本轮新增的抽取结果
    all_evidence: Annotated[list, append_unique]
    sources: Annotated[list, append_unique]
    conflict_hashes: Annotated[dict, merge_dict]

    # ---------- 报告与收尾 ----------
    report_markdown: str
    summarized_chunks: int

    # ---------- 预算 ----------
    cost_tokens: int
    started_at: float
    elapsed_s: float

    # ---------- 事件流（供 SSE 推送） ----------
    events: Annotated[list, operator.add]


class ProbeState(TypedDict, total=False):
    """探针子图自己的状态。换词重试只污染这里，不影响主图。"""

    session_id: str
    question: str
    time_sensitive: bool

    attempt: int
    queries: dict                # {"zh": [...], "en": [...]}
    tried_queries: list          # 所有试过的词，用于诊断
    light_results: list          # [{url,title,snippet,engine}]

    ambiguity: dict
    user_choice: str             # 澄清交互返回的候选 label
    retry_queries: dict
    # 轻扫的 LLM 是否真的给出了判断。False 表示这次调用没产出可用的 depth，
    # 与「判断结果为 thin」是两回事——格式失败不能当成关于世界的结论。
    judged: bool

    deep_urls: list
    deep_pages: list             # [{url,title,text,published}]

    signals: dict                # 领域轮廓
    quality: str                 # substantive / thin / farm
    degraded: bool
    headline: str

    events: Annotated[list, operator.add]


class ExtractBatchState(TypedDict, total=False):
    """抽取子图状态。主循环看不到批的细节。"""

    batches: list                # [[{source_id,title,url,published,text}, ...], ...]
    results: list                # [{source_id, claims, source_nature, ...}]
    failed_batches: int
    # 送了但没判出性质的 source_id。**必须和 failed_batches 分开**：
    # 抛异常是「这一批失败」，没判到是「这一批的一部分没回来」，
    # 原来后者被静默丢掉（2026-10-14 修，见 extract_batch._run_nature_batch）
    unjudged_sources: list
    events: Annotated[list, operator.add]


class WriteState(TypedDict, total=False):
    """write 子图状态。

    主图只收到最终的 report_markdown 与汇总事件，看不到任务级细节。
    """

    # ---- 主图传入 ----
    session_id: str
    question: str
    outline: list                # 已批准的大纲，节只是装配的容器
    signals: dict                # 探针轮廓
    segments: list               # 全部证据段落 [{ref, heading_path, summary, content, ...}]
    on_topic: list               # 通过段级闸门的那些
    src_lines: list              # 全量来源清单（主 agent 要靠它写 cautions）
    n_no_summary: int            # 概括缺失的段落数，必须一路报到报告里

    # ---- 主 agent 产出 ----
    spec: dict                   # 写作规约：thesis/glossary/units/cautions/sections
    tasks: list                  # 任务列表，粒度由主 agent 定，上限 MAX_TASKS
    spec_capped: bool            # 任务数是否触顶被截断
    tool_rounds: int             # plan 取全文用了几个工具轮次

    # ---- 执行 ----
    drafts: dict                 # {task_id: {section_id: 正文}}，一节一份
    violations: dict             # {task_id: [引用问题]}，程序校验的结果
    failed: list                 # [{task, reason}]
    assembled: str               # 按大纲顺序拼好的草稿，审查的输入
    sections_written: int
    uncovered_tasks: list        # 有产出但没有任何小节采用的任务
    assemble_warnings: list      # 装配自检命中：重复正文/放错小节/多节任务缺节
    plan_failed: str             # 主 agent 规划失败时的原因

    # ---- 审查与返修 ----
    audit: dict                  # 审查结论
    rework_rounds: int
    rework_effective: bool

    report_markdown: str         # 最终报告，只由 final 节点写

    events: Annotated[list, operator.add]
