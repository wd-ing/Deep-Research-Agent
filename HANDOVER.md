# 交接文档

> 面向接手这个项目的人（包括未来的我）。**新窗口先读这一份。**
> 优化轮结论在 `OPTIMIZATION_LOG.md`；架构设计在 `DESIGN_R4~R9_*.md`；
> 逐条决策的时间线在 `DECISIONS.md`（历史档案，只在追溯「当时为什么那么想」时才翻）；
> **产品蓝图与实际实现的差距在 `GAP_CHECK.md`（2026-10-11 实测核对）**。
>
> 最近更新：2026-10-10

---

## 一、这是什么

一个**本地优先的单人深度研究 Agent**。给定问题，它多轮检索、抓取、抽取证据、
搭框架、分块写作、交叉审查，最后产出一份带段级引用的报告。

**不做**：多用户、权限、团队协作、移动端、代码沙箱、向量数据库、表格/PDF 解析。
这些在 `DECISIONS.md` 里明确排除过，别重新提议。

**技术栈**：纯 Python 单栈。FastAPI（`server.py`）+ LangGraph（只用 StateGraph /
SQLite checkpointer / interrupt，不用它的 agent 抽象）+ Vite/React（**前端一行没写**）。

---

## 二、当前状态

### 主图

```
START → probe(子图) → outline → retrieve → extract(子图) → assess
                                                                  │
                              ┌───────────────────────────────────┘
                              ↓
                       apply_revision ─→ assess ⇄ retrieve
                              │
                        await_approval（interrupt，审批点已后置）
                              │
                     write(子图) → finalize → END
```

**审批点在第一次 `assess` 之后**，不在 `outline` 之后——
大纲是在只有探针轮廓时写的，让用户审批一份未经材料检验的框架没有意义。
assess 顺带输出 `outline_review` 把框架校准到材料撑得住的粒度。

### 三个子图

| 子图 | 做什么 |
|---|---|
| `probe` | 轻扫 → 歧义澄清 → 深读 → 领域轮廓（大纲的 `grounded_in` 全部指回这里）|
| `extract_batch` | 段落概括 + 来源性质判定（+ 段级相关性闸门）|
| `write` | 主 agent 规划 → 并行子 agent 写作 → 审查 → 定点返修 |

### 平台

`config/models.yaml` 的 `providers:` 档案管理，**阶段一行 `provider: xj189` 就切平台**。
模型名 / 地址 / 密钥 / 预算 / 思考档位全在配置里，**代码零硬编码**。

当前主用 **minimax（MiniMax-M3）**——xj189 未恢复前的临时默认；
`internai` / `opencode` 保留备用。切回 xj189 只改 `defaults.provider` 一行。

⚠️ 换 provider **不只是改一行**：协议形状不同，防线要逐条核对。
已踩的：截断检测认不出 Anthropic 形状的 `usage` / `stop_reason`
（坑 51）。下次换平台前先读坑 51 和坑 52。

⚠️ **xj189 前面有 nginx 的 60 秒读超时墙**。开了流式的环节已绕过
（实测跑过 245 秒），流式状态见 `config/models.yaml` 的 `stream:` 字段。

### 模型分档

xj189 的思考旋钮是 `reasoning_effort`，**省略该字段才是关思考**，
传任何值（含 `none`）都是开——**与平台文档相反**。

档 0（关）：`relevance` / `summarize` / `extract` / `probe_light` / `probe_degraded`
档 1（low）：`assess` / `outline` / `write_section` / `write_revise`
档 2（high）：`probe_outline` / `write_plan` / `write_audit`

### 证据流转

```
网页正文 → 切段（累积到 1000 token，段落边界落刀）
        → chunk（约 700 token）落库
        → extract 阶段写概括（约 38 字）← 写作的证据入口
        → 段级闸门（relevance）筛出 on_topic
        → write 子图：主 agent 规划 → 子 agent 拿证据清单写
                     → 不够时自己调工具往下挖：
                       read_segments（看某段全文）
                       read_context（某段 + 邻段）
                       read_source（整篇正文）
```

**段号方案**：来源按 `rowid` 编 `S1 S2`，段按来源内 `rowid` 编 `S1·5`。
引用写成 `[S1·5]`。`node_write` 与 `tools/evidence.py` **必须用同一套编号**。

---

## 三、代码地图

| 路径 | 是什么 |
|---|---|
| `research_agent/server.py` | **FastAPI 服务**：REST + SSE + 审批。前端第一轮已补齐事件推送（astream）与引用查询 |
| `research_agent/graph/runner.py` | 主图装配（边在这里）+ **两个图实例**：同步 `get_graph()` / 异步 `get_agraph()` |
| `research_agent/graph/nodes.py` | 主图节点：probe/outline/retrieve/extract/assess/apply_revision/write/finalize（**最容易出问题的地方**）|
| `research_agent/graph/state.py` | `ResearchState` / `WriteState`（**漏声明字段会被 LangGraph 静默丢弃**）|
| `research_agent/graph/subgraphs/write.py` | write 子图：规划 / 并发写作 / 审查 / 返修（1,000+ 行）|
| `research_agent/graph/subgraphs/extract_batch.py` | 段落概括 / 来源性质 / 段级闸门 |
| `research_agent/graph/subgraphs/probe.py` | 探针四步 |
| `research_agent/graph/prompts.py` | **全部提示词集中在这里**（改行为先看它，600+ 行）|
| `research_agent/llm/provider.py` | provider 分发 / SSE 流式 / 重试 / 限流分类 / 记账 |
| `research_agent/llm/toolloop.py` | 工具循环 |
| `research_agent/tools/evidence.py` | 三个证据工具 + 段号索引 |
| `research_agent/db/repo.py` | 裸 SQL 薄封装，5 个迁移 |
| `frontend/src/` | **前端（Vite+React+TS）**：`useSession.ts` 是全部状态与 SSE 接入 |
| `config/models.yaml` | **平台与模型配置的唯一真相源** |

代码约 7,000 行，测试约 7,000 行。

---

## 四、怎么跑

```bash
cd "/Users/wd/Documents/Deep Research Agent"

# 完整流程（含 write，约 25 分钟）
.venv/bin/python tests/e2e_run.py "你的问题" /tmp/x.db

# 只到 assess（write 打桩，约 15 分钟）——改规划/检索类先跑这个
.venv/bin/python tests/probe_assess_run.py "你的问题" /tmp/x.db

# 看账
.venv/bin/python tests/cost_report.py /tmp/x.db <session_id>

# 离线自检（不花 API）
.venv/bin/python tests/align_offline.py       # 框架优化循环
.venv/bin/python tests/assess_offline.py      # assess 重写
.venv/bin/python tests/smoke.py              # 冒烟（慢，要下 embedding 模型）

```bash
# 换平台前先预检
.venv/bin/python tests/preflight_xj189.py

# 测子 agent 对新约定的遵守率（改提示词约定后跑它）
.venv/bin/python tests/lead_mark_probe.py
```

**改前端时用回放，别真跑**（25 分钟 + 几十万 token → 几秒 + 零成本）：

```bash
cp /tmp/ra_fe2.db /tmp/ra_replay.db          # 任何跑过研究的库都行
.venv/bin/python tests/replay_server.py --speed 1 --db /tmp/ra_replay.db
# 前端照常连 http://127.0.0.1:5180，点「新建研究」看全过程
```

它**只把「跑图」换成回放录好的事件**，其余全走真代码
（真 FastAPI / 真 SSE / 真 SQLite / 真 EvidenceTools），
所以「mock 通过」基本等于「真跑也通过」。
⚠️ 报告页与引用抽屉要用**左侧已有的真实会话**测——
回放会话没有 chunk，段号取不到东西（见该文件顶部说明）。

**跑 server（前端后端）**：

```bash
cd "/Users/wd/Documents/Deep Research Agent"
set -a; . ./.env; set +a          # ⚠️ uvicorn 不会自己读 .env
RA_DB_PATH=/tmp/ra_fe.db .venv/bin/python -m uvicorn research_agent.server:app \
    --host 127.0.0.1 --port 8000

# 看事件流（另开一个终端）
curl -N http://127.0.0.1:8000/api/stream/<session_id>
```

| 端点 | 干什么 |
|---|---|
| `POST /api/sessions` | 建会话 |
| `POST /api/sessions/{sid}/run` | 启动研究（`?streaming=false` 退回同步路径）|
| `GET /api/sessions/{sid}/approval` | **待审批的框架**（没有就 `waiting:false`）|
| `POST /api/sessions/{sid}/approve` | 审批并续跑 |
| `GET /api/sessions/{sid}` | 报告 + 来源 + 主题 |
| `GET /api/sessions/{sid}/seg/{ref}` | **按段号取原文**（`S1·5`），报告里点引用走它 |
| `GET /api/sessions/{sid}/segments?refs=A,B` | 批量取多段 |

SSE 事件类型：`connected` / `node`（节点到达）/ `progress`（该节点的结构化事件）/
`thinking`（思考增量，**已后端合并**）/ `done` / `error`。

`.env` 四个 key：`TAVILY_API_KEY` / `OPENCODE_API_KEY` / `MINIMAX_API_KEY` /
`XJ189_API_KEY` / `AGNES_API_KEY`。**绝不可回显、不可提交**（已在 `.gitignore`）。

---

## 五、排查一轮研究

```sql
-- 每一轮的判断、检索了什么、为什么停
SELECT round, kind, detail_json FROM run_trace WHERE session_id=? ORDER BY at;

-- 每阶段耗时
SELECT stage, COUNT(*) n, ROUND(SUM(latency_ms)/1000.0) sec
FROM llm_call WHERE session_id=? GROUP BY stage ORDER BY SUM(latency_ms) DESC;

-- 审查历史（含 rework_check 逐条核对结果）
SELECT audit_history_json FROM write_trace WHERE session_id=?;
```

**`report` 表从 R5 起就没更新过**——测试 harness 把报告写文件不写 DB。
查历史报告读 `/tmp/e2e_report_*.md`，**不要查 `report` 表**。

⚠️ **失败的调用（504/429）在 DB 里没有任何记录**，它们是异常不是记账行，
只能从 stdout 数 `⚠️` 行。

---

## 六、已知地雷

**`drafts` 的结构**（R6 改过）：`{task_id: {section_id: 正文}}`。
`node_assemble` / `node_revise` / `_check_all_citations` / `node_final` 都碰它——
改的时候这几处要一起看。

**LangGraph 静默丢弃未声明的 state 字段。**2026-10-09 为此查了整整两轮
（`pending_revision` 没声明 ⇒ 框架校准全程空转，症状却和「模型没给」一模一样）。
**在 `ResearchState` / `WriteState` 里加字段时，先确认它被声明了。**

**`_predict_stop` 是停止判据的唯一真相源。**别再写第二份——
2026-10-10 我复制过一版，导致原函数变死代码、只有测试还在调它。

**子 agent 的标题编号由装配层统一编**（`_renumber_headings`）。
提示词里明写「不要自己编号」**不是多余的**：模型一旦不写编号，
程序就无从判断它原本想编在哪一层。反过来，提示词管不住的
（`##` 自造标题）由程序兜底。改动时**两处都要看**。

**图节点名和 LLM stage 名是两套命名，别混。**
node 是 `probe` / `outline` / `assess` / `write`，
stage 是 `probe_outline` / `outline` / `assess` / `write_plan` / `write_audit` /
`write_section` / `write_revise`（`config/models.yaml` 的 `stages`）。
拿 node 去查 thinking[stage] **永远查不到**——
2026-10-11 前端思考面板就栽在这，表现是面板一直显示
「这一阶段还没有思考内容」。前端有 `NODE_TO_STAGE_PREFIX` 映射表兜着。

**SSE 事件去重不能用索引游标。**
`events` 在 LangGraph 里是累加器，每个节点返回的 delta 都是「迄今全部」。
按索引切增量时，游标会被跨节点的累计长度带偏，**把后续增量整段吞掉**——
症状是「node 事件有 X，但它的事件一条都没推」。
2026-10-12 实测：`retrieve` 恰好是被吞的那个，而它恰是最该展示的。
已改成 `_event_key()` 内容指纹去重（`server.py`）。

**React Hooks 必须无条件执行。**写前端组件时，
`if (!data.length) return null` **不能放在任何 Hook 之后**——
两种情况 Hook 数量不同，React 直接崩（整页白屏 +
`Rendered more hooks than during the previous render`）。
纯计算别包 `useMemo`，包了也是多余的 Hook。踩过。

**`<!-- lead -->` 标记是删引言标题的唯一依据**（2026-10-11）。
子 agent 若想在正文开头写一句概括本节的小标题，必须先打这个标记，
装配时它会被降成正文段落、不进目录也不编号。
**不要改成「程序用相似度判断哪个标题是引言」**——
实测那样 7 个只删对 3 个，而「界面问题的物理本质」这类真子标题会被误删
（它和节标题共享关键词）。判「重复」还是「相关」需要理解语义，
程序做不到，且误删是静默的。

**测试库要建 session。**`write_trace.session_id` 有外键，
`RA_DB_PATH` 指向的库里没有对应 session 时，每次工具使用记录都撞外键失败。
这类失败**不报错**，只是让记录悄悄丢掉。

**`llm_call` 表会混进历史 session。**统计必须带 `where session_id=?`。

⚠️ **端点里要用 `asyncio.create_task`，端点就必须是 `async def`。**
同步端点被 FastAPI 丢进线程池，那里**没有运行中的事件循环**，
`create_task` 直接 `RuntimeError: no running event loop` → 500。
**我第一版只把 `run_session` 改了、`approve` 漏了，结果点了批准就 500**
（同一个错犯两次）。凡是端点里要 create_task，先确认 def 前面有没有 async。

⚠️ **`tests/parallel_check.py` 当前是坏的**（2026-10-11 发现，**在干净的
`bcdb673` 上同样失败，不是最近改动引入的**）。现象是断言
`calls == [len(b) for b in batches]` 失败，而它上面打印的
`各批请求的段数：[]` 是**空列表**——mock 一个请求都没截到，
但同一次运行 120/120 条概括全部产出、失败 0 批。
所以更像是 C1-j 修完 ContextVar 记账之后测试的 mock 没跟上，
而不是并发真的乱序。**跑它之前先修 mock，别把失败当成真 bug 去查代码。**

## 六之二、跑 server 的两个坑（2026-10-11 踩的）

**① uvicorn 不会自动读 `.env`。**
`RA_DB_PATH=... .venv/bin/python -m uvicorn research_agent.server:app`
这样起服务，`XJ189_API_KEY` 是空的，跑到 `probe` 就报
`ConfigError: 未设置 XJ189_API_KEY`。**测试脚本是自己读 `.env` 的，
server 不是**。要么 `set -a; . ./.env; set +a`，要么给 uvicorn 装
`--env-file .env`。**看到「未设置 XXX_API_KEY」先查这里，别去查代码。**

**② `astream` 要单独的异步图，`SqliteSaver` 用不了。**
`SqliteSaver` 只支持同步方法，`astream()` 直接抛
`NotImplementedError: does not support async methods`。
所以 `runner.py` 里有**两个**图：`get_graph()`（同步，5 个测试调用点依赖）
和 `get_agraph()`（异步，server 进度推送用）。两条路径写同一个
`checkpoints.db`，所以能接力。

⚠️ **别把 `AsyncSqliteSaver.from_conn_string()` 的返回值直接传给
`compile()`**——它返回的是异步上下文管理器，不是实例，会报
`Invalid checkpointer provided ... Received _AsyncGeneratorContextManager`。
`get_agraph()` 里手动 `__aenter__` 并把 cm 存成模块级变量持有。

---

## 七、踩过的坑（只留会再踩的）

### 1. 避坑注释写在别处 ≠ 那个坑被填上了

`parallel_labelled` 的 docstring 里明明白白写着「冷缓存 8 并发只 3 命中」，
而 write 阶段压根没调用它、自己起了个 `ThreadPoolExecutor`。
**排查并发问题要看实际调用的是哪个函数，不要读同文件里的警告。**

### 2. 让模型回答一个它答不出来的问题，它就会编一个答案

`rework_effective` 问「上一轮返修有没有用」，而审查环节**从来没拿到上轮的 findings**，
只能笼统自评。实测它一边写「已有明显改善」一边填 `false`——
**字段和它自己的话矛盾，而下游代码采信了 false。**

**遇到需要判断的字段，先问：它做这个判断时手上有依据吗？**
把依据给它，比催它仔细有效。

### 3. 拿现象直接推原因，同一个问题我连猜错六次

查 504 时依次说过「多节点并发跑满」（查时间戳发现主图完全串行）、
「是排队」（拿相关性做的推断）、「降预算就安全」（**算完账发现会截断正文**）、
「拆多次调用」（**没算输入成本，+68k token**）。

**用户三次指出**：定位不清楚别乱猜 / 不考虑是否能执行 / 连原因都不查清。

**可用的做法**：现象 → 列出所有可能 → 设计**能区分它们**的实验 → 跑 → 再下结论。
本次的分界实验是「同参数、流式 vs 非流式」，一次就排除了三个假设。

**相关不等于因果。**「relevance 也 504」当时看着像有力证据，实际是相关性。

### 4. 读不到 ≠ 没有

三次同形状：`thinking.budget_tokens`（参数名猜错）、
`prompt_tokens_details.cached_tokens`（字段在最外层）。
**都是「读不到」被当成了「没有」。**

尤其当同一件事在别处明明存在时。**拿到 0 先把原始报文打出来看一眼。**

### 5. 证据被静默丢弃比报错危险得多

`summarize_segments` 捕获异常后继续，没有概括的段落在
「要有概括才进证据池」里被滤掉。**177 段里 12 段消失，零提示。**

> 凡「过滤掉没有 X 的」，如果 X 由模型生成，
> **静默失败率就等于模型的不稳定率**。这类地方必须有差额统计。

### 6. 提示词里写了 ≠ 系统接得住

`WRITE_PLAN_TASKS` 明确允许「两节耦合时一个任务写两节」，模型照做了，
`node_assemble` 却不支持。表现是「子 agent 把内容写错了」，
实际是这个任务**在系统里根本不可能写对**。

> 审计提示词时逐条问：**模型真这么做了，系统接得住吗？**

### 7. 设计授权了但没实现，会伪装成别的问题

出现过四次：`claims` 字段硬编码空数组、`node_apply_revision` 从没被触发、
`max_tokens` 硬条件读了个恒为 0 的字段、`pending_revision` 没在 state 里声明。

**症状都像「模型没干活」。**改一轮之前先问：**这个机制真的被调用过吗？**

### 8. 自检覆盖不到出问题的地方

write 子图结构自检 7/7 全过，而连续三个 bug 全在 `node_write` 那一层——
**自检恰好绕开了出问题的地方**。

还有一个更隐蔽的：`_predict_stop` 变死代码之后，
`assess_offline.py` 还在测它，**全绿，而它已经不决定任何行为了**。

### 11. 构造的测试用例里，**前提也是你造出来的**

2026-10-11 我想验证「审查抓不抓得出材料不足」，写了个探针：
手工草稿里让一个只分到 2 段的任务写出 6 个批次的数据（数字无出处），
然后跑审查——**三次全部 `blocked=0`**，我据此写下「确认的缺陷」。

**用户当场质疑：会不会是子 agent 调工具在别处找到的？** 去查真实数据：

- `tool_reads = {read_segments: 21}`——子 agent 确实调工具下挖过
- 143 段证据、410,575 字正文，报告里 **17 个数值断言全部能在证据中找到出处**
- 而且子 agent **没有编造的条件**：它手上只有分配给它那几段的概括，
  要写具体数字必须调 `read_segments` 取原文

**结论：「`blocked=0` = 材料确实够」是对的，我被自己的坏探针带偏了。**

> 我的探针草稿是**手写的**，「2 段写出 6 批次」这个前提是我自己造的，
> 然后拿它去证明审查抓不到编造——**先假定结论，再验证它**。

真实回归要**从已有 session 的产物里取**，不要手工构造。
要造测试用例，先问：**这个前提在真实运行里出现过吗？**
没出现过就不能拿它当证据。

### 12. 审查的职责是跨任务一致性，不是逐条核证据

**（用户提出，2026-10-11）** 我当时想给审查补证据正文来核对数字，
用户指出定位偏了：审查该看的是**前后逻辑、一致性、明显矛盾**。

这个判断和代码里的分工本来就自洽：

| | 谁负责 | 为什么 |
|---|---|---|
| 跨任务矛盾 | **审查** | 子 agent 互相看不到对方正文（`node_dispatch` 的最小权限设计），只有审查看得见 |
| 单节证据忠实度 | **子 agent 自己** | 它手上有证据、有工具（`read_segments` / `read_context` / `read_source`）|

让审查代劳核证据，是把子 agent 本职工作往上推，
既撑爆输入又越权。v5 抓到的两个问题——**整节未写**、**硫化锂数据前后矛盾**——
恰好都在审查该管的范畴内，而且都抓到了。

**分工是对的，别动它。**

### 9. 模型不照做，先怀疑措辞把它推到了对立面

子 agent 不按约定输出 `<!-- lead -->` 标记，遵守率只有 **1/3**。
把提示词从「系统看到标记**就会删掉它**」改成
「加上标记它**就会被识别成引言**，不进目录、不编号」——
**同一批用例立刻变成 3/3**（`tests/lead_mark_probe.py` 可复现）。

第一版措辞把标记描述成「你写的东西会被删」，是个损失，
模型本能规避；而「不进目录」对它自己也是好处。

**测遵守率时，一个不够低的数字先别急着判方案不可行——
换个说法再测一遍。** 差的那部分可能不是它不听话，
而是你把话说成了对它不利的形式。

### 10. 真实数据回放比读代码更容易抓 bug

`_renumber_headings` 第一版有四个 bug：`### 0.1`（节序号没拼进去）、
`1.0.1`（计数器偏移一位）、`500强企业排名` 被剥成「强企业排名`
（正则要求标点可选）、引言标记三种落法只认一种。
**读代码时看不出来**，是把 v5 的真实标题灌进去才全暴露的。

而且这四个 bug 里有三个是我「修一个、冒出一个」补出来的——
分支互相抢行，每修一条又漏一种。**最后是重写整个循环、
把四种落法归到一个通道才干净的。**

> 写这类「逐行改写」的程序，拿真实输入回放，
> 并把所有**形态变体**都列出来当用例——别只测一个样本。

### 13. 后端把数据分到了两个端点，前端却只拉一个

`/api/sessions` 硬过滤 `deleted_at IS NULL`，回收站在 `/api/trash`——
这是**有意的**（防「删了还在列表里」）。但前端侧栏拿 `/api/sessions`
那一份在本地过滤出「回收站」视图，于是这一档**永远是空的**。

后端怎么切分数据，前端就得怎么取；**跨端点的语义切换不能只在前端解决**。

> 已经后端 7 场景实测全过、前端渲染也确认「正常」了——
> 因为空列表渲染得毫无破绽。**「看起来对」和「有数据」要分开验。**

### 14. `null` 同时是哨兵值和真实取值

`renaming` 用 `null` 表示「没在改名」，而「未分类」节点的 `id` 也是 `null`。
`renaming === n.id` 在初始状态就成立，那一行被预填着「未分类」的
输入框顶掉了——看起来像「未分类变成可编辑的了」。

凡是**哨兵值和真实取值域重合**的地方（`null` / `0` / `""` / `-1`），
都要单独想一遍：`undefined` 有没有可能混进来？两个含义会不会同时成立？

### 15. 后端给的顺序不一定能直接渲染

`folders_with_counts` 是扁平列表，`ORDER BY f.sort_order, f.created_at`，
而 `sort_order` **按父节点各自编号**——子节点的号可能比父节点小。
实测数据里子节点真的排在了父节点前面，照数组顺序渲染会彻底错位。

同理，依赖**祖先/后代关系**的聚合（子树的会话合计）必须**自底向上**算，
按数组顺序累加会漏掉「子排在父前面」的那部分。

> 后端给扁平列表 + 一个排序键，和「给你一棵能直接渲染的树」不是一回事。
> 嵌套一上来就必须自己按 `parent_id` 组装。

### 16. 量「思考关了没有」要用平台原始响应，不能用自己的钩子

**今天第二次栽在探针上**（第一次是 `blocked` 那个坏探针）。

`reasoning_content` **只在 `SSEAccum`（流式路径）里被捕获**。
非流式的调用**钩子压根不触发**，量出来永远是 0——
看起来像「思考关干净了」，实际什么都没测到。
`llm_call` 表也只记 token，不记思考长度。

正确量法：按 `provider.py` 里 payload 的构造**逐字段复刻一份**，
直连平台读原始响应。而且**必须做反向对照**——
拿一个确实开了思考的 stage 跑同一套量法，证明它能测到。

| stage | 发出的 reasoning_effort | 平台返回 |
|---|---|---|
| `session_summary` | (未发送) | 0 字符，**字段不存在** |
| `write_plan` | `high` | 322 字符，字段存在 |

> 探针报「没问题」的时候，先问一句：**这个量法真能测到问题吗？**

### 17. 相邻两张表的同名列不一定都有

`source` 有 `char_count`，`chunk` **没有**——只有 `token_count`。
按记忆给 `chunk` 写上 `c.char_count` 直接 500。

### 18. 刷新类 bug：先看请求发没发出去，再看数据对不对

阶段 5 修一个「标记排除后详情页不刷新」的问题。第一反应是查后端、
查数据形状——**都正常**。最后是浏览器 network 记录直接指认的：

- 两次 unignore 后面**都**跟着 `GET /api/sources/{id}`
- 最后一次 ignore 后面**只有**列表 GET，详情 GET 压根没发

区分「发了但数据旧」和「压根没发」，两者的排查方向完全相反。
前端任何「操作成功但界面没变」的问题，都值得先这么看一眼。

**根因是两个状态不一致**：`onPane` 切视图时清了 `srcCur` 却没清 `srcDetail`，
而刷新逻辑写的是「按当前选中的那条刷」——它被整段跳过了。

**改法不是补一句清空**，而是把判断依据换掉：**按刚改的那条刷详情，
不要按当前选中的那条**。两者本来就可以不是一回事——
在列表里标记 A、右边打开着 B 是完全正常的操作。

### 19. 查「空转代码」不能只 grep SQL——state 字段没有表

清尾巴时的一个差点漏掉的发现：历史召回那整条链路
（`recalled_chunks` → `recalled_approved` → `earlier` → 报告「认知演进」）
**在任何 SQL 文件里都查不到**，因为 `recalled_chunks` 根本没有对应的表——
它只是 LangGraph state 里的一个字段，从来没人写入。

所以：查「建了表没人用」用 grep SQL；
查「定义了字段没人读」得 grep state 定义和图节点。
**两类空转长得一样，查法不同。**

顺带：`migration` 目录只按**文件名**记进 `schema_migrations`，
**没有 checksum 校验**——所以改历史迁移文件（如 `0001_init.sql`）
对已经跑过它的库零影响，不必为了「历史不可变」而多写一个迁移。

> 写 JOIN 或跨表查询时，列名逐个对着 `.schema` 核，不要按「这张表大概有」
> 去推。今天第八次栽在「字段不能猜」上。

### 20. 拿「可能为 null 的状态」和「可能为 null 的 id」比相等

拖拽排序要给源行加 `opacity-40`，写的是 `dragId === n.id`。
而 `dragId`（没在拖时）和「未分类」那一行的 `id` **都是 `null`** ——
`null === null` 恒真，**「未分类」永远半透明**。

判断 id 是不是「没有值」的写法不同：`dropAt?.id === n.id` 没事，
因为可选链给的是 `undefined`，`undefined === null` 为假。
但 `a === b` 这种裸比较两个都可能是 null 的值就会炸。

⇒ **凡是比较两个可能为 null 的值，先显式排除 null。**

### 21. 会等比缩放的 SVG 里不能放文字

图表 `viewBox` 宽 640，侧栏面板实际只有 368px → 缩放 0.575，
`fontSize=9` 的刻度实际渲染成 **5.2px**，肉眼根本看不清。

文字不跟 viewBox 缩放，所以**不要把文字画在 SVG 里**。
做法：SVG 只画会被缩放的图形（柱、线、网格），
刻度/标签用同一坐标系的 HTML 绝对定位（`top: (yv/H)*100%`），
字号永远是 CSS 像素，跟容器宽度无关。

### 22. 坐标轴标签配反了，只有截图能发现

`gridY` 是**从下往上**排的（值 0 在最底部），标签却按 `[max, max/2, 0]` 配——
画出来是「0 在顶、160 在底」：**柱子朝上长，刻度往下走**。

这个错误在 DOM 里完全看不出来：柱子的 y 坐标是对的、标签文字也在、
`aria-label` 也对，只有把图看下来才发现两者对不上。

⇒ **凡是「几何 + 标注」两套坐标的东西（图表、比例尺、时间轴），
光核对数据不够，必须看一眼渲染结果。**

### 23. `overflow:hidden` 不是滚动锁，探针量到的「还能滚」是这么来的

P2 给段落抽屉加背景滚动锁，容器从 `overflow-auto` 切成 `overflow-hidden`。
用浏览器探针去滚那个容器，回报是 **`moved: true, actualDeltaY: 262`**。

**不是锁失效了。** `overflow:hidden` 挡的是**用户滚动**（滚轮/触控板），
元素仍然是一个 scroll container，`scrollTop` / `scrollIntoView` 这类
**程序化滚动照样能推动它**——探针正是走程序化那条路，所以量到了位移。

⇒ 断言「锁住了」之前先问**这个量法测的是什么**。
真要挡程序化滚动，得在滚动事件里把 `scrollTop` 写回去，
那是另一回事（也更少人需要）。

顺带记一条实测：**遮罩点击能验证命中测试**。抽屉打开时点报告区以外的
坐标能关掉抽屉，说明 `fixed inset-0` 那层确实吃到了指针事件——
于是滚轮也会打在它身上（它不可滚动、祖先也不可滚动），
「背景滚不动」在鼠标路径上是成立的。
**没验成的是键盘那条**：抽屉开着时焦点还在报告里的引用按钮上，
按 PageDown 会不会从背后滚动，我这次没能测到（点开抽屉后回车没再触发
`onClick`，焦点不在按钮上，那一步等于没测）。
要补就是「打开抽屉时把焦点移进抽屉」——那是焦点管理的方案问题，不是 bug，先不动。

### 24. SSE 的外层字段被前端丢掉 → 环节归类跟着「当前节点」漂移

2026-10-14 修掉的 bug。服务端发的是 `{kind:"progress", node:<图节点>, event:e}`
（`server.py:832`），**`node` 在外层**，而 `useSession` 只存了内层 `ev.event`。
`buildPhases` 里那句 `/write|finalize/.test(e.node) || currentNode === 'write'`
于是恒为 false，归类退化成「渲染那一刻正在跑哪个节点」——
**写作阶段整个「规划」环节消失，一到 finalize 又全部弹回规划**（v5 里写作占 58%）。

⇒ **SSE 消息是嵌套的：外层是传输元数据（node / kind），内层是业务事件。
拆包时要把整个外层一起留着**，别顺手 `.push(ev.event)` 就完事。
前端类型 `SseEvent` 本来就写对了（`{kind:'progress', node, event}`），
**类型写对了不等于用对了**——用的时候还是只取了内层。

**这个 bug 为什么能活这么久**：它长得像对的。
`phaseOf()` 里有注释专门解释「`summary` 必须跟 `finalize` 同属收尾」，
但那个分支**永远走不到**，因为调用方从没传过真实 node。
⇒ **代码里的防护性注释也要验证它真的会执行**，别只读注释就放心。

### 25. 回放服务只测得到审批为止，写作阶段根本测不到

`tests/replay_server.py` 的 `FakeGraph` **永远返回审批中断**，
`/api/sessions/{id}/approval` 恒为 `waiting: true`。于是批准之后回放确实
把 after-approval 的事件发出去了，但前端在下一次 `loadApproval` 时又被
翻回审批页——**实测点了「通过，开始写作」，界面纹丝不动**。

后果：**工作流视图的写作阶段从来没被前端测过**，坑 24 那个 bug 自然活到现在。

⚠️ 录制数据本身没问题（`run_after_approval.json` 里 7 条 progress 全带 `node`），
是**回放器的图桩不支持「审批之后」这个状态**。

⇒ 真要测写作阶段，要么给 `FakeGraph` 加一个「审批已放行」的开关，
要么单独录一段 after-approval 的事件直接喂进去。
在那之前，**工作流视图的验证只能靠函数级探针**（`buildPhases` 是纯函数，
esbuild 打个包就能在 node 里跑真实数据）。

### 26. Tailwind 里 class 的书写顺序不决定胜负

`className="rpt-table text-2xs"` 想压过 `.rpt-table` 自带的 `text-sm`，**压不住**。

Tailwind 的工具类全在**同一个 layer**，同层之间按**生成顺序**决胜，
**不看 class 属性里的书写顺序**。实测产物 CSS 里
`.text-sm` 在偏移 15743、`.text-2xs` 在 15538 → `text-sm` 赢，
写在后面的 `text-2xs` 是**死代码，而且不报任何错**。

⇒ 要压过默认档只有两条路：
① 用**不进 layer 的自定义类**做复合选择器（`.rpt-table.is-compact`，0,2:0），
   这也是 `index.css` 里那些类的正确用法；
② 改 `tailwind.config.js`，让那一档本身更小。

⚠️ 推论：**不进 layer 的自定义类会压过整个 utilities layer**。
`index.css` 里 `.sel` / `.rpt-table` / `.wf-*` 要当「基础规则」写，
别拿它们当工具类使（那会让里面的 `@apply` 永远赢过组件上的工具类）。

### 27. Vite dev server 会给陈旧 CSS，而且**不报错**

2026-10-14 验收报告区时，我一度认定「`text-2xs` 全项目都没生效」，
为此来回改口两次——**两次都不对**。真因是 dev server 还在吐旧 CSS，
`npx vite --force` 重启后立刻恢复（侧栏 meta 行 24px→16px，
`3 天前` 46px→32px，都是 `query(kind="semantic")` 的 `rect`）。

⇒ **改了 `tailwind.config.js`、或怀疑某个工具类没生效时，
先 `vite --force` 再决定要不要查代码。** 一个不报错的 dev server 不是证据。

### 28. 内置 Browser 的三个测量陷阱（都让我下错过结论）

① **截图是裁剪 + 缩放过的，不能拿像素量字号。**
那张 1048px 宽的截图对应的其实是 535 CSS px（viewport 是 728），
我按它算出来的字号偏了一倍，还据此怀疑 `text-2xs` 坏了。
**判断字号只能看 `query(kind="semantic")` 的 `rect`**：
改 CSS 前后 rect 会变（24→16），这才算数。

② **`rect` 在 CSS 生效前是旧值。** 连续读几次会拿到同样过期的数字，
改完样式要 reload 再读。

③ **`scroll` 的选择器会命中错误的容器。**
`.overflow-auto` 全项目有 6 处，第一个命中的是**侧栏**
（`Sidebar.tsx:347`），不是报告容器（`App.tsx:282`）——
滚了半天报告纹丝不动，还以为滚动坏了。报告容器要用 `main > .overflow-auto`。

⚠️ 另：内置 Browser 的 **FilePanel 宽度会变**。写这条时是 728 CSS px，
报告正文可见区被常驻条（进度条 + 目录）压到 ~120px，4 列的表没法验收。
**2026-10-15 实测已到 1112px**，「4 列宽屏观感」这条遗留因此可以结掉 ——
但结论是反的，见下：

> **来源库根本不在主区，在侧栏**（固定 ~287px / 正文 227px）。
> 视口从 728 变到 1112 对它**零影响**，宽屏根本不是它的变量。
> `rect` 实测确认行仍是 63px、元信息行 16px 单行、域名 147px，
> 和窄屏时一模一样。**先确认面板挂在哪，再决定要不要换窗口。**
>
> ⚠️ 顺带又踩了一次同一条：从 JPEG 截图上「看」出行距像 65/61 两种抖动，
> `rect` 一量全是 63px 统一 —— 又是压缩伪影。**截图像素一律不算数。**

### 29. 自己写的探针也会静默失败：`<link href="./">`

`frontend/probes/wf_view.tsx` 原来是**从输出目录里已存在的 `.css` 反推文件名**
去拼 `<link rel="stylesheet" href="./${css}">`。第一次跑时目录是空的，
于是静默生成 `href="./"` —— 指向目录而不是 CSS 文件，
整页**完全无样式**，**不报错、不空白**，只是「看起来怪朴素的」，
差点被我当成「排版没生效」去找别的原因。

⇒ 改成从 `dist` 取名，找不到直接 `throw`。
教训和 26/27 同源：**静默失败比崩溃贵得多**。
探针是拿来当证据的，探针自己出错时会把错误结论一起带出来。

⚠️ 配套：`wf_view.tsx` 必须用 `--format=cjs` 打包（esm 下
`react-dom/server` 报 `Dynamic require of "util" is not supported`），
路径用 `process.cwd()` 拼（cjs 下 `import.meta.url` 为空）。

### 30. 同一个下标可以有两套含义——来源性质判定整批静默丢弃

2026-10-14 美化来源库时发现：41 个来源只有 20 个有 `source_nature`。
按入库顺序看得很干净——**每轮的第 0 批全有，其余全 NULL**。

根因是 `_render_nature` 渲染给模型看的下标是 **全局**位置（`items` 里的），
`_run_nature_batch` 校验时却拿它比 `len(batch)`（**切片**长度，=8）。
第 2 批起必然越界 → `continue` 整批丢弃，不抛异常、不计数、不进 `failed_batches`。

⇒ **提示词里的下标和校验用的下标必须是同一套。** 修法不是在校验处打补丁，
是让两者都按**批内**编号。

⚠️ 配套的两个坑：
- `extract_all` 里 `out.update(sum_out)` 会**覆盖** `events`。
  给子图事件加的字段到不了前端，得接到 `nodes.py` 真正被消费的那个事件上。
- `state.py` 漏声明字段会被 **LangGraph 静默丢弃**（已记在「最容易出问题的地方」那张表里）。

⇒ **端到端也验过了**（2026-10-15，`tests/verify_nature_e2e.py`）：真节点真 LLM
真落库，只不往下接写作。12 个来源 → 2 批 → **12/12 全部判出，空的 0 个**，
第 9~12 条（原来的第 2 批）都有性质和判定理由。58k token / 7.4 分钟。
真实事件存进 `tests/fixtures/verify_nature_e2e.json`，是工作流探针的第 ④ 个用例。

⚠️ **`NULL` 和 `'unknown'` 是两回事**：NULL = 代码把它丢了（真 bug），
`'unknown'` = 模型看了材料判不出来（合法结论）。这次修复把这个区分恢复了
（`it.get("source_nature", "unknown")` 改成 `or "unknown"`）。
**写验收脚本时别把两者混在一起当失败**——我第一版就混了，
把 3 个「模型判不出来」误报成「代码又丢了」，差点去改一个没坏的东西。

⇒ 探针 `tests/probe_nature_batch.py`：把 LLM 换成**完美桩**（下标全对、性质全合法），
模型侧不可能出错，此时的缺口就一定是代码的锅。**验收静默失败，先证明不是模型的锅。**

⚠️ 配套的「假未判」：`nature` 为空会让来源库把它归到「未判」组，
界面上看起来像模型判不出来，**实际是代码丢的**。已写
`tests/backfill_source_nature.py` 回填（21/21 补齐，两份库都已跑）。

### 31. 内联 `onClose` 进 effect 依赖 → 焦点归还静默失效（修了一处、漏了一处）

`useDialog` 的注释里写着「`onCancel` 必须走 ref、依赖只能是 `[enabled]`」。
**但 `Menu.tsx` 有一模一样的毛病，没人记得去改它。**

`Menu` 的 effect 依赖是 `[onClose, anchor]`，而 `onClose` 全是从父组件传进来的
内联箭头（`() => setMenuAt(null)`）——侧栏一直在轮询，**每次重渲染都是新函数**：

```
effect 第 N 次跑 → before = document.activeElement
                  ↑ 此时焦点已经在菜单里 → before 被改写成「菜单项」
Esc → 菜单卸载 → giveBack() 往一个【已脱离 DOM】的节点调 focus()
     → 静默无效 → parentElement 兜底链也断了 → 焦点掉回 <body>
```

**症状极像「没修」**：菜单关得好好的，就是焦点没了，没有任何报错。
而且它在**鼠标打开**时也可能不发作（鼠标还悬着 → `group-hover` 让按钮可见）。

⇒ 修法：`onClose` 走 ref 且**不进依赖**；`before` / `byKeyboard` 改成 ref，
只在**第一次** effect 跑时记（`grabbed` 哨兵），这样重跑也改写不了。

⚠️ **这次的教训不是「要用 ref」，是「同一类 bug 要连着搜一遍」。**
我在 `useDialog` 上刚踩过并写进了注释，转身写 `Menu` 又踩了一次。
**注释只能提醒读到它的人，不能提醒没读到这里的人。** 凡是处理
「打开时记焦点 / 事件监听 / 订阅」的 effect，先 grep 一遍同类组件对不对齐。

浏览器实测（两处用法都过）：
- 文件夹菜单：键盘打开 → 等 6 秒（跨过多轮轮询重渲染）→ ↓ 移动仍在原位
  （证明 effect 没重跑抢焦点）→ Esc → 焦点回到 `⋯`
- 会话行「移到文件夹」菜单（`align="right"`，同样的内联 onClose）：同上
- 方向键会**跳过禁用项**（index 0 时「上移」禁用被跳过、「下移」可用）
- 「上移」「下移」各点一次，`sort_order` 与侧栏顺序都真的变了（已还原）

### 32. 焦点归还的兜底分支浏览器里验不到——别假装验了

`giveBack()` 里有个兜底：文件夹那个 `⋯` 是 `hidden` + `group-focus-within`，
**键盘打开菜单时 Esc 那一刻焦点已在菜单里、按钮重新变 `display:none`**，
`focus()` 对不可见元素是静默无效的，所以要退到同行第一个可聚焦元素。

这条分支**在浏览器里验不到**：鼠标只要还悬在行上，`group-hover` 就一直让它可见，
主路径先成功了；把鼠标移开又会触发「点外面关闭」，那本来就是另一条路径。

⚠️ 这跟坑 11（`all([])` 恒为真）是同一类：
**测不到 ≠ 通过。** 说「兜底分支验过了」就是撒谎，只能说
「主路径实测通过，兜底是代码层面的防御，未实测」。

### 33. 自己写的探针也会静默失败（坑 29 的变种）：SSE 解析手搓

2026-10-15 查 write_plan 空返回时写了个复现探针，里面手搓了流式解析：
`if not line.startswith("data: ")`。

**xj189 发的行是 `data:{...}` —— 冒号后没有空格。**
于是每一行都被跳过、content 恒为 0，测出「5/5 必空、reasoning 也是 0」，
我据此断言「不是 thinking 吃光预算」并写进了结论。

**生产的 `_sse_lines` 是对的**（`startswith("data:")` + `line[5:]`）。
坏的只有探针 —— 但因为它"测得很像那么回事"，我基于它连错判了两次。

⇒ **探针里的解析必须 import 生产的那个，不要重写。**
已这么改：`tests/probe_plan_tasks_empty.py`、`tests/probe_plan_tasks_ablation.py`。

⚠️ **探针写完先跑一个"一定该成功"的对照**（极简提示词），
确认解析器能出东西，再去解读正式结果。否则你测的是解析器，不是被测对象。

### 34. n=1 的「趋势」是噪声，不是信号

上一个坑的后半段。测「预算 4,096 / 8,192 / 16,384 对思考量的影响」时
**每档只跑一次**，得到 3,433 → 10,957 → 23,656 字，
我读成「预算越大思考越多、正文越少」，据此说"加大预算更糟"。

**同一预算下思考量能差 4.9 倍**（16,384 连打三次：8,593 / 11,401 / 41,771 字）——
我读的是噪声带。真实成功率：4,096 → 1/14（~7%），16,384 → 2/3（~67%）。

⚠️ **任何"随 X 变化"的结论：至少 n≥3，且先看组内方差。**
组内离散度比组间差异大的时候，组间差异什么也说明不了。

（用户 2026-10-15 的归谬最干净：**如果 `max_tokens` 能影响思考行为本身，
那 `reasoning_effort` 这个参数就没有存在意义了。**）

### 35. 默认值按「多数」定，会系统性坑掉「少数」

`max_output_tokens: 4096` 是按「多数环节输出很小」定的（12 个环节里 8 个用不到 800）。
但**需要真思考的那几个正好不够用**：

| 环节 | effort | 上限 | v5 实测最大 | 占比 |
|---|---|---|---|---|
| write_plan | high | 4,096 | 7,654 | **187%** |
| assess | low | 4,096 | 5,593 | **137%** |
| write_section | low | 4,096 | 5,035 | **123%** |
| probe_outline | high | 4,096 | 3,966 | 97% |
| **write_audit** | high | **8,192** | 5,025 | 61% ✅ 从没失败 |

`write_audit` 是现成对照组：**唯一给足预算的高思考环节，也是唯一一次都没失败的**。

⚠️ 关键点：**「空返回就翻倍预算」被当成了日常路径**，
而那个 187% 说明它**一直在靠翻倍硬撑**。翻倍应该是意外兜底 ——
**次次都要触发，就该一开始设够。**

⇒ 防复发：`tests/check_token_budget.py` 从 `llm_call` 直接算
「实测最大输出 / 配置上限」，超 100% 报 🔴、超 70% 报 ⚠️。
⚠️ 它**只统计 `status='ok'`** —— 空返回的行输出是 0，混进来会把水位压低、
造成假的安全感，而那正是它要抓的东西。

### 36. 删除只做了一半：提示词还在承诺已经没有的功能

2026-10-15 查 write_plan 空返回时发现的三处**互相矛盾**的残留：

| 地方 | 状态 |
|---|---|
| `prompts.WRITE_PLAN_TASKS` 第 378 行 | **"可以调用工具取原文——取完再继续判断"** |
| `node_plan_tasks` 的实现 | `complete_json()`，**一个工具都不传** |
| `MAX_TOOL_ROUNDS = 3` / `MAX_TOOL_INDICES = 30` | 定义了，**零使用点** |

docstring 说工具循环是"有意删掉"的。**但那次删除只做了代码那一半**：
常量没删、提示词没改。模型于是被持续教着去做一件它做不到的事。

⚠️ **这和坑 27 删掉的 `keep_last` 是同一类**：
删除时必须把「承诺了这个代码」的地方一起清掉——
常量、提示词、注释、docstring 都是承诺。

⇒ **改回去**：`node_plan_tasks` 加回 `run_tool_loop(max_rounds=MAX_TOOL_ROUNDS)`，
失败退回单次调用；`plan_tool_reads` 记进 `write_trace`。
离线桩测 `tests/probe_plan_tasks_toolloop.py` 三条路径全通。

⚠️ **"提示词承诺的能力被模型当成失败原因"这个假设仍然没有验证**：
`SSEAccum` 把 `content` 和 `tool_calls` 分流、`text_only()` 只返回 content，
理论上模型若真去调工具会被静默丢弃、进而被判成空返回——
但探针没跑完，xj189 的 443 端口就 ConnectTimeout 了。**别当结论用。**

### 37. 平台挂了 vs 代码坏了：先分层定位，别直接重试

2026-10-15 深夜探针跑到一半开始 ConnectTimeout。分层诊断：

| 检查 | 结果 |
|---|---|
| DNS 解析 | ✅ 正常（202.107.178.138） |
| **TCP 443** | ❌ **超时** |
| HTTPS 握手 | ❌ ConnectTimeout |
| 对照：另一个 HTTPS 站点 | ✅ 404，0.39s |

⇒ **平台侧不可达**，不是本地网络。
用户 2026-10-16 确认：**平台当时在更新服务，他自己的网页也登不上** ——
所以不是我调用过密触发的限流，是平台故障。

⚠️ **归因要分清"我做了什么"和"它怎么了"。** 我第一反应是"几十次调用把它打限流了"，
听起来合理但**没有证据**；用户一句话就推翻了。**记录原因时别把猜测写成结论。**

⚠️ **诊断顺序：DNS → TCP → HTTPS → 对照站点**。
不定位就重试，只会把「平台挂了」误记成「代码又坏了」，
然后照着一个不存在的 bug 去改代码。

### 38. 本机有 TUN fake-ip：DNS/TCP 层诊断在这台机器上不可信

坑 37 那套分层诊断有个前提，**在你自己这台机器上不成立**。

**现象**：`socket.gethostbyname("ai.xj.189.cn")` 返回 `198.18.0.7`，
这是 RFC 2544 测试段（Clash/Surge 的 fake-ip 池），**不是真实服务器地址**。
`dig @8.8.8.8` 和 `dig @223.5.5.5` **也返回同一个假 IP** ——
TUN 在网络层改写 DNS 报文，指定公共 DNS 服务器没用。

**后果**（我自己踩了）：拿 fake-IP 去 `create_connection`，
**0.0s 就"连上了"**，于是得出"TCP 恢复、平台在变好"的结论。
实际上连的是本地代理栈，跟平台一点关系都没有。
真实 IP 只能从**加密 DoH** 取（阿里/腾讯/Cloudflare 三家一致：
`202.107.178.138`）。

**规则**：
1. 这台机器上判断远端服务，**只信两个信号** —— TLS/HTTPS 是否成功、
   同一条路上的**对照站点**是否正常。DNS 和 TCP 都不能采信。
2. **`0.0s` 的连接耗时是可疑信号**，不是"网络好"。
3. 探测脚本里解析出来的 IP **先确认是不是 198.18.0.0/16**，是就当没解析出来。
4. 现成脚本：`/tmp/check_platform_layers.py`（DoH 取真实 IP 后再分层测）。

**这次的实际结论**：用真实 IP 重测（09:28），
TLS 握手 20s 超时、对照站点 HTTP 200 ⇒ **平台和昨晚是同一个形态，没恢复**。

### 39. 换平台：`reasoning_effort` 的语义是**反的**，不能照抄

2026-10-16 换到中国信通院 discovery-api。同一个模型
（`deepseek-v4-flash-0731`，xj189 提供的那个），**effort 语义完全相反**：

| | xj189 | internai |
|---|---|---|
| 省略 | **关**思考 | **开**思考 |
| `none` | **开**思考 | **关**思考（n=3 全 0、方差为零） |

**后果**：配置里 7 个环节**故意省略** effort 来关思考
（probe_light / probe_degraded / extract / relevance / summarize /
dedupe / session_summary），直接换 base_url 会把它们**全部反向打开**。
已全部显式补 `reasoning_effort: none`。

⚠️ **教训：同一个模型 id ≠ 同一个行为。** 聚合平台会在中间改语义，
**换平台必须重测旋钮，不能沿用上一个平台的结论**。
`config/models.yaml` 里 xj189 档案那段「省略即关思考」的注释，
在 internai 档案下是**错的** —— 所以每个档案各写各的实测结论。

⚠️ **另**：internai 上 low/medium/high 在简单题上分不出差别
（中位 58~70 字，组内范围完全交叠）。**但这不能推出「effort 无效」** ——
测试题思考只用 60 字，是天花板效应；xj189 当初也是这么误判的。
**档位有没有用要在 write_plan 那种真需要规划的活上实测。**

### 40. 预检通过 ≠ 生产能用（第二次形态）

internai 预检 9 项全过，「工具调用」流式非流式都拿到了 `tool_call`。
但走生产 `complete_with_tools` 的第一次冒烟，**拿到 0 个 tool_use**。

`to_openai_tools` 的 docstring 里记过同款坑（预检手搓格式，
测的不是项目真正会发的）。这次不一样，但仍栽在同一处。

**定位方法：逐变量隔离。** 从预检形态逐个加生产才有的东西
（temperature / reasoning_effort / stream / system 消息），
A~F 六组**全过** ⇒ payload 没问题。再测生产路径真实调用率 **10/10**。

⚠️ **那一次的 0 个成因不明**，payload 一致、变量全隔离、
后续 10 次全正常。`complete_with_tools` **故意不加 `tool_choice`**
（它要的就是「模型自己决定要不要读原文」），所以「没调工具」
**可能合法** —— 但我没有证据断定它合法，记为未解释。

**规则**：预检只用来发现能力边界，**验收必须走 `LLMClient`**。
探针脚本 `tests/smoke_intern_ai.py` 才是有效的验收。

### 41. 🔴 工具循环第二轮 400：Anthropic 对话被直接塞进 OpenAI 端点

2026-10-17 全量复跑实测，`write_section` **10 次里 5 次** 400：

```
internai 400 upstream_request_rejected
  "4 validation errors for ValidatorIterator
   0.ChatCompletionContentPartTextParam.text  Field required
   [input_value={'id': 'chatcmpl-tool-a03…', 'type': 'tool_use'}]"
```

`run_tool_loop` 按 Anthropic 协议攒对话（assistant 带 `tool_use` 块、
user 带 `tool_result` 块），`_opencode_tools` 之前**原样透传**。
**xj189 不校验内容块形状**，所以一直藏着；信通院严格校验就炸。

⚠️ **症状完全静默**：400 被 `_run_task` 的兜底吃掉，降级成单次调用、不报错 ——
「子 agent 拿不到证据全文」没人知道。5/10 降级率意味着
**报告有一半小节是在没读过原文的情况下写的**。

修法见 `_anthropic_msgs_to_openai()`，三个要点缺一个还是 400：
`tool_use` → `tool_calls[]` 且 `arguments` 要 JSON **字符串**；
`tool_result` → **独立的** `role:"tool"` 消息；
只有 tool_use 没正文时 `content` 必须是 **`None`** 而不是 `""`。

⚠️ **预检为什么没发现**：只测了**第一轮**（模型发工具调用），
第二轮（我们回传结果）根本没跑。
**凡是涉及多轮协议的，预检必须自己把对话拼出来跑第二轮。**
守门测试 `tests/test_toolloop_protocol.py`（离线 8 种形状 + **在线真跑两轮**）。

### 42. 截断被 `_parse_json` 抢救成成功，静默丢字段

`probe_outline` 输出 **8,192 = 上限（100%）**，四组信号只交三组：
`subtopics` 5 条正常，`entities` / `jargon` / `controversies` **全是 0**，
而 `degraded: false`、`quality: substantive`。

guard 原来只判 `if not txt`（空），而 `_parse_json` 会把写了一半的 JSON
**抢救成合法对象** ⇒ 缺字段的结果被当成正常返回。

⚠️ **这一类**：只要某环节顶到上限，任何 JSON 环节都可能静默丢字段。
xj189 上被掩盖（那个环节最大输出 3,966，从没撞过 8,192）。

已修：`SSEAccum` 记 `finish_reason`；新增 `_is_truncated()`
（主信号 `finish_reason=="length"`，兜底 `completion_tokens >= budget`）；
`_opencode` 流式非流式两条路都接上，与空返回共用翻倍逻辑。
`probe_outline` 8,192 → 16,384。
**第 2 个信号是推断不是事实**，所以只在「正好顶到上限」时触发。

### 43. YAML 里裸 `off` 是布尔 —— 自己设计语义值时踩的

给「关思考」设计平台中性的语义值 `off`，测试立刻报 7 个环节全是 `False`。

**YAML 1.1 把裸 `off` / `on` / `yes` / `no` 解析成布尔。** 链条：
`== "off"` 判不成立 → 翻译不发生 → `if cfg.reasoning_effort:` 是假 →
payload 不带该字段 → **平台按默认开思考**。不报错，静默多烧 token。

改用 `disabled`，并在 `validate()` 加形状校验：
非字符串直接报错（点名「多半是 YAML 布尔」），未知档位也报错。

⚠️ **规则：新增任何「关键字」配置值前，先确认它不是 YAML 1.1 的布尔/空值字面量**
（`y|Y|yes|Yes|YES|n|N|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF`）。
**这类坑必须让它响，不能让它安静地走默认值。**

### 44. 换平台是**改一行**，不是改 14 行

`ModelRegistry.stage()` 本来就是 `s.get("provider") or d.get("provider")` ——
**单点机制一直都在**，只是 14 个环节各自硬写 `provider:` 把它绕过了。
现在环节一律不写，由 `defaults.provider` 决定。

⚠️ **光单点化不够**：那 7 个「关思考」环节在两个平台写法**正好相反**
（internai 发 `none` / xj189 不发字段）。写死字面量的话，
切回 xj189 会让它们**反向打开思考**（占全部调用的 68%），不报错只烧钱。

所以环节写 `disabled`，**由档案声明怎么关**
（`internai.effort_off: none`；xj189 不声明 = 不发字段），
翻译收口在 `ModelRegistry.stage()` **一处**。
守门测试 `tests/test_platform_switch.py` **真跑两遍注册表**断言行为，不看配置。

### 45. 标题编号在一节内重复 —— 事件里看不出来，只能看产物

2026-10-17 全量跑的产物：

```
sec1: ### 1.1 … 1.8   然后又是 ### 1.1 … 1.5     ← 重复
sec2: ### 2.1 … 2.10  然后又是 ### 2.1 … 2.6     ← 重复
```

⚠️ 事件里 `headings_renumbered: 37` **显示重排确实跑了** ——
所以**从事件计数上看不出这个问题，只有读产物才看得见**。

根因：一节可以由**多个任务**拼成，而 `_section_body` 对每个任务各调一次
`_renumber_headings`，每次新建计数器 ⇒ 每个任务都从 `节.1` 重新数。
修法：调用方按节持有计数器、逐任务传进同一个函数。

写测试时又抓到**第二个缺陷**：只把第 0 位换成新节序号的话，
上一节的层级计数会被带过来（`[1,2,1]` 换节变 `[2,2,1]`，编出 `2.3`）。
**换节必须整份重置。** 真实调用点每节新建 list 碰不到，但那是靠调用方自觉的坑。

### 46. 加了新功能要记得更新测试桩 —— 症状极隐蔽

`tests/write_struct.py` 场景 2（返修路径）在 2026-10-17 动手前**就已经是失败的**。
根因在更早：`39b26f6` 给 `node_plan_tasks` 加回了 `run_tool_loop`，
而测试的 `Fake` 只在 `complete_json` 里认 `write_plan` → 分派拿到子 agent 的
正文垃圾 → `write_plan_error` → `by_task` 空 → `targets` 空 →
`need_rework` 恒 False → **整条返修路径测不到**。

⚠️ **隐蔽在于：场景 1（不返修）照样过，只有场景 2 才炸。**
一个桩没跟上，等于把一整条分支的护栏悄悄摘了。

**排查手法**：改动前先 `git stash` 跑一遍，确认失败**不是自己引入的**，
再往更早的提交追根因。

### 47. 🔴 桩的形参对不上调用点，测试会**假通过**

④（给 `write_plan_tasks` 挂强制工具交卷）给 `run_tool_loop` 加了
`final_tool=` 形参。`tests/probe_plan_tasks_toolloop.py` 里的
`fake_loop` 没跟着加 —— 调用点抛 `TypeError`，
**被 `node_plan_tasks` 自己的 `try/except` 接住、退回单次调用、
然后照样打印「✅ 走工具循环」**。

比坑 46 更阴一层：坑 46 是桩**认不出**新 stage，分支被静默跳过；
坑 47 是桩**根本调不到**，异常被生产代码的容错吃掉，
而容错本来就该有（分派不是非走工具循环不可）。

**两条手法**：

1. 桩的形参必须**逐字抄**调用点，包括新增的关键字参数。
2. 光断言「结果好看」不够 —— 要断言**真的走了哪条路**
   （`tool_choice` 的取值、调用次数、传进去的工具名）。
   本轮 `tests/test_plan_tasks_forced.py` 场景 ⑪ 就是干这个的：
   断言 `final_tool` 传下去了、且任务**真的落下来了**
   （段号和小节 id 都对，而不是「有东西」）。

⚠️ 同理，自己写的探针也可能假通过：本轮 `probe_plan_tasks_forced.py`
第一版把 `segment` **先过滤再编号**，而生产是**先编号再过滤**——
段号会整体错位，分派出来的 `S3·5` 指向另一段。**先读生产代码的顺序再抄。**

### 48. 🔴 空 `tools` 数组会被信通院 400（xj189 一直没校验）

```
internai 400 upstream_request_rejected
`tools` must not be an empty array. Either provide at least one tool
or omit the field entirely.
```

`run_tool_loop` 的最后一轮按设计就是「不带工具」，`_opencode_tools`
就把空数组原样发了。**这个洞从 2026-10-15 工具循环加回来起就一直躺着**
—— 与坑 41（第二轮 400）同一形态：**平台的宽松把协议错误盖住了**。

修法两处：

1. `_opencode_tools` / `_minimax_tools`：空列表时**不发 `tools` 字段**。
2. `run_tool_loop`：有 `final_tool` 时最后一轮改成**只给交卷工具**。

第 2 条顺带治了一个更阴的毛病：提示词写着「调用 `submit_tasks` 交卷」，
而最后一轮手上没这个工具 → 模型**凭空编一个同名 `tool_call`**。
实测对照组里真发生了（`tool_calls=['submit_tasks']`，列表里根本没有它）。

⚠️ 写测试时我自己也踩了一次：传 `FINAL` 而不是 `[FINAL]`，
迭代 dict 得到的是**键**，`to_openai_tools` 全跳过 → 又变回 `tools: []`。
**测试造出来的报文形状和线上那个 400 一模一样**，差点以为是代码没修。

### 49. A/B 对照只改一半，比没有对照更糟

给「改造前」当对照组时，**只摘了代码没换提示词** ——
新提示词写着「调用 `submit_tasks` 交卷」，代码却不提供它，
于是对照组模型开始编造同名工具调用。

它会给出一个**看起来有结论、其实两边都不是要比的东西**的数字。
凡是改了提示词的改动，对照组要**连提示词一起还原**（`probe_plan_tasks_forced.old_prompt()`）。

### 50. `complete_with_tools` 强制通路的三处「不许假装」

`tool_choice` 传工具名 = 平台必须调它。三个地方必须显式失败而不是降级：

| 情况 | 行为 | 理由 |
|---|---|---|
| `tool_choice` 名字不在 `tools` 里 | `ConfigError`（附现有工具名） | 名字拼错会静默变成普通调用 |
| `arguments` 被 `max_tokens` 截断 | `EmptyOutput`（经 `_reject_broken_args`） | `{}` 会伪装成「成功的空结果」——这正是 `write_plan_tasks` 当年 `tasks=0` 的病根 |

⚠️ **这一行已作废**：原来写着「MiniMax provider → `ConfigError`，没接
`tool_choice`」。实测 MiniMax **认**这个字段，已撤销拦截并接上强制。
（为什么会误判：`run_tool_loop` 那次我把两个变量混在一起测了 ——
见坑 52。）

⚠️ 流式与非流式**必须对称**。`_openai_compat_forced` 的流式分支早有
破参数标记，非流式分支没有 —— 同一条路两种待遇，这类洞只有在
逐分支核对时才会发现。

⚠️ 还有第四处：**桩要接在正确的方法上**。`write_plan_tasks` 是
`stream: true`，走 `.stream()` 而不是 `.post()`；第一版只桩了 `.post`，
于是测试**真的发了一次请求**（拿到 401）。桩没接对位置时，测试会
悄悄变成一次真实调用 —— 比不写桩更危险。

### 51. 换 provider 时，整段防线跟着协议形状一起漏掉

`_is_truncated`（截断检测）只读 `finish_reason == "length"` 和
`usage["completion_tokens"]` —— **两个都是 OpenAI 形状**。
Anthropic 形状的对应物是 `stop_reason == "max_tokens"` 和 `output_tokens`。
而且 `_minimax` 压根没调它，原来只判 `if not text`。

⇒ 切到 minimax 之后，被 `max_tokens` 切断的正文**当成成功返回**：
   `degraded: false`，不报错，不重试，报告静默少一截。
   `_openai_compat` 里这道防线是有的 —— **它不是漏写，是漏搬**。

同一个洞的另一个样子在同一个文件里：`_record` 早就
`output_tokens or completion_tokens` 两个都读了，只有 `_is_truncated` 漏了。
**记账对、告警错**是最难发现的一种不一致，因为数字看着有。

**教训**：协议形状不同的 provider 之间切换，**逐个字段核对**，
不要假设「另一条路有的这里也有」。最省事的检查：
把每个后端分支读到的 usage 字段列出来对一遍。

守门测试 `tests/test_minimax_truncation.py`。它测两件事，缺一不可：
`_is_truncated` 认不认新字段名（①）、`_minimax` **有没有真的调它**（②）——
只测 ① 的话，② 没接上时测试照样全绿。

### 52. 「`tool_choice` 是保证」这句话，是从一次成功里读出来的

原来的记录写「实测 MiniMax 认 `tool_choice`，4/4 全部正确」。
那 4/4 的提示词**本身就点名要调工具**，而真正决定成败的是提示词。

分变量 n=10 复测（同一 `tool_choice` 形状）：

| 提示词 | 思考 | 强制 | 真调工具 | 真·编造 |
|---|---|---|---|---|
| 点名工具 | adaptive | 是 | **10/10** | 0 |
| 点名工具 | disabled | 是 | 6/10 | 0 |
| 点名工具 | disabled | 否 | 5/10 | **1/10** |
| 只说「查一下 X」 | disabled | 否 | 3/3 | 0 |
| 中性提示 | adaptive | 是 | **1/3** | 0 |

⇒ 强制 6/10 对不强制 5/10，在噪声内。**真正起作用的是提示词。**
⇒ 生产提示词（`WRITE_PLAN_TASKS`）恰好是点名 + 开思考那一格，
   所以线上 Phase B 实测 3/3 —— **两层都过，不代表 `tool_choice` 本身是保证。**

同一条也解释了更早的一次误判：我说过「MiniMax 不支持强制」，
那次**同时**用了 `thinking: disabled` —— 两个变量没分开。
**关思考 + 工具 + 提示词点名** 这一格命中率只有 5~6/10，
而那正是当初看起来像「不支持强制」的形态。

⚠️ 顺带纠正一个更宽的说法：「关思考 + 发工具就会编造」也说宽了 ——
「只说『查一下 X』」不点名时，关思考照样 3/3 正常调用。
真凶是**点名 × 关思考**。`run_tool_loop` 的 guard 因此比真实危险条件更宽，
**保留不撤**（多挡只多花 token，漏挡是假证据进报告），但文案已改成实测口径。

**教训**：断言「平台支持 X」时，提示词必须**跨格取样**。
只在「模型本来就想做 X」的提示词上测，测的是提示词，不是接口。

### 53. 探针在「配置写错」时给出的结论，比没有探针更坏

`tests/preflight_model.py` 是「换模型的生产路径预检」——
不手搓 payload，让 `LLMClient` 按真实代码路径发出去（这个设计是对的）。
但它有两个洞：

**① 硬编码 `provider="internai"`，而 `BASE_URL` 取自默认 stage。**
默认平台从 internai 换成 minimax 之后，**两边各取一半**：
拿 `MiniMax-M3` 去打信通院端点。于是每个检查都 404，而输出是这样：

    平台：https://api.minimaxi.com/anthropic      ← base_url 来自 minimax
    ❌ ① 非流式基础   RuntimeError: internai 404   ← provider 却是 internai

**② ⑤⑥⑦ 把 `report(..., "✅", ...)` 写死**，永远不会失败。
于是 5 项报错、2 项报 ✅，整份输出**主动告诉人「没问题」**。

另外 ⑥ 原来直接 `r.json()`，解析 404 的 HTML 正文会抛
`JSONDecodeError: Extra data: line 1 column 5` —— 报错与真实原因无关。

**教训**：

- 探针里**任何写死的 provider / 密钥名 / base_url 都是雷**，
  配置一变它就变成一台「稳定输出错误结论」的机器。
  现在它按模型名去 `providers:` 档案里反查，查不到就**报错退出**。
- **「测不到」必须算「不通过」**。`ok_count == 0` 时报 ❌，
  并额外打一行「这一项什么都没测到」。
- 协议不适用时报 `➖`，**既不算通过也不算失败** ——
  说 ✅ 是替配置背书，说 ❌ 是在说平台坏了。
  （⑤⑥⑦ 是手搓 OpenAI 形状的探针，对 Anthropic 协议的平台结构上不适用；
  讽刺的是那个文件自己的文件头就写着「本项目不手搓」。）

**探针和被测代码一样，会骗人。** 每次拿到预检输出，先问一句：
「这个 ✅ 是真的测出来了，还是因为我没写失败分支？」

### 54. 只在一个地方写 provider 分派 = 那个 provider 的某些环节永远不工作

`complete_json_forced` 里原本是**无条件**调用 `_openai_compat_forced`：

    blocks, usage = self._openai_compat_forced(cfg, system, prompt, tool)

于是 provider=minimax 的环节拿 OpenAI 形状拼 URL：
`https://api.minimaxi.com/anthropic/chat/completions` → **404**。

**`summarize` 一轮 24 次全部 404，一个都没成功。**

为什么这么久没发现：它是**唯一**用 `complete_json_forced` 的环节，
而此前的验证都打在 write_plan* / relevance 上，够不到它。

**认这条路的线索（很便宜，值得记）**：同一个库里两种报错前缀——

    summarize  → "minimax 404: ..."     小写 = `_openai_compat`
    relevance  → "MiniMax 429: ..."     大写 = `_minimax`

`raise RuntimeError(f"{cfg.provider} …")` 和
`raise RuntimeError(f"MiniMax {status} …")` 两处格式不同，
**前缀大小写就是走哪条代码路径的指纹**。

### 55. 护栏写在一条路上 = 另一条路完全裸奔

「关思考 + 发工具在 MiniMax 上会编造」的 guard 原本只在 `run_tool_loop` 里。
而 `summarize` 走 `complete_json_forced` —— **一条护栏都没覆盖**。

护栏接上之后，`summarize` 在 MiniMax 上就**必须开思考才能跑**，
所以这不是「防御性加固」，是**它本来就该有的一道关**。

已抽成 `_check_minimax_thinking_tools()` 放在 `toolloop.py`，
两条路都调（`provider.py` 反向 import，无环）。

⚠️ **这是同一个形态第三次**：
空 `tools` 数组 400、流式分支、`_is_truncated`，全是「只补了一条路」。
**新增一条路径时，先数一遍同类路径有几条。**

### 56. 报错的**前缀**是廉价的路径指纹

见坑 54。补一条：**改了 provider 之后，扫一遍 `llm_call.error` 里的报错前缀**，
不一致就是「有的环节还走老路」的信号。成本是一条 SQL。

顺带一条实测：MiniMax 的 `usage.input_tokens` **只算未命中缓存的部分**，
命中部分单列成 `cache_read_input_tokens`。所以 `input_tokens=74` 配上
`cache_read=3456` 是一份正常的 3,500 token 输入，
**不是「提示词空了」**——差点据此误判 outline 又退化了。

---

## 八、待办

### 最近的

| 事项 | 说明 |
|---|---|
| **产品化五阶段** | **✅ 全部完成（2026-10-12）**。方案见 `DESIGN_R11_PRODUCT.md`（砍掉跨会话召回与上传语料，专注「检索→写作」+ 资产体系）。① 会话管理 ✅ ② 文件夹树 ✅ ③ 会话摘要 ✅ ④ 来源库 ✅ ⑤ 标记排除 ✅ |
| 产品化范围 | 2026-10-12 用户定：✅ 回收站（软删除）✅ 标记排除无用来源 ✅ 归档保留（语义=纯整理，「值得留、别出现在默认列表」，**不再是进入历史召回**）✅ 文件夹支持嵌套 ✅ 会话摘要（生成时机=**报告写完时**，不是归档时）❌ 跨会话召回 ❌ 上传语料。~~❌ 报告目录~~ → ✅ **2026-10-13 已做**（`components/Toc.tsx`，见下「前端打磨三轮」） |
| 「对话/资产」双视图 | ✅ **已在阶段 4 落地**：侧栏顶部「对话 / 来源库」切换。来源库只在切过去时才拉数据（要按 session 建段号索引算引用数，比列会话重） |
| **本轮要做的四项** | **✅ 四项全部完成（2026-10-13）**。① `round` 口径写清（不是 bug，是语义约定）② 失败调用留痕（迁移 `0008`）③ 拖拽排序（repo + 端点 + 原生 DnD）④ 检索轮次图表（新端点 + SVG 图表）。下面三行有各自的细节 |
| 前端展示形态 | ✅ 已定：**对话式工作流**（三层：大环节→子环节→明细）。思考/正文流式已推翻删除（`2026-10-12`），见 `DECISIONS.md` |
| 阶段 3 的坑 | 会话摘要的 stage 已加（`session_summary`，**省略 `reasoning_effort` 关思考**，实测平台不返 `reasoning_content`）。重跑探针：`tests/probe_session_summary.py`（默认只读，`--write` 才落库，`--thinking <stage>` 做反向对照） |
| 检索轮次图表 | ✅ **本轮完成**。`GET /api/sessions/{sid}/trace` 按轮合并 `retrieve`+`assess`（**只取事件口径**，见下节），前端 `RoundChart.tsx` 画「命中/采用」柱 + 「累计段」折线。⚠️ **数据必须走 DB 不能走 SSE 事件**：会话跑完后 `useSession` 直接 return、不开 EventSource，`progress` 恒空，只从事件推的话刷新一次图就没了 |
| 失败调用留痕 | ✅ **本轮完成**。迁移 `0008_llm_call_failure.sql` 加 `status`/`error`；`provider._record_error()` + 三条失败路径。`complete_json` 解析失败记**两行**（ok 行 token 确实花了 + error 行业务没成），`session_cost()` 不受影响。探针 `tests/probe_call_failure.py` |
| 拖拽排序 | ✅ **本轮完成**。`repo.reorder_folders()` + `PATCH /api/folders/order` + `FolderTree` 原生 HTML5 DnD。**只做同级排序**，跨层级走菜单「移到…」 |
| 引用 hover 预览 | ✅ **2026-10-13 已做**（原记录「⬜ 下轮」已过时）。新端点 `GET /api/sessions/{id}/seg_preview`，前端 `RefChip` 用 **portal + `position:fixed`** 出气泡（见 `DECISIONS.md`）。**原生 `title` 必须去掉**，留着会和气泡同时出现 |
| **前端审计与修复** | ✅ **2026-10-14**。全量扫了 19 个文件/3950 行，修掉四条会骗人的缺陷：① 工作流视图环节归类错乱（写作阶段「规划」整个消失，根因是 `useSession` 丢掉了 SSE 外层的 `node`）② 侧栏写操作静默失败（`act()` 无 catch，删不掉看起来像删掉了）③ `jpatch`/`jdel` 错误信息还是老格式 ④ 新建/启动/审批/归档失败完全无声。另有 12 条一致性缺口 + 6 条清理项列在 `DECISIONS.md`，等美化时一并处理。**⚠️ 坑 25：回放只测得到审批为止，写作阶段从没被前端测过** |
| **前端打磨三轮** | **✅ P0/P1/P2 全部完成（2026-10-13）**，都是只动前端、不动后端。**P0**（`27507bc`）：手写表格样式（`prose-sm` 原来是空类，typography 从未安装）、`notice` 通道、7 处静默吞错、来源库失败态、`ErrorBoundary`。**P1**（`fef594c`）：目录+进度条、新建研究对话框、加载态。**P2**：抽屉 Esc/遮罩/滚动锁、引用气泡键盘可达、引用按复用次数分三档、标题层级 |
| **前端美化四段** | ① **✅ `79cc7e6`** 全局 tokens + 侧栏 ② **✅ `19f845c`** 报告阅读区 ③ **✅ `77c9884`** 工作流视图（拆开两个 `kind:"write"` → 「报告完成」/「证据闸门」；环节摘要不再重复 badge 的数；标签列宽**按环节各算各的**；`buildPhases` 加 memo——不 memo 会每次渲染触发 `scrollIntoView` 把上翻的用户拽回底部；空状态给一句「接下来会做什么」）④ **✅ 本轮** 来源库（**54% 的行在换行**——四项中位总宽 236px 超过 227px 可用宽；改单行+域名吸收+统计右对齐，行高 61/79 两种变 63 一种，域名 69px→147px，同视口 7 行→10 行；分组标题从紫色 badge 压成小圆点）。**四段全完成**。**⚠️ 坑 26/27/28/29/30/31/32：Tailwind class 顺序、vite 陈旧 CSS、内置 Browser 三个测量陷阱、探针 `<link href="./">` 静默失败、同一下标两套含义、内联 onClose 进依赖毁掉焦点归还、验不到≠通过** |
| **收尾：a11y + 两个 bug + 阶段性能** | **✅ 2026-10-15**。a11y：菜单方向键导航/随滚动关闭/焦点归还、三个模态共用 `useDialog`（Tab 陷阱 + 锁滚 + 归还焦点，**原来三个都没有焦点陷阱**）、文件夹菜单加上移/下移（拖拽的键盘等价路径，走**同一个** `commitReorder`，到头时**禁用而非隐藏**）。bug：`useSession` SSE 断线后���轮询泄漏（孤儿 interval + 跑完不自停，为此给 `/approval` 端点加了 `status` 字段）、`Sidebar` 的 `ago()` 不刷新（30s 心跳，挂在 `SessionRow` 不是 Sidebar）。阶段性能：`assess`/`apply_revision`/`summary` 补 `@_timed_node`（`assess` 单次 LLM 155.5s 却完全没墙钟记录；**下一轮实测它占 111s/10%，在 v5 里完全不可见**）。另删 5 处死代码，**保留 `_fallback_queries` 墓碑**（记着为什么删）。**⚠️ 坑 31：Menu 里踩了和 useDialog 同一个坑，同类 bug 要连着搜一遍** |
| **全量复跑 + 预算重定标** | **✅ 2026-10-15**。第一次在当前代码上跑完整流程（与 v5 同题、真实规模）。**结果：`status=done`、43 来源/91 段全入库，但报告是空的** —— `node_plan_tasks` 空返回 → `tasks=0`。降级完全正确（三处都写明原因，无静默失败）。根因：**`max_output_tokens` 定得太小**（write_plan 实测最大 7,654 = 上限 4,096 的 **187%**，一直在靠翻倍硬撑）。实测 4,096 成功 **1/14**、16,384 成功 **2/3**。改：write_plan→16,384/32,768，assess/write_section/probe_outline→8,192/16,384（**assess 在同一轮里也已经失败过一次**）。顺带修防线二（它只写在非流式分支，而所有思考型环节全是 `stream: true`，**一次都没跑过**）与写死模型名的错误信息。加 `tests/check_token_budget.py` 防复发。**⚠️ 坑 33/34/35：探针 SSE 手搓解析造出假数据、n=1 的趋势是噪声、默认值按多数定会系统性坑掉需要真思考的少数** |
| **已换平台：中国信通院 discovery-api（2026-10-16）** | ✅ xj189 维护 19h+ 无恢复（四次复查形态一字不差），用户拍板换第二套方案。OpenAI 协议 `https://discovery-api.intern-ai.org.cn/v1`，模型 **`deepseek-v4-flash-0731`**（xj189 提供的同一个，旧数据能接上）。密钥 `INTERN_AI_API_KEY` 在 `.env`（gitignore）。预检 **9/9 通过**，生产路径冒烟 **4/4 通过**，预算体检全绿。⚠️ **坑 39：effort 语义与 xj189 完全相反**；⚠️ **坑 40：预检过了生产照样可能挂** |
| **全量复跑同题（internai）** | 🔄 进行中：`.venv/bin/python tests/e2e_run.py "固态电池的产业化进展，以及各家技术路线的分歧点" /tmp/ra_internai_v1.db`，会话 `s_efb71e9cbf66`。**看两件事**：① 报告有没有正文（v6 是空的）② `write_plan` 的 `tool_calls` 假设（生产路径 10/10 能拿到 tool_use，但**在 write_plan 真实载荷上没验过**） |
| ⬜ **`write_plan` 上实测 effort 档位有没有用** | internai 的 n=3 消融里 low/medium/high 在简单题上分不出差别（中位 58~70 字、组内范围完全交叠），**但那不能推出「effort 无效」** —— 思考只用 60 字，是天花板效应（xj189 当初就这么误判过）。**必须在 write_plan 那种真需要规划的活上测**，每格 n≥3，记成功率「和」思考字数分布。现阶段保持 high 不动 |
| **分派失败兜底：先不做** | ❌ **用户 2026-10-16 定：先不做，等验证工具循环效果**。工具循环刚加回来、一次没测过；兜底会**吃掉失败率、掩盖真问题**，且它绕过全局决策、切出来的任务粒度大概率不对（「正文是错的」比「正文是空的」更难发现）。**触发条件**：工具循环**仍**压不住空返回率，再讨论，且必须带降级标注。详见 `DECISIONS.md` |
| 存疑节高亮 | ✅ 已在 `ReportView` 里做了（琥珀色区块） |
| ~~流式第二步：正文增量~~ | ✅ **2026-10-13 核实：早就做完了，原记录过时**。`server.py:723` 推 `{"kind": "draft"}`（`reasoning` 推成 `thinking`），带 40 字/0.25s 节流和收尾 flush；`types.ts:53` 有类型、`useSession.ts:108` 在消费。剩下的**已知限制**不是待办：正文增量无法归属到具体小节（`write_section` 是 4 并发，`run_tool_loop` 拿不到 task_id） |
| `blocked` 分类的实测数据 | v5 跑出 `blocked=0`。**2026-10-11 两度核对，结论是「材料确实够」**：①判据在 `prompts.py:488-518` 写得很完整；②真实数据复核——143 段证据，报告里 **17 个数值断言全部能在证据中找到出处，零编造**（`tool_reads={read_segments:21}`，子 agent 确实调过工具）。**中间我一度用构造草稿得出相反结论，那是坏探针，已作废**（见坑 11）。真正材料稀缺的场景还没测到，`bad_assignment` / `spec_conflict` 两类仍无真实数据。 |
| `assess` 思考档位 | 配的 `low`，但实测 `low` 判断力约等于「关」。要改 high 还是关，待定。 |

### 本轮新踩的坑（20、21、22）

见第七节。三条都是**浏览器实测才发现的**，读代码和跑 tsc 都不会报：
「未分类」常驻半透明（`null === null`）、SVG 刻度缩到 5px 看不清、坐标轴标签上下颠倒。

### ⚠️ `run_trace` 的 `round` 有两套口径——都对，但别混用

一开始以为这是 bug，查完发现**两边都是自洽的**（2026-10-13 核实）：

| 记法 | 语义 | v5 实测值 |
|---|---|---|
| `run_trace` 的 `node:*` | **进入该节点时，已完成几轮** | `node:retrieve` = 0,1,2　`node:extract` = 1,2,3　`node:write` = 3 |
| `retrieve` 事件 | **本次是第几轮** | 1, 2, 3 |

`retrieve` 看起来「差 1」，是因为它是**唯一执行中就推进 `round`** 的节点。

**别去统一它们。** 试过给 `node:*` 整体 +1：`node:retrieve` 看着对了，
但 `node:extract` 会从 1,2,3 变成 2,3,4——extract 确实是在完成 1/2/3 轮
时执行的，当前值本来就是对的。**统一会把本来对的改错。**

⇒ 画「第 N 轮」图表**一律用事件口径**（`assess` / `retrieve`），
`node:*` 只用来对齐耗时，不单独当轮次。已写进
`repo.save_node_timing` 的 docstring 和 `node_retrieve` 里。

### 已知缺口（2026-10-11 对照 `DECISIONS.md` 早期蓝图实测，详见 `GAP_CHECK.md`）

> **2026-10-13 已清空四项空转**（用户定：「清干净，不留尾巴」）：
> 矛盾结论、历史召回、话题标签、上传语料。表、repo 方法、端点、
> 前端类型、测试脚本一并删除。迁移 `0007` 删老库的表，`0001_init.sql`
> 同步改掉（`schema_migrations` 只记文件名、无 checksum，改历史迁移是安全的）。
> 下面这张表只剩两项**仍未处理**的。

| 缺口 | 实证 |
|---|---|
| 🔴 **库内检索根本不存在**（原写「时间衰减接不到排序」，是错的） | **只有 web 检索**（searxng/tavily 拿 URL → 抓取 → 切段 → **全部段落进写作**）。没有打分、没有排序、没有截断。`chunk_fts` 虚表 + 4 个同步触发器已建好、143 行一直在维护，但**全库没有一条 FTS5 MATCH 查询**，`cosine_topk()` 同样零调用。所以 `time_decay()` 从初始提交 `fe5129c` 就在，**一次都没被调用过**——它不是「没写完」，是给一个不存在的环节预留的零件。**2026-10-13 已删**（`embed.py` 末尾留了说明注释）。**向量化本身也已停算**（`chunk.embedding` 143 个向量算是历史存量，新会话不再写）——详见下方「向量化已停算」 |
| ✅ `claim` 表 | **不是缺口，是已被取代**。用户 2026-10-13 补充：`claim` 是早期设计，后期换成了 `superseded_by`——段级/来源级的取代（`supersede_chunk()` + 抓取时的 `UPDATE source SET superseded_by`），比 claim 设想的结论级**更细**。「不静默覆盖」这个原始诉求已经被满足，`claim` 没有任何东西需要接手。**文档当时没记下这次替换** |

### 关于「库内检索」这个缺口的判断

**别默认它是待办。** 三个选项量级完全不同：

1. **什么都不做**（现状）——v5 证明在这个规模（41 来源 / 143 段）下能出好报告：`blocked=0`、17 个数值断言全部有出处、零编造。代价是 token 随证据池线性增长（v5 用了 465k）。
2. **先有排序**——FTS5 和向量**都已就绪**，缺的只是查询端。这是**质变**：token 大降，但**会改变报告看到的东西**，可能漏掉关键段落。必须用真实数据验证，不能拍脑袋。
3. **只接时间衰减**——不成立。没有排序就无处衰减。（原来的 `time_decay` 属于这一类，所以删了。）

`cosine_topk()`、`stack_blobs()`、`ready_chunks()` **2026-10-13 已删**：
它们是「按相似度召回段落」这套实现的上下游零件，而这个环节从来没被接上。
详见下方「向量化已停算」。

**为什么清这些**：它们不是「没写完」，是**永远不可能被触发**——
表和数据行都在，但写入方根本不存在。清掉的收益不只是代码变短，
而是**代码不再谎报功能存在**（比如「认知演进」一节，
用户以为报告里有，其实从来没出现过；`time_decay` 则让人以为「排序已经存在、只差接一下」）。

### 向量化已停算（2026-10-13）

**全库没有独立的向量数据库**——无 faiss/chroma/milvus/qdrant/weaviate/pgvector/pinecone
依赖。所谓「向量」就是 `chunk.embedding` 这一列 BLOB（**384 维** float16 = 768 **字节**/段，
143 段共 107 KB）。

核实结果：

| 项 | 真实情况 |
|---|---|
| `embed_document()` 生产调用点 | 只有一处（`node_finalize`，2026-10-13 已删） |
| `cosine_topk()` | 零生产调用，**已删** |
| 按向量相似度检索的查询 | **全库 0 条** |
| `chunk_fts` FTS5 虚表 + 4 触发器 | 零 MATCH 查询（**由触发器维护、算力成本≈0，故保留**） |

所以停算的理由不是「向量检索以后可能有用」，而是它**算出来就再没被读过**——
v5 那一轮 143 段花了 **251 秒（整轮 1591s 的 15.8%）** 在写一列没人读的数据。

⚠️ **最容易踩的坑**：`repo.complete_chunks()` 里 `status='complete'` 和 `embedding=?`
原本在同一条 UPDATE 上。只删向量化不管这个，段落会**永久卡在 pending**，
而 `node_finalize` 正是靠 `WHERE status='pending'` 选段的，二次运行会把它们全部重跑一遍。
已同步把 `embedding=?` 从这条 UPDATE 里摘掉。

顺带发现：`complete_chunks()` 的签名收着 `token_count`，但列表推导用 `_` 把它丢了，
**从来没进过 SQL**——所以 `nodes.py` 里那次 `count_tokens()` 是纯白算
（chunk 插入时 `add_chunk` 已经写过 token_count 了）。一并删掉。

**`embedding` 列保留**（选项 C 未采纳）：历史会话那 143 段向量还在，将来真要做
库内检索时不必改 schema。代价是这列从此只会越来越空。

**删完后 `finalize` 节点耗时 315s → 0.033s**（真实库 143 段回放实测，
status 全部正确翻成 `complete`，`cost_tokens` 正确回写，二次运行仍幂等）。
`embed.py` 整个模块现在**零生产调用方**，`sentence_transformers` 重依赖仍挂在依赖树里。

### 中期的

> ⚠️ 这里原来第一条写的是「**前端（一行没写）**：报告阅读 + 引用跳转、审批界面、`assemble_warnings` 高亮」——
> **2026-10-13 核实为彻底过时并删除**：四项全在（`ReportView.tsx` 153 行、`ApprovalView.tsx` 81 行、
> 引用跳转 `withRefs`、`ReportView.tsx:60` 存疑节琥珀色高亮），前端共 3047 行。这已是这张表第二次腐化。

- **Agnes 接入**：`DESIGN_R7_SECONDARY_MODELS.md` 有完整方案（接 `write_plan`/`probe_*`/
  `outline`/`extract`，不接 `write_audit`/`relevance`），需要 provider 适配 + 令牌桶
- `dedupe`：配置里有、图里零调用点，**记着的待办**不是废代码。实测按内容哈希做触发率为 0，真要干活得是语义级去重
- ~~`assign`~~：✅ **2026-10-13 删掉**。它要映射的「断言」已被 `superseded_by` 取代（`claim` → 段级/来源级取代），分派现在由 `assess` 带框架一起做。**环节不存在，不是待接入**——和 `claim`/`time_decay` 同一类
- ~~段落概括 JSON 失败率~~：✅ **已彻底解决**（2026-10-13 核实）。`extract_batch.py:263` 用
  `complete_json_forced(..., name="emit_summaries", schema=...)` 强制工具调用，代码注释记着
  「同一组数据实测 3/3 通过」；**v5 真实数据 143/143 段概括完整，0 段为空**。
  `write.py:1219` 的 `n_no_summary` 只是兜底分支，正常不触发

### 明确不做的

降级路径（`LLMClient` 无 provider 切换）——单点依赖 MiniMax/xj189，
但两个平台同时挂的概率低。探索式深度研究的 UI 形态。

---

## 九、工作约定

1. **测试范围**：改了哪块只测哪块，**用现有 session 续跑**而不是重跑全流程
   （省时间也省搜索额度）
2. **先核对后动手**：决策与细节核对完再写代码
3. **问题先报告**：测试遇到问题先报告、一起定位，判断是否要改架构。
   **小 bug 可以直接修，架构级问题必须先说。**
4. **能力优先**：能交给 agent 自主判断的不写成程序死规则；
   引导性规则优于硬约束，硬约束只用于合规性与底层限制
5. **结构性修改一律先说方案再改**
6. **每个决定记进 `DECISIONS.md`**，状态同步进本文
