/** 后端数据形状。字段全部来自真实运行（session s_557e7fdec792 的 SSE 流与表结构），
 *  不确定的地方标了 `?`，不要凭想象加字段。 */

export type View = 'idle' | 'running' | 'awaiting' | 'done' | 'error'

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
  recalled?: unknown[]
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

export interface SessionDetail extends SessionSummary {
  report: string | null
  sources: SourceRow[]
  topics: string[]
  open_contradictions: unknown[]
}

/** 主图的 7 个节点，顺序取自 runner.py。
 *  ⚠️ 真实轨迹会走两遍检索（assess → apply_revision → retrieve），
 *  所以阶段条不能是单向进度。 */
export const STAGES = [
  { key: 'probe', label: '探测', nodes: ['probe'] },
  { key: 'outline', label: '框架', nodes: ['outline'] },
  { key: 'retrieve', label: '检索', nodes: ['retrieve'] },
  { key: 'extract', label: '抽取', nodes: ['extract'] },
  { key: 'assess', label: '评估', nodes: ['assess', 'apply_revision'] },
  { key: 'write', label: '写作', nodes: ['write'] },
  { key: 'finalize', label: '收尾', nodes: ['finalize'] },
] as const
