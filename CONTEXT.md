# CONTEXT · 交接文档

> 这份文档的定位：**接手这个项目前必须先读的东西**。
> 架构、仍然有效的决策、踩过的坑（76 条）、工作约定、已知缺口。
>
> 工程层面**已经知道、但有意押后**的欠账另记在 [`TECH_DEBT.md`](TECH_DEBT.md)
> （`checkpoints.db` 无清理机制、守门测试污染默认库等）。本文第十节记的是
> **研究层面**的未验项，两者别混。
>
> ⚠️ **它取代了曾经的三份大文档**：`DECISIONS.md`（305 KB 决策流水）、
> `HANDOVER.md`（48 KB，含大量已过期的待办）、8 份 `DESIGN_R*.md` 设计稿。
> 那些文件里的绝大多数结论**已经被后续实测推翻或取代**，留着会误导后续决策 ——
> 这正是它们被删掉的原因。仍然成立的部分已经折进本文。

---

## 一、这是什么

一个**本地优先、单人自用**的深度研究 Agent：
多轮检索 → 抓取 → 证据处理 → 框架 → 分块写作 → 交叉审查 → 带段级引用的报告。

代码规模：后端 `research_agent/` 约 10,900 行 Python；前端 `frontend/src` 约 4,400 行 TS/TSX。

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

**审批点在第一次 `assess` 之后**，不在 `outline` 之后 ——
大纲是在只有探针轮廓时写的，让用户审批一份未经材料检验的框架没有意义。
`assess` 顺带输出 `outline_review` 把框架校准到材料撑得住的粒度。

### 三个子图

| 子图 | 做什么 |
|---|---|
| `probe` | 轻扫 → 歧义澄清 → 深读 → 领域轮廓（大纲的 `grounded_in` 全部指回这里）|
| `extract_batch` | 段落概括 + 来源性质判定（+ 段级相关性闸门）|
| `write` | 主 agent 规划 → 并行子 agent 写作 → 审查 → 定点返修 |

### 证据流转

来源 → 分块 → 段落（写作取材的最小单位，约 700 token）→ 一句话概括
（检索时用它快速判相关性，展示时用它替代正文）。
**概括缺失的段落在写作阶段根本看不到** —— 所以概括批次失败是有界重试的，见第六节。

---

## 三、平台与模型

`config/models.yaml` 的 `providers:` 档案管理，
**阶段一律不写 provider，由 `defaults.provider` 统一决定**
（档案里的模型名 / 地址 / 密钥 / 关思考怎么表达随之继承）。
**换平台只改这一行。**

| provider | 协议 | 当前用途 |
|---|---|---|
| `minimax` | Anthropic Messages（`/v1/messages`） | **当前默认** |
| `internai` | OpenAI 兼容（`/v1/chat/completions`） | 备用，两轮全量都验过 |
| `xj189` | OpenAI 兼容 | 备用 |

⚠️ **换 provider 不是只改一行**：协议形状不同，防线要逐条核对。
已踩过的：截断检测认不出 Anthropic 形状的 `usage` / `stop_reason`（坑 51）、
`complete_json_forced` 没有 provider 分派导致整个环节 404（坑 54）。
**换平台前先读第五、六节。**

### 切换时必须一起改的东西

- **`summarize` 的 `reasoning_effort` 必须是 `low`**（MiniMax 这条路上「关思考 + 发工具」
  会让模型假装调用并编造结果，护栏 `_check_minimax_thinking_tools` 会**直接拒绝**）。
  换到 xj189 / internai 才能改回 `disabled`。
- `providers.minimax` **故意不写 `effort_off`**：`_minimax` 把 `reasoning_effort`
  翻译成 `thinking.type`，写成 `none` 会让「关思考」走错字段。
- 各 provider 的 `rate_limit_per_min` 是**实测值**，不是猜的
  （internai 45、minimax 40）。换平台先测再填，见第六节。

### 两轮全量的实测对照（2026-10-18）

| | internai `data/refs/ra_v11_full.db` | minimax `data/refs/ra_v12_minimax.db` |
|---|---|---|
| 结果 | `done` | `done` |
| 写作阶段 | 29/29 全过 | **33/33 全过** |
| 报告 | 22,379 字 | **28,230 字** |
| `relevance` | 232 次零失败 | 208 次零 429 |
| `summarize` 批次失败 | 2/32（6%） | 12/32（37.5%，**孤例、未能复现**） |

⚠️ minimax 那次的 `summarize` 失败率高，但事后用**同一批真实段落 + 生产批量口径**
重测 32 个批次**零失败**（串行 100%、4 并发 100%）。
「提示词」和「并发」两个假设都被 A/B 实验推翻，**病因仍未知**。
重试是针对「一次失败就丢 12 段证据」这个独立缺口加的，不依赖病因是否查清。

**2026-10-18 第三次复测**（`tests/probe_summarize_replay.py`，真实 MiniMax）：
把那 98 段丢失的 chunk **原样重放** —— 12 批 × 连跑两轮，
**24/24 调用全成、98/98 全救回、一次重试都没触发**，
罪魁来源（80 段综述丢 72 段）也整段恢复。
⇒ 判定：那晚的失败是**平台瞬时劣化**（10 次失败挤在 23 秒并发窗口），
不是配置/内容问题。`tool_choice` 在 M3 上是暗示不是保证（provider.py:1934），
偶发回文本是它的固有抖动，重试 + 报告披露（坑 57）已经把这个失效闭环了。
⚠️ 顺带发现：**探针用不存在的 session_id 记账会被 FK 静默拒掉**
（`llm_call` 有 `REFERENCES session(id)`，repo 开了外键，
`_record` 又 `except: pass`——探针脚本要先补一条桩 session）。

---

## 四、代码地图

```
research_agent/
├── llm/
│   ├── provider.py     ★ 统一调用入口、全部 provider 后端、限流、guard
│   │                     · SSEAccum（OpenAI 形状）/ AnthropicSSEAccum（Anthropic 形状）
│   │                     · _minimax / _minimax_tools / _openai_compat_forced
│   │                     · _is_truncated（认两套 usage/stop_reason 形状）
│   │                     · SendRateLimiter（挂在 httpx request event hook）
│   └── toolloop.py     工具循环执行器 + _check_minimax_thinking_tools 护栏
├── graph/
│   ├── nodes.py        主图各节点
│   ├── prompts.py      全部提示词
│   ├── state.py        ResearchState / WriteState ★ 加字段必须在这里声明
│   └── subgraphs/{probe,extract_batch,write}.py
├── db/repo.py          SQLite 读写（llm_call / chunk / source / write_trace …）
├── server.py           FastAPI + SSE
config/models.yaml      ★ provider 档案 + 各环节预算/effort/stream
tests/                  守门测试 + 生产路径入口（见 README）
data/refs/              ★ 真实跑出来的参考库（见下）
```

### 参考库（`data/refs/`）

真实跑出来的 SQLite 库，既是探针与体检脚本的**输入**，也是文档里那些实测数字的出处。
**原来全躺在 `/tmp`** —— macOS 会清，一次重启就没了，而重造一份要 25~40 分钟
加几十万 token。2026-10-17 用 `VACUUM INTO` 搬到这里（WAL 折进主文件、
`integrity_check` 全 ok、行数与原件逐项核对一致），`/tmp` 的原件故意留着没删。

| 库 | 内容 | 谁在读它 |
|---|---|---|
| `ra_v12_minimax.db` | minimax 全量，`done`，28,230 字 / 37 来源 / 288 次调用 | 注释与文档里的实测出处（prompts / extract_batch / test_summarize_retry）|
| `ra_v11_full.db` | internai 全量，`done`，22,379 字 / 44 来源 / 308 次调用 | 前端回放的源库（README）|
| `ra_xj189_v5.db` | 「v5」那一轮，23,130 字 —— 预算定标那批数字的出处 | `check_token_budget.py`、`probe_session_summary.py` |
| `ra_internai_v1.db` | 18,200 字 / 39 来源 / 109 段 | **4 个探针的 `SRC_DB` 输入**（minimax_plan / forced_choice / plan_tasks_forced / relevance_model）|
| `ra_v6_full.db` | 更早一轮，6,152 字 | `check_token_budget.py` |
| `ra_verify_nature.db` | 来源性质端到端验收的真实数据（58k token / 7.4 分钟）| `verify_nature_e2e.py` |

⚠️ **`data/` 整个被 `.gitignore` 排除**（`*.db` 也单独排除），所以这些库**不在 git 里**。
搬到这里只解决「`/tmp` 被清」，**不解决「`data/` 被删」** —— 真要保命得自己备份到仓库外。

⚠️ **探针的 `WORK_DB` 仍然留在 `/tmp`，不要改。** 探针是 `shutil.copy(SRC_DB, WORK_DB)`
之后往 WORK_DB 里写的，一旦让它指向这里，跑一次探针就把参考库写脏了。
只有**输入**路径搬了。

⚠️ 没搬的那些：`ra_forced_choice` / `ra_forced_probe` / `ra_minimax_probe` 是
`ra_internai_v1` 的工作副本（跑一次就能再生）；`mig_old` 是 `ra_xj189_v5` 的副本；
`ra_v10_full` 没有任何地方引用、write 基本没产出（4,182 字、`write_trace` 为 0）。

## 五、仍然有效的决策（别推翻，除非有新的实测证据）

这些不是「当时的偏好」，是**踩过坑之后定下来的**。改之前先看对应坑号。

| 决策 | 为什么这么定 | 相关坑 |
|---|---|---|
| **预算一次给够，重试时一动不动** | `max_tokens` 是思考与正文的**共享天花板**；「失败就翻倍预算」是把墙抬高让模型想得更远再撞一次。实测 @16,384 时 reasoning 恰好顶满、正文 0 | 33/34/35 |
| **重试的动作是降 effort，不是加钱** | 同上。梯子 `high → medium → low`，走完还失败就抛 | — |
| **超时按吞吐推导，不是拍脑袋** | 最慢 62 tok/s，65,536 跑满需 1,057 秒 ⇒ 600 秒的墙比满预算还近 | — |
| **拆 `write_plan` 成 spec / tasks 两个 stage** | 两者预算需求差一个量级，共用一个 `max_output_tokens` 必然一边不够 | — |
| **结构化输出是为了让长输出更稳定，不是为了解决崩溃** | 用户原话。强保底靠**三层**：Phase A 自愿交卷 → Phase B 强制 → `_parse_json` 老路兜底 | 50 |
| **Phase B 的「强制」在不同平台上强度不同** | internai/xj189 是硬的；**minimax 上 `tool_choice` 只是暗示**（实测强制 6/10 对不强制 5/10）。降级等于把「保证」变成「也许」 | 52 |
| **主动限流挂在 httpx request event hook** | 6~8 个发送点各埋一次必漏；且埋在外层**数不到内部重试**。按发送速率限，不限并发 | 47 |
| **限流值一律实测，不猜** | minimax：6 并发打满 60 秒只成功 50/3248 ⇒ 上限约 50/min；按 40/min 配速 60/60 全过 | — |
| **`reasoning_effort: disabled` 是平台中性的语义值，不是字面量** | 由 `providers.<名>.effort_off` 翻译成 `none` 或省略。写死 `none` 切到别的平台会**反向打开思考**（静默多烧 token）| 39 |
| **刻意不用 `off` 当档位** | YAML 1.1 把裸 `off` 解析成布尔 false ⇒ 翻译不发生 ⇒ 思考反而默认打开 | 39 |
| **`grounded_in` 是大纲小节的唯一硬约束** | 缺它的节直接丢弃 —— 「模型说它有」不算数，必须能指回实际观察到的材料 | — |
| **子 agent 的标题编号由装配层统一编** | 提示词管不住的由程序兜底，两处都要看 | — |
| **`<!-- lead -->` 是删引言标题的唯一依据** | 改成「程序用相似度判断」实测 7 个只删对 3 个，而且误删是**静默的** | — |
| **概括批次失败要有界重试** | 一次失败 = 12 段证据静默丢失，报告照样写完不报错。只重试一次、只重试失败/不完整的批次 | — |
| **审批点后置到第一次 `assess` 之后** | 让用户审批一份未经材料检验的框架没有意义 | — |

---

## 六、已知地雷（改代码前先扫一眼）

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

---

## 七、跑 server 的两个坑（2026-10-11 踩的）

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

---

## 八、踩过的坑（76 条 · 只留会再踩的）


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

### 57. 🔴 参数传到了函数，却没进 state —— 唯一的告警口就这么断了

2026-10-17 阶段 0 修的。`run_write()` 的签名里有 `n_no_summary`，
`nodes.py` 也**正确地算出来并传了进去**，但 `st: WriteState` 那个 seed dict
**漏了这一项**。于是 `node_final` 里 `state.get("n_no_summary")` 恒为假，
报告里那句「有 N 个证据段落没能生成概括」**一次都没出现过**。

实测后果（两份参考库）：

| 库 | 无概括段数 | 报告告诉读者了吗 |
|---|---|---|
| v12（minimax）| **98/246（40%）** | ❌ 没有 |
| v11（internai）| 17/262（6%）| ❌ 没有 |

而这些段**真的进不了写作**：`_brief()` 过滤 `if (g.get("summary") or "").strip()`,
主 agent 的证据清单里压根没有它们。所以 v12 那轮有 **40% 的证据对写作阶段不可见**，
报告看起来完整。

⚠️ **这是坑 7（设计授权了但没实现）的新形态**：以前是「下游没实现」，
这次是**管线全都对，只有一个 dict 字面量漏了一行**。
链路上每一环都能单独验过，合起来还是断的。
⇒ **验「这个值到得了终点吗」要端到端取一次，不能逐段看代码。**

### 58. 🔴 硬判据读的字段从来没人写 —— 20 分钟超时从没生效过

同一天修的，和坑 57 同源但更隐蔽。`_predict_stop` 的第四条判据读
`state["elapsed_s"]`，而全仓库对它的「写」只有两处：`server.py` 把它
**播种成 `0.0`**，以及一个**事件载荷**里的同名键（不是 state 写入）。
`started_at` 声明了，也从没写过。

⇒ 判据恒等于 `0 >= 30*60` ⇒ **永远 False**。参考轮实测 27.4 分钟，
本该被这条拦下的场景一次都没被拦过。

⚠️ 和 `max_tokens=800_000` 那条（见 `HARD` 的注释）**是同一个病，同一个文件**：
那条当年也是「判据读的字段只在循环之后才写」，发现后删掉了。
**删掉一条之后，没有人去数剩下三条有没有同样的毛病。**
⇒ 坑 55 那句「新增一条路径时先数一遍同类路径有几条」，
**对判据同样成立**：加硬判据时逐条确认它读的字段有没有人写。

**修法的关键是语义选择**：不用「`started_at` 到现在」的墙钟，
而是在 `_timed_node` 里**累加各节点自己的耗时**。
因为 `await_approval` 是 `interrupt()`，人去审批可能挂几个小时 ——
墙钟会把人的思考时间算进机器预算，超时就变成「用户午饭回来研究自己废了」。

### 59. 同一个函数名定义了两遍，而死掉的那份返回的是非法值

`nodes.py` 里 `route_after_assess` 定义了**两次**，Python 保留后者，
前者是死代码 —— 而它 `return "apply_revision"`，**根本不在
`runner.py` 的路由映射里**（只有 `revise` / `retrieve` / `write`）。
现在无害，但谁动一下代码顺序它就会变成一个找不到出边的图。

⚠️ 讽刺的是：同一个文件里 `_should_stop` 的 docstring **正好在讲这件事**
（「2026-10-08 我另写了一个同名逻辑，结果 `_predict_stop` 变成死代码」），
而那个教训下面十几行就躺着同一个毛病。
⇒ 和坑 31 同一条：**注释只能提醒读到它的人**。
这类问题要靠「一个名字在文件里出现几次」的机械检查，不靠记性。

### 60. 拆了 stage，忘了跟着改按名字写死的白名单

`provider.py` 的 `_NO_TEXT_DELTA_STAGES = {"outline", "write_plan", "write_audit"}`
用来挡「JSON 正文别流给前端」。而 `write_plan` 在 2026-10-17 已拆成
`write_plan_spec` / `write_plan_tasks`，**两个新名字都不在集合里** ⇒
这两个环节的 JSON 增量又开始推给前端了，正是这个集合注释里写的
「只会看到一坨没闭合的括号」。

⇒ **凡是按 stage 名字写死的集合，都是拆/改 stage 时的隐藏依赖。**
`config/models.yaml` 的 `stages:` 键是真相源，这类集合必须跟着它走。

### 61. 一整个配置文件没有任何代码读取，而且和真值矛盾

`config/search.yaml`（67 行，README 里还列着）**没有任何代码加载**：
后端唯一的 `yaml.safe_load` 读的是 `config/models.yaml`。
它的值全是写死在 Python 里的默认值，而且已经**对不上**：

| search.yaml 写的 | 代码里真正的值 |
|---|---|
| `loop.max_rounds: 8` | `HARD max_rounds=4` |
| `loop.timeout_minutes: 20` | `HARD timeout_min=30` |
| `chunking.max_tokens: 500` | `RecursiveSplitter(max_tokens=2000)` —— **差 4 倍** |
| `loop.max_tokens_per_session: 500000` | 该判据 2026-10-08 已删 |

⇒ 照它调参会**完全没有效果**，照它读参数会**看错一个 4 倍的值**。
已在文件头加了显眼警告；「接上还是删掉」记进 `TECH_DEBT.md` 待定。

⚠️ **查「配置有没有生效」的办法**：grep 这个文件名 + grep `safe_load`，
不是看文件里写了什么。配置文件长得比代码更像真相，但它没有编译器护着。

### 62. 判「改了没有退化」之前，先量**方法自身的噪声底**

2026-10-18，段级闸门从逐段调用改成 12 段/批（192 次 → ~17 次）。
一致性探针拿 v12 里 192 段 LLM 判过的段重放批量路径：

| 对比 | 一致率 |
|---|---|
| 批量 vs 库里的旧判（第一轮） | 94.8% |
| 批量 vs 旧判（第二轮） | 90.1%，第一轮翻的 10 段全部复现 |
| **逐段重判 vs 旧判（同方法对照组）** | **90.0%** |

⇒ **旧判法自己重判自己都只有 90%**（temp=0 也不完全确定——
M3 的 MoE/批处理本身有抖动）。库里那批 verdict 是一次带噪采样，
不是金标准。批量化的系统性差异 ~5%，**低于方法自身的噪声底** ⇒
批量化没有造成可检测的质量退化。

⇒ **凡是对「判定一致性」做 before/after，先跑同方法对照组**。
没有 90.0% 这个数，94.8% 会被误读成「批量退了 5.2%」；
有了它，94.8% 的意思是「在噪声底以下」。

顺带实测：对照组 60 次单段调用就撞了一次 40/min 限流（等 19.9s）
——批量化的收益是真实的。

### 63. 离线重放先毙掉一个「看起来稳赚」的优化

2026-10-18，想放宽规则层：`_blob()` 有概括时只用「标题+概括」，
正文前 600 字不参与匹配 —— 把正文加进去，规则免费放行应该更多。

**离线重放（零 token，规则层是确定性代码）**，同一份段重算：

| 库 | 多放行 | 其中当时被 LLM 判无关（误放） |
|---|---|---|
| v12（minimax）| +42 段 | 2（5%） |
| v11（internai）| +102 段 | **23（23%）** |

content[:100/200/300] 三个窗口都试过，v11 的误放率稳定在 ~18–23%。
同一个问题、同一套关键词 —— 差别在**召回语料**：v11 的无关段
词面上更像主题（命中率高但语义不对）。

⇒ **结论：不做。** 两个理由：① 误放率是语料相关的，不能按 v12 的
5% 上线然后在别的问题上放 23% 的垃圾；② 闸门批量化落地后，
省 42 次单段调用只等于省 ~4 个批次调用 —— 收益已经塌了。
**这条教训的形态：优化的价值会随相邻改动贬值，先动的先吃掉收益。**

### 64. 阶段一（闸门批量化+前移）全量验证结果（2026-10-18）

同一问题（固态电池产业化）两版各跑一次全量。**跨 run 对比始终被
「每次检索回来的语料不同」干扰**（v13 122 段 vs v12 246 段），
所以只能比结构量不能比绝对值：

| 量 | v12 | v13 | 结论 |
|---|---|---|---|
| relevance 调用 | 208 次 / 63.5k in | **44 次 / 15.2k in** | 批量化生效（−79% 调用） |
| 闸门位置 | write 头部堵 ~4.8min | 摊进两轮 extract 后 | write 即时进入 |
| assess 的池子 | 246 段未过滤 | 50→113 段已过滤 | r2 assess 直接 `has_enough=true` |
| summarize 失败 | 12/32 | 0/24 | 佐证「平台瞬时劣化」 |
| 报告 | ~20k 字（结构完整） | 41k 字 / 126 处 [Sn·m] 引用 | — |

**顺带抓到并修掉的存量 bug**：`write_plan_spec` / `write_plan_tasks`
走 `complete_json`（手写 JSON，非强制工具），输出是全流程最大的
JSON，char 6962 处坏一次 → 整条写作线退化成 6.9k 占位报告
（降级路径本身诚实，明说了「没产出正文」）。已加一次性有界重试
（b99ddeb），重跑时写作完整跑通：8 任务 / 8 节 / 审查返修 1 轮。

**写作阶段仍有两段 ~45–50s 非 LLM 静默**（末段 write_section 结束 →
首次 write_audit 之间、末次 write_revise 结束 → 二次 audit 之间）。
定位候选：checkpoint 序列化大 state（drafts+segments 全文在 state
里过 checkpointer）——与 checkpoints.db 膨胀同根，治疗见
TECH_DEBT #1，不是顺手能改的。

**小瑕疵已修**：个别小节的成品把 ` ```markdown ` 围栏和 `# secN`
原始标题漏进终稿（本报告 2 处）。两个根因都堵上了：① 装配前加
`_unwrap_fence` 剥整包围栏（只剥「首行开尾行闭」的整包壳，
节内真代码块不动，剥壳数进 `fences_unwrapped` 事件字段）；
② `HEADING_RE` 从 `#{2,6}` 放宽到 `#{1,6}`，`_NUM_PREFIX_RE`
的 `secN` 分支补全角冒号形态（实测 `sec3：`）。`_demote_headings`
与 `_count_demoted` 顺带补了围栏感知（代码块里的 `##` 不再误降）。

**重跑工具**：`tests/rerun_write.py <sid> [db]` —— 从
checkpoints.db 里 `next==('write',)` 的快照分叉续跑 write→finalize→
summary，前面节点不重复。**教训：checkpoint 断点续跑本来就是这条
链路该有的能力，别为了验证一个修复就重花一轮检索的钱。**

### 65. 前端产品化开工：A1 路由 + A2 回放换代（2026-10-18）

**A1（已提交 f21cd3b）**：`frontend/src/router.ts` —— URL hash
`#/s/{sid}` / `#/sources/{id}` 是唯一真相源，App 不再自己存
sid/pane state；刷新不丢、前进后退生效、链接可分享。选 hash 不选
真路径的理由：部署后零服务端配置、不引 react-router（依赖洁癖）。
验收：router 行为级用例全过 + 真服务下刷新/直开/前进后退验证。

**A2（已提交）**：回放 fixture 换代 + **事件流自动落库**。

- `0009_sse_event.sql`：新表 `sse_event`（id 自增排序 / at 毫秒精度 /
  kind / node / event_json 全信封）。无外键——日志表，宁可留孤儿
  也不能让 emit 因 FK 丢一条。
- `server._emit`：每条事件先给订阅者投递，再 try/except 落库——
  **落库失败绝不能弄挂一次运行**（SSE 本来就是尽力送达）。
- `tests/dump_events.py <sid> [db] [--from-node X] [--to-node Y]`：
  导出成 fixture 格式。以后每次真跑自动产出可回放事件流，
  **手工录 fixture 这条路彻底关闭**。
- 三份 fixture 用 v13 真跑（s_90b3c5d17069）真实值重录：
  run_to_approval 28 条（含 probe_error/gate/outline_revision/
  assess 全字段），run_after_approval 38 条（含 plan_spec 真实
  thesis+spec_uncertainty、write_revise、audit×2、rework_effective=
  false 的真实收尾形状），approval.json 换真 8 节框架（6 节带
  材料校准说明）。draft 增量用终稿真实切片。
- 验证：回放服务 `--speed 0` 全路径跑通，落库 30 + 72 条，
  dump 闭环导出成功。
- 顺带修：`replay_server` 启动提示语从「4 节框架」更正为真值。

**旧 fixture 的病**：审批后那份还是 pre-v13 形状（无 gate/
plan_spec/revise/新 audit），前端新渲染分支从没被真实数据验过。
现在 fixture 是「真跑长什么样就录什么样」。

**教训**：测前端用的 fixture 必须能**自动从真跑再生**——
否则后端每改一次事件形状，前端测试就在测一个不存在的旧世界。

### 66. 前端产品化 B/C/D 段收官（2026-10-18）

阶段方案在开工前定稿（用户拍板）：**A1→A2→B→C→D**；
审美基准 Notion/Linear 式，手写 Tailwind，不引 UI 组件库。

**B 段 · 等待体验**（B1 后端 emit、B2–B4 纯前端，全部零 LLM 成本）

- B1：`progress_scope`/`fire_progress` contextvar 通道，仿 `delta_scope`。
  关键认知：节点 `events` 数组是 LangGraph `astream(updates)` **节点跑完
  才吐**，环节内进度必须走另一条直通 `_emit` 的链路；worker 线程靠
  `submit_run_in_context` 显式复制 contextvar。六类新事件：
  retrieve 每查询 / extract 每来源 / summarize 每批 / gate 每批判定 /
  write 每任务完成 / write_plan_spec 实时可见。
- B2：当前环节秒表（`nodeAt` 时间戳驱动）+ 呼吸动画 + 累计 token
  （10s 轮询 approval 端点的便宜字段）。**ETA 明确不做**——
  算不准的数字比没有数字更烦。
- B3：标签页标题实时状态 + Notification 完成/审批提醒 +
  会话列表 running 徽标。
- B4：`?debug=1` 才渲染 thinking/draft 原始流折叠面板。
  **默认不渲染**是用户定的产品判断：模型思考流信息密度低
  （audit 单次 thinking 25k 字符），直接渲染=噪音墙。
  替代方案=「细粒度结构化事件」（B1），每条仍是真产出不是噪音。

**C 段 · 报告产出物**

- C1：复制 MD / 下载 .md / `@media print` 打印样式（打印=PDF，零依赖）。
- C2：报告内查找条用 **CSS Custom Highlight API**——不往 React 渲染的
  DOM 里插 `<mark>`（会和 React 重渲染打架）。带计数、作用于报告
  滚动容器。
- C3：`0010_degraded.sql` 给 session 加 `degraded` 字段；
  `plan_failed` 的报告在头部 + 会话列表标「降级产出」。

**D 段 · 管理与检索**

- D1：`GET/PUT /api/config` 读写 models.yaml。PUT 校验 YAML 可解析 +
  `stages` 非空映射，坏配置不写入。**不做运行时热切换**——
  ModelRegistry 是启动时读一次的进程级单例，「保存即生效」是谎言，
  界面明说「重启后端服务后生效」。侧栏底部 ⚙ 入口 → `#/settings`。
- D2：`GET /api/sessions/search?q=` 命中标题/问题/报告正文，
  正文命中带定位片段（matched_in + snippet，Python 侧定位，
  SQL substr 对多字节不可靠）。声明在 `{sid}` 路由之前。
  侧栏搜索框 300ms 防抖；本地标题/问题即时过滤保留，
  搜索失败静默退回本地过滤。LIKE 够用，不上 FTS5 倒排索引。
  （另一个验证插曲：Python urllib 走系统代理对 localhost 返回 502，
  curl 直连正常——测本地服务别用 urllib。）

**验收**：全部经 tsc + vite build；D1/D2 端点在回放服务（v13 真实
数据副本）上冒烟通过；B 段行为级测试全绿（contextvar 跨线程传播、
批量进度计数、失败重试路径）。提交链：10129e2 → 64a243a →
cb09180 → 7fcbe97 → 22761c9 → 8b8c3e3 → da4cc29。

**遗留**：回放仍是「先连接再放事件」，不落库回放到新连入的订阅者
——持久化是为 fixture 导出建的，不是 live backfill（文档已注明）。

### 67. 视觉/体验轮（2026-10-18，5c95acf）

用户确认的范围：**全方向做；不要键盘快捷键；要侧栏折叠；要欢迎面板。**
暗色不做（tailwind.config 注释里 2026-10-11 已定）。

- **E1 设计原语**：`.btn` 基底 + `btn-primary/ghost/danger/danger-solid`
  四档（原来十几处按钮各写各的尺寸字色）；`:focus-visible` 统一蓝色
  outline（Tab 焦点可见是底线）；通知分 err/ok 两级——**原来只有红色
  一档**，「已复制」和「复制失败」长一个样，红被用成通用色、真报错
  失去警示力；`.skel` 骨架 shimmer 换掉来源库的「加载中…」文字。
- **E2 侧栏折叠**：44px 图标轨（💬📚＋⚙»）。**状态在内存不进 URL**——
  「我暂时想宽点看报告」是临时偏好，不该污染可分享的链接。
- **E3 欢迎面板**：idle 首页原来只有一行字。现在是 说明 + 主按钮 +
  最近 6 场研究卡片（标题/状态点/相对时间/token）。
- **E4 四阶段进度轨**：sticky 顶部轨（✓/呼吸点/灰），解决「想知道走到
  哪一步得滚回顶部」的问题。点击=先展开（openSignal 信号驱动
  PhaseBlock setOpen）再滚动——**展开必须先于滚动**，否则目标还没高度
  滚不到位。环节吸顶头让位 `top:41px`（轨高）。
- **E5 回到顶部**：滚过 ~10% 才浮出（刚打开时不挡内容）；
  滚的是报告滚动容器不是 window；`aria-hidden`+`tabIndex=-1`
  同步禁用，隐形按钮不该能被 Tab 到。
- **ago/tokens 提取到 `lib/format.ts`**——欢迎面板和侧栏共用。
- 排版复查结论：报告 mdComponents 的标题分级/表格/引用块已精细
  （坑 62 前就修过），本轮不动。

### 68. 回放 fixture 补齐 B1 细粒度事件（2026-10-18）

**问题**：B1 的六类细粒度事件（`search_query`/`extract_source`/
`summarize_batch`/`gate_batch`/`write_task`/`gate_retry`）后端早已
实现（10129e2），但 v13 fixture 是 fire_progress 落地**之前**录的——
录制时通道不存在，不是回放丢的。回放里这些行一条没有，前端 B1
渲染分支在回放里完全验证不到。

**方案**：`tests/enrich_fixture.py` 从真实 DB 合成这批事件注进
fixture（用户拍板的方案一，不是等下次真跑）。

- 数据全部真值：查询串/轮次命中 ← `run_trace`；来源标题/性质 ←
  `source`（按 fetched_at 分钟分轮 25+16）；段数/批次数 ← fixture
  `segment_summary` 的 `batches` 真值（r1=10、r2=14，不是拍的
  ceil(53/12)）；gate 批规模 = `by_method.llm`（规则放行段不进
  LLM 判定），批大小 12（GATE_LLM_BATCH）；write_task.chars ←
  报告各 `## ` 节真实长度（**注意跳过开头的 `# 标题` 块**，
  否则下标错一位 t1 拿到 172 字报告头）。
- 时序：细粒度事件排在「node 起、环节汇总事件到」之间；
  窗口塞不下时锚点及之后事件整体后移，原相对间隔不变。
- **合成诚实的边界**：per-query hits 是合成值（搜索命中在判重**前**，
  总和 > 轮次 hits 正常）；`nature_batch`/`gate_retry` 只在真实发生
  时才造（v13 零缺失零掉队 → 不造），不为了演示硬凹失败分支。

**回放器顺手修**：`FakeGraph` 续跑结束后补 `finish_session(tid,
"done")`——真图是 nodes.py 里落的，回放绕过真节点导致会话列表
永远 running（状态只是 cosmetic，但「回放会话显示 running」会误导
排查）。

**验收**：注入后 to=77 条/after=94 条；回放全量走通——
SSE 落库实测 20 query + 41 source + 24 summary + 9 gate + 11 task
（含 3 条 revise），批准→done 一次到位无回环。

### 69. 开发服务改 launchd 常驻（2026-10-18）

**反复踩的坑**：`nohup cmd &` 起的 vite/回放服务挂在 agent shell
会话树下，会话回收、终端关闭、休眠唤醒都可能把它们带走——
一周内 5180 失联三次，每次根因不同（IPv6-only 绑定 / 进程树被
收 / 僵死 wedged）。`nohup` 只挡 SIGHUP，挡不住这些。

**方案**：`~/Library/LaunchAgents/com.devin.dra.{replay,vite}.plist`
——RunAtLoad + KeepAlive + ThrottleInterval=10，崩溃后 ~10s 自动
拉起，重启/登出自动恢复，不再依赖任何终端。vite 用 node 绝对路径
直跑 `frontend/node_modules/vite/bin/vite.js`（绕开 nvm PATH 依赖）。

**回放 db 挪位**：`/tmp/ra_v13_gate.db` → `data/refs/ra_v13_gate.db`
——/tmp 重启即失，launchd 常驻服务不能指向它。

管理命令：
```bash
launchctl list | grep dra            # 状态（PID/上次退出码）
launchctl unload/load -w <plist>     # 停/启
tail -f ~/Library/Logs/dra-{replay,vite}.log
```

注意：KeepAlive 下 `kill` 不是停机方式（会被自动拉起），停服要
用 unload。**这两个 agent 是开发便利设施，不属于仓库内容**——
plist 存在 ~/Library，换机/换人要重建。

### 70. 工作流三级层级 + 完成自动收起（2026-10-18，用户定）

**动机**：B1 细粒度事件让每轮检索/抽取炸出 50+ 平铺行，
跑完的历史把正在跑的挤没了。用户拍板：层级化 + 跑完收一行 +
**跑完默认全收**（只留 4 行概要 + 报告入口）。

**三层结构**：L0 进度轨（已有）→ L1 大环节（一行折叠条）→
L2 环节组（粗粒度事件当「组长」）→ L3 明细行（细粒度事件流）。

**归组规则**（workflow.ts `groupSteps`）：细粒度事件按 kind
挂到同类粗粒度汇总行下。两种挂法对应两种时序——forward 组长
最后落地（retrieve/extract/gate，攒的孤儿被它收编）；backward
组长先落地（write_dispatch → write_task 流式挂入）。孤儿明细
没等到组长就平铺，宁可显示也不藏（中途断掉的 run）。

**自动收起的触发**：组=组长落地那刻；大环节=不再是最后阶段那刻。
都是「转换时刻」触发（`useEffect` 依赖 auto/defaultOpen 变化），
用户手动展开后不被顶回。运行中的组不折（B1 的流式心跳就是看点）。
⚠️ 计数穿透折叠——组行尾 ⚠n、阶段徽标都计入 children。

**顺带补了两个整段被吞的粗事件**：`write_audit`/`write_revise`
原来 default→null 不上屏——自检→返修→复检是写作叙事最关键的
一环。审查显示 `6 项发现 · 高严重 3 · 需返修`，复审显示
`复查 3 项 · 3 项未闭合` + check_detail 状态码翻人话
（changed_form=「换了措辞，问题还在」）。
顺手修了 labelOfWrite 撞名：write.py 子图收口事件叫「返修」
会和 write_revise 撞，改「写作汇总」。

**验收**：esbuild 直跑 buildPhases 喂 v13 fixture——
130+ 平铺行收成 27 顶层环节（抽取1轮 ▸35 条、闸门2轮 ▸5 条、
并行写作 ▸8 条、返修 ▸3 条），tsc/build 绿。

**遗留**：v13 fixture 在审批接缝处有重复的 assess r1 事件
（上半尾一条 + 下半头一条），界面上「评估（第1轮）」出现两次——
这是录制接缝的数据事实，真实新跑不会有，不修。

### 71. 视觉重构：暖纸文档感（2026-10-18，用户选 C）

四选一的方向题（Linear 深色 / Stripe 冷灰 / Notion-Readwise 暖纸 /
编辑杂志风），用户选暖纸——产品本质是「交付一份 4 万字报告」，
阅读面 > 监控面。

**最大杠杆是 token 重映射，不是逐文件改**：
- `gray` 整阶换成 warm stone（40+ 处 gray-* 一键变暖）；
  `white` 重定义成 `#FCFBF7` 暖白；新增 `paper #F8F6F0`（主底）/
  `paper2 #EFEBE2`（侧栏，拉出桌面/纸张两层关系）。
- ink `#26221D`、muted `#7C756C`、line `#E6E1D7`、accent `#2E50C8`
  （蓝保留，cream 上的蓝就是 Notion 味）、live `#3E8F4F`。

**签名件**：
- **衬线**只用在报告 h1/h2 + 首页欢迎标题 + 报告态页头——
  `fontFamily.serif` 走系统栈（Songti SC/STSong/Noto Serif/Georgia），
  不引外部字体（依赖洁癖+离线）。
- **主按钮改墨色实心**（Things/Craft 式）：彩留给「可点」语义，
  纸面上的主行动用墨。
- `::selection` 暖米 #E8DFCB——默认蓝底白字和纸底打架。

**踩的坑**：`.btn` 原来裸写在 unlayered 层，优先级**高于**
utilities——`btn-primary px-5` 里的 px-5 是死代码（审批页
「通过」按钮的大 padding 一直没生效）。挪进 `@layer components`
后 utilities 才能正常覆盖。

打印强制回纯白 `body`（纸色不打印）。

### 72. 视觉重构二轮：图标系统 + 交互细节（2026-10-18）

承接坑 71 的方向继续产品化，这轮是「设计和细节」层面。

**图标系统**（components/Icon.tsx）：手写内联 SVG，零依赖。
16px 网格 / 1.5px 圆头描边（lucide 风格），`stroke=currentColor`
自动跟字色。~16 个：chat/book/cog/play/plus/search/copy/download/
printer/arrow-up/chevrons/alert/check/x/folder/trash/external。
替掉了全部 emoji 与字符符号（💬📚⚙＋▶«»↑▴▾▸✗）——
emoji 渲染随平台漂移、笔画粗细不一、不能控制颜色尺寸。

**引用 chip 保留**（用户拍板：倾向工具产品风，不走上标细字
的出版物风）。

**交互细节**：
- tailwind 加暖调阴影档 `soft/panel`（`rgba(60,50,35)`），
  对话框/抽屉/浮层全走 panel + backdrop-blur。
- 折叠组「N 条」按钮改 chip 质感（rounded-full 浅底）。
- TOC 开合 ▴▾ → 旋转 chevron；blockquote 去掉 accent 蓝
  （accent 语义=可点，引用块不可点）换暖灰线+paper2 底；
  inline code 换 paper2 暖灰底。
- 统一空态组件 `Empty.tsx`：图标 + 一句话，替掉三处裸文本行
  （来源库空/搜索无命中/会话空）。
- `document.title` 的 ⏳✋✓✗ 保留——那是 OS 标签页状态，
  SVG 管不到，emoji 在那里是对的工具。

**验证**：build + tsc 过；vite 进程走 launchd 自愈（kill -9 后
~10s 端口恢复），tailwind.config 改动需要重启 vite 才生效。

### 73. done 不落视图 + 流式归组前置 + 报告残留清理（2026-10-18）

**两个 done 缺口**（用户报「收尾一直卡住、看不到报告」）：
- 前端 SSE `done` 处理链全程没人 `setView('done')`——live 视角
  view 永远卡 `'running'`，呼吸点一直闪。修在 `loadApproval` 的
  `status==='done'` 分支：done 事件/断线轮询/刷新三条路都受益。
- 回放器续跑只落 DB 没发 `done` 事件，前端连处理链都进不去。补上。

**回放会话克隆真数据**：`_start` 把 donor 的 report+source+chunk
克隆进新会话（段号靠 rowid 次序算——`ORDER BY rowid` 是刚需，
`ORDER BY id` 是随机 hex 序；source_id 要重映射防主键撞；
donor 必须限定「有 source」，否则克隆到克隆、断链）。

**流式归组**（用户要求「明细出现时就缩进，不是跑完才收进去」）：
`groupSteps` 把 pending 桶换成**合成组长**（`live-N` id，
label「检索进行中」等）——首个明细到达即入组缩进显示；
真组长落地时原位替换，孩子移交。终态结构与原来一致
（esbuild 直跑 fixture 验证：0 残留合成组长）。

**活跃点**：StepRow 的 `live`（本环节最新产出行）现在也驱动
Dot/标签绿色——原来只有阶段头和收尾合成行是绿的。

**报告残留**：v13 存量报告里有装配泄漏（```` ```markdown ````
围栏包住 `# secN` 任务头，整节渲染成代码块）。DB 存量已清
（全部 report 行 41270→41171）；渲染层 `cleanReportMd` 兜底。

**新增 `flow` view**：报告头部「← 回看流程」→ WorkflowView 以
`running=false` 全折叠回看；顶部 sticky 条给「查看报告 →」回程。

### 74. 绿点不走的两层根因 + 流式文字（2026-10-18）

**「绿点不跟流程走」其实是两层 bug 叠的**：
- `_live` 合成组长（坑 73）故意不给 `state:'running'`——
  给了它就被 lastReal 排除，live/green 会错落到前一行。
  但 `PhaseBlock.running` 当时是从 steps 里推的 → 合成组长
  在时整个环节判成「没在跑」→ live 全灭、组行收起。
  修：`kind:'_live'` 标记 + PhaseBlock 认它。
- 更底层：`running` 从 steps 推本身就不对——**LLM 静默间隙**
  （assess/audit 单次 ~77s）steps 里既没有 running 也没有 _live，
  绿灯只在事件到达瞬间闪。修：running 改由「view==='running'
  且这是最后一个非空环节」（`active` prop）驱动，静默期绿点
  停在最新产出行。

**流式文字**：`useTypewriter`——活跃环节里新挂载行的摘要
逐段打出（~28ms/跳）。只在活跃环节启用（回看/已完不重打）、
`prefers-reduced-motion` 直显。

**开关组统一**：`.sel`（左竖蓝条）被误用在段落控件/筛选条上
——那是**列表选中**的语义。段落控件激活态改墨色填充
（`bg-ink text-paper` + 轨道底），与 btn-primary 同语言；
`.sel` 本身也去蓝条换暖调深纸色（用户：蓝竖线和暖纸不搭）。
蓝只剩「可点」语义：focus 环/外链/引用 chip/拖拽指示/进度条。

**会话卡片化**：侧栏条目白底+hairline+`shadow-soft` 立在
paper2 轨道上；「新建研究」换回 `btn-primary` 墨色
（用户拍板，撤回此前的降权设计）。

### 75. 写作正文走结构化交卷（2026-10-19）

**现象**：v13 写作草稿里 `write_section`/`write_revise` 的正文被污染，
四类形态全部实测到过——开头前言（「下面是我重写的 sec2 正稿」）、
英文推理/自检混进正文、MiniMax 伪工具调用标记
（`]<]minimax[>[<tool_call>…`）当正文、```markdown 围栏包 `# secN` 头。
最糟的一次：返修输出**只有英文推理**，却覆盖了本来完好的旧稿。

**根因**：`run_tool_loop` 把最后一轮的**全部 text 块**当正文，
`_split_sections` 只 `strip()`——模型在正文前后说的任何话都成正文。

**方案**：正文改走 `submit_section` 工具参数（`sections[].body`），
schema 与实验版逐字一致（`tests/exp_submit_section.py`）。

- `meta["via"]` 记账：`voluntary` / `forced` / `forced_retry` / `fallback`，
  下游据此决定信不信任这份产出。
- `forced_thinking: disabled`（`StageConfig` 新字段，**唯一合法值**，只准
  MiniMax）：强制交卷轮只负责把已想好的正文填进参数，不需要再想。
- MiniMax 关思考+点名工具会编造调用或回文本——实验 1/10、
  生产验收实测 ~1/8～2/8——`run_tool_loop` 在环节配了
  `forced_thinking: disabled` 且首轮没交上时，**再关思考**重试一次
  （首次强制不传 thinking，让老桩不炸）。⚠️ 重试**不开**思考：
  开思考的强制轮会先把预算烧在 thinking 上（v13 t7 验收实测
  8192 token 想完一字未写，两轮烧 ~17k 后照样失败）——
  编造是随机事件，同档位再来一次比换档位成功率高得多。
  重试仍可能失败：走 fallback 被判失败进降级，不会带脏文本入稿。
- fallback（没拿到交卷的自由文本）只做**无歧义的机械清洗**：剥整包围栏、
  去开头 `# secN` 头、污染检测 + 200 字符下限（验收抓到过 30 字符
  的「正文」被照收）；有任何标记或过短就整体判失败，
  **不猜哪段是正文**——猜出来的正文比缺一节更糟。
- 返修 merge 保护：新稿命中 `STRONG`（空/伪工具/英文整块），或旧稿干净而
  新稿 < 旧稿 40%，就保留旧稿并记 `rejected`（进 `write_revise` 事件的
  `kept_old`）。旧稿自身是垃圾时短新稿照收。
  **⚠️ 40% 是经验值不是实测**，过严会误杀「确实该大删」的返修。

**实验数据**（真实 API，`tests/exp_submit_section.py`，n=5 × t7/t5r）：

| 方案 | t7 干净 | t5r 干净 | 输出 token |
|---|---|---|---|
| A 现行（自由文本） | 0/5 | 0/5 | — |
| B `final_tool` 交卷，开思考 | 1/5 | 3/5 | — |
| C 同 B，输出上限×2 | 4/5 | 4/5 | 16.8k / 11.4k |
| D 同 B，强制轮关思考 | 4/5 | 4/5 | **9.1k / 8.2k** |

**验收实测**（`tests/exp_submit_prod.py`，生产入口 `_run_task`，v13 真实任务）：

| 任务 | 基线（现行文本通道，n=5） | 改后（生产入口） |
|---|---|---|
| t5 返修 | 0/5 干净 | 5/5 干净（n=5） |
| t2 | 0/5 干净（3 次伪工具调用） | 5/5 干净（n=5） |
| t7（最难：证据多、正文长） | 0/5 干净 | **26/37 干净**（5 批：4/5、6/8、6/8、3/8、7/8，批间方差很大） |

- **没有一次脏文本入稿。** 不干净的 11 次 t7 里：约一半是 fallback 被判失败
  （进降级，不带污染入稿），其余是**交上来了但很短**（342/984/1393 字符，
  被 `too_short<1500` 标出，来自第 4 批）。**短正文的成因没有查清**——
  可能是模型真只写了这么多，也可能是预算被思考吃掉后写得草。
  首稿通道目前没有长度保护（返修有 40% 条款），这是已知缺口。
- 失败模式：证据读完后的那一轮思考吃光 `max_tokens`、一字未写 →
  强制轮（关思考）里模型仍想读证据，调 `read_segments/read_context`
  而不交卷 → 同档位重试也一样。**所以这不是 ~10% 的随机编造，
  对 t7 更像「模型还没读完」的结构性失败**——已由 P2 缓解
  （t7 8/8，见下面「浪费思考轮」段）。
- 验收中抓出并修了两处：重试开思考会吃光预算（改为再关一次）、
  fallback 放过 30 字符正文（加 200 字符下限）。
- **P2 固化后生产入口复测**（`exp_submit_prod.py`，无任何 payload 补丁，n=8 × 3 任务）：

  | 任务 | 可用 | 干净 | 输出 token | 耗时 | 正文长度中位数 |
  |---|---|---|---|---|---|
  | t7 | 8/8 | 8/8 | 4.8k（P0 现状 10.2k） | 56s（123s） | 3.3k（3.1k） |
  | t5 返修 | 8/8 | 8/8 | 5.7k | 51s | 4.8k |
  | t2 | 8/8 | 8/8 | 4.2k | 75s | 4.3k |

  24/24 干净，via：forced 23、voluntary 1，没有 fallback、没有重试。
  **局限**：n=8、单一模型（MiniMax M3）、v13 这一批任务；
  正文长度只是质量的粗代理，**没做内容精度的人工对比**。

**浪费思考轮已由 P2 解决**（2026-10-19 固化，实验
`tests/exp_submit_prod.py` 的 EXP_P，生产入口 `_run_task`，n=8）：

| 方案 | t7 可用 | t5r 可用 | 平均输出 token | 耗时 | 正文长度中位数 |
|---|---|---|---|---|---|
| P0 现状 | 7/8 | — | 10.2k | 123s | — |
| P1 非强制轮 `max_tokens`→2048 | 6/8 | — | 4.0k | 51s | — |
| **P2 = P1 + 强制轮钉「立即交卷」** | **8/8** | **8/8** | **5.1k** | **58s** | 3.6k vs 3.1k（不降） |

两个机制：① 环节配置 `round_max_tokens: 2048`，只压**非强制轮**
（读证据/决定交卷的短决策轮），强制交卷轮产正文、不受限；
② Phase B 首次与重试都在末条 user 消息后追加 `final_nudge`
（`write.py::SUBMIT_NUDGE`，措辞与实验版逐字一致）。
外加兜底：Phase A 调用抛异常（自愿交卷参数被 max_tokens 截断）
不再向上传播，直接进 Phase B——比退回单次调用多留一次
结构化机会。⚠️ n=8、批间方差大；取舍：长读证据轮会被截断，
等于少读一点证据（用精度换成本，属预期）。

**已知未解决**：
- 多节任务的 `sections[]` 归位**只有单测覆盖**，没在真实 API 上验过。
- 关思考强制轮「不交卷、去调读证据工具」：实测 t7 约 2/8、
  同档位重试救不回来——已由 P2 的 `final_nudge` 缓解（t7 8/8），
  但机制上它仍是关思考+点名工具的一条已知弱点。

### 76. `must_cover`/`not_yours` 被回成字符串时逐字拆成单字（2026-10-19）

**现象**：v14 真跑里 `write_plan_tasks` 回来的任务，
`must_cover`/`not_yours` 被存成了**逐字拆开的单字列表**
（8/8 任务各 12 个单字）。模型当次的原始返回没保留，推断是回了字符串
（普通串或 JSON 编码的数组，形态未证实）；v13 同路径是正常列表。
子 agent 拿到的「必须写到」要求因此不可读。

**根因**：`_normalize_tasks` 原来直接迭代
`(t.get("must_cover") or [])`——字符串可迭代，逐字展开，
正好被 12 项上限截成 **12 个单字**。同形态的老坑：
「可迭代 ≠ 是数组」，凡是 `for x in 模型字段` 都躲不开。

**修复**：`_as_str_list()`（write.py）——字符串先按 JSON 数组试解，
解不开就当**一整条**；None/空串归空。`must_cover`/`not_yours`
两个字段都走它。回归测试：`test_plan_tasks_forced.py` ⑬ 组。

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
6. **每个决定记进 `CONTEXT.md`**（原 `DECISIONS.md` 已删）
7. **自己判断错了要当场纠正并说明** —— 下面 76 条里至少有 6 条是我猜错之后
   被自己的实验推翻才写进去的

---

## 十、已知缺口（写下来，别假装没有）

| 缺口 | 状态 |
|---|---|
| `summarize` 37.5% 失败的病因 | **判定为平台瞬时劣化**（2026-10-18 第三次复测：丢失的 98 段原样重放 ×2 轮，24/24 调用全成、98/98 救回）。配置/内容假设均排除 |
| `summarize` 重试的真实收益 | **仍未验**——复测里零失败，重试路径根本没被触发。它是防瞬时抖动的兜底，真实收益要等下一次平台劣化 |
| 工具侧参数被截断 | **只有告警，没有硬防线**（Anthropic 形状返回已解析对象，抛异常代价大于收益）|
| `run_tool_loop` 的 MiniMax guard | 比真实危险条件**更宽**（把 3/3 正常的那种也挡住了）。保留：多挡只多花 token，漏挡是假证据进报告 |
| MiniMax 限流的 **token 维度** | 未测。40/min 只保证请求速率；真实调用 token 量差几个量级（本轮全量也没撞）|
| high vs low 的产物质量差异 | 未评估 |
| 前端写作阶段 | **已解**（A2 起）：fixture 用 v13 真跑重录含 write 全程；回放服务可随时从 `sse_event` 表再导出 |
| `bad_assignment` / `spec_conflict` 两类审查 | 仍无真实数据 |
| 写作段 ~90s 非 LLM 静默 | **未定位**——候选是 checkpointer 序列化大 state，与 TECH_DEBT #1 同根 |
| assemble 清标题不干净 | **双保险**（坑 64 后端已修 + 坑 73 前端 `cleanReportMd` 渲染兜底；v13 存量报告已清洗）。待下次真跑观察 `fences_unwrapped` |

---

## 十一、要改代码前先确认的清单

- 加 state 字段 → **必须在 `ResearchState` / `WriteState` 里声明**，否则 LangGraph 静默丢弃
- 改 provider → 读第六节（76 条坑）+ 「切换时必须一起改的东西」
- 写/改守门测试 → provider **钉死**，不许跟着 `defaults` 漂；桩要接在**正确的层**上
- 加一条新的发请求路径 → **数一遍同类路径有几条**（这轮栽了三次：空 tools 400、流式分支、截断检测）
- 断 provider 协议形状 → **逐个字段核对** usage / stop_reason / tools / tool_choice，
  不要假设「另一条路有的这里也有」
