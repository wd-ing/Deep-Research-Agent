# DESIGN_R8_FRONTEND.md · 前端三轮推进方案

> 2026-10-11。目标：把 `server.py` 那套**已经建好但没接上东西**的能力接通，
> 让研究过程从「命令行黑箱 25 分钟」变成网页上看得见的一步一步。
>
> 决策记录在 `DECISIONS.md`；本文只讲**做什么、为什么、怎么验证**。

---

## 一、三轮划分

| 轮 | 做什么 | 结束的标志 |
|---|---|---|
| **一轮：后端接口与能力补齐** | 报告入库、节点进度外推、审批 payload 暴露、引用查询接口、流式思考回调 | `curl` SSE 能看到全过程；前端要的每个数据都有接口 |
| **二轮：前端框架完成** | Vite+React 骨架、研究发起页、审批页、报告阅读页（Markdown+引用跳转） | 三块页面能用，从提问到读报告全链路通 |
| **三轮：美化与体验** | 思考过程实时展示、进度可视化、交互打磨 | 好看且好用 |

**为什么一轮要先把后端做全**（用户提出）：否则会出现「写一半前端、
发现后端缺东西、回头补、再改前端」的循环。接口一次给全，前端只写一遍。

---

## 二、现状核对（2026-10-11 实测，不是回忆）

### 已经有的

| 能力 | 位置 | 状态 |
|---|---|---|
| REST API 12 个端点 | `server.py` | 可用 |
| SSE 骨架 | `_subs` / `_emit` / `/api/stream/{sid}` | **建好了但没数据流** |
| 审批 interrupt | `node_await_approval` + `POST /approve` | payload 很完整，**但没暴露给前端** |
| 节点结构化事件 | 每个节点返回 `events` | **在 state 里累加，但一次都没推出去** |
| 段号→原文 | `EvidenceTools._read_segments` | 有能力，**没 HTTP 接口** |
| LLM 流式 + 思考 | `SSEAccum` 攒 `reasoning_content` | 有能力，**没回调** |

### 三个必须先解决的断点

**① `report` 表是空的（实测 0 行），而前端取报告正是读它。**
`server.py:90` 从 `report` 表拿 markdown。测试 harness 写文件不写库，
所以一接前端就会拿到 `null`。→ **一轮必修**。

**② `_emit` 全项目只在 `server.py` 调 3 次**（`done` / `error`），
图侧那些丰富的 `events` 一次都没推。`run_session` 用 `invoke()`，
跑完才返回。→ **一轮必修**。

**③ 审批 payload 内容很全**（框架 + 每节材料支撑 + 修订记录 + 可信度 + 材料统计），
但它走 interrupt 内部，**server 没有任何端点能读到它**。→ **一轮必修**。

### 两个好消息

**`events` 已经是累加器**：`state.py` 里 `events: Annotated[list, operator.add]`。
每个节点返回的事件自动合并到 state —— **推进度不需要改任何节点代码**。

**LangGraph 1.2.12 的 `CompiledStateGraph` 有 `stream` / `astream`**，
且支持 `stream_mode`。（注：我一度查 `StateGraph` builder 得出「没有 stream」，
**那是查错了对象**——结论下早了。）

---

## 三、第一轮：后端接口与能力补齐

### 3.1 报告入库 —— ✅ 核对后免做

**原以为的断点**：以为 `node_write` 没写库。**实际 `nodes.py:1264` 早就有
`repo.save_report(sid, md)`**，代码是好的。

`report` 表实测 0 行，真正原因是**从没通过 server 路径跑过研究**——
v5 那些轮用的是 `/tmp/ra_xj189_v5.db`，而 server 连 `data/research.db`。
**测试 harness 写文件不写库**（见 HANDOVER 地雷区）。

⇒ 不用改代码，但**必须用 server 跑一轮验证**它真的能落库。

### 3.2 节点进度外推 —— ✅ 已跑通

**做法**：`run_session` 加 `streaming` 参数（默认 true），走
`astream(stream_mode="updates")`；`?streaming=false` 退回原来的 `invoke`。

**推的是什么**：
- `{"kind":"node","node":"probe"}` —— 节点开始/结束
- `{"kind":"progress","node":…,"event":{…}}` —— 该节点新增的 `events` 条目

**为什么算增量**：`events` 是累加器（`Annotated[list, operator.add]`），
每次拿到的都是**迄今全部**事件，不切差集就会反复推同一批。

**三个必须记住的坑**：
1. 端点必须是 **`async def`**。同步端点被 FastAPI 丢进线程池，
   那里没有运行中的事件循环，`asyncio.create_task` 直接
   `RuntimeError: no running event loop`（第一版就栽在这）
2. **必须有独立的异步图**。`SqliteSaver` 只支持同步方法，
   `astream()` 抛 `NotImplementedError`。
   `runner.py` 现在有 `get_graph()`（同步，5 个测试依赖）
   和 `get_agraph()`（异步）两个实例，写同一个 `checkpoints.db` 所以能接力
3. `AsyncSqliteSaver.from_conn_string()` 返回的是**异步上下文管理器不是实例**，
   直接传给 `compile()` 会报 `Invalid checkpointer provided`，
   要手动 `__aenter__` 并把 cm 存成模块变量持有

**run 与 approve 要各记一个 `seen` 游标**（`_last_seen`），
否则审批后会重发第一轮的旧事件。

**实测**：真实研究跑起来，`probe` / `progress` / `thinking` 事件实时到达。

### 3.3 审批 payload 暴露

**做什么**：新增 `GET /api/sessions/{sid}/approval`，
用 checkpointer 的 `get_state(config)` 读 interrupt 处的 state，
把 `node_await_approval` 组装的 payload 吐出来。

**为什么**：审批界面必须显示「哪几节材料偏薄」「这份框架可不可信」，
否则用户批的是未经检验的框架。

### 3.4 引用查询接口

**做什么**：新增 `GET /api/sessions/{sid}/seg/{ref}`（`ref` 形如 `S1·5`），
内部复用 `EvidenceTools` 的段号索引，返回该段全文 + 来源信息。

**为什么**：报告里 `[S1·5]` 可点击查看原文，是引用可信度的关键。
**编号方案必须复用 `EvidenceTools` 那一套**（`node_write` 与它是同一套，见
`HANDOVER` 已知地雷第一条）。

### 3.5 流式思考回调 —— ✅ 已跑通（但做了合并）

**做法**：`provider.py` 加一个 contextvar 钩子 `_DELTA_HOOK`，
`SSEAccum` 收到 `reasoning_content` 时回调。**默认 None = 完全不启用**，
现有 7 个开流式的环节都没传，行为一个字不变。

**为什么用 contextvar 不用传参**：`complete()` 有几十个调用点
（节点、子图、工具循环、并发子 agent），串参数会污染整条调用链。

**跨线程能继承**（已实测）：write 子图用 `ThreadPoolExecutor`，
而 `run_in_context` 会 `copy_context()`——**连 `_DELTA_HOOK` 一起复制了**，
所以并发子 agent 的思考增量不会丢。
（同 C1-j 那个坑：并发不继承 contextvar，必须靠 `submit_run_in_context`。）

**⚠️ 必须合并，一块一帧会炸**（实测数据）：

| | 一块一帧 | 合并后 |
|---|---:|---:|
| `probe_outline` 一轮的块数 | **2,336** | **7** |
| 平均每块 | 7.2 字符 | **546 字符** |
| 内容总量 | 16,892 字 | 3,821 字（早期） |

**合并规则**：攒够 40 字或 0.25 秒推一次；收尾时把残余推掉（带 `final: true`），
不让最后几句话丢。**内容一个字不少，只是帧数降下来了。**

**只推 reasoning，不推 text**：正文增量留给第三轮（那时前端才真的需要
逐字显示正文）。关思考的环节（relevance/summarize/extract 等 5 个）
在 `_NO_DELTA_STAGES` 里，不推。

### 3.6 一轮的验证方式

```bash
# 报告入库
sqlite3 data/research.db "SELECT COUNT(*) FROM report;"

# 进度外推：跑一次真实研究，同时 curl SSE
.venv/bin/python -m uvicorn research_agent.server:app &
curl -N http://127.0.0.1:8000/api/stream/<sid>
# 期望：看到 retrieve / extract / assess / write 等阶段事件实时到达

# 审批 payload
curl http://127.0.0.1:8000/api/sessions/<sid>/approval

# 引用查询
curl http://127.0.0.1:8000/api/sessions/<sid>/seg/S1·5
```

**一轮不写任何前端代码**，全部用 curl 验证。

---

## 四、第二轮：前端框架

Vite + React + TypeScript，`frontend/` 目录，`/api` 代理到 8000。

### 4.1 三块页面

1. **发起研究** — 输入问题、启动、SSE 显示实时进度（阶段/轮次/命中/缺口）
2. **审批** — 展示最终框架 + 每节材料支撑 + 可信度说明，通过/驳回
3. **报告阅读** — Markdown 渲染，`[S1·5]` 点击展开原文段落

### 4.2 二轮的验证方式

浏览器里从「输入问题」走到「读完报告」，全程不碰命令行。

---

## 五、第三轮：美化与体验

- 思考过程实时展示（用一轮的 3.5）
- 进度可视化（检索轮次曲线、命中/接纳、缺口收敛）
- 引用跳转体验（hover 预览、侧栏展开）
- 存疑与未完成一节的高亮

---

## 六、风险

| 风险 | 应对 |
|---|---|
| `astream` 改动影响现有 `invoke` 路径 | 新增函数，不改旧的；测试照旧跑 |
| 思考增量太碎（174 块） | 前端合并，或后端按 N 帧合并一次 |
| 前端写完发现接口不够 | 一轮的目的就是避免这个；新需求记进本文再补 |
| `report` 表历史数据为空 | 老 session 报告在 `/tmp/e2e_report_*.md`，前端可加「从文件导入」兜底 |

---

## 七、明确不做

- 多用户 / 权限 / 团队（`HANDOVER` 第一段已排除）
- 移动端适配（`server.py` 绑 0.0.0.0 是留口子，不等于要做）
- 部署上线（本地自用，`localhost` 够）
