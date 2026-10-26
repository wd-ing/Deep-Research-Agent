"""把节点组装成 LangGraph 主图。

只用 StateGraph / interrupt / SQLite checkpointer（P6 确认），
不用它的 agent 抽象与 tool 装饰器。

主图结构（2026-10-08，审批点后置）：

    probe(子图) → outline → ┐
                            ▼
    retrieve → extract(子图) → assess ─┬─→ await_approval(interrupt) → assess
                  ▲                   │                                  │
                  │                   └─→ retrieve（还要搜）              │
                  └──────────────────────────────────────────────────────┘

    assess 说够了 → apply_revision → await_approval → write → finalize → END

**审批为什么在第一次 assess 之后**：大纲是在只有探针轮廓时写的
（探针深读 2 页 4,192 字符，正式材料 39 来源 140 段——差 13 倍），
它的粒度天然可能超过材料能支撑的程度。实测有一节要求
「四家企业分别披露了哪些研发、中试、装车验证或量产节点」，
而材料全是媒体转述、没有任何企业公告——**这个要求从一开始就做不到**。
让用户审批一个未经材料检验的框架，等于给未验证的承诺盖章。

代价是审批推迟一轮检索 + 抽取（约 2 分钟），但那两步本来就要跑。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .nodes import (
    node_apply_revision, node_assess, node_await_approval, node_extract,
    node_gate,
    node_answer,
    node_finalize, node_outline, node_probe, node_retrieve, node_write,
    node_summary,
    route_after_assess, route_after_gate, route_after_revision,
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
    g.add_node("gate", node_gate)
    g.add_node("assess", node_assess)
    g.add_node("answer", node_answer)
    g.add_node("apply_revision", node_apply_revision)
    g.add_node("write", node_write)
    g.add_node("finalize", node_finalize)
    g.add_node("summary", node_summary)

    g.add_edge(START, "probe")
    g.add_edge("probe", "outline")
    # 审批点在第一次 assess 之后，不在 outline 之后——理由见文件头
    g.add_edge("outline", "retrieve")
    g.add_edge("retrieve", "extract")
    # 段级闸门插在抽取与评估之间：assess 从此在过滤后的真池子上
    # 判断「材料够不够」，闸门调用也摊进各轮、不再堵在写作头部。
    g.add_edge("extract", "gate")
    # 快速档（2026-10-27，C1）：gate 后不评估、不审批、不写子图——
    # 直接进 answer 一次性产出简报；标准档路径一个字不变。
    g.add_conditional_edges(
        "gate", route_after_gate,
        {"answer": "answer", "assess": "assess"},
    )
    g.add_edge("answer", "finalize")
    g.add_conditional_edges(
        "assess", route_after_assess,
        # 未审批 → 先按材料校准框架（没有 outline_review 时 apply_revision 是空转）
        # 已审批 → 只剩「继续搜」和「收工」两个选择，绝不再碰框架
        {"revise": "apply_revision", "retrieve": "retrieve", "write": "write"},
    )
    g.add_conditional_edges(
        "apply_revision", route_after_revision,
        {"approve": "await_approval", "retrieve": "retrieve", "write": "write"},
    )
    g.add_edge("await_approval", "assess")      # 审批完立刻回裁判，不白跑一轮
    g.add_edge("write", "finalize")
    # 摘要放在 finalize **之后**：报告要先落库，摘要才有东西可读。
    # 它是独立的节点而不是 finalize 内部的一步——失败只发事件不上抛，
    # 绝不能因为一次限流把已经写完的研究判成失败。
    g.add_edge("finalize", "summary")
    g.add_edge("summary", END)

    return g.compile(checkpointer=checkpointer)


def _ckpt_detail(config: dict, extra: dict | None = None) -> dict:
    """从 langgraph 的 config 里抠 thread_id / checkpoint_ns。"""
    cfg = (config or {}).get("configurable") or {}
    d = {"thread_id": cfg.get("thread_id"),
         "checkpoint_ns": cfg.get("checkpoint_ns")}
    if extra:
        d.update(extra)
    return d


def _serde_bytes_probe(saver) -> object:
    """给 saver.serde.dumps_typed 包一层，把序列化出的字节数记进 thread-local。

    不包的话拿字节数只能把 checkpoint 再序列化一遍——**而序列化本身正是
    要测的东西**（1.1MB 级的 state），为观测付出一倍序列化开销是本末倒置。
    包 serde 是零成本旁路：原本就要跑的那次 dumps_typed 顺手报个数。

    ⚠️ 只对「同一个对象只会被序列化一次」的调用读数——put 一次 dumps
    一次 checkpoint，多读会拿到别人的数。
    """
    tl = threading.local()
    orig = saver.serde.dumps_typed

    def dumps_typed(obj):
        out = orig(obj)
        try:
            tl.last_bytes = len(out[1]) if isinstance(out[1], (bytes, str)) else None
        except Exception:
            tl.last_bytes = None
        return out

    saver.serde.dumps_typed = dumps_typed
    return tl


def wrap_sync_saver(saver):
    """给 SqliteSaver 的 put/put_writes 记 checkpoint span（2026-10-21）。

    子类化而不是 monkey-patch 实例方法：put_writes 是批量写，单独计时。
    返回类型是 SqliteSaver 子类，`compile()` 的校验与行为不变。
    """
    from ..trace import record_span

    tl = _serde_bytes_probe(saver)

    class TracedSqliteSaver(type(saver)):
        def put(self, config, checkpoint, metadata, new_versions):
            t0 = time.time()
            tl.last_bytes = None
            try:
                return super().put(config, checkpoint, metadata, new_versions)
            finally:
                d = _ckpt_detail(config, {"bytes": tl.last_bytes})
                record_span("checkpoint", "put", t0, time.time(), d,
                            session_id=d.get("thread_id"))

        def put_writes(self, config, writes, task_id, task_path=""):
            t0 = time.time()
            tl.last_bytes = None
            try:
                return super().put_writes(config, writes, task_id, task_path)
            finally:
                d = _ckpt_detail(config, {"bytes": tl.last_bytes,
                                          "writes": len(writes or [])})
                record_span("checkpoint", "put_writes", t0, time.time(), d,
                            session_id=d.get("thread_id"))

    saver.__class__ = TracedSqliteSaver
    return saver


def wrap_async_saver(saver):
    """`get_agraph()` 的 AsyncSqliteSaver 版。测的是 **await 的真实耗时**——
    异步 saver 在事件循环里写，同步节点里的 span 写入在 worker 线程里写，
    两边抢同一个 SQLite，等不等的真相就是要看等的部分，不排除。"""
    from ..trace import record_span

    tl = _serde_bytes_probe(saver)

    class TracedAsyncSaver(type(saver)):
        async def aput(self, config, checkpoint, metadata, new_versions):
            t0 = time.time()
            tl.last_bytes = None
            try:
                return await super().aput(config, checkpoint, metadata,
                                          new_versions)
            finally:
                d = _ckpt_detail(config, {"bytes": tl.last_bytes,
                                          "loop_thread": threading.current_thread().name})
                record_span("checkpoint", "aput", t0, time.time(), d,
                            session_id=d.get("thread_id"))

        async def aput_writes(self, config, writes, task_id, task_path=""):
            t0 = time.time()
            tl.last_bytes = None
            try:
                return await super().aput_writes(config, writes, task_id,
                                                 task_path)
            finally:
                d = _ckpt_detail(config, {"bytes": tl.last_bytes,
                                          "writes": len(writes or [])})
                record_span("checkpoint", "aput_writes", t0, time.time(), d,
                            session_id=d.get("thread_id"))

    saver.__class__ = TracedAsyncSaver
    return saver


def get_graph():
    """编译带 SQLite checkpointer 的主图。

    checkpointer 让研究跑到一半能恢复，人工审批的 interrupt 状态也靠它保存。
    探针子图自己的换词重试状态不落盘——它是一次性的。

    ⚠️ 这是**同步**实例，`invoke()` 用它。异步 `astream()` 用不了它：
    `SqliteSaver` 会抛 `NotImplementedError: does not support async methods`
    （2026-10-11 实测），要配 `get_agraph()` 那个。
    5 个测试调用点依赖本函数，所以它保持原样。
    """
    CKPT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CKPT_DB), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return build_graph(wrap_sync_saver(SqliteSaver(conn)))


_AGRAPH = None
_AGRAPH_CM = None


async def get_agraph():
    """**异步**主图，供 `astream()` 用（server 的进度推送走这条）。

    为什么需要单独的实例：`SqliteSaver` 只支持同步方法，
    `astream()` 会直接抛 `NotImplementedError`。同一个 thread_id 的
    checkpoint 落的是同一个 `checkpoints.db`，所以两条路径能接力——
    前半程 sync invoke 跑到审批点，后半程 astream 续跑，反之亦然。

    ⚠️ `AsyncSqliteSaver.from_conn_string()` 返回的是**异步上下文管理器**，
    不是实例（2026-10-11 实测：直接传给 compile 会报
    `Invalid checkpointer provided ... Received _AsyncGeneratorContextManager`）。
    所以这里手动 `__aenter__` 拿到 saver 再编译，并把连接持有住——
    出了这个作用域连接就关了。
    """
    global _AGRAPH, _AGRAPH_CM
    if _AGRAPH is None:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        CKPT_DB.parent.mkdir(parents=True, exist_ok=True)
        cm = AsyncSqliteSaver.from_conn_string(str(CKPT_DB))
        saver = wrap_async_saver(await cm.__aenter__())
        _AGRAPH_CM = cm          # 持有引用，别被 GC 掉把连接关了
        _AGRAPH = build_graph(saver)
    return _AGRAPH
