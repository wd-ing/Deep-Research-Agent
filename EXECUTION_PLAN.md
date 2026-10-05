# 深度研究 Agent · 执行方案

本文件由 `DECISIONS.md` 的七个阶段决策汇总而成。P1 到 P7 已全部定稿，本文只做汇总与落地，不引入新决策。

- 生成日期：2026-10-03
- 决策依据：`DECISIONS.md`（102 条）
- 状态：v1 全部功能已纳入，分六个板块递进交付

---

## 一、技术栈汇总

| 层 | 选型 | 关键约束 |
| --- | --- | --- |
| 模型 | Gemini 3.8 Flash 为主，配置化 provider 支持多模型 | 抽取与去重环节必须设 `thinkingBudget: 0` |
| 编排 | LangGraph 的 StateGraph + SQLite checkpointer + interrupt | 不用其 agent 抽象与 tool 装饰器，不用 LangSmith |
| 搜索 | SearXNG 自建，**部署在服务器** | 开 JSON 输出、关限流器、引擎按来源分别返回 |
| 正文提取 | Trafilatura | 接受 JS 渲染页面抽不到的已知限制 |
| 向量化 | multilingual-e5-small，384 维 | query 加 `query: ` 前缀，文档加 `passage: ` 前缀，不可省 |
| 存储 | SQLite 单文件，裸 SQL 加薄封装 | float16 存向量，按需加载不常驻 |
| 全文检索 | SQLite FTS5 内置 | 支持中文分词，先粗筛再向量精排 |
| 切分 | 自实现递归字符切分 | 分隔符优先级，中英混合，按 token 不按字符 |
| 后端 | Python + FastAPI + SSE | 数据库与检索同进程，绑 0.0.0.0 预留手机访问 |
| 前端 | Vite + React + React Flow | 本机优先，窄屏降级推 v2 |
| PDF | PyMuPDF 抽正文与参考文献 | v1 不抽表格 |

---

## 二、部署形态

```
本机
├── FastAPI 服务（:8765，绑 0.0.0.0）
│   ├── LangGraph 研究循环
│   ├── 检索与入库
│   └── SQLite（research.db，含向量）
├── Vite 开发服务器（:5173）
└── 前端静态资源

服务器
└── SearXNG（Docker，限 512M）  ←─── HTTP JSON 访问
```

**分家理由**：SearXNG 是唯一同时满足「最重常驻」「不接触私人数据」「最易出故障」三条的组件。数据库、embedding 推理、PDF 解析一律留本机——e5-small 在 Mac 上向量化几百段只要几秒，搬到服务器反而多一层网络依赖。

**可逆性**：搜索层是可插拔 provider，SearXNG 地址写在配置里。哪天想搬回本地 Docker，改一行配置即可，不动任何代码。

---

## 三、目录结构

```
research-agent/
├── pyproject.toml
├── config/
│   ├── models.yaml          # 各环节模型配置
│   └── search.yaml          # SearXNG 地址、引擎、超时
├── research_agent/
│   ├── server.py            # FastAPI 入口 + SSE
│   ├── graph/
│   │   ├── state.py         # AgentState 定义
│   │   ├── nodes.py         # plan / retrieve / extract / assess / write
│   │   └── checkpointer.py  # SQLite checkpointer
│   ├── llm/
│   │   ├── provider.py      # 统一调用入口，按 config 分发
│   │   └── capability.py    # 模型能力探测（结构化输出/关thinking/稳定性）
│   ├── search/
│   │   ├── base.py          # SearchProvider 抽象
│   │   ├── searxng.py       # SearXNG 实现
│   │   └── normalize.py     # URL 规范化
│   ├── ingest/
│   │   ├── extract.py       # Trafilatura
│   │   ├── split.py         # 递归字符切分
│   │   ├── summarize.py     # 概括句生成（独立上下文，批量）
│   │   └── embed.py         # e5-small 封装
│   ├── db/
│   │   ├── schema.sql
│   │   ├── migrations/
│   │   └── repo.py          # 裸 SQL 薄封装
│   ├── retrieve/
│   │   ├── history.py       # 两级向量粗筛到细筛
│   │   ├── fts.py           # FTS5 查询
│   │   └── cosine.py        # 批量余弦
│   └── docs/
│       ├── pdf.py           # 正文 + 参考文献
│       └── markdown.py
├── web/                     # Vite + React
└── tests/
```

---

## 四、数据 Schema

```sql
-- 会话
CREATE TABLE session (
  id            TEXT PRIMARY KEY,
  title         TEXT NOT NULL,
  question      TEXT NOT NULL,
  status        TEXT NOT NULL,       -- running / paused / done / archived
  archived_at   TEXT,                -- 只有归档后才进历史检索
  cost_tokens   INTEGER DEFAULT 0,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

-- 报告，与 session 一对一
CREATE TABLE report (
  id            TEXT PRIMARY KEY,
  session_id    TEXT UNIQUE REFERENCES session(id),
  outline_json  TEXT,                -- agent 在 plan 环节自定的大纲
  markdown      TEXT,
  version       INTEGER DEFAULT 1,
  created_at    TEXT NOT NULL
);

-- 来源
CREATE TABLE source (
  id              TEXT PRIMARY KEY,
  session_id      TEXT REFERENCES session(id),
  url             TEXT NOT NULL,
  url_normalized  TEXT NOT NULL,     -- 规范化后用于判重
  domain          TEXT,
  title           TEXT,
  source_type     TEXT,              -- official / community / academic / blog / media
  engine          TEXT,              -- 来自百度还是必应，SearXNG 按引擎分别返回
  source_date     TEXT,              -- 内容自身时效，参与衰减；抽不到则 NULL
  fetched_at      TEXT NOT NULL,
  content_hash    TEXT,              -- 与其他 source 相同即同源转载
  char_count      INTEGER,
  http_status     INTEGER,
  superseded_by   TEXT REFERENCES source(id)
);

-- 段落，最小储存单元
CREATE TABLE chunk (
  id            TEXT PRIMARY KEY,
  session_id    TEXT REFERENCES session(id),
  report_id     TEXT REFERENCES report(id),
  source_id     TEXT REFERENCES source(id),
  origin        TEXT NOT NULL,       -- web / upload / history
  summary       TEXT,                -- 一句话概括，召回确认时展示
  content       TEXT NOT NULL,
  heading_path  TEXT,                -- 递归切分时继承的原段标题
  token_count   INTEGER,
  embedding     BLOB,                -- float16, 384 维
  status        TEXT DEFAULT 'pending',  -- pending 时不参与检索
  source_date   TEXT,
  ingested_at   TEXT NOT NULL,
  superseded_by TEXT REFERENCES chunk(id)
);
CREATE INDEX idx_chunk_status ON chunk(status);
CREATE INDEX idx_chunk_session ON chunk(session_id);

-- 主题，用于浏览与可选过滤
CREATE TABLE topic (id TEXT PRIMARY KEY, name TEXT UNIQUE, slug TEXT, created_at TEXT);
CREATE TABLE session_topic (session_id TEXT, topic_id TEXT, updated_at TEXT);

-- 调用记账
CREATE TABLE llm_call (
  id TEXT PRIMARY KEY, session_id TEXT, stage TEXT, model TEXT,
  input_tokens INTEGER, output_tokens INTEGER, latency_ms INTEGER, created_at TEXT
);

-- 上传的文档
CREATE TABLE ingested_doc (
  id TEXT PRIMARY KEY, path TEXT, title TEXT, doc_type TEXT,
  source_date TEXT, ingested_at TEXT, parsed_at TEXT
);

-- 全文索引
CREATE VIRTUAL TABLE chunk_fts USING fts5(summary, content, heading_path, content='chunk', content_rowid='rowid');
```

**两阶段入库**：`status='pending'` 时检索层跳过它。顺序是——研究结束先落正文，再批量生成概括句，再算向量，最后置 `complete`。任一步失败可单独重跑。

---

## 五、核心流程

```python
# 主循环，LangGraph StateGraph
while True:
    # 1. 检索：5 条 query 并行（中文组 + 英文组）
    urls = search(queries_zh + queries_en, language=["zh-CN", "en"])

    # 2. 判重
    urls = [u for u in urls if not is_visited(u)]
    urls = [u for u in urls if not time_sensitive or not in_cache(u)]

    # 3. 抓取：8 并发，同域间隔 1s
    pages = fetch(urls)   # 403/429 直接跳过不重试

    # 4. 抽取：thinkingBudget = 0
    evidence = extract_entities_and_claims(pages)

    # 5. 同源合并：正文 hash 比对，近似才调 LLM
    evidence = dedupe(evidence)

    # 6. 缺口评估
    gap = assess_gap(outline_progress, evidence)

    # 7. 停止判定
    if consecutive_no_new >= 2: break
    if round >= 8 or url_count >= 50 or elapsed > 20min: break
    if cost_tokens > 500_000: break
    if not gap.needs_search: break

# 8. 写报告
report = write(outline, evidence)   # 内联引用 + 认知演进一节

# 9. 收尾（独立上下文，命中缓存）
summaries = batch_summarize(report.paragraphs)   # 一次批量调用
embed(summaries + paragraphs)
mark_chunks_complete()
```

**历史注入只影响 plan**。研究开始前先跑两级检索（会话概括粗筛 → 段落概括细筛），结果在 plan 阶段展开让你确认，确认完收起进上下文。**不进入证据层。**

**时间敏感短路**：问题含「最新 / 近况 / 现在 / 目前」时，历史内容只用于缩小检索范围，**禁止作为作答依据**，且必须绕过 7 天抓取缓存。答案引用历史结论时必须带时间前缀。

---

## 六、交付板块

### 进度总览

| 板块 | 状态 | 说明 |
| --- | --- | --- |
| M1 地基 | ✅ 完成 | 冒烟 7/7。SearXNG 实测 142MB 磁盘 99MB 内存，启停 2.1s / 0.3s |
| M2 单会话研究 | 🟡 代码完成，冒烟 8/9 | 仅 LLM 环节待 API key 才能端到端验证 |
| M3 前端 | ⬜ 未开始 | — |
| M4 跨会话 | ⬜ 未开始 | — |
| M5 语料 | ⬜ 未开始 | — |
| M6 收尾 | ⬜ 未开始 | — |

### M1 地基（完成）

**验收**：SearXNG 接通并返回 JSON；LLM provider 与 thinking 硬约束生效；递归切分全片在预算内；e5 向量化 Top-1 正确；DB schema 与两阶段入库往返正常。

**实测数据**：SearXNG 磁盘 142MB、内存 99MB、启停 2.1 秒与 288 毫秒，8 个引擎可用（搜狗 / 360 / 夸克 / 必应 / DuckDuckGo / 维基 / arXiv / PubMed）。中文检索 30 条量级、零异常。

**过程中修掉的问题**：

1. `add_source` 与 `add_chunk` 把生成的 id 存进局部变量未写回，主键列为 NULL 而返回值是未入库的 id。SQLite 的 TEXT PRIMARY KEY 不自动 NOT NULL，会静默接受，属最危险的一类
2. schema 中 `CREATE INDEX ... ON source_id` 漏表名
3. 显式列出列名使 `DEFAULT` 失效
4. 配置文件出现重复 `engines` 键，导致 82 个默认引擎关不掉
5. 启停脚本内文件删除命令走可恢复删除通道，使停止阻塞 2 分钟
6. 系统 Python 缺失 CA bundle 导致全机 pip 无法访问 HTTPS，改写用户级 pip 配置修复

### M2 单会话研究（代码完成，待 key 验证）

**验收**：一个新名词能产出带内联引用的报告；过程中可断点恢复；触发硬上限时优雅停止；计划审批可中断。

**冒烟 8/9**：状态机编译、时间敏感判定、五条停止条件路由、抓取与正文抽取、403/429 跳过、缓存 TTL、FastAPI 接口、SSE 事件流全部通过。第 9 条 LLM 抽取需 `GEMINI_API_KEY`。

**抓取器实测行为**（5 个真实站点）：

| 站点 | 判定 | 原因 |
| --- | --- | --- |
| typesafe.ai（JEV 官方博客） | 抽出 9085 字符 | 正常 |
| arxiv.org | 抽出 2173 字符 | 正常 |
| 掘金 | 拒绝 | captcha 验证码页 |
| 极客公园 | 拒绝 | blocked:403 |
| example.com | 拒绝 | too_short 正文不足 200 字 |

三类拒绝全部判对原因。发现通用浏览器 UA 会被维基百科 403，后续应改为诚实地标识应用名。

### M2 单会话研究

LangGraph 状态机、plan 节点与 interrupt 审批、检索循环、抽取与去重、报告生成、SSE 推送。

**验收**：给一个新名词，产出带内联引用的报告，引用可点开看原文；过程中断点可恢复；触发三重硬上限时优雅停止。

### M3 前端

Vite + React 骨架、研究过程实时可视化、实体扩展链路图（React Flow）、报告阅读器与证据侧边卡片、计划审批界面。

**验收**：一轮研究能实时看到查询词、命中 URL、抽取到的实体与扩展链路；点引用能展开对应证据；plan 阶段可勾选历史召回段落。

### M4 跨会话

显式归档、两阶段入库、概括句批量生成、两级向量检索、FTS5 粗筛 + 向量精排、时间敏感短路、认知演进一节。

**验收**：同一话题第二次研究能召回上次段落且 token 消耗下降；问「最新表现」时强制联网且答案带时间；同源转载被合并为一个信源；矛盾结论在报告单列一节。

### M5 语料

PDF 与 Markdown 上传、PyMuPDF 正文与参考文献抽取、Markdown 按标题层级切段、语料段落接入同一检索层。

**验收**：上传论文后能通过它回答相关问题，引用带来源页码；论文参考文献被抽出并可检索。

### M6 收尾

成本记账、URL 规范化与内容 hash 判重、7 天缓存与绕过逻辑、抓取礼貌性、审计日志、主题浏览页。

**验收**：单次研究 token 消耗可查；同源转载正确合并；403/429 不重试；审计日志不含任何凭据；能按主题翻看历史会话。

---

## 七、必须遵守的硬规则

以下每一条都对应一个已知的失败模式，实现时不要简化。

1. **`thinkingBudget: 0` 用在抽取与去重环节。** Flash 类模型的已知失败模式是 thinking 吃光输出预算导致返回空。
2. **e5 的 `query: ` 与 `passage: ` 前缀不可省。** 漏掉直接掉质量。
3. **时间敏感问题强制绕过历史作答与抓取缓存。** 这是最危险的静默失效——回答看起来完全合理但内容是旧的。
4. **只有显式归档的会话进历史检索。** 未完成的、被放弃的会话天然被排除。
5. **召回必须可见可排除。** 不透明的历史注入无法察觉污染。
6. **冲突显式记录，不静默覆盖。**
7. **矛盾结论在报告单列一节。** 已实测：两个方法可信的独立测试会给出相反结论，只写一边就是误导。
8. **403/429 停止，不换 IP 不重试。**
9. **审计日志永不记 cookie、token、API key、请求头。**
10. **未设上限的向量化输入一律加 `query: ` / `passage: ` 前缀。**

---

## 八、已排除项

以下功能在讨论中确认过但明确不纳入 v1，列此防止后续被误加回来。

| 排除项 | 原因 |
| --- | --- |
| 小红书、X、微信公众号原站抓取 | 维护成本持续或技术不可行，走转载降级方案 |
| 托管 deep research API | 编排逻辑不可控，与跟随式检索相悖 |
| 商业搜索 API | 成本与配额风险，v1 用 SearXNG |
| 实体图谱 | 依赖实体消歧，段落级检索已足够 |
| 向量数据库 | 本规模下手写余弦更快更省内存 |
| 主动多角度找对立观点、裁决队列 | 会引发局部过度思考 |
| 表格 PDF 解析 | 坑深，v1 价值主要在网络检索链 |
| JS 渲染页面抓取 | 需要 Playwright 兜底，推 v2 |
| 登录态与凭据保存 | v1 不做，但审计日志规矩现在就定 |
| 订阅型信源 | 推 v2 |
| 手机端布局与鉴权 | 推 v2，架构已预留绑 0.0.0.0 加 token |
| LangSmith 可观测性 | 私人研究内容不上传 SaaS |
