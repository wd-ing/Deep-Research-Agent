import type { ProgressEvent } from '../types'
import { STAGES } from '../types'

/** 阶段条。⚠️ 检索会走两遍（assess → apply_revision → retrieve），
 *  所以不能做成单调递增的进度条 —— 已完成的阶段允许再次高亮。 */
export function StageBar({ current }: { current: string | null }) {
  const activeIdx = STAGES.findIndex((s) =>
    (s.nodes as readonly string[]).includes(current ?? ''))
  return (
    <div className="flex items-center gap-1 border-b border-line bg-white px-6 py-3">
      {STAGES.map((s, i) => {
        const done = activeIdx >= 0 && i < activeIdx
        const active = i === activeIdx
        return (
          <div key={s.key} className="flex items-center gap-1">
            {i > 0 && <span className="h-px w-4 bg-line" />}
            <span
              className={[
                'rounded px-2 py-0.5 text-xs',
                active ? 'bg-accent font-medium text-white'
                  : done ? 'bg-gray-100 text-gray-600'
                  : 'text-gray-300',
              ].join(' ')}
            >
              {s.label}
            </span>
          </div>
        )
      })}
      {activeIdx < 0 && current && (
        <span className="ml-2 text-xs text-muted">当前：{current}</span>
      )}
    </div>
  )
}

/** 一条事件渲染成什么。
 *
 * ⚠️ 字段名全部照**真实 fixture**（`tests/fixtures/run_to_approval.json`）写，
 * 不是我猜的——第一版按 `hits` 写，结果 `probe_deep` 显示成「? 条命中」。
 * 读不到就整段不显示，不要用假数据补位。 */
function describe(e: ProgressEvent): { head: string; body?: string; tone?: 'warn' | 'error' } {
  const s = (k: string) => e[k] as string | undefined
  const n = (k: string) => e[k] as number | undefined
  switch (e.kind) {
    case 'probe_light':
      return { head: '轻扫检索', body: `${n('hits') ?? '?'} 条命中` }
    case 'probe_deep':
      return { head: '深读', body: `${n('read')} / ${n('tried')} 篇 · ${n('chars')} 字符` }
    case 'probe_judged':
      return { head: '探针判定',
        body: `${s('quality') ?? ''}${e.ambiguous ? ' · 有歧义' : ''}` }
    case 'probe_quality':
      return { head: '探针质量', body: `${s('quality') ?? ''} · 深读 ${n('deep_urls')} 篇` }
    case 'probe_outline':
      return { head: '归纳领域轮廓',
        body: `${n('subtopics')} 子话题 / ${n('entities')} 实体 / ${n('controversies')} 争议`
          + (e.degraded ? ' · 已降级' : '') }
    case 'assess': {
      const gaps = e.gaps_by_urgency as Record<string, number> | undefined
      const gapTxt = gaps
        ? Object.entries(gaps).map(([k, v]) => `${k} ${v}`).join(' / ')
        : n('gaps') ? `${n('gaps')} 个缺口` : ''
      const thin = e.thin_sections as string[] | undefined
      const parts = [
        `第 ${n('round')} 轮`,
        `本轮新增 ${n('new_segments')} 段`,
        gapTxt,
        thin?.length ? `偏薄 ${thin.join('、')}` : '',
      ].filter(Boolean).join(' · ')
      return { head: '评估', body: parts }
    }
    case 'extract':
      return { head: '抽取', body: [
        `${n('sources')} 来源`,
        `${n('segments')} 段`,
        `概括成功 ${n('summarized')}`,
        n('errors') ? `失败 ${n('errors')}` : '',
      ].filter(Boolean).join(' / ') }
    case 'write_plan_tasks':
      return { head: '分派任务', body: `${n('kept') ?? n('tasks')} 个任务 / 可用 ${n('segments_available')} 段` }
    case 'write_dispatch':
      return { head: '并行写作', body: `${n('ran')} 个任务 / 首个串行 ${n('first_serial_s')}s` }
    case 'write_assemble':
      return { head: '装配', body: [
        `${n('sections')} 节`,
        n('headings_renumbered') ? `重编号 ${n('headings_renumbered')}` : '',
        n('warnings') ? `自检告警 ${n('warnings')}` : '',
      ].filter(Boolean).join(' · ') }
    case 'write':
      return { head: '报告完成', body: [
        `${n('chars')} 字`,
        n('sections_written') ? `${n('sections_written')} 节` : '',
        n('rework_rounds') ? `返修 ${n('rework_rounds')} 轮` : '',
      ].filter(Boolean).join(' · ') }
    case 'write_audit_error':
      return { head: '审查失败', body: s('error')?.slice(0, 160), tone: 'error' }
    default:
      return { head: e.kind }
  }
}

/** 停止理由单独拎出来显示 —— 这是看进度时最想知道的一件事。 */
function StopReason({ e }: { e: ProgressEvent }) {
  if (e.kind !== 'assess') return null
  const why = e.stop_would_be as string | undefined
  const layer = e.stop_layer as string | undefined
  if (!why) return null
  return (
    <div className="mt-1 rounded bg-amber-50 px-2 py-1 text-xs text-amber-900">
      停止判据：{why}
      {layer ? <span className="ml-1 text-amber-700">（来自 {layer}）</span> : null}
    </div>
  )
}

export function Timeline({ progress }: { progress: ProgressEvent[] }) {
  if (!progress.length) {
    return <div className="px-6 py-10 text-sm text-muted">等待第一个事件…</div>
  }
  return (
    <div className="px-6 py-4">
      {progress.map((e, i) => {
        const d = describe(e)
        return (
          <div key={i} className="border-l-2 border-line py-2 pl-4">
            <div className="flex items-baseline gap-2">
              <span className="text-sm font-medium text-ink">{d.head}</span>
              {d.body && (
                <span className={`text-xs ${d.tone === 'error' ? 'text-red-600' : 'text-muted'}`}>
                  {d.body}
                </span>
              )}
            </div>
            <StopReason e={e} />
            {typeof e.spec_uncertainty === 'string' && e.spec_uncertainty && (
              <div className="mt-1 text-xs text-muted">
                规约存疑：{e.spec_uncertainty.slice(0, 180)}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
