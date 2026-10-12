import type { ProgressEvent } from '../types'

/** 一个大环节。 */
export interface Phase {
  key: string
  label: string
  /** 摘要行：大环节永不折叠，一眼看到整体进度 */
  summary: string
  steps: Step[]
}

/** 一个子环节。**同名的可以重复出现——检索/评估本来就是多轮循环。 */
export interface Step {
  /** 稳定的身份，往上翻/定位靠它 */
  id: string
  label: string
  /** 一行摘要，永不折叠 */
  summary: string
  /** 需要用户注意（材料不足 / 审查发现问题 / 环节失败） */
  warn?: boolean
  /** 状态：运行中 / 已完成 */
  state?: 'running' | 'done'
  /** 收起后才看的明细。**没有就不渲染这一块** */
  detail?: string[]
}

/**
 * 把 SSE 事件整理成「四个大环节 + 子环节」的工作流。
 *
 * ## 为什么只有四个大环节
 *
 * 用户要的是「知道在干活」，不是看内部有 11 个节点。
 * 11 个节点里只有四组是用户心智里的「阶段」，其余（探测、抽取、装配、
 * 收尾）都是某一步的细节，压成一行摘要就够了。
 *
 * ## 循环怎么表达
 *
 * **不单独设计「循环」这个东西**——检索/评估本来就是多轮的，
 * 让同名子环节重复出现就是最直白的表达：
 * 「检索（第 1 轮）… 检索（第 2 轮）… 评估（第 2 轮）」。
 * 硬造一个「循环 1/2/3」的层级反而更难读。
 *
 * ## 展示标准（用户定的）
 *
 * 1. 让用户知道**在干活**——每步都有摘要
 * 2. 让用户知道**怎么干的活**——检索词等明细可展开
 * 3. 让用户知道**核心信息**——⚠️ 只在需要判断时出现
 * 4. **不必要的信息一律不给**：没有明细就没有「收起后」，不塞占位符
 */

const PHASE_ORDER = ['规划', '审批', '写作', '收尾'] as const
type PhaseName = (typeof PHASE_ORDER)[number]

function n(e: ProgressEvent, k: string): number | undefined {
  return typeof e[k] === 'number' ? (e[k] as number) : undefined
}
function s(e: ProgressEvent, k: string): string | undefined {
  return typeof e[k] === 'string' ? (e[k] as string) : undefined
}

/** 当前节点属于哪个大环节。`assess`/`apply_revision` 都算规划。 */
function phaseOf(node: string | null, view: string): PhaseName {
  if (view === 'awaiting') return '审批'
  if (node === 'write' || node === 'finalize') return node === 'finalize' ? '收尾' : '写作'
  return '规划'
}

/** 单个 progress 事件 → 一个子环节。没有就返回 null（不留空条目）。 */
function toStep(e: ProgressEvent, seq: number): Step | null {
  const id = `${e.kind}-${seq}`
  switch (e.kind) {
    case 'probe_light':
      return { id, label: '轻扫检索', summary: `命中 ${n(e, 'hits') ?? '?'}` }
    case 'probe_deep':
      return { id, label: '深读', summary: `${n(e, 'read')}/${n(e, 'tried')} 篇 · ${n(e, 'chars')} 字` }
    case 'probe_outline':
      return {
        id, label: '归纳领域轮廓',
        summary: `${n(e, 'subtopics')} 个子话题 · ${n(e, 'entities')} 实体 · ${n(e, 'controversies')} 争议`,
      }
    case 'outline':
      return { id, label: '定框架', summary: `${n(e, 'sections') ?? '?'} 个小节` }
    case 'retrieve': {
      const rd = n(e, 'round') ?? 0
      const lines: string[] = []
      const q = e.queries as { zh?: string[]; en?: string[] } | undefined
      if (q?.zh?.length) lines.push(`中文：${q.zh.join(' / ')}`)
      if (q?.en?.length) lines.push(`英文：${q.en.join(' / ')}`)
      return {
        id, label: `检索（第 ${rd} 轮）`,
        summary: `搜 ${(q?.zh?.length ?? 0) + (q?.en?.length ?? 0)} 组词 · 命中 ${n(e, 'hits') ?? '?'} · 采用 ${n(e, 'accepted') ?? n(e, 'new_sources') ?? '?'}`,
        detail: lines,
      }
    }
    case 'extract':
      return {
        id, label: `抽取（第 ${n(e, 'round') ?? '?'} 轮）`,
        summary: `${n(e, 'sources')} 来源 → ${n(e, 'segments')} 段 · 概括成功 ${n(e, 'summarized')}`,
        warn: (n(e, 'summarize_failed_batches') ?? 0) > 0,
        detail: (n(e, 'summarize_failed_batches') ?? 0) > 0
          ? [`${n(e, 'summarize_failed_batches')} 批概括失败，这些段落的要点没进写作材料`]
          : undefined,
      }
    case 'assess': {
      const thin = (e.thin_sections as string[] | undefined) ?? []
      const gaps = e.gaps as number | undefined
      const urg = e.gaps_by_urgency as Record<string, number> | undefined
      const parts = [`累计 ${n(e, 'segments_total')} 段`, `本轮新增 ${n(e, 'new_segments')}`]
      if (gaps) parts.push(`缺口 ${gaps}${urg ? `（${Object.entries(urg).map(([k, v]) => k + ' ' + v).join(' ')}）` : ''}`)
      return {
        id, label: `评估（第 ${n(e, 'round')} 轮）`,
        summary: parts.join(' · '),
        warn: thin.length > 0,
        detail: [
          ...(thin.length ? [`材料不足：${thin.join('、')}`] : []),
          ...((e.gap_detail as string[] | undefined) ?? []),
          ...(s(e, 'stop_would_be') ? [`停止判据：${s(e, 'stop_would_be')}（来自 ${s(e, 'stop_layer')}）`] : []),
        ],
      }
    }
    case 'outline_revision': {
      const nar = e.narrowed as { id: string; to: string }[] | undefined
      const rem = e.removed as string[] | undefined
      const add = e.added as string[] | undefined
      const detail: string[] = []
      nar?.forEach((x) => detail.push(`${x.id} 收窄为「${x.to}」`))
      rem?.forEach((x) => detail.push(`删除 ${x}`))
      add?.forEach((x) => detail.push(`新增 ${x}`))
      if (e.reason) detail.push(`理由：${String(e.reason)}`)
      return {
        id, label: '校准框架',
        summary: `收窄 ${nar?.length ?? 0} 节 · 新增 ${add?.length ?? 0} · 删除 ${rem?.length ?? 0}`,
        detail,
      }
    }
    case 'write_plan_tasks':
      return { id, label: '制定写作规约', summary: `${n(e, 'kept') ?? n(e, 'tasks')} 个子 agent · ${n(e, 'segments_available')} 段可用` }
    case 'write_dispatch':
      return { id, label: '并行写作', summary: `${n(e, 'ran')} 个子 agent · 首个 ${n(e, 'first_serial_s')}s`, state: 'done' }
    case 'write_assemble':
      return {
        id, label: '装配成文',
        summary: `${n(e, 'sections')} 节${(n(e, 'headings_renumbered') ?? 0) > 0 ? ` · 重编号 ${n(e, 'headings_renumbered')} 处` : ''}`,
        warn: (n(e, 'warnings') ?? 0) > 0,
      }
    case 'write_audit_error':
      return { id, label: '审查', summary: '环节失败，未拿到审查结论', warn: true, detail: [s(e, 'error') ?? ''] }
    case 'write':
      return {
        id, label: labelOfWrite(e),
        summary: `${n(e, 'chars') ?? '?'} 字 · ${n(e, 'sections_written') ?? n(e, 'sources') ?? '?'} 节`
          + ((n(e, 'rework_rounds') ?? 0) > 0 ? ` · 返修 ${n(e, 'rework_rounds')} 轮` : ''),
        warn: (n(e, 'failed') ?? 0) > 0 || (n(e, 'violations') ?? 0) > 0,
        detail: [
          ...(s(e, 'spec_uncertainty') ? [`规约存疑：${s(e, 'spec_uncertainty')}`] : []),
        ],
      }
    default:
      return null
  }
}

function labelOfWrite(e: ProgressEvent): string {
  return n(e, 'rework_rounds') ? '返修' : '报告完成'
}

/** 把事件流整理成四个大环节。 */
export function buildPhases(
  progress: ProgressEvent[],
  currentNode: string | null,
  view: string,
): Phase[] {
  const phases: Record<string, Phase> = {}
  for (const p of PHASE_ORDER) phases[p] = { key: p, label: p, summary: '', steps: [] }

  progress.forEach((e, i) => {
    const st = toStep(e, i)
    if (!st) return
    // 装配/审查/写作属于写作环节；其余归规划
    const phase = phaseOf(
      /write|finalize/.test(String((e as { node?: string }).node ?? '')) || currentNode === 'write'
        ? (currentNode ?? '')
        : '',
      view,
    )
    phases[phase].steps.push(st)
  })


  // 当前正在跑的大环节：**只在确实有后续事件时才加**。
  // 审批时不要塞「审批环节 / 进行中」——那时规划已经跑完，
  // 审批页本身就是结论，再叠一层反而让人以为还有东西在跑。
  if (view === 'running' && currentNode) {
    const cur = phaseOf(currentNode, view)
    const last = phases[cur].steps[phases[cur].steps.length - 1]
    if (!last || last.state === 'running') {
      phases[cur].steps.push({
        id: `run-${cur}`, label: phaseRunningLabel(cur), summary: '进行中…',
        state: 'running',
      })
    }
  }

  for (const p of PHASE_ORDER) phases[p].summary = summarize(p, phases[p].steps)
  return PHASE_ORDER.map((p) => phases[p]).filter((p) => p.steps.length > 0)
}

function phaseRunningLabel(p: PhaseName): string {
  return { 规划: '规划中', 审批: '待你批准', 写作: '写作中', 收尾: '收尾中' }[p]
}

/** 大环节摘要：一句话说清这组干了什么。 */
function summarize(name: string, steps: Step[]): string {
  const done = steps.filter((x) => x.state !== 'running')
  const warn = done.filter((x) => x.warn).length
  if (!done.length) return '进行中…'
  let base = `${done.length} 步`
  if (name === '规划') {
    const rd = done.filter((x) => x.label.startsWith('检索')).length
    const ex = done.filter((x) => x.label.startsWith('抽取')).length
    const as = done.filter((x) => x.label.startsWith('评估')).length
    const bits: string[] = []
    if (rd) bits.push(`检索 ${rd} 轮`)
    if (ex) bits.push(`抽取 ${ex} 轮`)
    if (as) bits.push(`评估 ${as} 轮`)
    if (bits.length) base += ' · ' + bits.join(' · ')
  }
  if (warn) base += ` · ${warn} 处需注意`
  return base
}
