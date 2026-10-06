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


def keep_last(a: list, b: list) -> list:
    return b if b else (a or [])


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
    plan_queries: dict           # {"zh": [...], "en": [...]}
    plan_approved: bool

    # ---------- 循环 ----------
    round: int
    visited_urls: list
    url_count: int
    consecutive_no_new: int
    stop_reason: str
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
    earlier: list                # 此前研究的已确认段落
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
