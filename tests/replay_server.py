"""前端测试用的回放服务器：**零 API 调用、零搜索额度**。

## 它是什么

真跑一轮要 25 分钟、烧几十万 token，而改前端时几乎每几分钟就要看一次效果。
这个工具把「跑图」换成「回放录好的事件」，**其余一切都走真代码**：

- 真 FastAPI（同一个 `research_agent.server:app`）
- 真 SSE（同一个 `_emit` / `/api/stream/{sid}`）
- 真 SQLite（同一个 DB——报告、来源、段号索引全是真的）
- 真 `EvidenceTools`（点引用取原文那条路也是真的）

**唯一被替换的**：图对象本身 → 换成按 fixture 时间戳把事件 `_emit` 出去。
所以前端看到的报文形状、时序、断点行为，和真跑**完全一致**。

## 怎么用

```bash
# 1) 准备一份带真实报告的库（任何跑过研究的库都行）
cp data/refs/ra_v11_full.db /tmp/ra_replay.db

# 2) 起回放服务器（speed=0 瞬间发完，纯测 UI 终态；1.0 是真实节奏）
.venv/bin/python tests/replay_server.py --speed 1 --db /tmp/ra_replay.db

# 3) 前端照常连 http://127.0.0.1:5180，点「新建研究」就能看到全过程
```

**要让回放会话出现在列表里**（可选，方便反复点）：
```bash
curl -X POST http://127.0.0.1:8000/api/replay/start
```

## 前提：段号只在「有真实证据」的 session 里有效

段号（`S1·5`）靠 `chunk` 表的 rowid 顺序算出来。
**回放会话自己不检索，`chunk` 表里没有它的段落**——
所以在回放会话里点引用会取不到东西，或者取到别的来源。

⇒ **报告页和引用抽屉要用左侧已有的真实会话测**（它们有真实 chunk），
回放只用来测「进度视图」和「审批视图」。这不是回放的缺陷，
是段号本来就依赖真实数据。

（实测确认过：复制 DB 不会让段号错位——41 来源、S2 在两个库里
都是 xueqiu.com，一致。）

## 刻意不做的事

**不做「晚连补发历史」。** `/api/stream` 的队列是连接时才建的，
所以事件在连接之前放完就收不到，要补就得改 SSE 端点 + 维护事件缓冲——
复杂度远超收益（实测实时接收 41 条事件完全正常）。
回放的用法是「点了新建研究就看着它跑」。

真跑时前端全程在线，**本来就不存在「错过事件」**，所以这不影响它对生产的代表性。

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--speed` | 1.0 | 时间倍速。`0` = 瞬间全发，`5` = 5 倍速 |
| `--db` | `$RA_DB_PATH` | 用哪个库 |
| `--port` | 8000 | |

## 为什么这么设计

**不另写一套 mock 服务。** 另写一套的话两边行为会慢慢漂移，
「mock 通过、真跑不通过」是最消耗时间的坑。
复用真 app 的话，mock 通过基本就意味着真跑也通过——
唯一差异就是数据是录的，不是现算的。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def install(srv, speed: float) -> dict:
    """把 server 里的图换成回放器。**只动这一处**，其余全是真代码。"""
    phases = _load("run_to_approval.json")["events"]
    after = _load("run_after_approval.json")["events"]
    approval = _load("approval.json")
    state = {"speed": speed, "approved": set(), "sessions": []}

    def _timeline(is_resuming: bool) -> list[dict]:
        return after if is_resuming else phases

    class FakeGraph:
        """接口只对齐 `server.py` 真正用到的那几个方法。

        `astream` 不 yield 任何东西，而是直接 `_emit`——
        因为 server 的 `_task_stream` 会把 astream 的 yield 当事件转发，
        而我们要的是「事件原样出现在 SSE 上」，不绕那道转换。
        """

        async def astream(self, input=None, config=None, *, stream_mode=None, **kw):
            """⚠️ server 走的是 `agraph()` 不是 `graph()`，两个都得替换（见 install）。

            这里**直接 `_emit`**而不 yield：server 的 `_task_stream` 会把
            yield 当节点更新再转一道，而我们要的是事件原样出现在 SSE 上。
            所以走 async generator 但一个都不 yield。
            """
            tid = (config or {}).get("configurable", {}).get("thread_id")
            resuming = hasattr(input, "resume")   # approve 传的是 Command
            if resuming:
                state["approved"].add(tid)
            last, sp = 0.0, state["speed"]
            for item in _timeline(resuming):
                if sp > 0:
                    gap = (item["at"] - last) / sp
                    if gap > 0:
                        await asyncio.sleep(gap)
                last = item["at"]
                srv._emit(tid, item["event"])
            if resuming:
                # 真图跑完由 nodes.finish_session 落 'done'——回放绕过真节点，
                # 不补这一句会话在列表里永远是 running。
                srv.get_repo().finish_session(tid, "done")
            # ⚠️ done 事件**两条路都要发**（2026-10-18 修）：
            # 原来只在 `isinstance(input, dict)`（首跑）发，续跑批完只落了
            # DB 状态没发事件——前端收不到 done 就一直停在 'running'，
            # 「收尾中」呼吸点永远闪、报告永远不翻出来。
            md_row = srv.get_repo().q1(
                "SELECT markdown FROM report WHERE session_id=?", (tid,))
            md = md_row["markdown"] if md_row else ""
            srv._emit(tid, {"kind": "done", "state": {
                "report": md or "（回放模式：报告在左侧已有的会话里）",
                "report_chars": len(md), "round": 2, "url_count": 29,
            }})
            return
            yield  # noqa: E501 —— 仅为让它成为 async generator

        def invoke(self, input=None, config=None, **kw):
            """同步回退路径同样要放事件，否则 `?streaming=false` 会退化成空跑。"""
            tid = (config or {}).get("configurable", {}).get("thread_id")
            resuming = hasattr(input, "resume")
            last, sp = 0.0, state["speed"]
            for item in _timeline(resuming):
                if sp > 0:
                    gap = (item["at"] - last) / sp
                    if gap > 0:
                        time.sleep(gap)
                last = item["at"]
                srv._emit(tid, item["event"])
            if resuming:
                srv.get_repo().finish_session(tid, "done")
            return {"report_markdown": "", "round": 1, "url_count": 29}

        def get_state(self, config=None, **kw):
            """审批 payload 从 fixture 取，不碰 checkpointer。

            真跑时它是 `Interrupt.value`；回放要的是**同一个形状**，
            而 `server.approval()` 只读这一个属性，所以造个壳就够。

            ⚠️ **已批准的会话必须不再返回 interrupt**（2026-10-18 修）：
            原来无条件返回，前端兜底轮询每 4s 拉一次 `/approval`，
            done 之后照样拿到 waiting=true → 又被拽回审批页 →
            再点批准再放一遍写作 → 无限循环。
            """
            tid = (config or {}).get("configurable", {}).get("thread_id")
            waiting = tid not in state["approved"]
            class _S:
                values = {}
                interrupts = ([type("I", (), {"value": approval})()]
                              if waiting else [])
            return _S()

    fake = FakeGraph()
    srv.graph = lambda: fake          # type: ignore[assignment]

    # ⚠️ **两个都要换**（2026-10-11 踩过）：
    # `graph()` 供 get_state/回退路径用，`agraph()` 才是 run/approve 的流式路径。
    # 只换 `graph` 的话，server 照样调 `await agraph()` 拿真图——
    # 表现为「事件一条都没有，但也不报错」。
    async def _agraph():
        return fake
    srv.agraph = _agraph               # type: ignore[assignment]

    # 刻意**不做**「晚连补发历史」：`/api/stream` 的队列是连接时才建的，
    # 补发要改 SSE 端点 + 维护一份事件缓冲，复杂度远超收益。
    # 回放的用法是「点了新建研究就看着它跑」——实测实时接收 41 条事件正常。
    # 真跑时前端本来就全程在线，不存在「错过事件」。
    return state


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--speed", type=float, default=1.0,
                    help="时间倍速；0 = 瞬间全发（纯测 UI 终态）")
    ap.add_argument("--db", default=os.environ.get("RA_DB_PATH"), help="用哪个库")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    if args.db:
        os.environ["RA_DB_PATH"] = args.db

    from research_agent import server as srv
    import uvicorn

    state = install(srv, speed=args.speed)

    from research_agent.db.repo import get_repo

    @srv.app.post("/api/replay/start")
    def _start(speed: float | None = None) -> dict:
        """建一个会话，让前端连它就能看到回放。

        标题带序号 + 时间：回放会连着建好几条，全叫同一个名字的话
        侧边栏里根本分不出哪条是哪条（实测踩过，攒到 9 条全一样）。

        `?speed=` 按次调倍速：服务用 `--speed 0` 起来测终态，
        前端的「演示回放」按钮想要有节奏地看时传 `?speed=8` 之类即可，
        不用为看演示重启服务。
        """
        from research_agent.db.repo import get_repo
        import datetime as _dt
        if speed is not None:
            state["speed"] = speed
        repo = get_repo()
        n = len(state["sessions"]) + 1
        stamp = _dt.datetime.now().strftime("%H:%M:%S")
        sid = repo.create_session(f"【回放 {n}】{stamp} 固态电池的产业化进展",
                                  "固态电池的产业化进展")
        state["sessions"].append(sid)
        # 克隆 donor 的 report + source + chunk 进新会话：回放跑完报告页
        # 要有真东西看（原来只发占位提示，done 之后是空的）。
        # ⚠️ 三处顺序都不能错（段号 S{源序}·{段序} 靠 rowid 次序算）：
        #   source 按 rowid 排 → S 编号不变；source_id 要重映射
        #   （id 是 TEXT 主键，原样复制会撞键）；
        #   chunk 按 **rowid** 排——按 id 排是随机 hex 序，段号会乱。
        # donor 必须是有 source 的会话：之前的回放会话也有 report+chunk，
        # 按 created_at 最新会克隆到「克隆的克隆」——来源为空、段号断链。
        donor = repo.q1(
            "SELECT session_id FROM report "
            "WHERE session_id IN (SELECT DISTINCT session_id FROM source) "
            "ORDER BY created_at DESC LIMIT 1")
        if donor and donor["session_id"] != sid:
            import uuid as _uuid
            ds = donor["session_id"]
            rid = f"r_{sid}"
            repo.x("INSERT INTO report (id, session_id, markdown, created_at) "
                   "SELECT ?, ?, markdown, ? FROM report WHERE session_id=?",
                   (rid, sid, _dt.datetime.now().isoformat(), ds))
            src_map: dict[str, str] = {}
            src_cols = None
            for srow in repo.q(
                    "SELECT * FROM source WHERE session_id=? ORDER BY rowid", (ds,)):
                nid = f"src_{_uuid.uuid4().hex[:12]}"
                src_map[srow["id"]] = nid
                if src_cols is None:
                    src_cols = [c for c in srow.keys()
                                if c not in ("id", "session_id")]
                repo.x(
                    f"INSERT INTO source (id, session_id, {', '.join(src_cols)})"
                    f" VALUES ({', '.join('?' * (len(src_cols) + 2))})",
                    (nid, sid, *[srow[c] for c in src_cols]))
            ck_cols = None
            for crow in repo.q(
                    "SELECT * FROM chunk WHERE session_id=? ORDER BY rowid", (ds,)):
                if ck_cols is None:
                    ck_cols = [c for c in crow.keys()
                               if c not in ("id", "session_id",
                                            "report_id", "source_id")]
                repo.x(
                    f"INSERT INTO chunk (id, session_id, report_id, source_id,"
                    f" {', '.join(ck_cols)})"
                    f" VALUES ({', '.join('?' * (len(ck_cols) + 4))})",
                    (f"c_{_uuid.uuid4().hex[:12]}", sid, rid,
                     src_map.get(crow["source_id"], crow["source_id"]),
                     *[crow[c] for c in ck_cols]))
        return {"ok": True, "session_id": sid,
                "note": "前端选中这个会话就能看到回放；或直接 POST /run"}

    @srv.app.get("/api/replay/state")
    def _st() -> dict:
        """探测口：真后端没有这个路由（404）。
        前端启动时探它，通了就显示「演示回放」按钮。"""
        return {"sessions": state["sessions"], "speed": state["speed"]}

    db = os.environ.get("RA_DB_PATH") or "(默认)"
    n1 = len(_load("run_to_approval.json")["events"])
    n2 = len(_load("run_after_approval.json")["events"])
    print(f"回放服务器 http://127.0.0.1:{args.port}")
    print(f"  库     {db}")
    print(f"  倍速   {args.speed}{'（瞬间发完）' if args.speed <= 0 else ''}")
    print(f"  事件   到审批点 {n1} 条 + 写完 {n2} 条")
    print(f"  审批   8 节框架，6 节带材料校准说明，可信度仍在补充材料")
    print(f"\n  前端连 http://127.0.0.1:5180")
    uvicorn.run(srv.app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
