# 交接文档

> 面向接手这个项目的人（包括未来的我）。写于 2026-10-05。
> 详细决策与实测数据在 `DECISIONS.md`（1,000+ 行，追加式日志），
> 架构细节在 `DESIGN_R4_WRITE.md`（write 子图）与 `DESIGN_R5_EVIDENCE.md`（证据流转）。

---

## 一、这是什么

一个**本地优先的单人深度研究 Agent**。给定一个问题，它多轮检索、抓取、
抽取证据、搭建框架、分块写作、交叉审查，最后产出一份带引用出处的报告。

不做的事：多用户、权限、团队协作、移动端、代码沙箱、向量数据库、表格/PDF 解析。
这些在 `DECISIONS.md` 里明确排除过，别重新提议。

**搜索分工**：Tavily 管英文、SearXNG 本机裸装管中文。
SearXNG 用 `.searxng/searxng.sh` 惰性启停——用到才起，60 秒空闲后看门狗关闭。

**技术栈**：纯 Python 单栈。FastAPI（`server.py`）+ LangGraph（只用 StateGraph /
SQLite checkpointer / interrupt，不用其 agent 抽象）+ Vite/React（前端尚未写）。

代码 5,959 行，14 个模型调用阶段，3 个数据库迁移。

---

## 二、架构

```
主图（LangGraph，9 个节点）
  probe → outline → await_approval(人工中断) → retrieve → extract
        → assess → apply_revision ⇄ retrieve
        → write → finalize → END

子图（主循环看不到内部）
  probe        轻扫 → 歧义澄清 → 深扫 → 领域轮廓
  extract      段落概括 + 来源性质
  relevance    段级相关性闸门
  write        主 agent 规划 → 并行子 agent 写作 → 审查 → 定点返修
```

### 证据流转（最重要的设计，见 `DESIGN_R5_EVIDENCE.md`）

```
网页正文
  ↓ 切段（段落累积到 1000 token，段落边界落刀）
chunk（段落，约 700 token）          ← 落库
  ↓ extract 阶段写概括
段落概括（约 38 字）                 ← 落库，成为写作的证据入口
  ↓ 段级闸门
on_topic 的段落
  ↓ write 子图
主 agent 看到段落概括，判断不够时调工具往下挖：
  read_segments（看某段全文） / read_context（连同邻段） / read_source（看整篇）
```

**段号方案**：来源按 `rowid` 编 `S1 S2`，段按来源内 `rowid` 编 `S1·5`。
引用写成 `[S1·5]`。`node_write` 与 `tools/evidence.py` **必须用同一套编号**。

### 模型分三档

| 档 | 模型 | 字段 |
|---|---|---|
| T0 | `MiniMax-M3` | 不发任何思考字段（官方文档：省略即关闭） |
| T1 | `MiniMax-M3.1-Flash-Preview` | `output_config.effort: low` |
| T2 | `MiniMax-M3.1-Flash-Preview` | `output_config.effort: high` |

14 个阶段的分配见 `config/models.yaml`。**换模型前先跑 `tests/preflight_models.py`**——
那个端点没有 `response_format`，严格 JSON 全靠提示词约束，不预检就换很容易翻车。

---

## 三、当前状态

### 已验证

| 项 | 状态 |
|---|---|
| 冒烟 7 项 | ✅ |
| JSON 修复 21 例 | ✅ |
| write 子图结构自检 7 场景 | ✅ |
| 切分 + 概括（23 个真实来源） | ✅ 672 段 → 53 块，中位 760 token |
| R4 旧架构端到端（断言作证据单位） | ✅ 64,196 字 / 699 引用 / 0 处非标准写法 |
| SearXNG 空闲自停（看门狗方案） | ✅ 进程退出后仍能回收 |

### 验证中

**R5 新证据链路的端到端**。已跑到 `write_revise` 18 次调用，有几个问题待确认：

1. **段级闸门触发了约 200 次 LLM 调用。** 根因：那次测试传入的探针轮廓是空的
   （`{"subtopics": [], ...}`），没有关键词可用，**全部落到 LLM 判定**。
   真实运行时轮廓非空，规则层能拦住大部分。**但这暴露了一个脆弱性：
   闸门成本完全取决于探针轮廓的质量。** 需要用真实轮廓复测确认。
2. **12 个段落没进闸门**——概括批次失败的那些。这次有数字可查了（`gate_method` 为空）。
3. `write_revise` 18 次调用、约 179k token，返修量偏大，需看是否收敛。

### 未做

- 前端（`server.py` 有 SSE 接口，前端一行没写）
- 引用跳转的前端实现（段级引用 `[S1·5]` 已经能定位到段，但界面还没做）
- `dedupe` 阶段是**占位，代码从未调用**——抽取阶段没有去重，同一事实可能由多来源重复进入
- 第二组模型与降级通道（技术债）
- 探索式深度研究的 UI 形态（P2 决策里提过，一直没做）

---

## 四、文件地图

| 路径 | 是什么 |
|---|---|
| `research_agent/graph/nodes.py` | 主图节点（717 行，最容易出问题的地方） |
| `research_agent/graph/subgraphs/write.py` | write 子图：规划、并发写作、审查、返修 |
| `research_agent/graph/subgraphs/probe.py` | 探针：轻扫/歧义/深扫/轮廓 |
| `research_agent/graph/subgraphs/extract_batch.py` | 段落概括 + 来源性质 |
| `research_agent/graph/subgraphs/relevance.py` | 段级相关性闸门 |
| `research_agent/graph/prompts.py` | **全部提示词集中在这里**，改行为先看它 |
| `research_agent/llm/provider.py` | 三档模型、guard 三道防线、工具调用、记账（828 行） |
| `research_agent/llm/toolloop.py` | 工具循环（协议坑都写在注释里） |
| `research_agent/tools/evidence.py` | 三个证据工具 + 段号索引 |
| `research_agent/ingest/split.py` | 段落切分 |
| `research_agent/db/` | 裸 SQL + 薄封装，3 个迁移 |
| `config/models.yaml` | 14 个阶段的模型与预算 |
| `tests/` | 7 个会重复跑的测试 |
| `tests/probes/` | 23 个一次性探测脚本 + README（决策背后的实测证据） |

---

## 五、踩过的坑（这一节最值钱）

**都是我自己犯的，但方式高度重复。**

### 1. 猜参数名，然后拿自洽的假象当结论

`thinking.budget_tokens` 是 Anthropic 的字段，MiniMax 不认。我测了
"0 / 1024 / 4096 / 不传"四组，思考长度清一色 ~35,000 字符，就下结论
"budget_tokens 无效，只能换模型"。**四次其实是同一次**——参数压根没生效。

同一轮还发生在我自己写的工具循环上：到了轮数上限我发一条纯文本 user 消息，
但协议上 `tool_use` 之后只接受 `tool_result`，所以每次到上限就 400。
子 agent 的工具能力**一直是关的**，日志里只是一行"退回单次调用"。

> **教训：遇到反常先查厂商文档。猜错参数名时，黑盒测试会稳定复现同一个假象，
> 看起来特别像结论。**

### 2. 连续测量失败时，先怀疑自己的构造

缓存"能否沿对话延长"这个问题我测了三版才做对：
第一版手写了假的 `tool_use_id`；第二版**去掉了 `tools` 字段**，
而前缀顺序是 tools → system → messages，前缀变了当然读不到；
第三版才造出干净条件。而"缓存不能延长"这个错误结论，正是建立在第二版上的。

### 3. 把"同一时刻"当成稳定标识

`ingested_at` 只有秒级精度。段号索引用 `rowid`、取段落用 `ingested_at`，
同秒插入的段顺序就乱了，`read_context S1·2` 返回了第 3 段。

### 4. 模型会填错字段，包括那些"本来没意义"的字段

`rework_effective` 回答的是"上一轮返修是否奏效"，但首轮没有上一轮，
模型顺手填了 `false`，路由据此判定"改不动"直接停摆——而它同时说了 `need_rework: true`。
两个字段自相矛盾时，代码采信了会导致停工的那个。

### 5. 静默丢证据

`summarize_segments` 捕获异常后继续，没有概括的段落在
"要有概括才进证据池"这一条里被滤掉。**177 段里 12 段消失，零提示。**
证据被静默丢弃比报错危险得多——报告看起来完整，实际上缺了料。

> **教训：凡是"过滤掉没有 X 的"，如果 X 恰好由模型生成，
> 静默失败率就等于模型的不稳定率。** 这类地方必须有差额统计。

### 6. 自检覆盖不到出问题的地方

write 子图的结构自检 7/7 全过，但连续三次 bug 全在 `node_write` 那一层
（变量名 `sources` 漏改两处、给只读的 `sqlite3.Row` 挂字段两处）——**自检恰好绕开了出问题的地方**。
都是分多次手改重构留下的残留。

### 7. 先定机制，再找数据

`need_full` 单次上限 30 条、`CLAIM_SUMMARY_CHARS = 70`——都是先定数字后看数据。
后来实测：断言中位数 **64 字**，截断线定 70，**57% 的断言"摘要就是全文"**，
那层区分基本是空的。同一批数据也直接推翻了"按段落切"的方案。

---

## 六、工作约定（用户定的，逐条遵守）

1. **测试范围**：write 板块做完之前，测试只打写作环节；先子图单独验证，再接主图。
2. **先核对后动手**：所有决策与细节核对完再写代码。
3. **问题先报告**：测试遇到问题不直接打补丁，先输出、一起定位、判断是否要改架构。
   小 bug 可以直接修，**大问题必须先说**。
4. **能力优先**：能交给 agent 自主判断的不写成程序死规则；
   引导性规则优于硬约束。硬规则只用于合规性与底层约束。
5. **遇到结构性问题先确认方案再改**，别擅自修改。

---

## 七、后续任务

### 立刻要做

1. **等 R5 端到端跑完，看四轴**：术语一致性、跨任务重复率、引用规范率、任务分布。
2. **用真实探针轮廓复测段级闸门**，确认 LLM 调用量回落到可接受范围。
3. **12 段无概括的问题**：要么修 JSON 失败率，要么在报告里显式标注"这 12 段未能概括"。

### 近期

4. `dedupe` 补实现（技术债，同一事实重复进入报告）。
5. 第二组模型与故障降级通道（技术债，目前单点依赖 MiniMax）。
6. 前端：报告阅读 + 引用跳转（`[S1·5]` 点开显示那一段原文）。
7. R4 里定过的 15 个未完成项见 `DECISIONS.md`「技术债登记」。

### 待你拍板

- **写报告环节的模型选型**（R4 遗留）：保留 `MiniMax-M3`，还是回退到
  `M3.1 + effort=low`？之前因为诊断错误选了 M3，现在证据已变。
- **主 agent mandate 是否过重**：实测 mandate 写得非常细（几乎在替子 agent 决定怎么写），
  这与"发挥 agent 能力"有张力。现在有效，但是否该瘦身要看你判断。
- **降级报告的醒目度**：plan 失败时产出的是一份"看起来像报告"的东西，
  用户可能意识不到整场崩了。要不要加更醒目的失败标记。

---

## 八、环境

```bash
cd "/Users/wd/Documents/Deep Research Agent"
.venv/bin/python tests/smoke.py          # 冒烟
.venv/bin/python tests/write_struct.py   # write 子图结构自检（慢，约 8 分钟）
.venv/bin/python tests/e2e_run.py "问题" /tmp/x.db   # 端到端
.searxng/searxng.sh status|start|stop
```

`.env` 三个 key：`TAVILY_API_KEY` / `OPENCODE_API_KEY` / `MINIMAX_API_KEY`。
**绝不可回显、不可提交**（已在 `.gitignore`）。
