# Deep Research Agent

本地优先、单人自用的深度研究 Agent。多轮检索 → 抓取 → 证据处理 → 框架 →
分块写作 → 交叉审查 → 带段级引用的报告。

> **接手这个项目前，先读 [`CONTEXT.md`](CONTEXT.md)** ——
> 架构、仍然有效的决策、56 条踩过的坑、工作约定、已知缺口。

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

25 分钟 + 几十万 token 的东西，用回放几秒跑完、零成本：

```bash
cp data/refs/ra_v11_full.db /tmp/ra_replay.db   # data/refs/ 里任何一份都行
.venv/bin/python tests/replay_server.py --speed 1 --db /tmp/ra_replay.db
```

它**只把「跑图」换成回放录好的事件**，其余全走真代码
（真 FastAPI / 真 SSE / 真 SQLite / 真 EvidenceTools）。

⚠️ 两个已知限制：
- 回放会话**没有 chunk**，段号取不到东西 —— 报告页与引用抽屉要用左侧已有的**真实会话**测
- **只测得到审批为止，写作阶段根本测不到**（坑 25）

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
| `test_toolloop_protocol.py` | **一半在线**：① 协议转换是离线的，② 会真跑两轮工具循环 |
| `replay_server.py` | 前端回放服务器（零 API 调用、零搜索额度）|
| `verify_nature_e2e.py` | 端到端验来源性质判定不再整批丢 |
| `probe_call_failure.py` | 验「失败调用也留痕」（`_record_error` + 三条失败路径）|
| `probe_session_summary.py` | 会话摘要 stage 的真实数据探针 |

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
CONTEXT.md          ★ 交接文档：架构 / 决策 / 56 个坑 / 已知缺口
```
