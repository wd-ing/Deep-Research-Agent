# Deep Research Agent

本地优先、单人自用的深度研究 Agent。多轮检索 → 抓取 → 证据处理 → 框架 →
分块写作 → 交叉审查 → 带段级引用的报告。

> **接手这个项目前，先读 [`CONTEXT.md`](CONTEXT.md)** ——
> 架构、仍然有效的决策、63 条踩过的坑、工作约定、已知缺口。
>
> 已知但有意押后的工程欠账在 [`TECH_DEBT.md`](TECH_DEBT.md)。

---

## 快速开始

```bash
cd "/Users/wd/Documents/Deep Research Agent"

# 1. 跑一轮完整研究（含写作，约 25~40 分钟，会真的花 token 和搜索额度）
.venv/bin/python tests/e2e_run.py "固态电池的产业化进展，以及各家技术路线的分歧点" /tmp/my_run.db

# 2. 跑守门测试（全部离线，零 API 费用，几秒）
for t in test_minimax_stream test_minimax_truncation test_forced_dispatch \
         test_outline_degrade test_summarize_retry test_plan_tasks_forced \
         test_rate_limiter test_guard_effort_ladder test_platform_switch \
         write_struct probe_plan_tasks_toolloop; do
  printf "%-28s " "$t"; .venv/bin/python tests/$t.py >/dev/null 2>&1 && echo OK || echo FAIL
done

# 3. 预算体检（读最近一轮的库，确认没有环节在顶天花板）
.venv/bin/python tests/check_token_budget.py --warn-only
```

`.venv` 已在仓库里。若需重建：`uv venv && uv pip install -e .`（依赖见 `pyproject.toml`）。

---

## 配置

### 密钥

复制成 `.env`（**已在 `.gitignore`，绝不可回显、绝不可提交**）：

```
TAVILY_API_KEY=…        # 搜索
MINIMAX_API_KEY=…       # 当前默认 provider
INTERN_AI_API_KEY=…     # 备用 provider
```

### 换 provider / 换模型 —— **只改一行**

`config/models.yaml` 的 `defaults.provider`。阶段一律不写 provider，
模型名 / 地址 / 密钥 / 「关思考」怎么表达，全部从 `providers:` 档案继承。

⚠️ **改之前先读 [`CONTEXT.md` 第三节「切换时必须一起改的东西」](CONTEXT.md#三平台与模型)**。
换 provider 不是只改一行：协议形状不同，防线要逐条核对
（已经因此踩过两次：坑 51 截断检测、坑 54 `complete_json_forced` 没有分派）。

改完先预检（走生产代码路径，不手搓 payload）：

```bash
.venv/bin/python tests/preflight_model.py <模型名> [provider]
```

---

## 跑 server（前端后端）

```bash
cd "/Users/wd/Documents/Deep Research Agent"
set -a; . ./.env; set +a          # ⚠️ uvicorn 不会自己读 .env
RA_DB_PATH=/tmp/ra_fe.db .venv/bin/python -m uvicorn research_agent.server:app \
    --host 127.0.0.1 --port 8000
```

前端另起：

```bash
cd frontend && npm install && npm run dev      # 默认连 127.0.0.1:8000
```

### 主要端点

| 端点 | 干什么 |
|---|---|
| `POST /api/sessions` | 建会话 |
| `POST /api/sessions/{sid}/run` | 启动研究 |
| `GET /api/sessions/{sid}/approval` | 待审批的框架（没有就 `waiting:false`）|
| `POST /api/sessions/{sid}/approve` | 审批并续跑 |
| `GET /api/sessions/{sid}` | 报告 + 来源 + 主题 |
| `GET /api/sessions/{sid}/seg/{ref}` | **按段号取原文**（`S1·5`），报告里点引用走它 |
| `GET /api/sessions/{sid}/trace` | 检索轮次图（**必须走 DB，不能走 SSE 事件**）|

SSE 事件：`connected` / `node` / `progress` / `thinking` / `done` / `error`。

### 改前端时用回放，别真跑

25 分钟 + 几十万 token 的东西，用回放约 80 秒跑完、零成本：

```bash
# 本机开发是 launchd 常驻的（坑 69），平时不用手动起：
#   前端 http://127.0.0.1:5180 · 回放 http://127.0.0.1:8000
#   库 data/refs/ra_v13_gate.db · 日志 ~/Library/Logs/dra-*.log
# 手动起的等价命令：
.venv/bin/python tests/replay_server.py --speed 0 --db data/refs/ra_v13_gate.db --port 8000
```

侧栏左下角「▶ 回放」按钮一键演示（0.6 倍速、全程约 80 秒）；
它只在探到 `/api/replay/state` 时出现——连真后端自动消失。

它**只把「跑图」换成回放录好的事件**，其余全走真代码
（真 FastAPI / 真 SSE / 真 SQLite / 真 EvidenceTools）。
fixture 用 v13 真跑录制，含 B1 全部细粒度事件（坑 68）。

回放里的补丁明细和 `run_health` 是合成演示数据；视觉与交互改动用 Chromium 截图验收
（无头浏览器 + 溢出/控制台/HTTP 断言），流程与脚本见 CONTEXT 坑 85/86。
前端新增只读端点：`GET /api/sessions/{sid}/review`（补丁明细 + 运行健康）、
`GET /api/sessions/{sid}/events`（已落库的可回放事件）、
`GET /api/sessions/{sid}/billing`（运行账单：分阶段节点耗时 + token/调用数，只读；耗时不含等待审批）。报告页顶栏「导出」支持
Markdown / PDF（浏览器打印）/ Word（前端生成 .docx）。

回放会话建的时候会把 donor 的 **report + source + chunk** 克隆进来
（坑 73）——报告页、引用抽屉、段号 S1·5 全部可用，全程可测。

---

## tests/ 里有什么

### 守门测试（离线，零 API 费用）

改了哪块就只跑哪个。它们全部**桩在生产判定入口上**、provider 钉死，
不跟着 `defaults` 漂。

| 脚本 | 守住什么 |
|---|---|
| `test_minimax_stream.py` | Anthropic 形状的 SSE 累加器 + 流式选路 + 截断检测 |
| `test_minimax_truncation.py` | 截断检测认不认 Anthropic 的 `usage` / `stop_reason` |
| `test_forced_dispatch.py` | `complete_json_forced` 的 provider 分派 + 「关思考+工具」护栏 |
| `test_outline_degrade.py` | `node_outline` 的框架退化分支（不抛异常、且留下痕迹）|
| `test_summarize_retry.py` | 概括批次的有界重试（两类失败、重试有界、不重跑成功的）|
| `test_plan_tasks_forced.py` | 分派环节强制工具交卷 |
| `test_rate_limiter.py` | 主动限流（event hook、端点分桶、配置改了跟着改）|
| `test_guard_effort_ladder.py` | 空返回/截断重试是**降 effort**、不抬预算 |
| `test_platform_switch.py` | 切平台后「关思考」仍然关着（两种平台的写法正好相反）|
| `test_fast_mode.py` | 快速档全链：mode 迁移/路由/caps/node_answer 四形态/降级 |
| `test_source_quality.py` | 坏页/非内容站的入库前丢弃（域名后缀、模板页、retrieve skipped）|
| `test_maintenance.py` | checkpoint 清理：分类计划、keep_latest、VACUUM 锁、自动开关 |
| `write_struct.py` | write 子图的结构自检：路由、装配、引用校验、返修收敛 |
| `probe_plan_tasks_toolloop.py` | `node_plan_tasks` 的工具循环接线 + 编造段号被丢弃 |
| `check_token_budget.py` | 预算体检：实测最大输出占配置上限多少 |

### 打真平台的入口（会花钱，逐个按需跑）

| 脚本 | 干什么 |
|---|---|
| `e2e_run.py <问题> <db>` | **端到端跑一次完整研究**（含人工审批点）|
| `preflight_model.py <模型> [provider]` | 换模型的生产路径预检，不手搓 payload |
| `probe_forced_choice.py [n] [模型]` | 直接打 Phase B 那个调用，验 `tool_choice` 强制 |
| `probe_plan_tasks_forced.py` | 分派环节改走强制工具交卷之后的在线验收 |
| `probe_minimax_plan.py [n]` | 真实载荷跑 `node_plan_tasks` |
| `probe_relevance_model.py` | `relevance` 闸门判定换模型对跑 |
| `probe_assess_run.py <问题> <db>` | 只到 assess（write 打桩），改规划/检索类先跑这个 |
| `cost_report.py <db> <session_id>` | 钱花在哪、时间花在哪、流程有没有空转 |
| `e2e_http.py <问题> <db>` | **走真实 server + SSE + 异步图**跑完整研究（自动审批），结束打 timeline 与 cost 报告；测时间一律用它，别用 `e2e_run.py`（同步图，checkpoint 路径不同）|
| `timeline_report.py <db> [sid]` | 阶段瀑布、LLM 并发度剖面、静默区间及「未解释秒数」、checkpoint 汇总（只读，老库也能跑）|
| `exp_write_preload.py` / `exp_outline_ab.py` / `exp_merge_gate.py` | 时间优化期间的 A/B 实验驱动（真 API；对照组口径见 CONTEXT 坑 87，`exp_merge_gate` 的对照组是坏的）|
| `exp_fast_answer.py` / `exp_gate_note.py` / `exp_plan_spec.py` / `exp_audit_trim.py` / `exp_spec_coverage.py` | 快速档与闸门/规约变体的回放实验（真 API；`ls tests/exp_*` 看全量）|
| `ground_scan.py` | 终稿数字与证据核对（**只适用标准档**——读 write 快照的 tasks/drafts，快速档没有这份快照）|
| `test_toolloop_protocol.py` | **一半在线**：① 协议转换是离线的，② 会真跑两轮工具循环 |
| `replay_server.py` | 前端回放服务器（零 API 调用、零搜索额度）|
| `verify_nature_e2e.py` | 端到端验来源性质判定不再整批丢 |
| `probe_call_failure.py` | 验「失败调用也留痕」（`_record_error` + 三条失败路径）|
| `probe_session_summary.py` | 会话摘要 stage 的真实数据探针 |

### 时间相关的环境变量（模块加载时读；默认值即当前生产值）

| 变量 | 默认 | 作用 |
|---|---|---|
| `RA_WRITE_WORKERS` | 12 | 写作/返修并行度（一次全并发，无批次/串行头）|
| `RA_MAX_REWORK` | 1 | 返修轮数；返修用完后末轮不再调模型，只做程序检查。2=旧行为 |
| `RA_WRITE_PRELOAD` | 1 | 写作任务把分配段全文附进 prompt、只发 1 次强制交卷；0=多轮读证据 |
| `RA_SEARCH_TIMEOUT_S` | 12 | 单次搜索超时 |
| `RA_FETCH_DEADLINE_S` | 20 | 单个页面抓取整体 deadline |
| `RA_OVERLOAD_RETRIES` / `RA_OVERLOAD_BASE_S` | 5 / 10 | MiniMax 过载重试 |
| `RA_SPEC_SLIM` | 0 | 写作规约压缩到最小（批 6 省 token；1=只传必要的字段）|
| `RA_AUDIT_TRIM` | 0 | 审查输入裁剪（批 6：只给审查需要的小节级材料）|

## 鉴权与本地开发

**本地开发设 `RA_AUTH=0`**——以 `local` admin 身份通行，一切行为与无鉴权版相同（`tests/_isolate.py` 已默认置 0，离线测试不用管）。生产/多人使用时 RA_AUTH 缺省即开，此时必须先用 CLI 发邀请链接：

```bash
.venv/bin/python -m research_agent.users add --id amy --name Amy     [--admin] [--standard 2 --fast 6]
# 输出里带一次性邀请路径 /api/auth/invite?t=<token>——发给对方，
# 打开后种下 HttpOnly cookie 即登录。rotate 轮换、disable 吊销即时生效。
```

| 变量 | 默认 | 说明 |
|---|---|---|
| `RA_AUTH` | 开 | `0`=本地开发固定 `local` admin，不建用户库 |
| `RA_USERS_PATH` | `data/users.json` | 用户库（只存 token sha256，权限 600） |
| `RA_AUTH_SECRET` | `data/auth_secret` | cookie 签名密钥（首次自动生成 600；改了就全员下线） |
| `RA_COOKIE_SECURE` | 关 | `1`=cookie 一律加 Secure（http 本地调试别开） |
| `RA_MAX_RUNS` | 3 | 全局同时在跑的研究上限；每人固定 1 场 |
| `RA_INTENT` | 0（测试） | 输入意图前置识别；多人部署建议开 |
| `RA_CKPT_SWEEP` | 开 | failed/僵尸 checkpoint 每 6h 自动清扫 |

数据按 owner 隔离：member 只见自己的会话/文件夹/来源；`GET/PUT /api/config`、`GET /api/search` 仅 admin；每日场次限额标准 2/快速 6（admin 不限），超额 429。
| `RA_AUDIT_TRIM` | 0 | 审查输入裁剪（批 6：只给审查需要的小节级材料）|
| `RA_CKPT_AUTOPRUNE` | 1 | 会话完成后自动裁剪旧 checkpoint（0=关；见下「维护」）|
| `E2E_MODE` | standard | `e2e_http.py` 的档位：`standard`/`fast` |

### 标准 / 快速两档（C1/C2）

POST `/api/sessions` 带 `mode`：`standard`（默认，完整管线：审批→分节写作→审查返修）
或 `fast`（探针→大纲→**一轮**检索→抽取→闸门→**一次成文**；无审批/无审查/无返修）。
快速档事件：`kind:"answer"`（chars/sources/segments_used/citations_total/citations_removed/degraded）；
前端按 `mode` 识别——快速档会话有「快速」标签、流程页是「整理作答」一步、无审批段。

`config/source_quality.yaml`：抓取后入库前的确定性过滤（招聘平台域名、未渲染模板页）。
规则缺失/损坏自动回落空表，不炸检索。

### 维护：checkpoint 清理（TECH_DEBT #1）

```bash
# 默认 dry-run，只打印计划表；--apply 才执行；--vacuum 需同时 --apply
.venv/bin/python -m research_agent.maintenance prune-checkpoints \
  [--db data/research.db --db data/refs/ra_xxx.db ...] [--stale-days 7] \
  [--delete-orphans] [--yes-really]
```

`--apply` 默认**只裁剪已完成会话的历史 checkpoint**（每个 thread 保留顶层
最新一份），不删任何 thread。孤儿 thread（所有传入研究库里都没有的
session）要删须显式加 `--delete-orphans`；若合并后 session 总数为 0 或
孤儿占比超半数，删除会被拒绝——说明 `--db` 很可能没指对，确认无误再加
`--yes-really`。`--db` 可重复/逗号分隔。**VACUUM 前要停掉占用该库的服务**
（锁超时会被拒并说明）。

### 怎么确认一个脚本是不是真的离线

**别看名字，看它发不发请求。** 本轮就有一个 `test_*.py`
名字和位置都在「守门测试」里，**实际有一半会打真平台**
（`test_toolloop_protocol.py` 的第二节）。
两种可靠的确认方式：

```bash
# ① 看它有没有真的把传输层换掉/桩掉
grep -nE "MockTransport|_client\s*=|fake|stub" tests/<脚本>.py

# ② 断网跑一遍（最直接）
env -u http_proxy HTTPS_PROXY=http://127.0.0.1:1 .venv/bin/python tests/<脚本>.py
```

只桩了 `client._client.stream` / `.post` 的**不算数** ——
那会绕过 httpx，限流器之类的管线钩子根本没被跑到
（这正是 `test_rate_limiter` 第一版栽的坑，见该文件顶部注释）。
正确的做法是换**传输层**（`MockTransport`）：请求走真实 httpx 管线，但不出机器。

⚠️ **打真平台的脚本会消耗 token 与搜索额度。** 跑之前先想清楚要验什么、
最少几次能验到（n=1 的结论是噪声，见坑 35）。

---

## 目录

```
research_agent/     后端（见 CONTEXT.md 第四节代码地图）
frontend/           前端（React + Vite）
config/models.yaml  ★ provider 档案 + 各环节预算/effort/stream
data/               默认 SQLite 位置（用 RA_DB_PATH 覆盖）
data/refs/          ★ 真实跑出来的参考库（探针输入 + 实测数字出处，见 CONTEXT 第四节）
tests/              守门测试 + 生产路径入口
tests/fixtures/     回放与端到端验的固定数据（replay_server / verify_nature_e2e 读它）
CONTEXT.md          ★ 交接文档：架构 / 决策 / 坑 / 已知缺口
TECH_DEBT.md        已知但有意押后的工程欠账
```
