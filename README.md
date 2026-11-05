# Deep Research Agent

把一个真问题，做成一份**逐句可回溯**的调研报告。

多轮检索 → 抓取原文 → 判定来源 → **你确认框架之后才动笔** → 分块写作 → 交叉审查 → 带段级引用的报告。
拿不准的地方盖「存疑」，不悄悄略过。

[English](README.en.md) · [主页](https://research.wding.site/homepage/) · [宣传片](https://research.wding.site/homepage/film.html) · [在线体验（凭邀请）](https://research.wding.site/)

> 贡献者 / 接手者先读 [`CONTEXT.md`](CONTEXT.md)（架构、仍然有效的决策、踩过的坑、已知缺口），
> 再读 [`TECH_DEBT.md`](TECH_DEBT.md)（已知但有意押后的工程欠账）。
> 开发与运维细节在 [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)，部署在 [`deploy/README.md`](deploy/README.md)。

---

## 它做什么

- **多轮检索 + 证据闸门**：每轮检索后先过一道段级闸门，过滤跑题、模板页和不可溯源的转存站，再判断「材料够不够」。
- **人工审批框架**：先出研究框架，由你确认后才进入写作；写作前后不会悄悄改框架。
- **逐句段落引用**：报告里每个判断挂着 `[S2·1]` 形式的引用，点开就是原文段落；来源按性质（一手/媒体/聚合/研究/社区）分类。
- **交叉审查与修订痕迹**：写完后由模型审查并返修，已应用的补丁可在「修订痕迹」里逐条查看；拿不准的断言盖「存疑」。
- **标准 / 快速两档**：标准档约 8~11 分钟（含审批）；快速档约 2~3 分钟出一份简报，之后可**一键补成深度报告**（复用已有检索，不重做探针和大纲）。
- **导出与回放**：导出 Markdown / PDF（浏览器打印）/ Word；每轮检索、每次调用都落库，可零 token 回放。
- **多用户**：邀请令牌换 cookie 登录，每人只看到自己的研究；每人每日场次限额、全局并发上限；`/api/config` 等仅管理员可用。
- **前置意图识别**：寒暄、「你是谁」、写诗翻译之类的输入不会烧一场研究，而是就地给出引导。

## 流程

```
START → probe（探针：对象/歧义）→ outline（框架）→ retrieve（检索）→ extract（抓取+抽取）→ gate（段级闸门）
                                                                                      │
                          快速档 ───────────────────────────────────────────────────┤
                            └→ answer（一次性简报）→ finalize → summary → END          │
                                                                                      ▼
                          标准档：assess（评估）→ [材料不够：retrieve 再来一轮]
                                         │      → [未审批：apply_revision → await_approval（等你批准）→ assess]
                                         ▼
                                   write（写作子图：规划 → 分块写作 → 审查 → 返修）→ finalize → summary → END
```

节点定义见 [`research_agent/graph/runner.py`](research_agent/graph/runner.py)，写作子图见 `research_agent/graph/subgraphs/write.py`。

## 快速开始（本地）

前置：Python 3.12、Node 22、一个 [火山方舟 Ark](https://www.volcengine.com/product/ark) 的 key（模型）、一个 [Tavily](https://tavily.com) 的 key（英文检索）。
中文检索走本机 SearXNG（可选，见下）。

```bash
git clone https://github.com/wd-ing/Deep-Research-Agent.git
cd Deep-Research-Agent

# 后端
python3.12 -m venv .venv
.venv/bin/pip install -e .          # 必须可编辑安装：代码按文件位置找 config/ 和 data/
cp .env.example .env                # 填入 ARK_API_KEY 和 TAVILY_API_KEY
set -a; . ./.env; set +a
RA_AUTH=0 .venv/bin/python -m uvicorn research_agent.server:app --port 8000
```

> 鉴权默认**开启**，且没有用户时服务拒绝启动。本地单人开发设 `RA_AUTH=0`（身份固定为本机管理员）。
> 对外服务见下面「部署与多用户」。

```bash
# 前端（另一个终端）
cd frontend && npm install && npm run dev      # → http://localhost:5180，/api 代理到 127.0.0.1:8000
```

打开 http://localhost:5180，输入一个调研问题。标准档会在评估后停在审批页等你确认框架。

**真跑会花钱**：一场标准档约 30 万 token，快速档约 6~11 万 token，另加搜索额度。
改前端时不必真跑，用回放服务（见 [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)）。

### 中文检索（SearXNG，可选）

中文题走 SearXNG（搜狗 / 360 搜索 / 夸克 / Bing），英文题走 Tavily。本机用 Docker 起一个即可（端口必须是 8888）：

```bash
cp deploy/searxng-settings.yml /tmp/searxng-settings.yml
sed -i.bak "s/CHANGE_ME_openssl_rand_hex_32/$(openssl rand -hex 32)/" /tmp/searxng-settings.yml
docker run -d --name research-searxng -p 127.0.0.1:8888:8080 \
  -v /tmp/searxng-settings.yml:/etc/searxng/settings.yml:ro searxng/searxng:latest
```

不起 SearXNG 时，英文题照常，中文题的检索会拿不到结果。

## 配置

| 位置 | 作用 |
|---|---|
| `.env` | `ARK_API_KEY`、`TAVILY_API_KEY`（密钥只放这里，`.gitignore` 已排除） |
| `config/models.yaml` | provider 与「环节 → 模型」映射，`stages:` 是真相源；改完要重启 |
| `config/source_quality.yaml` | 坏页/非内容站过滤规则、线索级来源（文库转存站）名单 |

两条容易踩的模型约束（`StageConfig.validate()` 会拦）：
- 简单环节用 `doubao-seed-2-0-mini`，`reasoning_effort: disabled`（档案会翻译成 `none`，真正关闭思考）。
- `glm-5-3-flash` 关不掉思考，**必须显式写 `low` 或 `high`**；缺省会按最高档思考，几分钟才返回。

Ark 端点用 `https://ark.cn-beijing.volces.com/api/plan/v3`，不是 `/api/v3`。

常用环境变量（调用时读取，默认值即生产值）：

| 变量 | 默认 | 作用 |
|---|---|---|
| `RA_AUTH` | 开 | `0` = 关闭鉴权（仅本地开发） |
| `RA_USERS_PATH` / `RA_AUTH_SECRET` | `data/users.json` / `data/auth_secret` | 用户库路径 / cookie 签名密钥 |
| `RA_COOKIE_SECURE` | — | `1` = cookie 加 `Secure`（HTTPS 部署） |
| `RA_MAX_RUNS` | 3 | 全局同时在跑的研究数（另有每人 1 个） |
| `RA_INTENT` | 开 | `0` = 关闭输入意图识别 |
| `RA_DB_PATH` / `RA_CKPT_DB` | `data/research.db` / `data/checkpoints.db` | 业务库 / checkpoint 库 |
| `RA_MODELS_PATH` | `config/models.yaml` | 模型配置路径 |
| `RA_CKPT_AUTOPRUNE` / `RA_CKPT_SWEEP` | 开 | 完成后裁剪 checkpoint / 失败与僵尸会话的定期清扫 |
| `RA_WRITE_WORKERS` | 12 | 写作子图并发度 |
| `RA_SEARCH_TIMEOUT_S` / `RA_FETCH_DEADLINE_S` | 12 / 20 | 单次检索 / 单页抓取上限（秒） |

其余时间与重试相关的变量见 [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)。

## 部署与多用户

小范围多人使用的做法（邀请令牌、限额、并发、Caddy/systemd、SearXNG 容器、tiktoken 离线坑）见 [`deploy/README.md`](deploy/README.md)。要点：

```bash
.venv/bin/python -m research_agent.users add --id amy --name Amy [--admin] [--standard 2 --fast 6]
# 命令只在终端打印一次邀请路径 /api/auth/invite?t=<token>，发给对方；
# list / disable / enable / rotate 即时生效。
```

必须单 worker（`--workers 1`）：运行注册表、限速、SSE 订阅都是进程内的。

## 测试

```bash
.venv/bin/python tests/runall.py      # 全量回归：全部离线、零 API 费用；各脚本 150 秒超时
.venv/bin/python tests/test_fast_mode.py    # 或只跑改动相关的那个
```

测试是脚本式（`main()` + `check()`），不是 pytest 收集；新增测试脚本必须在 import 应用模块之前 `import _isolate`，否则会写进默认库。

| 位置 | 内容 |
|---|---|
| `tests/test_*.py` | 离线守门测试（桩在生产判定入口上，provider 钉死） |
| `tests/intent_cases.py` + `exp_intent_eval.py` | 意图识别的 188 条标注用例与真实调用评测（会花少量 token） |
| `tests/experiments/` | 历史对照实验脚本（真平台调用，会花钱；保留为证据，不是回归） |
| `tests/probes/` | 探针脚本与前端 Playwright 验收脚本 |
| `tests/offline_stats/` | 读参考库做的离线统计（权威性、闸门、EmptyOutput、checkpoint） |
| `tests/e2e_run.py` 等 | 打真平台的端到端入口：`e2e_run.py "<问题>" <库路径>` |

## 仓库结构

```
research_agent/   后端：graph/（LangGraph 节点与子图）、llm/（provider 与护栏）、search/、ingest/、db/（SQLite+迁移）、server.py、auth.py、intent.py
frontend/         React + TypeScript + Vite 前端
config/           models.yaml、source_quality.yaml
deploy/           生产部署：systemd 单元、Caddy 片段、SearXNG 配置、部署说明
homepage/         产品主页与宣传片播放页（静态）
film/             宣传片制作脚本与成片
docs/             开发与运维文档
tests/            测试、实验、探针、离线统计
CONTEXT.md        架构 / 决策 / 踩过的坑 / 已知缺口
TECH_DEBT.md      已知但有意押后的工程欠账
```

## 已知局限

诚实列在这里，细节与证据在 [`CONTEXT.md`](CONTEXT.md) 与 [`TECH_DEBT.md`](TECH_DEBT.md)：

- 中文检索依赖 SearXNG；不起则中文题检索拿不到结果。
- 一场标准档约 30 万 token；快速档补深度的总成本**不比**从零跑标准档低，价值是体验连续。
- 审批页暂无「驳回」，只能通过或放弃重来。
- 报告质量按「流程健康」验收（零错误、引用在范围内、审查真的跑了、无空小节），不以篇幅衡量；规划产出方差较大（大纲节数 6~8 波动）。
- 来源权威性不按域名一刀切；对「文库转存站数字无法溯源」做了线索级标记，名单是人工维护的，只有少数域名。
- 坏页过滤在离线回放上命中，真实运行里尚未观察到触发。
- 单进程、单 worker；SQLite；面向小范围使用，不是大规模多租户服务。
- 模型的瞬时失败（如 `write_section` 偶发空输出）没有统计学结论。

## 许可证

[MIT](LICENSE)
