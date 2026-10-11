# 交接文档

> 面向接手这个项目的人（包括未来的我）。**新窗口先读这一份。**
> 优化轮结论在 `OPTIMIZATION_LOG.md`；架构设计在 `DESIGN_R4~R7_*.md`；
> 逐条决策的时间线在 `DECISIONS.md`（历史档案，只在追溯「当时为什么那么想」时才翻）。
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

当前主用 **xj189（DeepSeek-v4-flash-0731）**；`minimax` / `opencode` 保留备用。

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
| `research_agent/graph/runner.py` | 主图装配（边在这里）|
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

⚠️ **`tests/parallel_check.py` 当前是坏的**（2026-10-11 发现，**在干净的
`bcdb673` 上同样失败，不是最近改动引入的**）。现象是断言
`calls == [len(b) for b in batches]` 失败，而它上面打印的
`各批请求的段数：[]` 是**空列表**——mock 一个请求都没截到，
但同一次运行 120/120 条概括全部产出、失败 0 批。
所以更像是 C1-j 修完 ContextVar 记账之后测试的 mock 没跟上，
而不是并发真的乱序。**跑它之前先修 mock，别把失败当成真 bug 去查代码。**

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

---

## 八、待办

### 最近的

| 事项 | 说明 |
|---|---|
| **流式第二步：增量回调** | 平台**会**流式吐 `reasoning_content`（174 块增量），
前端可以实时显示「AI 在想什么」。**回调形态待定**（进程内回调 / 事件队列 / 其它），
取决于 `server.py` 现有 SSE 接口怎么接。**产品决策，不是技术难点。** |
| `blocked` 分类的实测数据 | v5 跑出 `blocked=0`。**2026-10-11 已核对 `prompts.py:488-518`，三类判据（`no_material` / `bad_assignment` / `spec_conflict`）写得很完整**，且明确要求「没材料可写」必须进 `blocked` 而非判 high；同轮审查正常产出 5/4/4 条 findings，材料 41 来源 143 段。**所以更可能是材料确实够，而非缺判据**——但这仍是推断，要坐实需要一个材料确实稀缺的案例。 |
| `assess` 思考档位 | 配的 `low`，但实测 `low` 判断力约等于「关」。要改 high 还是关，待定。 |
| 失败调用无留痕 | 504/429 是异常不是记账行，`llm_call` 查不到。要补需要动 schema。 |

### 中期的

- **前端**（一行没写）：报告阅读 + 引用跳转、审批界面、`assemble_warnings` 高亮
- **Agnes 接入**：`DESIGN_R7_SECONDARY_MODELS.md` 有完整方案（接 `write_plan`/`probe_*`/
  `outline`/`extract`，不接 `write_audit`/`relevance`），需要 provider 适配 + 令牌桶
- `assign` / `dedupe`：配置里有、图里零调用点，**记着的待办**不是废代码
- 段落概括 JSON 失败率：已改强制 `tool_choice` 绕过，**根因未查**

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
