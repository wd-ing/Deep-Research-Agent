"""把节点组装成 LangGraph 主图。

只用 StateGraph / interrupt / SQLite checkpointer（P6 确认），
不用它的 agent 抽象与 tool 装饰器。

主图结构（2026-10-04）：

    probe(子图) → outline → await_approval(interrupt) → ┐
                                                          │
    ┌─────────────────────────────────────────────────────┘
    │
    ▼
  retrieve → extract(子图) → assess → apply_revision ─┬─→ write → finalize → END
    ▲                                                 │
    └─────────────────────────────────────────────────┘
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .nodes import (
    node_apply_revision, node_assess, node_await_approval, node_extract,
    node_finalize, node_outline, node_probe, node_retrieve, node_write,
    route_after_assess, route_after_revision,
)
from .state import ResearchState

REPO_ROOT = Path(__file__).resolve().parents[2]
CKPT_DB = REPO_ROOT / "data" / "checkpoints.db"


def build_graph(checkpointer):
    g = StateGraph(ResearchState)

    g.add_node("probe", node_probe)
    g.add_node("outline", node_outline)
    g.add_node("await_approval", node_await_approval)
    g.add_node("retrieve", node_retrieve)
    g.add_node("extract", node_extract)
    g.add_node("assess", node_assess)
    g.add_node("apply_revision", node_apply_revision)
    g.add_node("write", node_write)
    g.add_node("finalize", node_finalize)

    g.add_edge(START, "probe")
    g.add_edge("probe", "outline")
    g.add_edge("outline", "await_approval")
    g.add_edge("await_approval", "retrieve")
    g.add_edge("retrieve", "extract")
    g.add_edge("extract", "assess")
    g.add_edge("assess", "apply_revision")
    g.add_conditional_edges(
        "apply_revision", route_after_revision,
        {"retrieve": "retrieve", "write": "write"},
    )
    g.add_edge("write", "finalize")
    g.add_edge("finalize", END)

    return g.compile(checkpointer=checkpointer)


def get_graph():
    """编译带 SQLite checkpointer 的主图。

    checkpointer 让研究跑到一半能恢复，人工审批的 interrupt 状态也靠它保存。
    探针子图自己的换词重试状态不落盘——它是一次性的。
    """
    CKPT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CKPT_DB), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return build_graph(SqliteSaver(conn))
