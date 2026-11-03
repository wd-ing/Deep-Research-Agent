# CONTEXT · 交接文档

> 这份文档的定位：**接手这个项目前必须先读的东西**。
> 架构、仍然有效的决策、踩过的坑（84 条）、工作约定、已知缺口。
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

## 八、踩过的坑（84 条 · 只留会再踩的）


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

**2026-11-04 决定（用户）：删除。** `config/search.yaml` 已 `git rm`（完整原文可用 `git show acccd62:config/search.yaml` 取回；代码里唯一的引用是 `tavily.py` 开头的 docstring，已把「禁用 /research 端点」的理由内联，不再指向该文件）。原文件里**仍然有参考价值的设计意图**（注意这些是当年的意图，不是现行配置，现行值以代码为准）：①分工——Tavily 主英文检索，SearXNG 主中文检索且不耗 Tavily 额度，两者都实现 `SearchProvider` 接口；②SearXNG 惰性启停（`.searxng/searxng.sh`，每次检索 touch heartbeat，空闲约 60s 回收），引擎 sogou/360search/quark/bing；③Tavily 用 `search_depth: basic`（1 credit），**禁用 /research 端点**（自带编排、4–250 credit/次，与跟随式循环冲突）；④抓取礼貌性：同域名间隔 ~1s、并发 8、遇 403/429/503 直接跳过、绝不重试/不换 IP；⑤含「最新/近况/现状…」等时间敏感词的问题强制走网络、绕缓存。**以后要把这些参数做成可配置，请新建被代码真正加载的配置，而不是恢复这个文件。**

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

- C1：复制 MD / 下载 .md / `@media print` 打印样式（打印=PDF，零依赖）。（2026-10-22 起这些入口收进顶栏「导出」菜单，见坑 86）
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

### 77. write_audit 在 M3 上思考吃光预算，迁到 M3.1-Flash low（2026-10-19）

**现象**：v14 重跑里 `write_audit` 三次调用全部被 `max_tokens` 截断
——预算梯度 8192→16384→16384，第 3 次还是原样重发。
开思考实测 5 次：输出 15.5k~25k token、217~349 秒，
结论不稳（high 0~2 条）、1/5 JSON 解析失败；
思考内容是逐节清点 + 反复「OK finalizing」——烧的都是巡回确认。
（`tests/exp_audit_*.py`，样本 `/tmp/exp_audit3/`）

**试过的岔路**：M3 上「关思考 + 强制工具」能用（5/5 真调用、
2.5k~7.2k token），但**质量不行**——high 数暴增 3~8 倍，
还把「草稿已如实声明的缺口」判成 high（违反 no_material→blocked
的分流规则），need_rework 10/10 全为真，等于审查退化成全报警。

**方案**：换 **MiniMax-M3.1-Flash + effort low**——M3.1 的
`output_config.effort` 是**真**旋钮（low 单次 7~12k token，
medium 8~17k，high 反而更糟：24.6k 仍被截断——**只能用 low**）。
⚠️ M3.1 **关不掉思考**（`thinking.type=disabled` 直接 400），
所以 `reasoning_effort` 只能当 truthy 开关发 adaptive，
深度归 `thinking_effort: low` 管；`validate()` 会拦死
reasoning_effort 缺省 / forced_thinking 这两种写法——
write_section/write_revise 必须留 M3 就靠这道护栏挡「顺手迁过去」。
⚠️ 型号名必须写 `MiniMax-M3.1-Flash`：`MiniMax-M3.1` /
`MiniMax-M3.1-Preview` 会被平台**静默别名回 M3**。

**M3.1 的新毛病**：工具入参 2/16 偏离 schema——整个结论套在
`audit`/`audit_result` 多余键里、列表包成 `{"item":[...]}`、
`need_rework` 是 `"true"` 字符串或带 `$text` 的 dict，
blocked 甚至漂成了 `blocker` 键名。
把这类结果当「审查通过」会静默放过问题，所以加了
`_normalize_audit_input`：解包两层、`item` 展平、`$text`/字符串
转 bool；归一不了判无效 → `node_audit` 重试一次
（429/HTTP 类与 ConfigError 不重试），仍不行走原有降级分支。
⚠️ 判无效是**严格**的：`need_rework` 给不出 bool、`findings` 或
`blocked` 键缺席都算无效——`blocker` 那种漂名不设别名白名单
（不猜模型会飘成什么键），代价是这类样本要多烧一次调用，
换来的是整栏不会静默丢。

**修复点**：`config/models.yaml` 新增 `providers.minimax31` 档案
（write_audit 钉死它，不跟 defaults.provider 走）；
`node_audit` 改用 `complete_json_forced` + `AUDIT_SCHEMA` +
`submit_audit` 强制工具；`_normalize_audit_input`；最多 2 次尝试，
事件记 `attempts`；`StageConfig.validate()` 的 M3.1 护栏。

**局限**：实验每格 n=3、只跑一份稿子；M3.1 的审查质量只抽读了几条
high，「不漏判/不误判」没有系统对比过——换稿或改规约后要复测。

---

### 78. 偶发空交卷丢一整节 + revise 救回后 failed 没清（2026-10-19）

**现象**：v14 真跑里 t2 交卷返回了工具结果但**正文全空**
（`_run_task` 报「交卷但正文全空」），整节就这么丢了——
同一任务拉出来复测 6/6 成功，纯属偶发。更糟的是返修把它
救回来之后，终稿「存疑与未完成」里**仍**写着
「以下任务未能完成：t2」，而 sec2 正文明明已经在报告里——
报告说假话。

**根因**：两个独立缺口叠在一起。① `node_dispatch` 对失败任务
从不重试，一次随机事件就是终审判决；② `node_revise` 只往
drafts 里并新稿，从不回头清 `failed` 里的旧记录——那条
「t2 失败」于是留着被 `node_final` 原样读出来。

**修复**：① dispatch 一批收齐后对**可重试**的失败重跑一次
（判据同 `_audit_retryable`：429/额度/HTTP 状态/ConfigError/
「没有分到证据」这类确定性错误不重试；每任务至多一次，仍败则
`reason` 记最后一次的）；事件加 `retried`/`recovered`。
② `node_revise` 返回时把 `failed` 里「drafts 已有非空正文」
的记录滤掉，事件记 `cleared_failed`——`WriteState.failed`
是普通 list（非 append reducer），直接返回过滤后的列表即生效。

**要如实承认**：t2 那次偶发空交卷的**根因没查清**——这两处
都只是兜底（重试 + 清记录），不是把病治好。若复发率高，
得回头抓工具循环的原始返回。

---

### 79. 三类确定性缺陷漏进终稿：护栏放在审查前（2026-10-19）

**现象**：终稿正文里漏出三类**程序 100% 可查**的缺陷——
① 内部任务编号句（「S29·8 在 t6b 的任务范围内，本节仅在……」）；
② 伪引用（`[S21 系报道口径,按规约交叉印证要求须与 S18、S20、S23、S24·1
交叉印证后再用]`）；③ 区间引用（`[S29·1—S29·7]`）。
而程序早就检出的 `violations` 只展示、不进返修——等于白查。

**设计**：护栏放在审查**之前**。dispatch/revise 收稿后先跑
`_auto_fix_drafts`（R1 括号规范化 / R2 伪引用取首 token /
R3a `secN` 换节名 / R3b `tN` 保守删句，纯函数幂等）；能修的程序修，
审查模型只看到修过的稿。修不掉的由 `_check_internal_ids`/
`_check_citations` 报成残留，`_check_all_citations` 并轨；
`node_audit` 把残留直接转成返修目标（`section=None` 走任务原始
covers；任务已被模型 high 点名时，违规文本并进它第一条 target，
不另立目标），`need_rework` 改为「模型说不用返修但残留还在 →
照样返修」。审查提示词同步改一句：格式问题程序管了，别重复提
finding。

**R3 的教训（首版推翻重做）**：首版 R3 见到 `tN`/`secN` 就删整句，
真实 drafts 复扫 7 处删句里**至少 4 处在删实质内容**——写手用
`secN` 指代别的节时，句子本身是正经判断（「……更可能是 pilot 与
小批量产线元年——另见 sec10」「设备/工艺细节……需在 sec6 与 sec8
中交叉印证」「（2）（3）两条口径提醒」）。教训：编号是脏的，但
**带着编号的句子不一定是元说明**。于是拆开：R3a 把 `secN` 换成
「{大纲标题}」一节（自指换「本节」，表格行原地换，标题行不碰，
查无此节留残留）——只替换、永不删；R3b 只删 `tN` 任务编号句，
且加了新闸门：句子里带 `[S` 引用就不删（可能是实质内容）、
表格/标题/超 120 字不删——保守地留给返修按语义处理。

**已知边界**：
· R3b 仍可能删掉「句子实质内容 + 句尾 tN 元说明」的混合句，
  但 `[S` 引用闸门把最常见的实质形态挡住了；剩下的进残留→返修。
· 区间/并列展开只保留 allowed 内的段；全越界、跨来源区间
  （S29·1—S30·2）、>12 段的区间一律不碰。
· 「第 N 节」这类中文序数交叉引用未做——只认 `t\d`/`sec\d` 字形。
· 换名依赖 outline 的 title，长标题会让句子变啰嗦（真实样例
  「另见 sec10」→「另见『产业化时间线判断：……』一节」），可读但笨。

**误伤扫描**（`/tmp/auto_fix_scan2.txt`，如实数字）：
v14 真实 drafts 上重做后共修 15 处——13 处 R3a 换名（零删除、
内容零损失）、1 处 R1 区间展开、1 处 R3b 删句（`S29·8 在 t6b 的
任务范围内` 那条纯元说明，无引用可删）；**删句类误伤降到 0**
（首版 7 处删句 ≥4 处误删实质内容）。修后残留只剩 2 处
（t5 的 S21 伪引用首 token 越界、t6a 的 S10·1 越界引用），
都会按新逻辑转成返修目标。

---

### 80. 整节重写式返修会带出新问题：改成定点补丁（2026-10-19）

**症状**：`node_revise` 对被审查点名的任务整节重写
（`_run_task` revise 路径，工具循环读证据后重写全文）。
重写本身是生成任务，会顺带写出新缺陷——真实运行里审查提的
**4 个 high 修完变成了 7 个**（重写稿里的新问题又被下一轮
审查逮住）；而且每次都重读证据、重写整节，token/时间重。

**设计**：把「改」从生成降成机械替换，分三层——
1. **审查产出可定位指令**（`AUDIT_SCHEMA` findings 加
   `quote` + `action`）：`quote` 是草稿里逐字摘出的那句，
   `action` ∈ `edit` / `delete` / `rewrite_section`。
   `WRITE_AUDIT` 提示词新增「修改指令要能被机械执行」一节，
   明说 quote 复制错了会退化成代价大的整节重写。
2. **每任务一次强制调用产 old→new**（新环节 `write_patch`，
   `submit_patches` 工具）：`patches:[{item,old,new}]` +
   `unresolved:[{item,reason}]`。程序只组装输入——修订项
   （小节/原句/问题/改法/**整段上下文**）、允许引用的段清单、
   修订项文字点过名的段全文（≤6 段×1500 字）。
3. **程序精确匹配 + 校验后才替换**，没被点名的文字逐字节不变；
   任何一步不过都**回退旧的整节重写路径**（`_run_task` revise
   保留，不删）——补丁是优化，旧路径是兜底，不是替代。

**校验规则**（按序拒收，理由进 `write_revise` 事件 `patch.rejected`）：
`old` 空/未找到/不唯一（strip 后可再试一次）→ `new==old` →
扩写过度（`len(new) > max(3·len(old), 300)`）→ `new` 先过
`_auto_fix_text` 再查 `_check_citations` / `_check_internal_ids`
/ `_contamination & STRONG`，命中即拒。被拒 item 所在**整节**
回退旧路径（该节全部 asks 一起喂）；模型明确放进 `unresolved`
的项**不**回退（白烧一次也改不了），进 `state["revise_unresolved"]`
→ `node_final` 存疑节写「返修未能解决」一行。
程序违规目标（坑 79 的 `section=None` target）也走补丁：
`_violation_items` 把每条残留违规按句定位成 quote=整句 的补丁项。

**`write_patch` 配置**（models.yaml）：M3 + `forced_thinking: disabled`
+ `reasoning_effort: low`——**关思考是取舍**：这是纯编辑任务，
改哪里（quote）、怎么改（fix）审查已给定，开思考只会推它顺手润色
没点名的文字。形状抄 `tests/exp_audit_nothink.py` 的 V1（关思考+
强制工具，实测 5/5）。风险与 write_section 同款：MiniMax 关思考+
点名工具偶发编造调用（~1/8）——靠「重试一次（`_audit_retryable`
同款判据：429/5xx/ConfigError 不重试）+ 校验不过回退整节重写」兜底。
`reasoning_effort: low` 是给 `_check_minimax_thinking_tools`
（发工具必须开思考的配置护栏）看的，真正关思考靠 `forced_thinking`
——同一机制在 write_section/write_revise 已验证。
⚠️ 不许迁 M3.1：`forced_thinking: disabled` 在 M3.1 上被
`validate()` 拦死（那个模型关不掉思考），这是有意的护栏。

**向后兼容**：缺 `quote`/`action` 的 finding（老 fixture、模型漂移）
`action` 落 `rewrite_section` → 直接旧路径，不发补丁调用；
`_normalize_patch_input` 缺 `patches`/`unresolved` 键判无效 →
重试一次 → 仍不行该任务全回退。

**局限（如实）**：真实数据上的收敛性（补丁命中率、回退率、
unresolved 占比）**还没有验证过**；省多少 token/时间也**没有实测**，
离线测试只证明了机制正确。补丁粒度是「句级替换」，跨段的大改、
结构调整仍会落到整节重写——那是设计内的分工，不是缺陷。

### 81. 提示词 A/B：补丁可用性、规划 grounding、写手规则、禁写清单（2026-10-19）
**背景**：终稿里仍有「无证据的具体数字/年份」（例：sec2.3 的 1978/1987/1988/2014/2022 与 711 Wh/kg，来自探针轮廓里的二手转述，被规划写进 mandate/must_cover，写手照办；sec10 的「70 USD/kWh」——⚠️ 这条后来查实是误报：证据原文是 "70 US dollars/kWh"，见坑 82）。审查拿不到证据原文，所以抓不到。用户提出先用提示词处理，做了四组 A/B（脚本 tests/exp_ab_{plan,write,audit,inject}.py，原始数据 /tmp/exp_ab/，均在 /tmp/ra_exp_ab.db 副本上跑）。
**E3 审查提示词 V0(7f1eaac) vs V1(HEAD，加了「整段 delete/edit」「标题行不当 quote」)，各 n=4，同一份草稿**：模型提的 high 里可补丁比例 V0 9/9、V1 18/19（均不含程序追加的违规目标）；`rewrite_section` 两组都是 0；标题行当 quote 两组都是 0；每次 high 数 V0=2,3,3,1，V1=5,4,3,7。**n=4 分辨不了真实运行里那次回退（19 条里 1 条）这种低频事件**；V1 的 high 数每个序号都不低于 V0，原因未查，不能当结论。保留 V1。
**E1 规划提示词 B（要求 mandate/must_cover 里的数字/年份必须能在分配的 segments 里找到），各 n=6**：规划产出的数字/年份 token 里「不在该任务分配证据全文里」的比例 A 121/299=0.405、B 76/211=0.360；每次均值 A 20.2、B 12.7（含各 1 次退化运行）。t2 的 1978/1987/1988/2014 在 B 的 5 次有效运行里仍有 4 次出现。B 的 plan_spec 更慢更长：77–102s、6.2–12.4k 输出 token（A） vs 136–268s、10.0–20.2k（B）。口径说明：token 判定是子串匹配，没做 ℃/°C、美元/USD、区间符号的归一化，存在假阳性，比例只能当量级看。
**E1 的意外发现**：12 次规划里 2 次（A_4、B_5）只交了 1 个任务（t1，covers=sec1），8 个小节没有任何任务，`plan_failed` 为 null、无告警。已加覆盖校验+重试一次（`node_plan_tasks`，tests/test_plan_coverage.py）。生产里该现象的真实发生率未知（此前真实跑 5 次未见，实验 2/12）。
**E2 写手规则 B（WRITE_RULES 加第 8 条「必须写到/mandate 是提纲不是证据」），t2 各 n=4**：forbid token 出现在正文且所在段落无 `[S` 的数量 A=5,2,5,4（均值 4.0）、B=6,5,5,7（均值 5.75）——**无改善**。模型把 mandate 里的明确要求（「须标注为单一中文来源」）当成高于通用规则的指令。
**E4 逐任务「无证据清单」注入 C / 同时改写 must_cover 的 D（forbid 由程序对照任务 segments 全文算出）**：t2 的 in_body 均值 A=7.00、C=5.50、D=5.75；in_body_nocite A=5.00、C=5.00、D=4.50；`gap_marks`（本次材料未覆盖/未找到独立信源）A=0.50、C=5.25、D=5.50。t3（n=3）in_body_nocite A=0.00、C=1.00、D=1.00，numeric_nocite_paras A=0.67、C=1.33、D=2.67。读正文：C/D 把写手从「当事实写+标注单一来源」改成「在缺口声明里把这些年份/数字逐条列出」，**具体数字仍进了报告**，只是框架变了；D 没有清洗 mandate 和 spec 里的同一批数字，写手从那里照样看得到。
**结论（只写事实）**：这四组实验里，提示词层面的改动没有稳定减少无证据具体数字进入正文；变化的是表述方式。规划产出的数字里约三到四成不在任务证据里。**尚未验证的方向**：① 在规划后用程序把 mandate/must_cover/spec 对应小节里「不在该任务证据全文」的数字/年份清洗掉再交给写手；② 对终稿正文做「数字/年份是否出现在该任务证据全文」的确定性核对并走补丁路径。对当前终稿 drafts 的粗扫：350 个 token 里 86 个不在对应任务证据全文——⚠️ **这个数字大部分是度量假阳性**（US dollars/USD/美元、billion/亿、℃/°C 写法不一致），宽松口径（只比数字串）重测只剩 21/370，且几乎全部来自探针页面泄漏而非写手编造，见坑 82。本坑里「提示词无效」的结论不受影响，但「86/350」和「70 USD/kWh 是真问题」这两条作废。
**实验附带事实**：E1~E4 过程中出现过 `RemoteProtocolError`/`SSL EOF`/`output new_sensitive (1027)` 等瞬时错误，均被现有重试/强制交卷路径兜住；`llm_call.session_id` 有外键，假 session 不预插会被 `_record` 的 `except: pass` 静默丢记账（做实验归属时踩到）。

### 82. 「无证据数字」不是幻觉：探针深读页面从未进入证据库（2026-10-20）
**结论先行**：坑 81 里追的 sec2.3 的 1978/1987/1988/1993/1997/2004/2014/2022、711 Wh/kg，sec6 的「~1300 项专利」，sec3 的 0.1 Ah/100 Ah/1000 圈，sec4 的 400–800 USD/kWh、3–5 倍，**全部是真实页面里的原文**（我重抓了探针当时读的 3 个页面逐个核对，除 800 ℃、100 GWh 两个缺口声明外都在页面前 6000 字内）。不是写手编造，是**证据边界泄漏**。
**路径**：`node_probe_deep` 抓 2~3 页（`bypass_cache=True`，连缓存都不写）→ 喂给 `probe_outline` → 页面就丢了；轮廓里的事实（带 `evidence_urls`）→ `assess` 当缺口复述 → `_brief` 把「探针轮廓」原样给规划 → 规划写进 mandate/must_cover，并**自己发现并补了一条 cautions**：「工程院文章（被引于探针轮廓但未列入 S 编号清单）…须标注单一中文来源」→ 写手照办，无引用写进正文。9 个历史会话共 24 个探针深读页，**0 个**进过 `source` 表。所以提示词 A/B 全部无效（写手在服从规划的指令，规划在补偿一个结构缺陷），审查也抓不到（证据里本来没有）。
**度量更正**：坑 81 的「86/350」「70 USD/kWh 是真问题」作废——前者主要是单位写法归一化缺失，后者证据里写的是 "70 US dollars/kWh"；「660 亿元（约 96 亿美元）」也对（66 billion yuan）。宽松口径（只比数字串，去千分位）v14 终稿：370 个里 21 个找不到，去重 14 个，其中 19/21 在规划产物里也出现；唯二不在规划里的是 660/96 的 billion→亿 换算。工具：`tests/ground_scan.py`（下界估计，不比单位）。
**真正属于模型的小错误只有三处**：探针给 711 Wh/kg 凭空加了「2024 年」；t3 把 0.1 Ah/100 Ah 对比挂上 `[S31·1][S38·1]`（这两段全文里没有）；1300 项被改写成「业内普遍引用」。
**原生修法（a0c1812）**：`node_retrieve` 第 1 轮把 `probe.deep_urls_read` 作为种子命中放在最前，走现有抓取/去重/分段/extract/gate，不绕任何环节；事件和 run_trace 带 `seeded_from_probe`。测试 `tests/test_probe_seed.py`。
**真实验证（n=2，全新会话，同题）**：v15 `s_a12bd249a605`、v16 `s_4eb82376b337`，两次深读页都 2/2 落库、`on_topic=1`、过闸门并出段落概括，终稿里 711 Wh/kg 现在引用 `S2·7`（工程院论文）。v15 用 ground_scan 扫：437 个里 13 处找不到数字串，逐个对证据原文核对，12 个是单位换算（$830M→8.3 亿美元、$13.5 billion→135 亿美元、1970s→1970 年代等），仅「领先约 22%—32%」未能核实；规划产物里同时出现的只有 2 处。⚠️ 两次的大纲都退化成 1 节（见下），所以这只验证了「泄漏被堵上」，不代表多节大纲下的规划质量。
**顺带暴露的独立问题**：`probe_outline` 是手写长 JSON，**解析失败没有任何重试**（`complete_json` 不重试，`node_probe_outline` 的 except 直接降级，`guard.retry_on_empty` 只管空输出）→ 退化成 1 节大纲。全库 ~23 个会话里 3 次（v9、v15、v16），v15/v16 连续两次是同输入；n=6 重放 0 失败，生产失败原文没落库，只知道是中段 `Expecting ',' delimiter`（未转义引号或漏逗号），具体子类未能确认。`complete_json_forced`（强制工具）早就存在。**已修，见坑 83。**

### 83. 模型迁移（plan_spec/plan_tasks/assess → M3.1-Flash low）与 probe_outline 强制交卷（2026-10-20）
**依据**：`tests/exp_model_ab.py`，v14 真实输入，M3 vs M3.1-Flash low 每格 n=3，12 次全成功（原始数据 /tmp/exp_model_ab/）。`write_plan_tasks` 输出 16–38k→6–10k token，耗时 140–424s→34–62s；`assess` 3.0–7.0k→2.3–2.4k token，24–56s→18–20s；`write_plan_spec` **没有 token/耗时收益**（5.3–7.4k→6.3k，48–65s→55–59s），M3.1 的规约约长 35%（cautions/术语表约两倍，读过内容站得住）——因用户决定一并迁，不是因为有收益。规划里无出处数字占比 A 30%/B 18%，但 B_1 只有 11 个数，n 太小，不当结论。
**配置**：三环节 `provider: minimax31` + `reasoning_effort: low` + `thinking_effort: low`，不带 `forced_thinking`（M3.1 关不掉思考，validate() 会拦）；max_output_tokens 32768/24576/16384，plan_* 超时 420s。`write_section/revise/patch` 仍是 M3 + forced_thinking disabled。退路：把 `provider` 改回 defaults 的老配置。守门测试 `tests/test_stage_migration.py`。
**真实跑（v17，`s_f8be467ec99a`，同题，23.4 分钟，全程 MiniMax 529 过载）**：plan_tasks 5 轮工具循环 0 错、14 任务全覆盖、tool_reads=6；plan_spec 19 cautions/19 术语；assess 3 次，思考 1969 字符触发「偏高」警告（未处理）。这是单次样本。
**probe_outline 改强制工具 `submit_outline`**（`probe.py` `OUTLINE_SCHEMA`/`_normalize_outline_input`，最多 2 次尝试，判据与 write_audit 同一套，抽到 `subgraphs/_retry.py`），stage 模型仍是 M3 high。v17 一次成功、8 条 subtopics、结构齐全；**但 v17 的 probe_light 两次 529 → 没判级 → 没深读，所以 `seeded_from_probe=0`，本次没能再测种子注入**（v15/v16 已测过）。「手写 JSON 解析失败」这一条在 v17 没有复发，但样本只有 1 次，不等于已证明根治。
**新观察到的错误形态**：write_audit 流式收到 `MiniMax 流式错误: {'type':'api_error','message':'unknown error, 722 (1000)'}`，原先被判不可重试 → 整轮审查/返修被跳过。已把「流式 api_error」归入瞬时类（`_retry.py` 的 `_TRANSIENT_RE`），**未真实复现验证**，只有离线判据检查。
**未验证**：多节大纲 + 深读成功 + 种子注入同时发生时的规划质量；迁移后写作/审查的终稿质量与 v16 的系统对比。

### 84. 平台拥堵期：统一过载重试、无概括段兜底、run_health（2026-10-20）
**背景**：MiniMax 官方确认近几天不限额导致 M3/M3.1 都拥堵，预计持续 4-5 天。v17 里 137 次 M3 调用 36 次 529（M3.1 只有 10 次、0 次 529、1 次流式 722，样本太小）。
**查清的事实**：`_minimax`（complete_json 路径）遇 529 只用 `retry_on_transport`（默认 2）、等 3/6 秒；**`_minimax_tools`（complete_json_forced 与工具循环底层）零重试**，第一次 529 就抛。summarize 的 `SUMM_RETRY=1` 是立刻重发。结果 5 个概括批次整批失败，44/162（27%）相关段落没概括——`_brief` 只列有概括的段，这些段分不到任务、写手看不到，报告照出、无降级标记。relevance 耗尽后逐段补判再放行，不丢证据（v17 有 13 段 `llm_degraded` 放行）。
**修法**：① `provider.py::_minimax_send` 统一入口，`_minimax`/`_minimax_tools` 都走它；只重试 5xx 过载与流式 `api_error`，独立额度（`defaults.overload_retry`，5 次，10/20/40/60/60s ±25%，env `RA_OVERLOAD_RETRIES`/`RA_OVERLOAD_BASE_S` 可覆盖）；429/4xx/内容过滤/seat_quota 不在此层重试；`_minimax` 里 5xx 不再吃 net_left，避免两层各重试。错误前缀不变，`_retry.py` 任务级判据照旧；叠加后单次调用最坏 ≈ timeout_s + 190s，任务级再 ×3。`_openai_compat*`（xj189/opencode，非现役）**未接入**。② 每次重试在 `llm_call` 记一行 `status='retry'`（token 0，`error` 固定格式 `MiniMax 529 ... | attempt k/5 | wait Ns`），`cache_stats.calls` 排除 retry 行并单列 `retries`。③ `write.py::_seg_summary`：无概括时用「[无概括·取原文开头] + 80 字」，规划清单、写手清单、补丁清单都用；规划事件带 `fallback_summary_segments`。④ `node_finalize` 开头产出 `run_health` 事件（落 run_trace、重入幂等）：按环节的 ok/errors/retries/等待秒数、无概括段数/相关段总数/涉及来源、闸门降级段数、未判来源数、失败任务数；有非零项就打一行 ⚠️。前端对未知 kind 容忍（`workflow.ts` default 返回 null）。
**一个差点漏掉的 bug**：助手把 `_overload_retry` 函数插在 `ModelRegistry` 的方法之间，导致 `session_header` 变成函数内部的死代码（离线测试全过，因为 opencode 不是现役 provider）。review 时发现，已移到类外。教训：往类中间插东西后要核对类的方法集合。
**真实冒烟**：`complete_json("relevance")` 真实撞 529 → 6 次请求、5 条 retry 行，等 9/25/34/69/63s，最终抛 `MiniMax 529`；`complete_json_forced("summarize")` 第 1 次 EmptyOutput（模型回文本不调工具）、第 2 次成功。
**未验证**：完整端到端下 run_health 的真实输出；重试后真实能恢复多少批次（冒烟里 relevance 5 次重试全败，拥堵期恢复率未知）；summarize 的 EmptyOutput（文本冒充工具结果）在拥堵期发生率可能升高，`complete_json_forced` 目前没有空载重试。

### 85. 前端视觉重构（精密仪表 × Kimi 克制风，2026-10-20/21，4 轮，提交 24fbf9e…61f37e7）
**取代坑 71/72 的暖纸文档风。** 用户参考 Kimi 订阅页，先在 `/tmp/mock` 画静态稿（首页/报告/进行中 × 无衬线/文楷），用户确认「视觉很满意」并选**全无衬线**（不打包字体；Kimi 的 MiSans 非 OFL，没用）。
**视觉语言**：`paper #FBFAF9` 近白暖中性底，侧栏 `paper2`；文字 = 黑色 + 透明度三级（90/60/42）；1px 浅线 + 大圆角，**无阴影**；大数字指标（`.metrics/.big`）、刻度线（`.ruler`、`.wf-track`）、分段胶囊（`.seg`）、黑色单一主按钮。图标为 `Icon.tsx` 内联 SVG（16px 栅格、1.5 圆角描边，Lucide/Devin 风格），不引入图标依赖。
**颜色语义（别混用）**：蓝 = 可点/选中；绿 = 进行中；**朱红 `seal #C4412F` = 批注/存疑/降级/已排除/不相关/修订痕迹**；真红 = 错误/破坏性操作；来源性质用 `.tg1–.tg5` 五个固定色（NatureBadge 与来源构成同源）。原先通用的琥珀警告全部改成 seal 或中性灰底说明。`RoundChart` 第三序列因此改紫。
**四轮内容**：① 令牌 + 外壳 + 首页；② 报告页（运行状况条、修订痕迹、证据边注）；③ 流程页 + 事件历史还原；④ 设置/来源库/审批页/浮层/空状态。
**不编造数据**：稿子里的「累计来源、引用覆盖率、来源构成」后端没有对应接口，首页只显示列表接口真实有的字段（场数、进行中、已完成、token）。报告运行状况条里「句子带引用比例」是前端从正文计算的，口径含对比/过渡类无引用句，数字偏低不是 bug。
**新增后端端点（均只读）**：
- `GET /api/sessions/{sid}/review`：返回补丁明细（旧文/新文/前 40 字上下文/审查问题，最多 60 条，`write_revise` 事件落库）+ `run_health`。老会话返回空数组。补丁校验/应用逻辑未动。
- `GET /api/sessions/{sid}/events`：读 `sse_event`，只回 `node/progress/done/error`（不含高频 `thinking/draft`），保留信封、升序、跳过坏 JSON，超 5000 条只留最新并带 `truncated`；返回 `events/last_id/truncated`。修了「已完成会话回看流程显示『正在启动』」——`useSession.ts` 原先对 `done` 直接返回、不拉历史。
- `assess` 事件新增 `section_material`（`{id,hits,best}`），`write_plan_tasks` 事件新增 `section_segments`、`outline_titles`，供流程页「材料厚度」刻度。**`section_segments` 是近似**：一个写作任务可能覆盖多节，只有任务级分配；旧事件没有 `outline_titles`，章节标题可能缺。
**前端事件管线**：`useSession.ts` 的 `applyEvent(ev, live)` 由 SSE 与历史回放共用；先缓冲 live 事件 → 拉历史 → 按事件指纹去重（与服务端 event-key 一致）→ 冲刷缓冲。历史事件不触发通知、不改视图。
**报告「修订痕迹」**：只在有补丁明细时出现；旧句划线、新句 seal，宽屏右侧朱批卡对齐，窄屏回退浮层，打印时全部隐藏。定位不到的补丁显示「未能定位」，不猜位置。已知限制：补丁文字落在加粗/代码/链接内部、或正文中重复出现（只标第一处）时标不准；标记里不渲染加粗等格式。
**视觉验收流程**（用户要求「不能只看文件」）：`/tmp/pw` 下用 `playwright-core` + 本机缓存 Chromium（**不进项目依赖，脚本在 /tmp 里，不在仓库中，机器重启/清理会丢**）。`shot.mjs` 等页面加载后截 2x 图；`check.mjs`（第 1 轮 7 状态）、`check_r2.mjs`（15 项：打印、窄屏、无补丁会话、点引用开抽屉）、`r3.mjs`（历史还原、接缝无重复、done→flow、空历史、窄屏）、`r4_after.mjs`（设置/来源库/审批/抽屉/对话框/菜单/toast/空状态）。每轮断言：无文档级横向溢出、无超宽元素、无控制台报错、无 HTTP ≥400；每轮截 before/after 对照。回放数据来自 `sse_event` 导出的 fixture，前端 dev 在 `127.0.0.1:5180`，回放服务由 LaunchAgent `com.devin.dra.replay` 管理。
**回放器伪影（不是生产行为）**：回放服务的 `get_state` 从时间 0 起就返回 interrupt，早期回放会话看起来在等审批；回放会话 `s_65c68185fcfe` 的 3 条补丁和 run_health 是**合成演示数据**，截图里整段划掉的样子比真实补丁（通常只改短语）夸张。真实运行下的表现要等 MiniMax 恢复后跑一次完整研究才能验证。
**已知小瑕疵**：历史回放不含 thinking/draft 增量；运行中会话没有可靠的事件时间戳，流程页没做时间列；报告表格内部可能比容器宽约 8px（页面本身无溢出）；来源列表日期被刻意截断；`check_r2.mjs` 在与其他检查并行时出现过一次 hover 测量抖动，单独连跑两次 15/15；流程页「已用约 0 token」在回放会话里是回放数据没带 token 的结果，真实会话需再确认是否该隐藏零值。
**未验证**：真实长研究下的流程页/报告页观感；窄屏 + 折叠侧栏的组合只抽查过。

### 86. 交互收尾：会话行右键菜单、顶栏导出菜单、Word 导出（2026-10-22，提交 1e58201）
**会话行**：原 hover 四个 emoji 按钮（重命名/移文件夹/归档/回收站）删掉，改右键菜单（`Menu` 新增 `point` 定位，与 `anchor` 二选一，翻折/夹紧逻辑同源）；hover/聚焦只留一个淡「⋯」，打开同一个菜单（可发现性，用户未明确要求，可删）。菜单：重命名 / 移到文件夹（同一菜单内切子视图 + 「‹ 返回」，不套二级浮层）/ 归档↔取消归档 / 分隔线 / 移到回收站（danger）。键盘菜单键、Shift+F10 走原生 contextmenu 事件。编辑态不抢右键。回收站里的行不在此次范围。`MenuItem` 新增 `icon`、`hint`；图标槽与打勾槽复用同一位置（动作项用图标，选择项用打勾）。
**顶栏**：删「归档」（和右键菜单重复）；复制 MD / 下载 .md / 打印 PDF 合并为「导出」菜单：Markdown(.md) / PDF（浏览器「打印/另存为 PDF」，**不是服务端 PDF**）/ Word(.docx) / 分隔线 / 复制 Markdown（保留，避免功能丢失）。「回看流程」「修订痕迹」不变。
**Word 导出**：纯前端，`src/lib/exportDocx.ts`，`unified + remark-parse + remark-gfm` 解析 → `docx` 库生成；动态 import，独立 chunk（约 400 kB），主包不变大。依赖钉死：`docx 9.10.0`、`unified 11.0.5`、`remark-parse 11.0.0`（均发布 ≥7 天；最初装的 docx 9.11.0 不满 7 天，已降级）。覆盖标题/粗斜删/行内代码/真超链接/嵌套列表/引用块/代码块/GFM 表格/分隔线，`[S2·7]` 原样当文本；正文 eastAsia 字体 PingFang SC → Microsoft YaHei、11pt；输入与「下载 .md」同一份 `report`，没有第二套口径。
**验证**：构建零错误；`/tmp/pw/r5.mjs` 32/32（hover 只有 ⋯、右键菜单、子视图、Esc/外点关闭、ArrowDown、已归档会话文案切换、header 按钮集合、.md/.docx 真下载、剪贴板逐字节一致、900px 无溢出、无控制台报错）；.docx 用 python-docx 读出 256 段/Title+多级 Heading/1 表格/240 处引用文本，macOS `textutil` 可转换；check/check_r2/r3/r4_after 串行回归通过（r4_after 里点已删按钮的步骤已改走新菜单）。
**未验证 / 已知**：**真实 Word/WPS 里的排版没看过**（只验结构合法）；⋯ 按钮 hover 时压在长标题末尾；Markdown 与 Word 共用 `file-text` 图标，靠 `.md/.docx` 副文案区分；Word 导出不含修订痕迹/运行状况条等页面层元素；r5.mjs 同样在 /tmp/pw，不在仓库。

---

1. **测试范围**：改了哪块只测哪块，**用现有 session 续跑**而不是重跑全流程
   （省时间也省搜索额度）
2. **先核对后动手**：决策与细节核对完再写代码
3. **问题先报告**：测试遇到问题先报告、一起定位，判断是否要改架构。
   **小 bug 可以直接修，架构级问题必须先说。**
4. **能力优先**：能交给 agent 自主判断的不写成程序死规则；
   引导性规则优于硬约束，硬约束只用于合规性与底层限制
5. **结构性修改一律先说方案再改**
6. **每个决定记进 `CONTEXT.md`**（原 `DECISIONS.md` 已删）
7. **自己判断错了要当场纠正并说明** —— 下面 86 条里至少有 6 条是我猜错之后
   被自己的实验推翻才写进去的

### 87. 时间优化（第二轮，2026-10-25）：29.0 → 11.1 分钟的来路、被推翻的假设、没验证的部分
**目标（用户）**：更看重时间，token 能降更好；一次完整研究 25 分钟→15 分钟，能到 10 分钟内更好。精度红线：**明面上看不出差错**——小节不缺、上下无明显矛盾、引用不出错，不深究逻辑。「快速/标准」两档的设计推迟（快速=速度快、篇幅短、质量稍差，天然承载篇幅预算与返修轮数这类旋钮）。
**埋点（提交 7de5e6c，先于任何优化）**：`trace_span` 表（节点/子图节点/checkpoint/工具循环轮次/检索子步骤/限流等待）、`llm_call` 新增 `started_at/wait_ms/ttfb_ms`、`tests/timeline_report.py`（阶段瀑布、并发剖面、静默区间及「未解释秒数」）、`tests/e2e_http.py`（走真实 server+SSE+异步图，自动审批；同步图 `e2e_run.py` 的 checkpoint 路径不同，基线必须走 HTTP）。单条 span 写库 ~0.16ms。
**基线序列**（同题，固态电池产业化；`data/refs/ra_v18…v21_*.db`，全部带 trace_span）：

| | v18 基线 | v19 批1 | v20 批2 | v21 批3 |
|---|---|---|---|---|
| 墙钟 | 29.0 min | 17.9 | 16.4 | **11.1** ⚠️ |
| probe+outline | 220s | 80 | 44 | 44 |
| 检索（retrieve+extract+gate+assess） | 7.2 min（3 轮） | 7.0 min（3 轮） | 3.9 min（2 轮） | 4.4 min（2 轮，料大 53%） |
| plan | 116s | 114 | 151 | 225（spec 含 1 次 JSON 重试） |
| dispatch | 540 | 143 | 168 | 150 |
| 审查+返修 | 460（3 审 2 修） | 310 | 420 | **0（429，没跑）** |
⚠️ **v21 的 11.1 分钟不是可比数字**：write_audit 唯一一次调用被 `429 已达到 Token Plan 用量上限 (2056)`（账户额度耗尽，不是分钟级限流）打回，审查降级、返修一次都没跑，所以「审查→返修→跳过」链路在 v21 里**没被实测**。含审查+返修的真实总时长估计 ~13~14 分钟，**待额度恢复后重跑 v22 确认**。单次运行方差也大（v19→v20 写作段 566→740s，报告 82k→100k 字；LLM 阶段耗时随篇幅与输出 token 波动），不要把一次运行的差当结论。
**每批做了什么与依据**：
- **批1（2c9d1bf）**：dispatch 一次全并发（`RA_WRITE_WORKERS`，默认 12；原来每批 5 个先串行 1 个，12 任务=3 批×(串行头+等最慢)，整场并行度 1.31，而缓存命中只有 8.3%，预热回不了本）；outline/probe_outline 迁 M3.1-Flash low + 预算一次给 16384（A/B n=3：outline 中位 198.5s→10.6s，M3 版 3/3 都吃 1~2 次护栏重试）；审查策略 A（返修用完后**末轮不再调模型**，只做程序检查，事件 `write_audit_skipped`，不进报告正文）；搜索超时；概括并发 4→8；urgency 停止线（≥2 轮后 high 缺口≤1 就收工，归 `satisfied` 不亮「材料不足」）；护栏丢弃的尝试补记 llm_call。
- **批2（f4aeee8）**：返修补丁段/整节重写段各去串行头（整节重写头约 60s）；fetch 整体 deadline 20s、搜索超时 25→12s；修 urgency 缺键语义（`gaps_by_urgency` 只输出出现过的桶，缺 `high` 键=0 个，原写法 `.get("high",99)` 在最该停的情形不触发，测试用例全带显式 high 键所以漏了）；概括与相关性判定合并成一次调用（规则层优先，规则没放行才采信模型 `relevant`，缺 key 回落原 LLM 闸门；段级 relevance 调用归零，`gate_all` 源级串行判定仍在）。
- **批3（8ea177e）**：**返修默认 1 轮**（`RA_MAX_REWORK`，用户决定；v19/v20 第 2 次审查 113~155s 只换来 2~4 个目标，第 2 轮占全场 28%）；**写作任务预加载证据**（`RA_WRITE_PRELOAD`，默认开；首稿任务把分配段全文附进用户消息、只发 1 次强制交卷，块超 70k 字符回退多轮工具路径）；write_revise 强制交卷预算 4096→8192；审查入参归一化失败记原始入参与原因（`normalize_fail`）。
**预加载 A/B（v20 证据重建 13 任务，只跑 dispatch，n=3×2，`tests/exp_write_preload.py`）**：dispatch 145→95s、调用 51→13、write_section 输入 token 644k→~150k（usage 口径不稳）、wait_ms 243s→0、**violations 23.3→1.7、证据外数字 27.3→6.7**（出乎意料：模型直接看全文后「超出任务分配段」引用和编数字都少了）、总字数 95.6k→88.6k。v21 实弹：15/15 任务走 preload、0 回退、其中 1 个任务 `submit_section` 在 8192 输出上限被截断（forced_retry 55s 才成功）→ 已排进批4（write_section 预算 16384）。
**被推翻/修正的判断（本轮自己猜错的）**：
1. **「~90s 非 LLM 静默 = checkpoint 序列化」是错的。** 实测 102 次 checkpoint 写入合计 0.87s（最大单次 68ms）。真相：v18 大纲节点里 52s 的「静默」是**护栏重试**——第一次调用思考吃光 4096 预算、正文为空，翻倍预算重发，**被丢弃的第一次尝试没有 llm_call 行**，所以看不见（事件流里能看到 outline 思考了两遍）。v13 写作段 50s×2 的静默大概率同一机制（坑 77 的 8192→16384 截断梯度），但没有当时的数据能核对。**教训：失败/被丢弃的尝试必须记账，否则「静默」永远查不出**——现在 8 个护栏丢弃点都落 `status='retry'`、`error` 前缀 `guard:<cause> | attempt k/n | budget X→Y` 的行（v19~v21 均 0 条，因为 M3.1 迁移后 outline 不再被顶断）。
2. **sidekick 误读 plan_json 当最终大纲**，报「sec7/sec8 没标题=破线」。`session.outline_json`（assess 校准后）才是审批通过的大纲；plan_json 是校准前原始稿。**以后验收「小节不缺」一律用 outline_json。**
3. **合并闸门 A/B 的对照组是坏的**：「分离闸门原样重跑」与库里旧判只有 49.5~55.5% 一致（几乎全判相关，没复现生产闸门），所以拿不到方法噪声底，「合并不比对照差」的结论无效（已把 prompts.py 里那段注释改成如实写法）。能确认的只有：合并 vs 库旧判一致率 74~84%、误丢（库判相关而合并判不相关）≤2.1%、更宽松（库判无关的段约 1/3 被放行）。真跑里通过率：v19 旧闸门 145/241（60.2%）、v20 合并 168/172（97.7%）、v21 233/263（88.6%）——**放行口径明显变松 ⇒ 材料更多、报告更长（v19 82k→v20 100k→v21 117k 字）⇒ 对时间是反向的**。要进一步提速，可以考虑把 `SUMMARIZE_GATE_NOTE` 里「宁可 true」收紧，或在篇幅预算里解决；**没做，没验证过收紧后的质量**。
4. 我自己曾把「v19 通过率 271/280」当成事实写给用户，实际是库里 v19 的 145/241；271/280 是更早某次的数字。数字出处要核到库，不要凭记忆。
**没验证/已知缺口**：①含审查返修的新基线（v22），额度恢复前不可得；②合并闸门变松对终稿质量的影响（只有明面检查：v19~v21 的引用/节覆盖都过线，v21 因审查失败残留 1 处全局越界 `S23·2`、1 处裸引用、1 处伪引用，由存疑节披露）；③`plan_spec` 走手写 JSON（`complete_json`），v21 又出现 1 次 JSONDecode 重试（+~70s）——应像 outline/audit 那样改强制工具提交，需要真实 API 验证，**待额度恢复**；④`extract.summarize` R1 144s（186 段/23 批）已成为写作之外最大的一段，候选是迁 M3.1（要先 A/B）；⑤5s 的 apply_revision→assess「调度缝」是 e2e 脚本 3s 轮询审批造成的，不是流水线问题；⑥审查入参归一化失败（v20 audit#2 连调两次 104s+51s）原因未知，已插桩，下次再发生读 `normalize_fail`。
**v22（2026-10-26，额度恢复后，含完整审查+返修的首个可比基线，`data/refs/ra_v22_final.db`，session `s_f29d6edbcbe9`）**：墙钟 **796s ≈ 13.3 min**（v18 29.0 → v22 13.3，达到 15 分钟目标，距 10 分钟还差 ~3 分钟）。40 源/248 段/报告 77k 字；95 次 llm_call，3 条 error（全是 write_section EmptyOutput），wait_ms=0。链路实测：audit 1 次 114s（high=1，目标 `t8→sec8:edit`，normalize_fail=[]）→ write_patch 1 次 8s 落地（修 sec8 与 sec6 的硬矛盾）→ 第二轮按 MAX_REWORK=1 跳过（`write_audit_skipped` 出现，`final_revise_targets=1`）。检索 2 轮（r2 `{low:7}` 停）。11 节 outline_json 与终稿 11 个正文 `##` 一一对应+存疑节，占位 0，全局越界 0，tN/secN 泄漏 0；裸 `[S5]` 类来源级引用 5 处（来源存在、正文里指聚合材料，不是错引）；存疑节 5 条（4 no_material + 1 规约自检）。阶段：probe+outline 53s、retrieve 44s、**extract 214s（summarize 124s，29 次调用）**、assess 44s、**plan_spec 140s（单次调用，M3.1，输出 5.3k tok，无 JSON 重试）**、plan_tasks 53s、dispatch 131s、**audit 114s（输出 10.3k tok）**、revise 8s。
**v22 新发现**：①3 条 write_section EmptyOutput（t1/t2/t3，同时起、均在 ~48.4s 失败、`finish_reason=None`、输出 token 0）→ forced_retry 53~83s 才成功，正是 dispatch 的关键路径（131s）。**它不是 16384 预算截断**：成功的同类调用输出仅 3~5k tok；v21 同类失败 finish_reason=tool_use、v20 revise 才是 max_tokens。`_reject_broken_args` 里「最可能是 max_tokens 截断」的措辞是猜测口径，对 finish_reason≠max_tokens 的情形会误导。真因未知（候选：流在 ~48s 被截断/连接中断、模型吐出坏 JSON），需插桩（保留已收到的 partial args 尾部、chunk 数、最后一块距今秒数、stop_reason）再判断；②plan_spec（140s）与 audit（114s）是单次大调用，耗时大头来自 M3.1 不可关的思考而非正文；③5s apply_revision→assess 调度缝仍是 e2e 脚本轮询所致。
**批 5（2026-10-26，离线+n=3 回放；未经完整真跑，待 v23）**：
- **write_section EmptyOutput 诊断**：插桩（`_broken_diag`、`STREAM_TIMELINES`）后在 v20 证据上复现 1/2（n=2）：`message_delta/message_stop` 都收到、工具参数只有 `{` 一个字符、stop_reason 为空、流 ~46s——是**模型/平台提前收尾放弃生成参数**，既不是 max_tokens 也不是连接被掐（`_reject_broken_args` 文案已按 max_tokens/无 stop_reason/其它 三类改）。v22 三条同一瞬间（17ms 内）死，疑同一外部事件。再跑 n=3 零复现。**看门狗被数据否决**：39 次成功的 write_section 流相邻事件最大间隔 min 20s/中位 41s/max 67s（M3+强制工具：delta 秒到、长静默、再吐参数），任何 ≤60s 的空闲看门狗都会误杀正常调用。**未做缓解**，失败率样本：v22 3/11、v21 1/15、v20 preload 实验 0/39；每次失败约让该任务多 55~80s（在 dispatch 关键路径上）。48s 常量核查：代码/配置里没有。
- **plan_spec 改强制工具提交 `submit_spec` + 默认瘦身（`RA_SPEC_SLIM=1`，0=完整提示词）**。n=3×3（v22 输入重建）：旧 complete_json 中位 46.5s/6.2k tok；强制+不瘦身 103.8s/7.9k（**慢 2.2×，所以不要只改强制不瘦身**）；强制+瘦身 38.3s/4.4k，3/3 成功、sections 全覆盖。下游 dispatch（v22 tasks，preload，n=2）：瘦身 spec violations 均 2.5 vs 完整 12、证据外数字 8 vs 9、字数 +4%、0 失败（样本小）。注意 v22 生产里 spec 实测 140s 而同批回放 46s，说明平台速度波动很大，**单次对比不可当结论**。
- **write_audit 默认瘦身（`RA_AUDIT_TRIM=1`，0=原提示词）**：n=3×2，v22 返修前草稿，生产 node_audit：墙钟中位 92.6→44.6s，已知鹏辉/sec6-sec8 矛盾 3/3 抓到，high 数 [1,1,2]→[3,4,4]（A1 的 high 我人工看过都是真问题：鹏辉/纯锂量产口径矛盾、PEO 电导率两节不一致、sec7 末尾元叙述泄漏）。**预设判据「输出 token ≤0.75×」未满足**（9094 vs 9604）：我判断它是时间的代理指标，时间指标大幅满足，故采纳——这是对预设判据的有意偏离。**已知混杂**：A0 在前 A1 在后，平台速度漂移未控制；high 变多意味着返修目标变多，要在 v23 里看返修耗时与是否改坏。下游只有 severity=high 触发返修，mid/low 条目无消费点（前端只显示条数/high 条数）。
**v23（2026-10-26，批 5 后，`data/refs/ra_v23_final.db`，session `s_834fd2e7f2b7`）**：墙钟 **489s ≈ 8.2 min**（v22 13.3）。⚠️ **工作量不同，不能把 13.3→8.2 全记在批 5 头上**：v23 只有 38 源/160 段/10 节/报告 64.6k 字（v22：40/248/11 节/77k）；检索+抽取段（批 5 没碰）从 v22 的 ~260s 降到 ~120s（summarize 124→57s）主要是料少。写作段 446→254s 才是批 5 相关：plan_spec 140→20s（submit_spec+瘦身，1 次调用，spec 条数正好压在上限 12/6/10）、plan_tasks 53→30s、dispatch 131→63s（**0 次 write_section 失败**，v22 是 3 次——批 5 没做缓解，只能算样本运气）、audit 114→91s（2 次调用：首次 50s 因**缺 `blocked` 键归一化失败**，重试 40s，浪费 ~41s；输出 5.8k/4.6k tok）、revise 8→49s（write_patch 3s `applied=0, old 未找到`→回退整节重写 t2/sec2，4 次 write_revise 共 46s）。链路：audit findings=4/high=1（t2→sec2 与 sec4/8/10 引用 S14·1 的矛盾，返修后该表述消失）→ 第二轮跳过（`write_audit_skipped`）；返修后程序检查留 1 条 violation（S14·1，进存疑节）。明面检查：outline_json 10 节与终稿 10 个正文 `##` 一一对应+存疑节，占位 0，全局越界 0，裸引用 `[S2]`×2（来源级），tN/secN 泄漏 0；ground_scan 638 个数字证据外 7（去重 4，其中 6 个规划产物自带）；存疑节只有 2 条（v22 是 5 条）。llm_call 71 行 1 error（extract JSONDecode，非 write_section）。**待观察**：①audit 瘦身提示词下 findings 只有 4 条/blocked=0，召回是否被压低没有同口径对照；②audit 缺 `blocked` 键的归一化失败（疑与瘦身提示词里「blocked 照常完整填写」有关，n=1）；③patch 的 quote 找不到→整节重写（+46s）是现有兜底路径，不是新问题但成本高；④e2e_http 就绪探测 bug 已修（/api/health 同步探 searxng 3s+tavily，旧 2s 超时永远失败且循环耗尽后假装就绪；searxng 当前未运行，搜索只走 Tavily，月额度 774/1000）。
**v24（同配置第二样本，`data/refs/ra_v24_final.db`，session `s_7cbfb208cc5b`）**：墙钟 **613s ≈ 10.2 min**，29 源/70 段/8 节/报告 50.8k 字（工作量比 v23 更小），73 次 llm_call **0 error**（无 EmptyOutput、无归一化失败）。dispatch 60s（0 失败）；plan_spec 58s；audit 132s×1（输出 10.3k tok，findings 8/high 5/blocked 3）；revise 73s（write_patch 3 条里 2 条「submit_patches 入参偏离 schema」被拒、1 条 applied，4 节回退整节重写，write_revise 16 次调用）。明面检查：8 节大纲一一对应，越界 0、裸引用 0、泄漏 0，存疑节 5 条。三次样本（v22 13.3 / v23 8.2 / v24 10.2 min）工作量各不相同（段数 248/160/70），**不要把差异全归于代码**；稳定结论：写作段 v22 446s → v23 254s → v24 ~370s，波动主要来自审查输出长度与返修回退。
**⚠️ 批 5 引入的回归（v24 发现）**：spec.sections 只有 1 项（outline 8 节），v18~v23 六次都完整覆盖。原因：`SPEC_SCHEMA.sections` 只要求每项有 id，SLIM_NOTE 的「每个小节都要有」没被遵守，缺的小节没有 role/must_cover/not_yours，审查 spec_issue 报「sec8 被 t6/t7 重叠覆盖」。**批 6 修复**（三层）：提示词无条件追加「必须覆盖全部小节（共 n 个：ids）」、schema 按 n 动态 `minItems`、代码校验覆盖→缺则重试一次→仍缺补空占位（不降级 plan_failed；事件 `sections_missing`/`sections_stubbed`）。回放（v24 输入，n=3）：未修版 3/3 完整（**没复现**，低频间歇），修后版 3/3 完整、17s。保险层真实价值要靠后续真跑里 `sections_missing` 事件观察。另：patch 拒绝现在留证（事件 `patch_reject`、span `write.patch_reject`，raw≤3000 字符），v24 那两条补丁入参到底怎么偏离的未知。**教训：批 5 的回放 n=3 样本发现不了低频的覆盖缺失；换了输出路径（手写→强制工具）必须加「覆盖/完整性」代码校验，不能只靠提示词。**
**v25 换题验证（2026-10-26，批 6 后，3 题串行；`data/refs/ra_v25_{cbam,rustgo,glp1}.db`）**：CBAM 638s/41 源/176 段/10 节/83.8k 字；Rust-vs-Go 534s/30/141/12/58.3k；GLP-1 608s/46/145/6/61.2k。**三题全部 8.9~10.6 分钟，明面检查全过**：outline 全覆盖无占位、全局越界 0、泄漏 0、failed_tasks 0、`sections_missing=[]`（批 6 保险三次都没触发，低频故障未再现）。audit 三题都 41~50s（findings/high：7/5、3/1、7/4）；T2 的 plan_spec 首调 `RemoteProtocolError`（60s 断连），一次性重试救回（41s）——submit_spec 保留的重试实弹救场；T3 revise 81s 最贵（patch 1/3 + 2 节回退整节重写）。证据外数字：10(6)/7(6)/**23(9)**（医学题数字密度高）。存疑节条目 6/2/8。**闸门噪声（非技术题材确实存在）**：T2 把猎头/HR 招聘文档（S12/S13/S14，「团队成本」被误判相关）与通用模板放进证据池（各被引 1 次，占 8/30 源位）；T3 把微博自媒体（S41 被引 14 次、当一手用）、律所营销页（S20×6）、book118 上传文档（规约禁用仍被引 3 次，审查抓到）、残缺百科/模板残留标题放进来，且引用面不小。结论只到「合并闸门+源级 relevance 在非技术题材放进低质/无关源」，**没有评估这些低质源对终稿的实际伤害**（引用的是整篇综述/一手？只看到被引次数）。候选：源级质量过滤（域名/类型黑名单：weibo/bilibili/book118/docx 上传类、招聘站）、收紧闸门；未做。
**配置旋钮（环境变量，模块加载时读）**：`RA_WRITE_WORKERS`(12)、`RA_MAX_REWORK`(1)、`RA_WRITE_PRELOAD`(1)、`RA_SEARCH_TIMEOUT_S`(12)、`RA_FETCH_DEADLINE_S`(20)、`RA_SPEC_SLIM`(1)、`RA_AUDIT_TRIM`(1)。未来「快速/标准」档的差别应落在这些旋钮 + 篇幅预算上。
**账户额度（2026-10-25）**：Token Plan 用量上限触发 429/(2056)，`GET` 类接口仍通；之前 10-18 也出现过同类、当晚恢复。长跑 + A/B 一天内会烧掉额度，真跑前先用一次极小调用确认。

### 88. 快速 / 标准两档（2026-10-27）：快速档 = 检索 + 一次性整理，不走写作子图
**用户定位**：快速档变成「信息检索和整理」——给对话 LLM 配一个专业一点的检索处理工具：检索一轮后把证据一次性交给 LLM 直接作答，不要大纲审批、评估循环、写作任务、审查返修。标准档 = 现有完整流程（行为一字不变，默认）。
**实现（C1 后端，未含前端 C2）**：`ResearchState.mode`（'standard'|'fast'，缺省/未知=standard；已在 state.py 声明）；`session.mode` 列（迁移 0012）；`POST /api/sessions` 接受 `mode`（Literal，非法值 422）；`run_session` 的初始 state 带 mode。图：`probe → outline → retrieve → extract → gate` 之后 `route_after_gate`：fast→`answer`→finalize→summary，standard→assess（老路）；快速档不经 await_approval。`_mode_caps`：快速档 (max_urls 24, 每语种查询 3)，标准档 (50, 5) 与原常量一致。`node_answer`：`_load_numbered_evidence`（从 node_write 抽出共用，编号逻辑必须单一来源）→ 证据行 `[Sn·m] heading | summary[:450]`，总 ≤80k 字符，超限从「段数最多的来源」末尾去段 → `LLM.complete("fast_answer", …)` 纯文本流式 → 同写作线的 `_auto_fix_text/_check_citations/_check_internal_ids`，修不掉的引用从正文删除并进存疑节 → 正文 + 存疑节 + `### 来源`。失败/空输出降级为「检索结果 + 来源清单」，`session.degraded` 记原因，会话不失败。提示词 `FAST_ANSWER_SYSTEM`（= V2；V1 备查）。SSE：`kind:"answer"` 事件字段 `chars, sources, segments_used, segments_dropped, segments_without_summary, citations_total, citations_removed, degraded`（+`note:"empty"`/`error`）；`draft` 事件 stage=`fast_answer`。
**这一轮的坑（再来一次的话先看这里）**：
1. **`fast_answer` 档照抄 write_section 的 `reasoning_effort: low` + `forced_thinking: disabled` 对纯文本无效**——`forced_thinking` 只管强制工具轮，纯文本 M3 上 `low` = 开自适应思考，冒烟里思考 43k~61k 字符把 16384 输出预算吃光（`stop_reason=max_tokens`），首调用 152s 作废、重试 121s，answer 274s、全程 7.3 分钟。改成 `reasoning_effort: disabled`（平台中性语义值，**不能写裸 `off`**，YAML 会解析成布尔）后 answer 30~53s。**纯文本环节要关思考，只认 `reasoning_effort: disabled`。**
2. 篇幅 V1「约 3000–6000 字」→ V2「约 2500–4000 字，硬上限 4500」（关思考后 n=3×2：V1 墙钟中位 59s/out 3.9k tok/正文 ~6.1k 字；V2 44s/3.0k/~4.7k 字）。我预设判据「字数中位 2500–5500」没写清口径——意图是**正文**（不含来源清单/存疑节），按正文口径 V2 满足，按报告全长口径（6677）不满足；采纳 V2，这是对判据口径的澄清，不是放宽。V2 要求 4–6 个 `##`，模型实得 6~8（把「分歧与不确定」也算一节），未调。
3. 驱动脚本用了 bash 4 的 `declare -A`，macOS 自带 bash 3.2 不支持，四题静默坍缩成同一题——跑批驱动用 `case`，或直接写 python。
**换题验证（快速档，各 1 次，旋钮全默认，`data/refs/ra_fast_{solid,cbam,rustgo,glp1}.db`）**：固态 198s、CBAM 286s、RustGo 133s、GLP-1 173s（标准档同题 613~796/638/534/608s）；来源 12/19/10/13、段 79/98/36/84；正文 4.6k/4.3k/4.6k/4.6k 字；越界 0、泄漏 0、裸引用无真错引；ground 替代口径证据外数 5/2/3/1（标准档 7/10/7/23，**口径不同只能参考**——`tests/ground_scan.py` 依赖写作子图快照，快速档跑不了）。EmptyOutput 复发 4 次（probe_outline×1、summarize×3），全重试救回；CBAM 因 summarize 失败有 12 段无概括，存疑节如实披露。F4（GLP-1）没有字面「分歧与不确定」节，由「证据短板」「长期监测」两节承担。
**已知局限/没验证**：①快速档没有审查兜底，低质来源被引较多（renrendoc 转存报告 ×19~27、雪球、网盘资源站、门户转述等），没评估对终稿的实际伤害；②来源只有 10~19 个（24 URL 上限），覆盖面远小于标准档；③ground 口径替代；④每题只跑 1 次，方差未知；⑤前端未改（C2）。**候选**：把快速档会话「升级为深度报告」（复用已检索证据，只补大纲+写作），接口未留；源级质量过滤。
**C2 前端（同日）**：`frontend/src/lib/mode.ts` 是档位文案/默认档（localStorage `ra.defaultMode`）的单一来源；新建对话框两张档位卡（radiogroup，←/→ 切换，只影响本次），设置页「默认研究档位」；侧栏 fast 会话灰色「快速」标签、报告页头「快速简报」标签+提示行（中性灰，不是 seal 色）；流程页 `answer` 事件 →「整理作答」步骤（阶段条 fast 去掉「审批」）；`vite.config.ts` 代理目标读 `VITE_API_TARGET`（默认仍 8000）；后端只补了 `/api/sessions` 列表 SELECT 的 `mode` 列（其它端点对 fast 会话本来就兼容，`/approval` 返回 waiting=false）。验证：构建零错误；playwright 19/20（`/tmp/pw/check_c2.mjs`，会随机器清理丢失），UI 全流程活跑一次（真点击新建→选快速→提交，2.5 分钟 done，运行中无「等待审批」，报告自动可见）；标准档目视回归无退步。**已知小瑕疵（C3 已修①②）**：①~~390px 窄屏头部溢出~~ 已修：真正的溢出源是固定宽侧栏 224px + main 最小宽 204px=428px，窄屏（<640px）改为自动折叠侧栏到图标轨 + 头部按钮只显图标（aria-label/title 保留），宽屏不变；②~~快速档「累计 ？段」~~ 已修：`/trace` 对 fast 会话用 chunk 总数回填 `segments_total`，前端缺字段时整项省略；③快速档「写作」阶段条标签沿用「写作」（未改）。

### 89. 来源质量（批 7，2026-10-27）：合并闸门 V2 + 坏页确定性过滤
**起因**：v25 换题里无关/低质来源进了证据池（Rust-vs-Go 的招聘/HR/通用模板文档、GLP-1 的未渲染模板页与乱码百科）。查清：招聘文档段的规则层关键词命中=0，是**合并闸门**（summarize 同批 `relevant`）放行的，根因是 `SUMMARIZE_GATE_NOTE` 里的「宁可 true 也不要 false」。**注意相关≠权威**：weibo/bilibili/雪球/门户转载等是相关的低权威来源，本批没动（权威性没处理，候选：按来源性质限制单源占比）。
**① 合并闸门 V2（已转正）**：`prompts.SUMMARIZE_GATE_NOTE = SUMMARIZE_GATE_NOTE_V2`（V1 保留）。V2 点名「招聘/求职、广告与法律维权营销、页面模板文字、任何行业都成立的通用方案模板」必须判 false，拿不准才回落 true。回放（`tests/exp_gate_note.py`，生产 summarize 同路径，n=2）：JUNK 28 段 false 率 V1 28.6% → V2 85.7%；CONTROL 90 段（全是规则命中段）V1 0% → V2 3.33%（3 段 jianshu 泛 K8s 教程）；BORDER 60 段（规则未放行的）V1 丢 1 → V2 丢 5（原产地规则科普、合规服务广告、合同模板、第三国案例——灰区）；schema 偏离 0。**预设判据「V2 CONTROL 误丢 ≤ V1+3pp」差 0.33pp 没过（3.33%>3.0%）**，我仍然采纳，理由是代码事实而不是放宽：`gate_segments` 里**规则层优先**，规则命中的段不采信合并判定，CONTROL 集全是规则命中段，所以那 3.33% 在生产里**不生效**；真正受 V2 影响的是规则没放行的段（BORDER，多丢 4/60≈6.7%，灰区背景读物/模板/营销）。**这是对预设判据的有意偏离**，没验证 V2 对终稿的实际影响。另：V2 在 JUNK 上的 4 个漏网是 GLP-1 坏页（正文确实在讲 GLP-1，relevant=true 是对的），应由②拦。
**② 坏页确定性过滤**：`config/source_quality.yaml`（`drop_domains` 只放招聘平台 liepin/zhaopin/51job/lagou/zhipin/jobxzc；`drop_title_regex` 与 `drop_text_head_regex` 拦未渲染模板 `{{…}}`/`${…}`/`<%…%>`）+ `research_agent/search/quality.py`（`page_defect`，YAML 缺失/损坏回落空规则不抛错，域名后缀匹配）；接在 `node_retrieve` 抓取后、`add_source` 前，命中进 `skipped`（`low_quality:<原因>`），两档与探针种子页共用。离线对照 data/refs 全部库：命中 3 页零误伤（GLP-1 的 `{{custom_sec.title}}` 页、RustGo 的 liepin/jobxzc）。**不覆盖**：renrendoc 等文库托管站（只能靠 V2 提示词）、baike 乱码页（无可靠判据）。

### 90. 发布前整理（批 8，2026-10-27）：全量离线回归 + checkpoint 清理机制
**全量离线回归**（40 个 `tests/test_*.py`，`RA_DB_PATH=/tmp/ra_fullsuite.db`，仅 `test_toolloop_protocol` 带 `--offline` 跳过在线段）：**37 过 / 3 挂**，3 个在干净 HEAD worktree 上原样复现，全是预存：`test_guard_effort_ladder`（write_plan_tasks 降档到 low 仍空）、`test_fallback_summary`（写手 listing 含 `[无概括` 前缀）、`test_minimax_truncation`（截断后预算翻倍，`_minimax` 保留翻倍而非降 effort）。**这三个没修**，是旧断言与现行行为的分歧，修之前要先判断该改代码还是改断言。默认库 data/checkpoints.db / research.db 前后字节不变（RA_DB_PATH 隔离有效；注意 checkpoint 库路径是模块常量，不随 RA_DB_PATH 变，今天所有 e2e 真跑都写进了默认 checkpoints.db）。
**checkpoint 清理**（TECH_DEBT #1 机制层，细节见该文档）：`maintenance.py`；「最新」= `max(checkpoint_id)`（真实库上与 rowid/parent 链尖三口径核对一致）；VACUUM 不能在事务里，需 `isolation_level=None` 专用连接 + `BEGIN EXCLUSIVE` 试探锁→ROLLBACK→VACUUM；孤儿判定要合并多个研究库（今天 e2e 的 session 在各自 /tmp/ra_*.db，单库会把它们全判成孤儿）。**安全护栏**：孤儿默认不删；`--delete-orphans` 在 session 总数为 0 或孤儿占比>50% 时拒绝，需再加 `--yes-really`；真实研究库路径不一定是默认 `data/research.db`（它现在是 0 session 的孤儿库），不带 `--db` 的 `--apply` 绝不会删 thread。自动清理只挂在**成功 done** 的 4 个落点（run/approve × 流式/非流式），失败/审批等待/出错不清理。/tmp 副本演练：752MB→4MB。**真实库未清理。**
**批 9 收尾（2026-10-27）**：
- **远程仓库**：`origin = https://github.com/wd-ing/Deep-Research-Agent.git`（私有），main 已推送；推送前扫过历史，`.env` 里的密钥值在 git 历史中 0 次出现。`data/`（含 `data/refs/` 约 84MB 参考库）仍被 `.gitignore` 排除、不在远程里。
- **真实 checkpoints.db 已清理**（用户确认全是开发/测试数据）：752MB → 4MB（删 37 个孤儿 thread，22 个 done 各留最新一行）。同时发现并清掉 5 个 10 月 11 日起的僵尸 uvicorn（8899 端口，SIGTERM 杀不掉，要 `kill -9`）。
- **前端补齐被丢弃的事件**（审计 P2，`workflow.ts`）：`write_audit_skipped`、`write_plan_spec.sections_missing`、`write_audit.blocked/normalize_fail`、`write_revise.patch.patch_reject`、`write_plan_error`。**`extract_error` 不是对外事件**——它只是 `extract_batch` 内部 results 记录（节点汇总进 `extract` 事件的 `errors`），不需要渲染。
- **批 7/8 端到端真跑（v26：标准档 Rust-vs-Go、快速档 GLP-1）**：批 8 自动清理**生效**（两次都只剩 ns='' 最新 1 行，清理后 `/approval /trace /events /review` 仍 200、waiting=false；清理释放的页靠 freelist 复用，库文件 4MB→14MB，未 VACUUM）。批 7：两库 `low_quality:*` 命中 0 条、噪声源（招聘/HR/`{{`/乱码标题）0 行——**但这不能证明过滤有效**：本次搜索结果里根本没出现这些页（v25 的坏页没被再搜到），只能说没回归。标准档 write_audit findings=3/high=0（没触发返修）、sections_missing=[]、`write_audit_skipped` 与 `patch_reject` 未出现，所以那两条前端新分支只在合成事件上验证过。v26 标准档的 llm_call 失败 3 条（summarize SSL EOF×2、write_section RemoteProtocolError×1）全部重试恢复。
- **踩坑：`tests/e2e_http.py` 的 urllib 默认读 macOS 系统代理**，本机代理把 127.0.0.1 请求代理出去回 502（curl 不走系统代理所以正常；此前能跑是因为没开代理）。已修：脚本内用 `ProxyHandler({})` 的 opener（实测默认 urlopen 502、无代理 opener 200）。v26 那两次是用 sidekick 的 `/tmp/drv.py`（同一套 API 序列）跑的，**不是字面的 e2e_http.py**；第二次还因为 8011 上旧 uvicorn 没杀掉，数据先落进了旧库，事后已拆开，`data/refs/ra_v26_rustgo.db`/`ra_v26_glp1_fast.db` 各自只含对的那条会话。
- **未解**：v26 标准档墙钟 ~1217s 里含约 10 分钟的审批等待（driver 轮询超时，手动 curl 放行），纯研究时间没有干净的数字；driver 轮询多次 connect timeout 是客户端/代理问题还是服务端事件循环被同步调用堵住，**没查清**。

### 91. 下一阶段方向：前端再设计（用户 2026-10-27 定，优先级高于所有低优先级残留）——**已完成（P1–P5，2026-11-03，见坑 92 末尾各期记录）；现在轮到下面的低优先级残留**
**用户的判断**：现在的前端「还是有点简单」，下一阶段主要推进前端设计。参考物：`homepage/deep-research-homepage.html`（+ `homepage/og-image.png`），是用户基于项目**中期**做的产品首页展示稿，**数据/功能/说明/内容不完善、不是最终版**（项目定稿后会出最终版），所以：只借鉴它的**视觉效果与产品化质感**（高级感、流畅度、动效/过渡、排版、层次、细节），**不要照搬其中的数据、功能描述或文案**。这两个文件是用户自己的未提交改动，不要提交、不要改。
**怎么做（用户明确的分工）**：具体改哪些方向、怎么改，**由接手的 agent 自己读那份 HTML、对照现有前端思考后提出方案，给用户过目再动手**（结构性改动先说方案，本项目一贯约定）。已有约束别丢：视觉令牌与颜色语义（坑 85：蓝=可点/选中、绿=进行中、朱红 seal=批注/存疑/降级、真红=错误）、不编造后端没有的数据、hooks 无条件、视觉验收用 playwright 截图 + 断言（坑 85/86，脚本在 /tmp/pw 会丢，需重建）、后端兼容（快速/标准两档，坑 88）。两档的 UI 差异（档位卡、快速标签、「整理作答」步骤）是新设计必须保留的功能。
**此后才轮到的低优先级残留**：①`write_section` 偶发 EmptyOutput（模型提前收尾只吐 `{`，无缓解）；②低权威来源被单源引用过多（权威性处理，未做）；③3 个预存失败测试（`test_guard_effort_ladder`/`test_fallback_summary`/`test_minimax_truncation`）；④TECH_DEBT #2–#7；⑤checkpoint 根因（全文塞进图状态）；⑥快速档升级为深度报告；⑦`data/refs/` 约 84MB 参考库不在 git/远程里，尚无备份。

### 92. 前端再设计方案（2026-10-27 用户批准，分 P1–P5 实施）
**对照结论**：`homepage/` 首页与产品共用同一套令牌（底色/文字三级/蓝绿朱红语义/.ruler/.seg/.mix），差距不在配色，在：①动效语言（统一缓动、入场上浮、错峰、数字滚动、刻度尺扫描）②层次（卡片只有线，浮层用分层长阴影）③签名组件（刻度轨 + 圆点步进器 + 大数字读数）④构图（留白、窄栏聚焦、编号小标题、等宽注释）⑤字阶。现状问题（回放服务 5180 截图）：来源库 656 条挤在 224px 侧栏、主区空白；报告页头部堆三条横栏且目录栏与正文栏未对齐（待确认是否仅 1440 宽）；流程页阶段条无圆点、完成会话「已用约 0 token」（回放数据没带 token，真实会话待确认）；首页看不出两档；侧栏长标题截断、分组不清。
**用户决定**：①**保持全无衬线**（不用首页的衬线标题，沿用坑 85）；②**做「运行账单」**（新增只读端点读 `run_trace`/`trace_span`，已完成会话的分阶段耗时与 token，放 P5；流程页「经过时间」也只在有真实数据时显示，不编造）；③**来源库改主区双栏**（左列表+筛选+性质构成色带，右详情，侧栏只留入口）；④分期顺序 P1→P5。
**分期**：P1 地基（动效令牌/三层高度/字阶/氛围网格仅首页与空状态）+ 首页 + 侧栏 + 新建对话框；P2 流程页 + 工序刻度轨（标准/快速各自的步骤集）；P3 报告页（三条横栏合一吸顶头部、阅读栏居中、目录左栏、阅读进度刻度、引用联动）；P4 来源库双栏；P5 审批页/设置页收尾 + 运行账单。不做：吸顶长镜头、逐字点亮、双语、首页文案/数字、深色模式、后端没有的数据。每期流程：lead 写 brief+判据 → sidekick 实现 → lead 审 diff → playwright before/after（无溢出/无控制台报错/无 HTTP≥400/390px+1440px/reduced-motion 无动画/标准+快速各验）→ 给用户看截图 → 提交推送。验收环境：后端 8012 + `data/refs` 副本里的真实标准/快速会话，前端 5181，**不碰 8000/5180**。无新依赖（CSS + IntersectionObserver/rAF）。
**P1 完成（地基 + 首页 + 侧栏 + 新建对话框）**：`index.css` 新增 `--ease/--ease-out`、`pop-in/grow-x/ruler-scan/eq/run-scan/view-in/dialog-in`、`.reveal/.stagger/.card-hover/.view-in/.paper-grid/.paper-lens/.brandmark`，全部在 reduced-motion 下关闭；`tailwind.boxShadow.float` 只给首页输入面板与新建对话框；`src/lib/motion.ts` 的 `useCountUp`（rAF、reduced-motion 直出、动画被打断时从当前显示值续走）。首页：标题 44px + 尺子扫描、网格氛围 + 跟随指针放大镜（触屏/reduced-motion 不挂）、指标数字滚动、续卡 running 加**不确定态**扫描条（不是进度）、fast 卡带 `tag-fast`、窄屏（<900px）输入面板底行纵向 + 指标 2×2、续卡 <760px 单列。**修了一个真 bug**：首页输入框 `submitHome` 原来永远传 `'standard'`，忽略「默认研究档位」设置；现在首页输入面板自带标准/快速分段（初值取 `loadDefaultMode()`，只影响本次提交、不写 localStorage），同时删掉过期的「约 20–30 分钟」文案（档位段上已写实测 8–13 / 2–5 分钟）。侧栏：分组改为 进行中 / 今天 / 近 7 天 / 更早，已归档折叠到底部（选中会话在归档里或筛选=已归档时自动展开），去掉「最近 N」重复标题。**验收教训**：只断言 `document.scrollWidth<=clientWidth` 会漏掉被 `overflow-auto` 容器吞掉的溢出（首版 390px 首页面板被撑坏却断言通过）；现改用「后代元素矩形不越容器」断言，5–6 个宽度（390/480/640/760/900/1440）各跑。验收：构建零错误；std+fast 两库、reduced-motion、`POST /api/sessions` 用 `page.route` 拦截（验收期间不启动真实研究）。已知小事：`.reveal` 在会话行跨组移动（如 running→今天）时会重播一次入场；P1 之后报告页/流程页未动。
**P2 完成（流程页）**：TrackRail 升级为「轨上节点步进器」——每环节一个 14px 圆点按钮（完成=绿实心对勾、当前=绿点+呼吸光圈、未到=空心灰环、**审批节点用 ink 黑**表示「这一步是你的」，仅标准档有；快速档 3 节点、「写作」**显示名**改为「整理作答」，phase key/`#wfphase-写作` 锚点不变，`phaseLabel(key, mode)` 轨与环节标题共用）；`aria-current="step"`、无数据环节 disabled。指标条数字走 `CountUp` 子组件（hooks 无条件）、「已用 token」并入指标条（`cost>0` 才出格，回放 cost=0 不出）、页脚 token 行删除；页头 32px + 进行中 pill + `tag-fast`；环节标题加等宽序号；SectionRow 厚度条 `sc-grow`（scaleX）+写作中行 `run-scan`；启动空态加 `paper-grid` 与空心步进器。**真 bug 两个**：①`.wf-phase-head{top:89px}` 写死轨高，窄屏轨文字换行必错位 → 改 ResizeObserver 写 `--wf-rail-h`（且 effect 依赖要含 `started`，否则首渲染走空态 return 时观察器永远挂不上）；②指标「数字+单位」无空格导致「453.6ktoken」成不可断词溢出。**验收环境教训**：`cp data/refs/x.db /tmp/y.db` 前必须 `rm -f /tmp/y.db-wal /tmp/y.db-shm`——上一个服务残留的 WAL 会被 SQLite 重放到新拷贝上，session 表变空（验收卡了两轮才定位）；旧事件信封的库（如 `ra_v22_final.db`）在流程页只显示「没有事件历史」，夹具用 `ra_v26_rustgo.db`（标准）与 `ra_v26_glp1_fast.db`（快速）。流程页「用时」仍只在 running 显示（done 无可靠结束时刻，真实耗时留给 P5 运行账单）。验收：构建零错误；标准 5 宽 × flow/running、快速档全绿（节点数、aria-current 唯一、点击跳转贴轨 ±2px[底限态除外]、Tab+Enter、cost=0 无 token 格、RM 无动画、无溢出）；`/replay/state` 404 为 done 会话既有行为。未做：before 截图（代码先改后补验，未做双前端对照）。
**P3 完成（报告页）**：根因先记——旧版目录条与正文错位是因为 sticky 区 `max-w-3xl` 居中、正文网格 `w-fit grid-cols-[640px_280px]` 居中，两个坐标系，**1440 以外也会错**（不是宽度特有）。改动：①常驻区瘦身——`RoundChart` 不再夹在 header 与报告之间，改作 `ReportView` 的 `lead` prop 渲染在正文列顶部随文滚走；底部常驻「来源：[S1]…」页脚删除，改为正文末尾「引用来源」区块（默认 12 个、可展开全部、芯片是蓝色按钮点开来源详情）；②**三档布局按滚动容器实测宽**：≥1160 `[176 目录|620 正文|260 批注栏]`（左列目录+查找+ink 竖向刻度进度尺，顶部只留 2px 刻度线）；1000–1159 `[640|280]` 顶部 TocBar 与正文同构（左缘对齐实测 0px 差）；<1000 单列。用户常用的 1440 视口+展开侧栏=容器 1188，落在三列档；③`--rpt-bar-h` 由 ResizeObserver 写入，`scroll-margin-top` 与 scrollspy 判定线（原写死 +96）都用它；④读数行：`.health` 升级，数字 count-up（`44%` 只滚数字拼后缀），`CountUp` 抽成共享组件（WorkflowView 同改）；⑤h2/h3 字阶、内联 `code` 长 token 加 `overflow-wrap:anywhere`（390px 下 `ObjectJsonIter` 类标识原先越界 170px）；⑥查找条改 `shadow-float`+`dialog-in`，位置跟 sticky 下沿；ReportView 根 `.view-in`（只有 opacity，沿用「transform 会破坏 sticky/fixed」的约束）。批注栏逻辑一行未改，rail 与悬停来源卡在 tier≥1 保持。验收：构建零错误；std(`ra_v26_rustgo`)+fast(`ra_v26_glp1_fast`) × 1900/1440/1300/760/390；对齐、无后代溢出、目录跳转±3px、scrollspy、竖尺单调增长、⌘F、来源展开/点击、读数终值、RM 无动画、print 仅正文均过。**修订痕迹夹具是合成的**（两个参考库 `review.patches` 都为空，route 注入 patch；朱批卡对齐在合成数据上验证，真实带补丁会话待后续回归）。未改：App header 结构、RoundChart 内部、RefChip/表格/修订标记样式。已知：1000–1159 档「查找」位于批注栏列起点（不在 TocBar 内），属有意。
**P4 完成（来源库主区双栏）**：来源库从侧栏列表改为主区「页头读数（来源/段/引用 count-up + 已排除 seal）+ 性质构成色带 + 搜索/性质 chips/排序 `.seg` + 左 380px 列表 | 右详情」。新增 `views/SourcesView.tsx`（含并入的列表分组、`DetailPanel`、排除确认流程），**删除 `SourceList.tsx` 与 `SourceDetail.tsx`**。侧栏在来源视图只留品牌/切换/新建 + 「按性质」纵向筛选（与主区 chips 同一个 `natureFilter` 状态，放 App；特殊值 `'__ignored'`=仅已排除）。双栏/单栏按**主区容器实测宽**（≥900 双栏；视口≈1152 起，1100 视口容器仅 848 → 单栏，这是有意的）；单栏下 `cur` 有值显示详情 + 「← 返回来源列表」。未选中右栏=概览（性质构成可点设筛选 + 被引用最多前 5，只列 citations>0）。段落原文 >380 字或 >5 换行才折叠（`line-clamp-6` + 展开/收起）。保留的教训：行容器不能是 button（嵌套非法）、「⊘」按钮 `group-focus-within` 才键盘可达、`libErr` 与 `lib===null` 区分（失败态主区展示+重试）、`openSourceFromReport` 清 `srcQ`。656 源性能：行用 `content-visibility:auto`、不给行挂入场动画；首屏行可见 494ms，8 屏滚动 longtask>100ms 仅 1 个。验收：构建零错误；小库（rustgo 32 源）+大库（`ra_v13_gate` 656 源，真实）；1920/1440/1100/900/899/760/390；搜索防抖恰好 1 次请求；筛选计数和=656；排除/恢复 POST 各 1 次（仅 /tmp 副本）；深链/回归/键盘/RM 均过。已知（**两条均已在 P5 修掉**：概览行加了 `session_title` 副标题；390px 列表态页头压缩，首条来源行首屏可见）：概览「被引用最多」在多场次含同一来源的库里会出现标题相同的行（真数据，未去重）；390px 页头原占首屏约 540px。

**P5 完成（审批/设置收尾 + 运行账单；前端再设计五期全部完成）**：①后端新增只读 `GET /api/sessions/{sid}/billing`（`server.py`，`tests/test_billing_endpoint.py` 18 项离线断言 + 三个真库逐项对 SQL 核对）。口径：耗时=`run_trace` 的 `node:*` 行 `detail_json.ms` 求和（排除 `node:maintenance.*`），**不含等待审批的时间，也不是会话总墙钟**；token/calls=`llm_call` 中 `status NOT IN ('retry','error')` 的行，`retries`/`errors` 单列；`total_tokens`=in+out（与 `repo.session_cost` 一致）；`cache_hit_ratio` 沿用 `cache_stats` 口径；node→阶段 planning{probe,outline}/research{retrieve,extract,gate,assess,apply_revision}/writing{write,answer}/wrap{finalize,summary}，stage→阶段按前缀；快速档 writing 标签「整理作答」；无任何 node/llm_call 行 → `available:false`；`by_stage.latency_ms` 是 SUM。不用 `trace_span`（老会话没有）。②流程页底部深色「运行账单」面板（产品里唯一一块深色面板）：仅 `status==='done'` 且 `available` 才渲染，取数失败静默缺席；总耗时/总 token CountUp；阶段条 scaleX=ms/totals.ms；点阶段展开 node 明细；`按环节·模型` 折叠表。三库（rustgo 标准/glp1 快速/ra_v22 老库）× 1440/390 逐项对 `/billing` JSON 一致。③审批页「这一步留给你」：STEP·审批 eyebrow（近黑点）、30px 标题、CountUp 读数（真实字段）、错峰大纲、底部吸附操作条（近黑主按钮+呼吸环，RM 关）；防连点=点击后本地 pending 锁 3 秒自动解除（onApprove 网络失败也不会卡死）。④设置页：eyebrow + 01/02/03 编号卡；「重启生效」改墨色注记；dirty 时底部吸附保存条，⌘S（仅 dirty 拦截）、beforeunload 提示；PUT 请求体不变。⑤P4 尾巴：概览「被引用最多」加 `session_title` 副标题；390px 列表态页头压缩，首条来源行在 844 视口首屏可见（y=297）。教训：账单取数门要用 `d.status==='done'` 而不是 `s.view`（流程页 view 恒为 flow）；验收 8012 若是旧进程没有新路由，要重启；`tests/test_*.py` 是脚本式（`main()`+`check()`），用 `.venv/bin/python tests/x.py` 跑，pytest 收集为 0。未做/已知：审批仍无「驳回」（后端空转）；账单只在流程页，报告页没有；账单面板「节点合计耗时」不等于用户感知的总时长（审批等待、排队空隙不计）。

### 93. 供应商切换：MiniMax 订阅到期 → 火山方舟 Ark（2026-11-03）
**背景**：用户的 MiniMax 订阅到期；改用火山引擎方舟订阅，两个模型组合：简单任务 `doubao-seed-2-0-mini-260428`（别名 `doubao-seed-2.0-mini`），困难/规划任务 `glm-5-3-flash-260828`（别名 `glm-5.3-flash`）。密钥在 `.env` 的 `ARK_API_KEY`（gitignored；用户是在对话里贴出来的，如有顾虑可在方舟控制台轮换，只需改 .env）。
**端点（实测，易踩）**：用户给的 `https://ark.cn-beijing.volces.com/api/plan` 不能直接用；真正可用的是 **`https://ark.cn-beijing.volces.com/api/plan/v3`**（OpenAI 兼容，`/chat/completions`）。官方文档的 `/api/v3` 与 `/api/coding/v3` 对这个 key 都是 401。
**两个模型的行为（原始 HTTP 实测，2026-11-03）**：
- **mini**：`reasoning_effort: none` = 真关思考（reasoning_tokens=0、0.6~2s）；**low/high 不是轻思考**（难题上 low 思考 3178 token/41s、high 5201 token/75s，不传也开思考）→ **只用 none**。工具调用 auto/required/指定函数三种 tool_choice 都遵守。限额 RPM 30000 / TPM 5M。
- **glm**：**关不掉思考**（`thinking.type=disabled` → 400）；**不传 effort = 默认 max 思考**（慢、烧 token）；none/minimal/low→low、medium/high→high、xhigh/max→max；实测 low 在难题上思考 0 字，high 约 400 token，max 120s+ 超时。吞吐约 50 token/s、首字延迟约 8s（极短输出也要 8~10s），比 mini 慢一个量级。真实任务 schema 上强制工具调用（指定函数/required）被遵守。限额 RPM 500 / TPM 1M。
- 两模型 prompt_tokens 口径不同；max_tokens 到 131072 都被接受；glm 缓存生效（第二次 cached 1792），**mini 探测未见缓存命中**（平台行为，未深究）。
**映射（用户定组合、lead 定环节；质量未验证）**：`ark_mini`（`reasoning_effort: disabled`→档案 `effort_off: none`）= probe_light/probe_degraded/extract/relevance/dedupe/summarize/session_summary/fast_answer/**write_section/write_revise/write_patch**；`ark_glm`（`reasoning_effort: low`）= probe_outline/outline/assess/write_plan_spec/write_plan_tasks/write_audit。**write_* 放 mini 是为速度/并发，是否换 glm 待 A/B**。`summarize` 原为 low 是 MiniMax「关思考+发工具会编造」的专属怪癖，mini 实测强制工具关思考正常，已改 disabled。
**代码改动**：`OPENAI_COMPAT += "ark"`；`StageConfig.validate()` 新增 glm 硬约束（provider=ark 且 model 以 glm- 开头：reasoning_effort 缺省/disabled→ConfigError；max/xhigh→ConfigError）。迁移时删除了 MiniMax 专属键 thinking_effort/forced_thinking/round_max_tokens（validate 对非 minimax 会拒绝）。限流器按 base_url 登记、两个 ark 档案同端点，取 max → mini 也受 glm 的 400/min 约束（一次研究的峰值远低于此，接受）。minimax/minimax31/internai/xj189 档案**保留**但订阅已失效。
**工具循环迁移结论**：write_section/write_revise 原靠 `forced_thinking: disabled` + `round_max_tokens: 2048` 做 MiniMax 工具循环的交卷轮；迁到 ark 后交卷轮走 `complete_with_tools(tool_choice=…)` → `_openai_compat_forced` 协议级强制，不依赖 forced_thinking；「同档位重试」「每轮 2048 上限」是 MiniMax 专属机制，ark 下不再有（每轮上限改由 `max_output_tokens` 管，**这是行为差异，真跑时留意单轮输出是否变长**）。
**验证**：`tests/test_ark_config.py`（离线）；`tests/preflight_ark.py`（真实调用，约 3.2 万 token）：17 环节全通、强制 JSON、工具循环、流式（usage 非 0，三条 `_opencode*` 本来就无条件发 `stream_options.include_usage`）、记账（llm_call 27 行 token 全>0、model 为带日期 ID）、并发冒烟 mini 20/20、glm 6/6 零 429/5xx。离线测试 37 个其余文件无新增失败；因硬编码 minimax 默认配置而改的测试：test_stage_migration（改写为 ark 断言）、test_submit_section/test_guard_retry/test_overload_retry（注入桩配置，不联网）、test_audit_forced（改 OpenAI 形状断言）。
**未验证 / 已知缺口**：①**还没有跑过完整研究**（标准档/快速档真跑 + 质量对比未做，新组合的耗时、返修率、`write_section` EmptyOutput 是否仍出现都未知）；②`classify_limit` 只认 xj189/MiniMax 文案，Ark 的 429 文案未知，落兜底（仍重试、不误停，缺细粒度分类与 Retry-After 语义）；③glm 单次调用慢（首字 ~8s、50 tok/s），write_plan_tasks/write_audit 的耗时要真跑才知；④`test_guard_effort_ladder` 现在失败为 glm 的 EmptyOutput（原本就是预存失败，单独排期）。
**基线真跑 v27（2026-11-03，`data/refs/ra_v27_ark_std.db`，标准档，题目与 v26 Rust 对 Go 完全相同）——⚠️ 快 ≠ 好，不可对外宣称「提速 3 倍」**：节点合计 239.6s（v26 MiniMax：709.2s）、总 token 242k（v26：453.6k）、38 次 LLM 调用（v26：55）、零重试/零错误/零 429/5xx、cache 命中 1.2%。**但产物薄得多**：报告 **8,034 字 / 5 个正文小节**（v26：**72,204 字 / 10 个小节 + 存疑**，每节 3~15k 字），来源 27/段落 94（v26：32/122）。对比规划链路：outline 节数 **5 vs 10**（glm outline 输出 695 token，M3.1 时 1548）；write_plan_tasks 任务 **5 vs 11**、`mandate` 平均 ~100 字 vs ~300 字、每任务分配段 8~15；`write_section`（mini，思考关）单节输出 **503~1052 token**（v26 M3：1840~5796）。即**两处同时变薄**：规划（glm low）把题目拆得更少、任务书更短；写手（mini none）每节只写 ~1/4~1/5 的量。耗时/token 的下降大部分来自**工作量变少**而非模型更快，没有可比性。另：`final_audit_skipped:true`、`final_revise_targets:3`（审查返修上限被触发后只做程序检查，需确认是否本轮行为）。**其它事实**：`/billing` 的「节点合计耗时」口径验证通过（净计算 ~4.3min，与 node 合计一致）；驱动教训——审批点会话 `status` 保持 `running` 不翻 `awaiting_approval`，**要看 `/approval` 端点的 `waiting`**（旧驱动按 status 轮询会空等；本次因此多等了 22.7 分钟，非平台问题）；`POST /api/sessions` 返回键是 `session_id` 不是 `id`。**备用档案 `ark_ds`（deepseek-v4-flash-ga-260731，别名 deepseek-v4-flash，`effort_off:none`）已加但无环节使用**：none/minimal 真关思考，low 会真思考（难题 1471 token），工具 forced/required 遵守，缓存生效（第二次 cached=2048），首字延迟 ~1.5~2s（glm ~8s），长输出吞吐 ~48 token/s（与 glm 同量级，mini ~86）。
**变体对照与定稿（v28/v29/v30，2026-11-03，同题 Rust 对 Go，每格 n=1，只能当方向性证据）**：以 `RA_MODELS_PATH` / `RA_CKPT_DB` 两个环境变量开关（新增，`tests/test_env_overrides.py`）并行跑变体，指标由 `/tmp/ark/metrics.py` 统一从库里采（脚本在 /tmp，会丢）。结果（报告字数/节数/mandate 均字/墙钟）：v26 MiniMax 72.2k/10/327/~20min含审批；v27 mini+glm low 8.0k/5/117/4.3min净；A（规划 ds low，写手 mini）11.5k/7/376/13.9min——ds low 在规划烧大量思考（write_plan_tasks 输出 16~22k token、120~250s）；B（规划 glm low，写手 glm low）21.6k/6/89/~5min；C（规划与写手全 ds：规划 low 写手 none）22.2k/7/230/12.3min；D（规划 ds none，写手 glm low）17.7k/5/179/6min——ds none 规划仍只有 5 节；E（规划 glm high，写手 glm low）23.0k/8/305/10.8min；**v30（定稿配置，默认路径验收）22.2k/8/89/6.95min 墙钟（节点合计 387s）、267k token、0 重试/0 错误、`final_audit_skipped:false`**。**结论**：①写手是主因之一——mini none 每节只写 500~1000 token（~1k 字），glm low / ds none 约 1.5~3k token（~2.7k 字）；②规划链路 glm low 偏薄（5~6 节），glm high 到 8 节，ds low 充实但太慢太贵，ds none 没有改善；③**mandate 均字在 glm 同配置下跨次波动很大（E 305 vs v30 89）**——说明规划产出有方差，别拿单次数字当规律；④**仍未追平 v26**：报告 ~22k 字 vs 72k 字，单节 ~2.5k vs ~6.5k 字，单节 output token（v30 [859..1904]）约为 M3 的 1/2~1/3；即写手深度/长度差距还在。
**定稿配置（已写入 config/models.yaml，头部有依据注释）**：mini none = probe_light/probe_degraded/extract/relevance/dedupe/summarize/session_summary；**glm high** = probe_outline/outline/assess/write_plan_spec/write_plan_tasks/write_audit；**glm low** = write_section/write_revise/write_patch/fast_answer。快速档（v30 fast，glp1 题）：墙钟 120s（v26 MiniMax ~278s）、回答 4,214 字（v26 5,854）、引用 14/14 无超范围、64k token；F1（fast_answer 用 mini）3,580 字、F2/v30（glm low）4.2~4.5k 字——选 glm（耗时差仅 ~16s）。
**已知缺口（Ark 迁移后）**：①写手深度未追平 v26（见上）——候选手段未试：写手 glm high、提示词/篇幅约束（注意 `RA_SPEC_SLIM` 默认=1，是篇幅压缩开关，见 write.py:50）、per-task 分配更多段；②规划产出方差大（n=1 的 A/B 不可靠），要做决策需 n≥3；③来源/chunk 数波动（v30 23 源 vs v26 32 源），检索非确定；④`classify_limit` 仍不认 Ark 文案；⑤Ark 套餐是「订阅」，未验证长期额度/限频表现；⑥新增 refs 快照：`data/refs/ra_v27_ark_std/ra_v28_{A,B,C}/ra_v29_{D,E,F1,F2}/ra_v30_{std,fast}.db`（本机备份已含，`~/Documents/deep-research-refs-backup-20261103.tar.gz`）。

### 94. 下一阶段待办（2026-11-03 压缩前落档；顺序已和用户对齐）
**用户的指令**：①先做**第一档**（小而确定），做完**再一起推送**（当前本地领先 origin 若干提交，未推）；②「直接修」——有把握修好的小问题直接修、修完报告；判定为要大改的，等其它修好后和用户一起处理；③权威性只做离线统计，**不改检索逻辑**；④checkpoint 根因、快速档升级为深度报告：不动，等用户定。
**第一档**：
1. **3 个预存失败测试分类**：`test_fallback_summary`（③ 项：写手 listing 含 `[无概括` 前缀、有概括段的概括原文也在——`_seg_summary` 在 write.py ~593–603，前缀 `[无概括·取原文开头]`，疑断言过期，需对照 listing 实际拼法）；`test_minimax_truncation`（② 项：断言「截断后预算翻倍（`_minimax` 保留的是翻倍，不是降 effort）」，疑断言过期，提交 b88cc5d 后重试逻辑统一）；`test_guard_effort_ladder`（**注意**：Ark 迁移后补了 ARK_API_KEY stub，该测试在 v30 验收轮里**通过**——但它走的是 `_opencode` 路径，需确认是真过还是桩没盖到；先复核再分类）。每项要给结论+证据：改代码的要有测试证明，改断言的要写清原因。偶发 `test_toolloop_protocol` 一并看是否 flaky。
2. **TECH_DEBT #2–#7 的低风险项**：#3 `pyproject.toml` 的 `ra` 入口点不存在、#4 `providers.opencode` 档案缺 model、#5 空的 `research_agent/docs/` 包、#7 `config/search.yaml` 无人加载且值与真值矛盾（可加醒目说明或删）；#6 参考库备份已做一次本机备份（见坑 93）；#2「离线守门测试会污染默认库 data/research.db」要先看改法再定是否属「小问题」。
**第二档（取证先行，不盲改）**：③`write_section` 偶发 EmptyOutput（MiniMax 时期现象；Ark 组合下 v28–v30 共 8 次真跑 EmptyOutput 全为 0——先确认是否随模型消失，别再为它投入）；④权威性：低权威来源被单源引用过多——用 `data/refs/` 现有库离线统计低权威来源（weibo/雪球/文库转存等）被引用占比，**不改检索逻辑**，数据出来后再和用户对齐。
**第三档（等用户定）**：checkpoint 根因（全文塞进图状态）、快速档「升级为深度报告」接口、批 7 坏页过滤的真实起效证据、写手深度追平 v26（见坑 93 已知缺口①）。
**第一档执行结果（2026-11-04）**：
- **3 个预存失败测试的分类（均为测试过期/脆弱，生产代码无 bug）**：①`test_fallback_summary`——**断言过期**：`write.py` 的 `_WRITE_PRELOAD` 默认=1，写手默认拿**原文**（`_preload_evidence`），概括 listing（含 `[无概括` 前缀）只在回退路径（预加载关/块超 70k 字符/返修）出现；已把 t3 拆成回退路径（`_WRITE_PRELOAD=False`，保留原断言）+ 预加载路径（断言两段原文都在、不走概括 listing）。②`test_minimax_truncation`——**fixture 漂移**：`_minimax` 的 `min(guard.max_output_tokens, budget*2)` 翻倍逻辑完好；失败是因为测试借用线上 `write_section` 配置，而它迁到 glm 后 `guard.max_output_tokens == max_output_tokens`（16384），没有翻倍余量；已在 `pin_minimax` 里拷贝 guard 并放大到 4 倍，测试不再依赖线上余量（provider.py 未动）。③`test_guard_effort_ladder`——**真通过但脆弱**：它走的确实是真实 `_opencode` 路径（桩的是 HTTP client），只是断言写死「起始 effort=high」靠 `write_plan_tasks` 现在恰好配 glm high；已把用到的环节钉成 `high`，并用 `RA_MODELS_PATH` 指向 write_plan_tasks=low 的副本复跑证明独立于线上配置。偶发的 `test_toolloop_protocol` 本轮多次全量跑均通过，未复现，不作 flaky 结论。
- **TECH_DEBT #2**：已修（`tests/_isolate.py`，详见 TECH_DEBT #2）；**默认库里的孤儿 `llm_call`（714 条）未删**。**#3**（删 `ra` 入口声明）、**#4**（删 `opencode` 档案与三条 blocked 备选）、**#5**（删空包 `research_agent/docs/`）已做。**#6** 本机备份已做（坑 93），异地未做。**#7** `config/search.yaml` 复核后判定「接上还是删」是二选一的产品决定，当时未动；**用户随后决定删除，已删（见坑 61）**。
- **验证**：全部 44 个 `tests/test_*.py` 在**带隔离**与**不带隔离**两种环境下退出码全 0；不带隔离那一轮 `data/research.db` 19 表逐表行数、`data/checkpoints.db` 字节前后不变。**没有重跑标准/快速档真跑**：本档改动只有测试文件、`pyproject`、已无引用的 `opencode` 档案与空包，不触及运行路径，坑 93 的 v30 对默认配置的验收仍然有效。
- **仍待用户定**：孤儿 `llm_call` 清理；第二档两项取证（`write_section` EmptyOutput 已随模型切换消失的确认、权威性离线统计）；第三档全部。
**第二档/第三档部分的执行结果见坑 95。**
**收尾动作**：第一档做完 → 更新文档 → `git push origin main`（push 前用 .env 里的密钥值扫一遍待推提交，SSL/HTTP2 报错重试即可）。


### 95. 第二档取证 + checkpoint 根因复查 + 写手深度目标调整（2026-11-04）
**用户决定（本轮）**：①`config/search.yaml` 删除（见坑 61）；②第二档直接离线测；③checkpoint 根因「可以查一下」（只调查，没改）；④**写手深度不再追平 v26**——长度不等于质量（长报告也可能是废话、也跟模型本身性能相关），只要流程没有大问题即可，所以**坑 93 已知缺口①「写手深度追平 v26」从目标列表里撤下**，以「流程健康」为验收：零 EmptyOutput/零错误、引用都在范围内、审查真的跑了（`final_audit_skipped:false`）、无空小节、无 `write_plan_error`/`write_audit_skipped`；⑤批 7 的证据并入第二档；⑥快速档升级深度报告等前置做完再说。**脚本**已存档于 `tests/offline_stats/`（`auth_stats.py`/`auth2.py` 权威性；`gate_stats.py` 闸门/坏页；`empty_stats.py` EmptyOutput；`ckpt_sample.py`/`ckpt_analyze.py` checkpoint）；它们读 `/tmp/auth/snap/` 下的一致快照（对 `data/refs/*.db` 逐个 `sqlite3 .backup` 而得，**不要直接读带 -wal 的原库**），重跑要先重建快照目录。
**③ `write_section` EmptyOutput（`llm_call` 全部 34 个参考库）**：MiniMax 时期 write_section 295 次调用中 EmptyOutput 4 次（1.4%）、write_revise 68 次中 2 次（2.9%）、write_patch 54 次 0；**Ark 时期 write_section 47 次、write_revise 6 次、write_patch 5 次，EmptyOutput 全为 0**；v31（见下）也为 0。**结论：Ark 下没出现，但样本不足以说「已随模型消失」**——按 MiniMax 时期 ~1.4% 的率，47 次里期望才 ~0.7 次，0 次与之无法区分。别再为它单独投入，但也别写「已解决」。（顺带：MiniMax 时期 summarize 有 20 次 EmptyOutput，是另一个环节的故障，Ark 下未见。）
**④ 权威性离线统计（只统计，没改检索）**。口径：来源编号=session 内 source 表 rowid 升序（同 `_load_numbered_evidence`）；引用=报告正文（去掉文末自动来源清单，否则每个来源都「被引用」；兼容 `[ S3·1]` 带空格写法）里的 `[S<n>·<m>]` 按来源计；权威分档=脚本内的**域名规则表**（low=社媒/问答/内容农场/文库转存/门户转载，如 weibo/xueqiu/zhihu/csdn/cnblogs/renrendoc/yunpan.plus/163.com/sohu.com；high=政府/教育/学术出版/官方文档，**很保守**；其余全进 mid），另与 LLM 判的 `source_nature` 对照。**规则表本身有不确定性**：163.com/sohu.com 同时承载通讯社稿件和自媒体号，归 low 偏严；high 偏窄所以 high 占比被低估。
- 标准档：MiniMax 时期 11 份（v18–v26，424 来源/8096 引用）low 占来源 27.4%、占引用 27.4%，high 6.8%/13.2%；Ark 时期 5 份有正文的（v28B/C/D/E、v30，142 来源/777 引用）low 15.5%/13.8%，high 4.9%/3.2%；快速档 9 份 low 26.9%/29.1%，high 16.2%/19.5%。**两个时期题目不同（MiniMax 多为固态电池，Ark 全是 Rust 对 Go），所以不能当作模型对比**；同题 Rust 对 Go 内部：v25 low 引用 30%、v26 1%、Ark 5 份 14%，跨次波动本身就很大。
- **单源集中度**（单个 low 域名占该报告引用的比例）：**renrendoc.com（文库转存）**在固态电池题上反复出现——v21 37%、v22 30%、v18 20%、v23 16%，快速档 fast_solid 58.5%；sohu.com 24%（v25 GLP-1）、bbs.csdn.net 16%（v25 Rust/Go）、xueqiu 15%（v24）、163.com 17%（v28B）。**用户观察到的「低权威来源被单源引过多」在数据里成立，但集中在特定题目（中文产业题的文库转存站），不是普遍现象**。
- **抽样读被引句子（2026-11-04，`tests/offline_stats/sample_low.py`，固定随机种子 7；renrendoc 读了 v21 的 6 句 + v22 的 4 句，sohu/163/csdn 各 3 句，共 19 句，样本很小，只能当定性判断）**：
  - **renrendoc.com 是真问题**：被引来源的标题形如「2026固态电池…分析及…前景.docx」「…评估报告-20260529.docx」「…研发项目建议书.docx」，正文是套了「2026」年份的模板化长报告（带 `\[1\]` 式脚注，却没有可追的原始出处）；被写手拿来支撑**具体数字**（如「硫化物设备投资较液态增加 30% 以上」「1GWh 产线设备成本约为液态 1.8 倍」「卤化物专利占比不足 5%、增速 210%」「2023 年全球设备市场 56 亿美元」）。写作/审查环节有一部分已自觉标成「线索级、不构成已验证事实」（抽样中明确读到 2 处写「线索」），但仍有大量数字句直接引它而不加限定。v21 里 658 个带引用的句子有 270 句引到它。**`source_nature` 的判定也不稳**：同类文档有判 `aggregator`（对）、有判 `research`（错，被标题唬住）、有判 `unknown`。
  - **sohu.com / 163.com 不能按域名一刀切**：抽到的是**转载**——163.com 那篇是 QCon 上字节跳动的 Rust 微服务演讲实录（一手内容的转载），sohu 那几篇是指南共识摘录/科普（有些年份较老，如 2018、2019）。权威性取决于原文而不是门户域名，所以我上面「低权威」统计里把门户归 low 是**偏严**，这两类的真实风险被高估。
  - **bbs.csdn.net**：一篇个人观点帖，写手把它当「作者亲历」用并有归属限定，用法恰当。
  - ⇒ **「域名低权威」是个粗糙的代理；真正需要处理的是一类特殊来源：文库转存/模板化报告站（renrendoc、docin、doc88、book118、wenku.baidu 等），问题在于「数字无法溯源」而不是「域名低」**。选项（都没做，等用户定）：(a) 把这类站标成「线索级」，写手提示词里要求数字不得仅凭它成立、审查加一条规则；(b) 在写手/审查阶段对单域名引用占比设软上限；(c) 在检索层降权或过滤——这条动检索逻辑，按你之前的指示不做；(d) 维持现状。**没有改任何检索/写作逻辑**。可选动作（等用户定）：写手/审查阶段对单域名引用占比设软上限、或把来源性质/权威档喂给写手提示词做取舍；二者都要先定义「低权威」名单，那是产品判断。
**默认库 `data/research.db` 的孤儿 `llm_call`（2026-11-04 查实，未清）**：库里 `session` 表 0 行；`llm_call` 714 行**全部 `session_id IS NULL`**，时间跨 2026-10-04 到 11-03，几乎都是测试/预检/探针脚本的记账（环节名带 `_t9`/`_t12`/`_pf_basic`/`_pf_forced`，以及 write_section/write_plan_tasks/relevance 在 MiniMax、internai、xj189、Ark 各档案上的探测）。代码里所有读 `llm_call` 的地方（`server.py` 账单、`nodes.py` run_health、`repo.py`、`tests/cost_report.py`/`timeline_report.py`）都带 `WHERE session_id=?`（或 `IS NOT NULL`），**NULL 行永远不会被读到**，所以**清不清都不影响功能**；清掉的唯一变化是这个 450KB 的库变小一点、少一点历史探测记录（早期预检的 token/延迟数字已在各坑里记录）。属于不可逆删行，若要清用 `sqlite3 .backup` 先备份，再 `DELETE FROM llm_call WHERE session_id IS NULL`。
**批 7 起效证据（离线重放，`gate_stats.py`）**：把 `page_defect` 回放到所有已存来源：批 7 之前的 10 个库（392 来源）命中 3 个（v25 GLP-1 的模板标题页、v25 Rust/Go 的 liepin、jobxzc），批 7 之后的 12 个库（287 来源）**命中 0**；真跑的 `low_quality:*` 跳过事件仍然是 0（含 v31）。⇒ **过滤器至今没有在真实运行里真正拦下过一页**（坏页只出现在批 7 之前的库里，之后的真跑没再搜到）；能说的只是「回放 3/3 命中、0 误伤，批 7 之后没有坏页漏进库」，**仍不能说它在线上起了作用**。合并闸门 V2：批 7 之后「merged 判定」段的关掉比例 98/291=33.7%，之前 105/887=11.8%，但题目构成不同（之后几乎全是 Rust/Go），不可归因。「招聘/{{/立即投递」关键词在批 7 之后 on_topic 段里的命中（每份 Rust/Go 约 3 段）逐条看是正常内容（讨论「招聘 Rust 工程师」），无泄漏；批 7 之前 v21 有 12 段命中 wang.eitech.edu.cn，未核查内容。
**v31 标准档（Rust 对 Go，默认配置，关自动裁剪以观察 checkpoint；`data/refs/ra_v31_std_ck.db`，尚未进本机备份包，要重打）**：墙钟 481s（审批点到达于 217s，等待 0）、285k token、6 小节（outline 6、tasks 6）、报告 20.1k 字、30 来源/30 个不同引用、`cite_over:false`、`llm_bad:[]`、`emptyout_rows:0`、`final_audit_skipped:false`、`mandate_avg` 102、无坏事件。**规划方差再添一例**：同样的 glm high 配置，outline 节数 v29E=8、v30=8、v31=6——n 越多越能看清，单次不是规律。
**checkpoint 根因复查（只调查，未改任何代码）**——TECH_DEBT #1 的旧数字已经过时，重新量了（`ckpt_sample.py` 每 5s 采样、`ckpt_analyze.py` 逐通道反序列化，真跑时 `RA_CKPT_AUTOPRUNE=0`）：
- 一场标准档共 **42 个 checkpoint，blob 合计 11.1MB**（库文件 12.0MB + 4.6MB WAL），最大单个 0.89MB；旧记录是单场 20~27MB。采样时序：库文件在 ~240s 起被撑大，完成后稳定。
- **根因已经转移**：旧文档的头号大户 `pending_extract` 现在整场只占 0.76MB（7%）；**现在 67%（7.4MB）来自 write 子图内的 10 个 checkpoint，每个都带着同样的 `segments`（394KB）+`on_topic`（349KB）**——即把全部证据段落全文塞进 WriteState，write 子图每个节点转移都重序列化一遍。且 `on_topic`（97 段）是 `segments`（112 段）的子集，同一份文本存了两遍；这些正文本来就在 `chunk` 表里（本场 chunk 正文合计 328KB）。
- **但影响很小**：①`dumps_typed` 序列化最大一份 0.89MB 状态只要 ~0.1ms，运行时间上可忽略；②自动裁剪（默认开）完成后每个 thread 只留最终 state（现役 `data/checkpoints.db` 26 个 thread 各 1 个 checkpoint，blob 合计 4.5MB，库文件 14.8MB 含空闲页）；③峰值磁盘 ~12MB/场。**只有失败/僵尸会话不会被自动裁剪**，每个残留约 12MB。
- **建议（等用户定）**：**降级为观察项，不建议现在动**。收益小（磁盘峰值 12MB、运行时间无感），而改动要碰 write 子图所有读 `state["segments"]/["on_topic"]` 的节点和测试夹具。若以后要治：窄改法是 `on_topic` 只存 ref 列表（去掉重复的一半，约 -47%），进一步把 `segments` 改成 id + 节点入口用 `_load_numbered_evidence` 读库；风险点是 write 节点和 audit/revise 都直接读正文、测试夹具大量直接塞 state。n=1，趋势明确但数字是单次。


### 96. 线索级来源机制（用户选定方案 a，2026-11-04）
**做了什么（不改检索、不丢来源）**：①`config/source_quality.yaml` 新增 `clue_grade_domains`（renrendoc/docin/doc88/book118/wenku.baidu/taodocs 六个文库转存域名，后缀匹配）；`search/quality.py` 加 `CLUE_MARK="⚠线索级"` 与 `clue_grade(domain)`；②`nodes._load_numbered_evidence` 给命中来源的 `src_lines` 行尾追加「⚠线索级（文库转存，其中的具体数字无法溯源，须另有来源印证）」——写手的预加载证据头、整篇报告末尾「### 来源」清单、快速档的来源清单都会带这个标记，编号与其他行格式不变；③`prompts.WRITE_RULES` 第 8 条：线索级来源的具体数字不得单独支撑断言，要并列引用一个非线索级来源，或写「据线索级材料称……（未获独立印证）」；`FAST_ANSWER_SYSTEM_V2` 加等价一行；④审查：`write._clue_only_numeric_sentences(draft, src_lines, cap=12)` 程序筛出「所有被引来源都是线索级、含数字（先删引用与 `\d{4}年`）、句中无限定语」的候选句（限定语 `_CLUE_QUAL_RE`：线索级/未获…印证/未经…验证/无法…核实/待…核实），在 `node_audit` 的 payload 里加 `线索级来源独撑的含数字断言`（空则不加键），`WRITE_AUDIT` 新增一条考虑项和一段说明（凡数字是断言核心且无限定语 → `findings` 判 high、`edit`、`fix` 为降级措辞）。**不接 violations/程序返修**，只作审查输入。**没有线索级来源的会话行为与改动前完全一致**（回归守卫测试覆盖）。
**review 时抓到的回归（已修）**：sidekick 把新块插进了 `node_audit` 的 `if rounds:` 块中间，使「上一轮你提出的问题」那个 payload 键只在有线索候选时才给——测试没抓到；已把新块挪到 `if rounds:` 之后，并补了 ④a/b/c 三个回归用例（手动复现 bug 后 ④b/④c 确实变红）。**教训：往一个带长注释的 if 块之间插代码要看缩进归属。**
**验证**：`tests/test_clue_grade.py`（离线，clue_grade、src_lines 标记、筛句 9 例、node_audit payload 三路、提示词断言、预加载头）；全部 `tests/test_*.py` 退出码全 0。
**端到端证据（只能当方向性，每格 n=1）**：①整场真跑 v32（固态电池题、默认配置、`data/refs/ra_v32_solid_clue.db`）：墙钟 673s、333k token、33 来源/33 引用、零错误——**但这场检索根本没抓到任何文库域名**（线索级来源 0、标记 0），所以只证明「无线索来源时行为不变」，没触发机制。②**写作子图重放对照**（同一份 v21 证据，同一套默认模型，`/tmp/ck3/replay_write.py`；开=默认、关=清空线索级规则；`data/refs/ra_v33_replay_clue_{off,on}.db`；指标脚本 `tests/offline_stats/clue_stats.py`）：「线索级独撑含数字句」**关 22 条 → 开 0 条**（v21 当年 MiniMax 全长报告 154 条）；引到线索来源的句子 66 → 12；on 臂的限定写法全部规范（「据线索级材料（文库转存的行业报告）称…（未获独立印证）」7 句，如「专利 12,843 件、年均增长约 45%（未获独立印证）」），另有 1 处写手主动声明「S28 为线索级的文库转存材料，数字均无法溯源」；两臂都 10 节、无 `<!-- task` 残留、墙钟 318s、无 plan_error；两臂各 1 条 `write_audit_skipped`（返修额度用尽后的合成收尾，正常）。
**快速档真跑 v34**（固态电池题，默认配置，`data/refs/ra_v34_fast_solid_clue.db`）：墙钟 136s、113k token、11 来源/11 引用无超范围、零错误；**检索同样没抓到文库域名**（线索级 0），所以也没触发机制，只证明 `FAST_ANSWER_SYSTEM` 新增一行没有副作用（版式、引用格式正常，无模板泄漏）。注：这场 113k token 比 v30 快速档（GLP-1 题，64k）高，题目不同、未深究。
**必须看清的局限**：①n=1/臂，写手有方差；on 臂报告更短（24.4k vs 31.1k 字，引用句 195 vs 239），**无法区分是「写手少用了文库来源」还是随机波动**——少依赖这类来源是预期副作用，但不能证明没丢有用内容；②**审查环节的贡献无法单独归因**：规则 8（写手）与审查候选同时生效，且 `write_trace` 只存最后一轮审查结论和各轮摘要计数（`audit_history_json` 的 findings 是数量不是明细），第 0 轮审查有没有提「线索」类 finding **取不回来**；③on 臂 `write_plan_tasks` 调用 4 次、输入 104k（off 1 次/24k）——这是规划环节的工具读段轮次，机制只改了来源清单行尾，我判断是方差，但没验证；④只有 6 个域名在名单里，没见过的文库站不会被标；名单是人工的；⑤非预加载回退路径（块超 70k 字符）与返修路径的写手 listing 不带来源行，看不到标记（返修时审查的 fix 指令本身带降级措辞）；⑥限定语豁免是正则，写手换说法（如「该文称」）会被判成漏网候选（偏严不偏松）；⑦「线索级」对应的是**数字无法溯源**，不等于内容错误，定性描述照常使用。
**⚠️ 文档债**：`data/refs/` 现新增 `ra_v31_std_ck / ra_v32_solid_clue / ra_v33_replay_clue_off / ra_v33_replay_clue_on` 等快照，**不在本机备份包里**（用户决定暂不重打）。

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
| 写作段 ~90s 非 LLM 静默 | **已查清（坑 87）**：不是 checkpoint（102 次写入合计 0.87s），是护栏重试丢弃的尝试没记账；现已补记 |
| 迁移到 M3.1 后的终稿质量 | **没有系统对比**（2026-10-20 坑 83/84）。v17 因 529 过载没跑完审查/返修；多节大纲 + 深读成功 + 种子注入同时发生的真实运行还没有 |
| `probe_outline` 强制交卷是否根治解析失败 | 样本仅 1 次成功，不等于证明 |
| 拥堵期过载重试的真实恢复率 | 冒烟 relevance 5 次重试全败（0/1），拥堵缓解后需要真跑看 `run_health` |
| `complete_json_forced` 空载重试 | 没有；summarize 偶发 EmptyOutput（文本冒充工具），拥堵期可能升高 |
| `assess` 在 M3.1 上思考占比偏高警告 | 未处理（v17 出现 1 次） |
| 来源性质 8/20 未判 | 老问题，v17 更严重（extract 被 529 打断），未单独处理 |
| 审查后仍有 1 处未核实的数字（v15「领先约 22%—32%」） | 种子注入后残余，未决定是否做窄的确定性兜底 |
| 迭代路线 | 三轮均已落地：① 正确性基本完成；② 时间优化 29.0→13.3（v22，首个含审查返修的可比基线）→8.2/10.2（v23/v24，工作量更小）→换题 8.9~10.6 分钟（v25），快速档 2.2~4.8 分钟（坑 87/88）；③ 前端产品化已完成并验收（坑 85/86/88·C2/C3）。④ **前端再设计 P1–P5 已完成**（坑 92：动效地基/首页/侧栏→流程页步进器→报告页三档布局→来源库主区双栏→审批/设置收尾+运行账单端点 `/billing`），远程仓库 `origin`（GitHub 私有）已建立并推送。⑤ **2026-11-03 供应商切换：MiniMax→火山方舟 Ark（mini+glm），定稿配置见坑 93，v30 验收跑通过（标准 6.95min/快速 2.0min，零错误），但报告深度未追平 v26**。**阶段性收尾时的残留见下面「收尾残留」行** |
| 收尾残留（2026-11-03 更新） | ①批 7（闸门 V2 + 坏页过滤）**仍无「真的起作用」的证据**——两次真跑 `low_quality:*` 命中都是 0（搜索根本没搜到这类页，只能说没回归）；批 8（checkpoint 自动清理）已真跑验证生效（完成后只剩 1 行）；②~~前端静默丢弃的事件~~已补齐渲染（审查跳过/规约缺小节/blocked/patch_reject/规划失败；`extract_error` 是内部结果记录无需渲染）；③`write_section` 偶发 EmptyOutput（模型提前收尾只吐 `{`）无缓解，每次多花 55~80s；④权威性：已做离线统计与抽样阅读（坑 95），**对「文库转存站数字无法溯源」这一真问题落了线索级机制（坑 96）**；域名低权威本身不处理（门户转载有一手内容）；⑤快速档无模型审查、来源只有 10~19 个；⑥3 个预存失败测试（`test_guard_effort_ladder`、`test_fallback_summary`、`test_minimax_truncation`）未判断该改代码还是改断言；⑦TECH_DEBT #2–#7；⑧~~无 git remote~~已建（`origin` = github.com/wd-ing/Deep-Research-Agent，私有）；**`data/refs/` 约 91MB 不可再生参考库不在 git/远程里；2026-11-03 已打本机备份 `~/Documents/deep-research-refs-backup-20261103.tar.gz`（同盘，非异地）**；⑨checkpoint 根因（全文塞进图状态）未治，靠完成后自动裁剪压住；⑩前端：审批页仍无「驳回」（后端空转）、运行账单只在流程页（报告页没有）、「节点合计耗时」≠ 用户感知总时长（不含审批等待）、修订痕迹朱批卡只在合成数据上验证过（真实带补丁会话待回归）、`homepage/` 展示稿定稿后要更新（用户自己的未提交改动） |
| assemble 清标题不干净 | **双保险**（坑 64 后端已修 + 坑 73 前端 `cleanReportMd` 渲染兜底；v13 存量报告已清洗）。待下次真跑观察 `fences_unwrapped` |

---

## 十一、要改代码前先确认的清单

- 加 state 字段 → **必须在 `ResearchState` / `WriteState` 里声明**，否则 LangGraph 静默丢弃
- 改 provider → 读第六节（86 条坑）+ 「切换时必须一起改的东西」
- 写/改守门测试 → provider **钉死**，不许跟着 `defaults` 漂；桩要接在**正确的层**上
- 加一条新的发请求路径 → **数一遍同类路径有几条**（这轮栽了三次：空 tools 400、流式分支、截断检测）
- 断 provider 协议形状 → **逐个字段核对** usage / stop_reason / tools / tool_choice，
  不要假设「另一条路有的这里也有」
