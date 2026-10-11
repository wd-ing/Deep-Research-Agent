# DESIGN_R9_FRONTEND_UI.md · 前端设计方案（二轮）

> 2026-10-11。**设计方案，尚未开工。** 一轮后端接口已实测通过（见
> `DESIGN_R8_FRONTEND.md` §3.6），本文只讲**前端长什么样、怎么组织、状态怎么管**。
>
> 所有字段形状都来自真实运行数据（session `s_557e7fdec792` 的 SSE 流 + 表结构），
> 不是想象的。

---

## 一、技术选型

| 项 | 选型 | 理由 |
|---|---|---|
| 构建 | Vite 7 | 快，配置少 |
| 框架 | React 19 + TypeScript | 界面本质是状态驱动；类型能挡住后端字段变动的错误 |
| 路由 | **不用 react-router** | 只有三个视图，用 `view` 状态切换即可。少一个依赖 |
| 状态 | **不用 Redux/Zustand** | 一个 `useSession()` hook + Context 足够 |
| 数据 | **不用 react-query** | SSE 是长连接、`fetch` 是一次性，混用反而绕。直接 `fetch` + hook |
| 样式 | **Tailwind + 少量 CSS 变量** | 排版为主，不搞设计系统 |
| Markdown | `react-markdown` + `remark-gfm` | 报告是标准 GFM |
| 依赖总数 | **≤ 6** | 个人工具，过度工程化是负资产 |

**服务端**：Vite dev server 代理 `/api` → `127.0.0.1:8000`，不用 CORS。

---

## 二、整体布局

**不做多页应用**。一个页面，三种模式，靠 `view` 切换：

```
┌─────────────────────────────────────────────────────────┐
│  顶栏：会话标题 / 状态徽章 / 成本 / 归档按钮              │
├──────────┬──────────────────────────────────────────────┤
│          │                                              │
│ 会话列表 │              主区域（三种之一）                │
│          │                                              │
│ (240px)  │   · 运行中 → 进度视图                         │
│          │   · 待审批 → 审批视图                         │
│          │   · 已完成 → 报告视图                         │
│          │                                              │
└──────────┴──────────────────────────────────────────────┘
```

**引用详情用右侧抽屉**（不是弹窗）——弹窗会挡住读者正在看的位置，
抽屉可以边看边对照。

---

## 三、三种视图

### 3.1 运行中（进度视图）

**这是前端最该做好的部分**——它取代的是「命令行转圈 25 分钟」。

数据来自两种 SSE 事件：

| 事件 | 用途 |
|---|---|
| `{"kind":"node","node":"probe"}` | 当前在哪个节点 → **阶段条** |
| `{"kind":"progress","node":…,"event":{…}}` | 结构化事件 → **时间线** |
| `{"kind":"thinking","stage":…,"delta":…}` | 思考增量 → **折叠面板**（二轮先不做，三轮做） |
| `{"kind":"done"｜"error"}` | 结束 |

**阶段条**（固定 7 段，来自 `runner.py` 的图结构）：

```
探测 ━━━ 框架 ━━━ 检索 ━━ 抽取 ━ 评估 ━ 写作 ━ 收尾
 ●      ○      ○      ○     ○     ○     ○
```
当前节点高亮。**注意真实轨迹会走两遍检索**（`assess → apply_revision → retrieve`），
所以阶段条不能是单向进度，要允许「已完成后再次进入」的高亮态。

**时间线**：每个 `progress` 事件渲染成一行。**不同 `event.kind` 渲染不同内容**，
实测的 11 种：

| `event.kind` | 显示什么 |
|---|---|
| `probe_light` / `probe_deep` | 检索词、命中数 |
| `probe_quality` / `probe_judged` | 质量判定 |
| `assess` | **轮次、本轮新增段数、缺口数与紧迫度分布、`stop_would_be`**（这行最有信息量） |
| `extract` | 来源数、段落数、概括成功数 |
| `write_plan_tasks` | 任务数 / 证据可用段数 |
| `write_dispatch` | 并发跑了几个任务、首个串行耗时 |
| `write_assemble` | **小节数、`headings_renumbered`、`lead_headings_dropped`**（今天刚加的字段） |
| `write` | 报告字数、返修轮数、`spec_uncertainty` |
| `write_audit_error` | **红条**——环节失败要显眼 |

**`assess` 的 `stop_would_be` 是最有价值的一行**：
「边际收益过低（本轮新增 0 段 = 0%，阈值 20%）」——这是系统为什么停下的理由，
真人看进度时最想知道的正是这个。

### 3.2 待审批（审批视图）

`GET /api/sessions/{sid}/approval` 的实测 payload：

```json
{"waiting": true, "approval": {
  "outline": [{"id":"sec2","title":"…","修订说明":"仅5段材料，最高吻合度2…需去掉数字要求"}],
  "框架可信度": {"种类":"satisfied","说明":"材料已经够了，每一节都有材料支撑"},
  "当前材料": {"来源数":29,"段落数":0,"过闸门":0},
  "已修订": true, "修订记录": [...]
}}
```

**关键设计决策：`框架可信度` 必须放在最显眼处。**

它是 `satisfied`（材料够了）还是撞了上限（只是尽力而为）——
**这两者的可信度完全不同**，用户有权在批准之前知道。
配色上：绿色（satisfied）/ 黄色（尽力而为）。

**`修订说明` 要能看出「改了什么」**。实测它不是中性描述而是明确指令：
「需去掉数字要求」「需收窄为可回答的突破方向」——
所以要**划红线对比**（原题面 vs 校准后），而不只是显示一句话。

⚠️ **实测发现并已修的数据问题**：`当前材料.段落数` / `过闸门` 一直显示 **0**。
根因不是「字段不存在」——是**读错了 state**：`segments` / `on_topic` 是
**`WriteState`** 的字段（`state.py:160-161`），主图 `ResearchState` 里根本没有，
所以 `state.get("segments")` 恒为 `None` → `len()` 恒为 0。

已修为读主图里正确的字段：来源数取 `len(sources)`、
段落数取 `len(all_evidence)`。真实值应是 **29 来源 / 63 段**（实测核对过）。

**教训**：显示一个错的 0 比不显示更糟。前端会照实渲染它，
所以这个 bug 只能从后端修。

### 3.3 已完成（报告视图）

`GET /api/sessions/{sid}` 取 `report` + `sources`。

- Markdown 渲染（GFM）
- **`[S1·5]` 变成可点击** → `GET .../seg/{ref}` → 右侧抽屉显示原文 + 概括 + 来源元数据
- 报告末尾的「存疑与未完成」一节**要视觉区分**（那是系统主动声明的弱点，
  和正文混在一起会让读者以为报告同样可靠）
- 来源清单：`source_nature` 有 `first_party` / `media` / `community` / `research`，
  **要标出来**——community 和 media 的可信度不同于 research

---

## 四、状态模型

```
type View = 'idle' | 'running' | 'awaiting' | 'done' | 'error'

SessionState = {
  sid, view, question, title, status, costTokens,
  progress: ProgressEvent[]     // SSE progress 累积
  currentNode: string | null
  approval: ApprovalPayload | null
  report: string | null
  sources: Source[]
  thinking: {stage, text}[]
}
```

**核心：一个 `useSession(sid)` hook 管全部状态。**

```
useSession
  ├─ fetch 一次性数据（session / approval / report）
  └─ EventSource 订阅 /api/stream/{sid}
       ├─ node     → setCurrentNode
       ├─ progress → append（去重：同 kind+round 只留一条最新的）
       ├─ thinking → 合并进缓冲（暂不显示，三轮用）
       └─ done/error → setView
```

**SSE 的两个坑**：
1. **`EventSource` 自带重连**，但断线期间的事件会丢。
   → 断线时切到轮询 `GET /sessions/{sid}` 直到恢复（后端有 checkpoint，重启不丢状态）
2. **审批是外部触发的**（用户点了批准，图才继续跑）。
   所以收到 `done` 之后还要轮询 `/approval`，因为**下一个 interrupt 可能马上来**

**断线重连的兜底**：`run` / `approve` 都可以带 `?streaming=false` 走同步路径。
前端不主动用它（会丢进度），但 server 出问题时能救。

---

## 五、组件划分

```
App
├─ Sidebar            会话列表 + 新建
├─ TopBar             标题 / 状态 / 成本 / 归档
└─ Main
   ├─ RunView         阶段条 + 时间线
   ├─ ApprovalView    可信度横幅 + 框架列表 + 通过/驳回
   ├─ ReportView      Markdown + 引用高亮
   └─ SegDrawer       引用原文抽屉
```

**刻意不做的组件**：
- 没有 `ErrorBoundary` 之外的通用组件库
- 没有 UI 组件库（MUI / AntD）——三块页面用不上
- 没有图表库（二轮的 `assess` 轮次数据用简单的数字/进度条就够，
  图表留到三轮）

---

## 六、二轮不做、三轮做

| 功能 | 轮次 | 状态 |
|---|---|---|
| 三块页面骨架 | 二轮 | ✅ `9813d33` |
| SSE 进度条 + 时间线 | 二轮 | ✅ `b55fa4f` |
| 审批界面 | 二轮 | ✅ `b55fa4f` |
| 引用点击抽屉 | 二轮 | ✅ `9813d33` |
| **思考过程实时展示** | 三轮 | ✅ `343207d` |
| 正文流式打字 | 三轮 | ⬜ 后端只推了 `reasoning`，`text` 要另外开 |
| 检索轮次图表 | 三轮 | ⬜ 二轮用数字就够了 |
| 引用 hover 预览 | 三轮 | ⬜ 锦上添花 |

### 思考面板的三个设计决定（2026-10-11）

**① 按 stage 分 tab，不合成一整屏。**
实测一轮的思考量：`probe_outline` 881 字 / `outline` 2,940 字 /
**`assess` 7,009 字**。而 assess 那 7,009 字里是**逐个小节的材料充足度判断**
（「sec2 沾边2段、最高吻合度1，属于 high，因为可能通过检索获得」）——
这是用户最该看到的决策依据，混在一整屏里会被前面的内容淹没。

**② 自动跟随滚动，但用户一上滑就停。**
正在读思考内容时被人一直拽到底部，是最烦人的一种自作主张。

**③ tab 名用中文，且要映射两套命名。**
⚠️ **图节点名和 LLM stage 名不是一套**（踩过）：
- node：`probe` / `outline` / `assess` / `write`
- stage（`config/models.yaml` 的 `stages`）：`probe_outline` / `outline` /
  `assess` / `write_plan` / `write_audit` / `write_section` / `write_revise`

直接拿 node 去 `thinking[node]` 查永远查不到——面板会一直显示
「这一阶段还没有思考内容」。所以有 `NODE_TO_STAGE_PREFIX` 映射表。

---

## 七、待确认的设计决策

1. **深色还是浅色？**（个人自用，我倾向跟随系统 + 一个切换）
2. **`当前材料` 的 0 要不要修后端？**（我倾向修，见 §3.2）
3. **驳回按钮：查清了，当前实现下没有实际用途**（见下节）。
4. **同时跑多个 session？** 后端支持（`_subs` 是按 sid 分的），
   但 UI 要不要做？个人自用我倾向先不做。

---

## 八、驳回路径：查清了（2026-10-11）

顺着 `nodes.py:328` / `route_after_revision` / `route_after_assess` 读下来：

```
approve(approved=false) → plan_approved=False
  → route_after_assess  → "revise"（重新走 apply_revision）
  → route_after_revision → _should_stop 为真 → "approve"（又回到审批点）
```

⇒ **驳回 = 原地再问一遍同一个框架**。没有任何「驳回后按用户意见改」的通路，
而 `apply_revision` 用的是 `assess` 刚生成的 `pending_revision`——
那还是**上一轮**的修订意见，重跑一遍结果相同。

**所以驳回按钮在当前实现下是空的**（理论上会无限循环）。

**二轮的处理（2026-10-11 用户确认）**：
- **只做「通过」**，不渲染「驳回」按钮
- 但**保留** `/approve` 接口的 `approved` 字段，前端传 `true`
- **后续再做**——「要不要真正支持驳回」是产品决策，不是前端该顺手做的

（如果要做「驳回 → 用户编辑框架 → 按新框架写」，那是一个真实的功能缺口：
需要在 `await_approval` 加「接收用户改后的大纲」的分支。**先不做**，等用户提。）

## 九、引用高亮的实现方式

报告里的 `[S1·5]` 在 Markdown 里是**纯文本**，要做成可点击：

- **倾向**：`react-markdown` 的 `components` 里拦截文本节点，正则替换成 `<a>`
  ——**不引入 remark 插件**，少一个依赖
- 备选：写个 remark 插件，更「正确」但多一个包

已决定：`components` 拦截。
