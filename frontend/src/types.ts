/** 后端数据形状。字段全部来自真实运行（session s_557e7fdec792 的 SSE 流与表结构），
 *  不确定的地方标了 `?`，不要凭想象加字段。 */

export type View = 'idle' | 'running' | 'awaiting' | 'done' | 'error'

/** `/api/sessions/{sid}/trace` 的一轮。
 *
 *  字段**大部分可选**：一轮可能只有 retrieve 没有 assess（跑一半停了），
 *  反之亦然。缺就是缺，不要拿 0 顶替——「本轮新增 0 段」和「没有这个数」
 *  在图上是两件完全不同的事。 */
export interface TraceRound {
  round: number
  /** 检索命中数 */
  hits?: number
  /** 通过筛选、真正收进来的来源数 */
  accepted?: number
  new_sources?: number
  /** 累计抓取到的来源数 */
  fetched_total?: number
  /** 累计段落数 */
  segments_total?: number
  new_segments?: number
  round_gain?: number
  has_enough?: boolean
  gaps?: number
  gaps_by_urgency?: Record<string, number>
  /** 停止判据——只在真正触发停止的那一轮有值 */
  stop_would_be?: string
  stop_layer?: string
}

export interface SessionTrace {
  rounds: TraceRound[]
}

export interface SessionSummary {
  id: string
  title: string | null
  question: string
  status: string
  cost_tokens: number
  created_at: string
  archived_at: string | null
  /** 软删除：有值表示在回收站里（2026-10-12） */
  deleted_at?: string | null
  folder_id?: string | null
  /** 报告写完时生成的结构化摘要（阶段 3 才有值） */
  summary?: { summary: string; key_points?: string[]; gaps?: string[] } | null
}

export interface Folder {
  id: string | null          // null = 「未分类」
  name: string
  parent_id: string | null
  sort_order: number
  sessions: number
}

export interface SourceRow {
  id: string
  title: string | null
  domain: string | null
  url: string
  source_date: string | null
  source_nature: string | null
  char_count: number | null
}

/** SSE `progress` 事件里的 `event` 字段。
 *  实测见过 11 种 kind，各自定义要显示什么。 */
export interface ProgressEvent {
  kind: string
  [k: string]: unknown
}

/** 前端**存下来**的一个 progress 事件 = 内层 event + 产出它的图节点。
 *
 *  ⚠️ `node` 必须一起存。服务端发的是 `{kind:"progress", node, event}`
 *  （`server.py:832`），`node` 在**外层**——只把 `ev.event` 存进数组的话，
 *  环节归类就只能靠「渲染那一刻正在跑哪个节点」去猜：
 *  一进写作阶段，**全部历史步骤会塌进「写作」，「规划」整个消失**；
 *  一到 finalize 又全部弹回「规划」（v5 里写作占整轮 58%，全程可见）。
 *  端到端复现见 `DECISIONS.md`「环节归类」一节。 */
export interface ProgressEntry extends ProgressEvent {
  /** 产出这个事件的图节点：probe / outline / retrieve / extract / assess /
   *  apply_revision / write / finalize / summary。 */
  node: string
}

export type SseEvent =
  | { kind: 'connected'; session_id: string }
  | { kind: 'node'; node: string }
  | { kind: 'progress'; node: string; event: ProgressEvent }
  | { kind: 'thinking'; stage: string; delta: string; final?: boolean }
  /** 正文增量。与 thinking 分开：thinking 是「AI 在想什么」，
   *  draft 是「这节要交付的东西」——语义不同，混在一起前端就分不清。 */
  | { kind: 'draft'; stage: string; delta: string; final?: boolean }
  | { kind: 'done'; state: DoneState }
  | { kind: 'error'; error: string }

export interface DoneState {
  report?: string
  report_chars?: number
  round?: number | null
  url_count?: number | null
  cost_tokens?: number | null
}

export interface ApprovalSection {
  id: string
  title: string
  question?: string
  kind?: string
  修订说明?: string
}

export interface ApprovalPayload {
  session_id: string
  question: string
  time_sensitive?: boolean
  probe?: Record<string, unknown>
  signals?: Record<string, unknown>
  outline: ApprovalSection[]
  已修订?: boolean
  修订记录?: { section?: string; op?: string; reason?: string }[]
  框架可信度?: { 种类: string; 停止原因: string; 说明: string }
  当前材料?: { 来源数: number; 段落数: number; 已抽取段数?: number }
  queries?: Record<string, string[]>
}

export interface ApprovalResponse {
  waiting: boolean
  approval?: ApprovalPayload
  note?: string
}

export interface SourceMeta {
  id: string
  title: string | null
  domain: string | null
  url: string
  source_date: string | null
  source_nature: string | null
  /** 产品化阶段 5：用户把这个来源标记为无用了 */
  ignored: boolean
  ignore_reason: string
}

export interface SegmentRow {
  段号: string
  来源号: string
  全文: string
  概括: string
  token: number
  /** 来源元数据由后端拼好——`来源号` 是显示编号 S2，和 source.id 不是一回事 */
  来源?: SourceMeta
}

/** 一个引用段号的 hover 预览（`GET /api/sessions/{id}/seg_preview`）。
 *
 *  **故意不含全文**：143 段全文几十 KB，而 hover 只看得到前两三行。
 *  `已排除` 是刻意带上的——被排除的来源不会从报告里消失，
 *  气泡是用户第一次看见这个标记的机会（不 hover 就永远看不见）。 */
export interface SegPreview {
  标题: string
  概括: string
  /** 正文前 180 字，空白已折叠 */
  摘录: string
  来源号: string
  域名: string
  名称: string
  性质: string
  已排除: boolean
  token: number
}

export type SegPreviews = Record<string, SegPreview>

/** 报告里被引用、但已被标记排除的来源（`GET /api/sessions/{id}`）。
 *  报告写完之后才排除，报告不会被改写——靠这条提示兜底。 */
export interface IgnoredCitation {
  id: string
  domain: string | null
  title: string | null
  citations: number
  reason: string
}

export interface SessionDetail extends SessionSummary {
  report: string | null
  sources: SourceRow[]
  ignored_citations: IgnoredCitation[]
}

/** 来源库的一条（产品化阶段 4）。字段取自 `GET /api/sources` 的真实返回。 */
export interface SourceLibItem {
  id: string
  session_id: string
  session_title: string
  title: string | null
  url: string
  domain: string | null
  source_date: string | null
  fetched_at: string
  source_type: string
  source_nature: string | null
  /** 服务端把 NULL 与 unknown 归一成了同一个 key */
  nature: string
  nature_label: string
  nature_reason: string | null
  char_count: number | null
  http_status: number | null
  /** 相关性闸门判定。0 = 留库但不参与报告 */
  on_topic: number
  segments: number
  tokens: number
  citations: number
  /** 产品化阶段 5：被用户标记为无用 */
  ignored: boolean
  ignore_reason: string | null
  ignored_at: string | null
}

export interface SourceLibrary {
  sources: SourceLibItem[]
  total: number
  segments: number
  citations: number
  by_nature: Record<string, number>
  nature_order: string[]
  nature_labels: Record<string, string>
}

/** `GET /api/sources/{id}`。段号由后端 EvidenceTools 生成，与报告正文一致。 */
export interface SourceDetailRow {
  id: string
  session_id: string
  session_title: string
  title: string | null
  url: string
  domain: string | null
  source_date: string | null
  source_nature: string | null
  nature: string
  nature_label: string
  nature_reason: string | null
  char_count: number | null
  on_topic: number
  ignored: boolean
  ignore_reason: string | null
  ignored_at: string | null
  segments: {
    id: string
    ref: string
    heading_path: string | null
    summary: string | null
    content: string
    token_count: number | null
    on_topic: number
  }[]
}

