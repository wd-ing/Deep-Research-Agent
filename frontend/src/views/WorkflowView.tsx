import { useEffect, useMemo, useRef, useState } from 'react'
import type { Phase, Step } from './workflow'
import type { ProgressEntry } from '../types'
import { Icon } from '../components/Icon'
import { useCountUp } from '../lib/motion'

/** 状态点的形状：完成=实心圆，进行中=呼吸环，需注意=菱形。
 *  **形状**比颜色更能区分——色弱用户也能读出来。
 *
 *  ⚠️ 告警色从琥珀改成朱红（seal，2026-10-21 第 3 轮视觉稿）：
 *  「需要人看」在稿子里是朱批章的语义，不是装饰色。 */
function Dot({ state, warn }: { state?: Step['state']; warn?: boolean }) {
  if (warn) {
    return (
      <span
        className="mt-1.5 h-2.5 w-2.5 shrink-0 rotate-45 rounded-[2px] bg-seal/70"
        title="需要注意"
      />
    )
  }
  if (state === 'running') {
    return (
      <span className="mt-1 flex h-3 w-3 shrink-0 items-center justify-center">
        <span className="wf-live-dot" />
      </span>
    )
  }
  return <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-gray-400" />
}

/** 粗略字宽：CJK/全角记 1em，其余 0.55em。差一点也不影响，只是列宽略有出入。 */
function labelEm(t: string): number {
  let w = 0
  for (const ch of t) w += /[⺀-﫿]/.test(ch) ? 1 : 0.55
  return w
}

/** 标签列宽：按**本环节**最长的标签算，让这个环节里所有摘要的左边缘对齐成列。
 *  不用全局固定宽度：规划环节有 7 字标签，写作环节最长只有 4 字，
 *  写死会让写作那边白空一大块。 */
function labelColWidth(steps: Step[]): string {
  const w = Math.max(0, ...steps.map((s) => labelEm(s.label)))
  return `${(w + 0.5).toFixed(2)}em`
}

/** 环节秒表（B2 活性信号）：当前环节跑了几秒，一秒一跳。
 *  长调用（audit 单次 ~77s）中间没有任何事件，这个跳动是
 *  「没卡死」最直接的证据。 */
function Elapsed({ since }: { since: number }) {
  const [, tick] = useState(0)
  useEffect(() => {
    const t = window.setInterval(() => tick((x) => x + 1), 1000)
    return () => window.clearInterval(t)
  }, [])
  const s = Math.max(0, Math.floor((Date.now() - since) / 1000))
  return <span className="tabular-nums">{fmtDur(s)}</span>
}

/** 分:秒——小时数超了也照样拼（一场研究 20+ 分钟，h:mm 会出现）。 */
function fmtDur(total: number): string {
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  return h > 0
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`
}

/** 流式文字：行**首次挂载**且环节在跑时把摘要逐段打出（~30ms 一跳），
 *  之后保持全文。只在活跃环节启用——回看/已完成会话不该整屏重打一遍。
 *  `prefers-reduced-motion` 下直接全显。 */
function useTypewriter(text: string, enabled: boolean): string {
  const reduced =
    typeof window !== 'undefined'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  const [n, setN] = useState(() => (enabled && !reduced ? 0 : text.length))
  useEffect(() => {
    if (!enabled || reduced) { setN(text.length); return }
    if (n >= text.length) return
    const step = Math.max(2, Math.ceil(text.length / 22))
    const t = window.setTimeout(
      () => setN((v) => Math.min(text.length, v + step)), 28)
    return () => window.clearTimeout(t)
  }, [n, text, enabled, reduced])
  return enabled && !reduced ? text.slice(0, n) : text
}

/** 环节组里的一条明细（三级）：比父行轻一档，告警用朱红透出来。 */
function ChildRow({ c, animate }: { c: Step; animate?: boolean }) {
  const shown = useTypewriter(c.summary, !!animate)
  return (
    <li className="text-2xs leading-5">
      <div className="flex items-baseline gap-2">
        <span className={[
          'shrink-0', c.warn ? 'font-medium text-seal' : 'text-muted/80',
        ].join(' ')}>{c.label}</span>
        <span className={[
          'min-w-0 flex-1 truncate',
          c.warn ? 'text-seal' : 'text-muted',
        ].join(' ')}>{shown}</span>
      </div>
      {c.detail?.map((d, i) => (
        <div key={i} className="pl-4 leading-5 text-faint">· {d}</div>
      ))}
    </li>
  )
}

/** 一个子环节。**运行中不折叠**（正在干，不能藏）；完成后明细可收。
 *  `live` = 本环节组的孩子还在往里流——此时保持展开看流；
 *  不再是最后一个时自动收拢。 */
function StepRow({
  step, labelW, runningSince, live, animate,
}: {
  step: Step; labelW: string; runningSince: number | null; live: boolean
  /** 所属大环节在跑 → 本行首次挂载时摘要逐字打出 */
  animate?: boolean
}) {
  const shownSummary = useTypewriter(step.summary, !!animate)
  const kids = step.children ?? []
  const hasKids = kids.length > 0
  const running = step.state === 'running'
  const hasDetail = !!step.detail?.length
  const auto = running || (live && hasKids)
  const [open, setOpen] = useState(auto)
  useEffect(() => { setOpen(auto) }, [auto])
  // ⚠️ 的明细默认展开——那是给用户判断的，不该藏
  const show = hasDetail && (open || step.warn)
  const kidWarns = kids.filter((k) => k.warn).length
  const collapsible = hasKids || hasDetail

  return (
    <li className="wf-step relative flex gap-3 py-[5px] pl-1">
      {/* live = 本环节最新产出行：流式期间它就是「当前在跑的点」 */}
      <Dot state={running || live ? 'running' : undefined} warn={step.warn || kidWarns > 0} />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          {collapsible && !running ? (
            <button
              onClick={() => setOpen((o) => !o)}
              aria-label={open ? '收起明细' : '展开明细'}
              className={[
                '-ml-1 -mr-1 shrink-0 rounded text-muted/70 transition-transform duration-150 hover:text-ink',
                open ? 'rotate-90' : '',
              ].join(' ')}
            >
              <Icon name="chevron-right" size={10} />
            </button>
          ) : (
            <span className="-ml-1 -mr-1 inline-block w-[10px] shrink-0" aria-hidden />
          )}
          <span
            className={[
              'shrink-0 truncate text-xs',
              step.warn ? 'font-medium text-seal' : 'text-ink',
              running || live ? 'text-live' : '',
            ].join(' ')}
            style={{ width: labelW }}
            title={step.label}
          >
            {step.label}
          </span>
          <span
            className={[
              'min-w-0 flex-1 truncate text-2xs leading-5',
              step.warn ? 'text-seal' : 'text-muted/90',
            ].join(' ')}
            title={step.summary}
          >
            {shownSummary}
          </span>
          {running && (
            <span className="shrink-0 text-2xs text-live">
              进行中{runningSince ? <Elapsed since={runningSince} /> : null}
            </span>
          )}
          {collapsible && !running && (
            <button
              onClick={() => setOpen((o) => !o)}
              className={[
                'shrink-0 rounded-full px-1.5 py-px text-2xs transition-colors',
                open ? 'text-muted hover:bg-black/[.04] hover:text-ink'
                : step.warn || kidWarns > 0
                  ? 'bg-seal/10 text-seal hover:bg-seal/15'
                  : 'bg-black/[.04] text-muted hover:bg-black/[.07] hover:text-ink',
              ].join(' ')}
            >
              {open ? '收起' : hasKids ? `${kids.length} 条` : '展开'}
              {!open && kidWarns > 0 ? ` · ⚠${kidWarns}` : ''}
            </button>
          )}
        </div>
        {hasKids && open && (
          /* L3 明细：缩进 + 左侧引导竖线 */
          <ul className="wf-detail ml-1 mt-1 space-y-1 rounded-r-md border-l-2 border-line bg-black/[.02] py-1.5 pl-3 pr-2.5">
            {kids.map((c) => <ChildRow key={c.id} c={c} animate={animate} />)}
          </ul>
        )}
        {show && (
          <ul className="wf-detail ml-1 mt-1 space-y-1 rounded-r-md border-l-2 border-line bg-black/[.02] py-1.5 pl-3 pr-2.5">
            {step.detail!.map((d, i) => (
              <li key={i} className={[
                'text-2xs leading-5',
                step.warn ? 'text-seal/90' : 'text-muted',
              ].join(' ')}>· {d}</li>
            ))}
          </ul>
        )}
      </div>
    </li>
  )
}

/* ------------------------------------------------------------------ */
/*  L0 刻度轨 + 指标条 + 小节卡片的数据推导                              */
/* ------------------------------------------------------------------ */

const ORDER = ['规划', '审批', '写作', '收尾'] as const
// 快速档（C2）没有审批环节：审批点只在标准档的 apply_revision 路由里，
// 快速档的事件流永远走不到它——阶段条给它留一格等于撒谎。
const FAST_ORDER = ['规划', '写作', '收尾'] as const

/** 显示名≠phase key：快速档的「写作」对外叫「整理作答」，
 *  锚点/#wfphase-写作/jumpToPhase 仍用 key——显示名只给界面。 */
const phaseLabel = (key: string, mode?: string) =>
  key === '写作' && mode === 'fast' ? '整理作答' : key

function num(e: ProgressEntry, k: string): number | undefined {
  return typeof e[k] === 'number' ? (e[k] as number) : undefined
}

/** 从事件流里挖流程页要的派生数据。**全部来自已落库的真实事件**——
 *  拿不到就不渲染对应 UI，不编。
 *  `writingNow` = 此刻确实在写作环节（由外部 view/node 判定），
 *  只用来区分「待写」和「写作中」。 */
function deriveFlow(progress: ProgressEntry[], writingNow: boolean) {
  // 最后一次的才有意义：多轮 assess/plan 里只有最新一版在描述现状
  const last = (kind: string) =>
    [...progress].reverse().find((e) => e.kind === kind)
  const plan = last('write_plan_tasks')
  const assess = last('assess')

  // 已读来源：retrieve 每轮的 accepted=本轮新采用来源数，逐轮累加=总采用。
  // 没有 retrieve 事件 → null（不显示，不猜）。
  const retrieves = progress.filter((e) => e.kind === 'retrieve')
  const sources = retrieves.length
    ? retrieves.reduce((a, e) => a + (num(e, 'accepted') ?? num(e, 'new_sources') ?? 0), 0)
    : null

  // 可用段落：最近一次评估的累计段数
  const segments = assess ? num(assess, 'segments_total') ?? null : null

  // 已写入字数：按 task id 取最后一次 write_task 的 chars 再求和——
  // 返修（revise:true）是同任务的覆盖写，**不能累加**否则双倍计。
  const taskChars = new Map<string, number>()
  for (const e of progress) {
    if (e.kind !== 'write_task') continue
    const c = num(e, 'chars')
    const t = typeof e.task === 'string' ? e.task : ''
    if (t && c !== undefined) taskChars.set(t, c)
  }
  const written = taskChars.size
    ? [...taskChars.values()].reduce((a, b) => a + b, 0) : null

  // 写作进度：已完成的非返修任务数 / 计划任务数（刻度轨与小节卡共用）
  const doneTasks = new Set(
    progress.filter((e) => e.kind === 'write_task' && e.ok === true && e.revise !== true)
      .map((e) => String(e.task)))
  const planTasks = plan ? num(plan, 'tasks') ?? null : null

  // 小节卡片：只有 plan 事件带齐两份新字段才画——老会话没有就整块不出现
  const segs = plan?.section_segments as Record<string, number> | undefined
  const titles = plan?.outline_titles as Record<string, string> | undefined
  const thinSet = new Set(
    (assess?.thin_sections as string[] | undefined) ?? [])
  let sections: {
    id: string; title: string; n: number; thin: boolean
    status: '已写' | '写作中' | '失败' | '待写'; revised: boolean
  }[] = []
  if (segs && titles) {
    // 顺序跟 outline_titles 的键序（= 大纲顺序），segs 里多出来的键收尾兜底
    const ids = [...Object.keys(titles),
      ...Object.keys(segs).filter((k) => !(k in titles))]
    const covering = new Map<string, ProgressEntry[]>()
    for (const e of progress) {
      if (e.kind !== 'write_task') continue
      for (const c of (e.covers as string[] | undefined) ?? []) {
        const arr = covering.get(c) ?? []
        arr.push(e)
        covering.set(c, arr)
      }
    }
    sections = ids.map((id) => {
      const n = segs[id] ?? 0
      const evs = covering.get(id) ?? []
      let status: '已写' | '写作中' | '失败' | '待写'
      if (evs.some((e) => e.ok === false)) status = '失败'
      else if (evs.length && evs.every((e) => e.ok === true)) status = '已写'
      // 尚无覆盖它的任务结果：写作阶段在跑算「写作中」，否则「待写」。
      // 注意这是近似——没有「任务开始」事件，在飞的和排队的分不开。
      else status = writingNow ? '写作中' : '待写'
      return {
        id, title: titles[id] ?? id, n,
        thin: thinSet.has(id) || n === 0,
        status,
        revised: evs.some((e) => e.revise === true && e.ok === true),
      }
    })
  }

  return {
    plan, assess, sources, segments, written,
    doneTasks: doneTasks.size, planTasks, sections,
  }
}

/** L0 刻度进度轨（替换旧分段轨）：四个大环节文字在上，
 *  下面是一条刻度轨，走完的段填绿刻度。填充比例必须有依据：
 *  - 已完成的环节整段填满；flow/回看态全部填满；
 *  - 当前环节只有写作能算比例（完成任务/计划任务），
 *    其它环节没有可量化依据就空着、靠绿点说「在跑」。 */
function TrackRail({
  phases, activeIdx, doneAll, writeFrac, writeDone, writeTotal, onJump, order, mode,
}: {
  phases: Phase[]
  activeIdx: number
  /** flow/done：全填满 */
  doneAll: boolean
  /** 写作环节内的比例（完成 write_task / plan.tasks），无依据给 0 */
  writeFrac: number
  /** 写作进度分子/分母：给环节名后面「写作 · 4/8」用 */
  writeDone: number
  writeTotal: number | null
  /** 点环节名 = 展开并滚过去（沿用旧轨的交互） */
  onJump: (key: string) => void
  /** 环节序列（C2）：标准档 4 段、快速档 3 段——列数和占比都跟它走 */
  order: readonly string[]
  mode?: string
}) {
  const segPct = 100 / order.length
  const fill = doneAll
    ? 100
    : Math.min(100, Math.max(0,
        activeIdx < 0 ? 0
        : activeIdx * segPct + (order[activeIdx] === '写作' ? writeFrac * segPct : 0)))
  return (
    <div className="mt-6">
      <div className="grid pb-2 text-[12.5px]"
           style={{ gridTemplateColumns: `repeat(${order.length}, minmax(0, 1fr))` }}>
        {order.map((k, i) => {
          const p = phases.find((x) => x.key === k)
          const done = doneAll || i < activeIdx
          const cur = !doneAll && i === activeIdx
          return (
            <button
              key={k}
              onClick={() => p && onJump(k)}
              disabled={!p}
              className={[
                'flex items-center gap-1.5 pb-2 text-left',
                p ? '' : 'cursor-default',
                done ? 'text-muted' : cur ? 'font-medium text-ink' : 'text-faint',
              ].join(' ')}
            >
              {cur && <span className="dot live" />}
              {phaseLabel(k, mode)}
              {cur && k === '写作' && writeTotal != null && writeTotal > 0 && (
                <span className="font-normal text-faint">
                  · {writeDone} / {writeTotal} 节
                </span>
              )}
            </button>
          )
        })}
      </div>
      <div className="wf-track relative">
        <div className="wf-track-fill" style={{ width: `${fill}%` }} />
        {/* 轨上节点：每环节一个圆点，位置与该段起点对齐——
            填充走到「下一个节点」才算本环节完成。审批节点用 ink。 */}
        {order.map((k, i) => {
          const p = phases.find((x) => x.key === k)
          const done = doneAll || i < activeIdx
          const cur = !doneAll && i === activeIdx
          const isApproval = k === '审批'
          const cls = cur ? 'wf-node cur'
            : done ? (isApproval ? 'wf-node done-ink' : 'wf-node done')
            : isApproval ? 'wf-node future-ink' : 'wf-node'
          return (
            <button
              key={k}
              onClick={() => p && onJump(k)}
              disabled={!p}
              aria-current={cur ? 'step' : undefined}
              aria-label={phaseLabel(k, mode)}
              className={cls}
              style={{ left: `${(i / order.length) * 100}%` }}
            >
              {done && <Icon name="check" size={9} strokeWidth={3} />}
            </button>
          )
        })}
      </div>
    </div>
  )
}

/** 小节卡片行（稿子 .sc）：序号 + 标题/副标题 + 厚度刻度 + 状态。 */
function SectionRow({
  i, sec, maxN,
}: {
  i: number
  sec: ReturnType<typeof deriveFlow>['sections'][number]
  maxN: number
}) {
  // 10 格相对厚度：n>0 至少 1 格（有材料就看得见），maxN 顶满
  const filled = sec.n > 0 && maxN > 0 ? Math.max(1, Math.round(10 * sec.n / maxN)) : 0
  const sub = sec.n === 0
    ? '无材料'
    : `${sec.n} 段证据 · ${sec.status === '已写' ? '已完成'
      : sec.status === '写作中' ? '正在写作'
      : sec.status === '失败' ? '写作失败' : '等待写作'}`
  return (
    <div className={[
      'grid grid-cols-[26px_minmax(0,1fr)_auto_64px] items-center gap-3 px-3.5 py-3',
      sec.status === '写作中' ? 'rounded-[11px] bg-live/[.06]' : '',
      i > 0 ? 'border-t border-line2' : '',
    ].join(' ')}>
      <span className="font-mono text-[11.5px] text-faint">
        {String(i + 1).padStart(2, '0')}
      </span>
      <div className="min-w-0">
        <div className="truncate text-[13.5px] leading-snug">{sec.title}</div>
        <div className="mt-0.5 flex items-center gap-1.5 text-[11.5px] text-faint">
          {sub}
          {sec.revised && (
            <span className="rounded border border-seal/45 px-1 text-[10px] leading-4 text-seal">已返修</span>
          )}
        </div>
      </div>
      <div
        className="sc-grow flex items-end gap-[2px]"
        title={`${sec.n} 段证据 · 相对其它小节；数值为任务级分配的近似`}
      >
        {Array.from({ length: 10 }, (_, k) => (
          <i key={k} className={[
            'block h-3.5 w-1 rounded-[1px]',
            k < filled ? (sec.thin ? 'bg-seal' : 'bg-ink') : 'bg-black/10',
          ].join(' ')} />
        ))}
      </div>
      <div className="flex items-center justify-end gap-1.5 text-[11.5px] text-faint">
        {sec.thin && sec.status !== '已写' && (
          <span className="seal">
            {sec.n === 0 ? '无材料' : '偏薄'}
          </span>
        )}
        {sec.status === '已写' && (
          <span className="flex items-center gap-1 text-muted">
            <Icon name="check" size={13} strokeWidth={2.5} />已写
          </span>
        )}
        {sec.status === '写作中' && (
          <span className="flex items-center gap-1.5 text-live">
            <span className="dot live" />写作中
          </span>
        )}
        {sec.status === '失败' && <span className="text-seal">失败</span>}
        {sec.status === '待写' && !sec.thin && '待写'}
      </div>
      {sec.status === '写作中' && <div className="run-scan col-span-full mt-1" />}
    </div>
  )
}

/** 一个大环节。**标题吸顶**——滚过长规划时仍知道自己在哪一段。 */
function PhaseBlock({
  phase, defaultOpen, isLast, runningSince, openSignal, active, num, mode,
}: {
  phase: Phase
  num: number
  mode?: string
  defaultOpen: boolean
  isLast: boolean
  runningSince: number | null
  openSignal: number
  active: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  const running = active
  const warns = phase.steps.reduce(
    (a, s) => a + (s.warn && s.state !== 'running' ? 1 : 0)
      + (s.children?.filter((c) => c.warn).length ?? 0),
    0,
  )
  const labelW = labelColWidth(phase.steps)
  const lastReal = [...phase.steps].reverse().find((s) => s.state !== 'running')

  useEffect(() => {
    if (openSignal > 0) setOpen(true)
  }, [openSignal])

  useEffect(() => {
    if (!defaultOpen) setOpen(false)
  }, [defaultOpen])

  return (
    <section id={`wfphase-${phase.key}`}
      className="border-t border-line2 first:border-t-0">
      <button
        onClick={() => setOpen((o) => !o)}
        className="wf-phase-head flex w-full items-center gap-2.5 px-4 py-2.5 text-left transition-colors hover:bg-black/[.02]"
      >
        <span
          className={[
            'shrink-0 text-muted transition-transform duration-200',
            open ? 'rotate-90' : '',
          ].join(' ')}
        >
          <Icon name="chevron-right" size={11} />
        </span>
        <span className="shrink-0 font-mono text-[11px] text-faint">
          {String(num).padStart(2, '0')}
        </span>
        <span
          className={[
            'shrink-0 text-[13px] font-medium transition-colors',
            running ? 'text-live' : 'text-ink',
          ].join(' ')}
        >
          {phaseLabel(phase.key, mode)}环节
        </span>
        {running && <span className="wf-live-dot" />}
        <span className="min-w-0 flex-1 truncate text-2xs leading-5 text-muted/90">
          {phase.summary}
        </span>
        {warns > 0 && (
          <span className="seal shrink-0">
            {warns} 处需注意
          </span>
        )}
      </button>

      {open && (
        <div className="relative px-4 pb-3.5 pt-1">
          {/* 时间轴竖线 */}
          {!isLast && (
            <span
              className="pointer-events-none absolute bottom-0 left-[13.5px] top-2 w-px bg-line"
              aria-hidden
            />
          )}
          <ul className="space-y-0.5">
            {phase.steps.map((s) => (
              <StepRow key={s.id} step={s} labelW={labelW}
                runningSince={runningSince}
                live={running && s.id === lastReal?.id}
                animate={running} />
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

/**
 * 工作流视图（第 3 轮视觉稿 live.html）：
 * 页头 → L0 刻度轨 → 指标条 → [小节卡片 | 执行记录] 两列。
 *
 * 数据全部来自已落库的事件流（/events 历史 + SSE），
 * 没有就不渲染对应块——**不编造**。
 */
export function WorkflowView({
  phases, progress, running, runningSince, cost, flow,
  histLoaded, truncated, question, startedAt, onOpenReport, mode,
}: {
  phases: Phase[]
  progress: ProgressEntry[]
  running: boolean
  runningSince: number | null
  cost: number | null
  /** 档位（C2）：'fast' 时阶段条用 FAST_ORDER（无审批）；
   *  缺省=standard 四段。 */
  mode?: string
  /** flow = 回看完已成的流程 */
  flow: boolean
  /** /events 历史已拉取完成（区分「还在拉」和「真的没有历史」） */
  histLoaded: boolean
  /** 历史被截断：顶部提示「较早的事件已省略」 */
  truncated: boolean
  question?: string
  /** 会话创建时刻（ISO）——running 时用「现在-创建」算用时 */
  startedAt?: string | null
  /** flow 态的回报告入口 */
  onOpenReport?: () => void
}) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const railRef = useRef<HTMLDivElement>(null)
  const started = phases.some((p) => p.steps.some((s) => s.state !== 'running'))
    || progress.length > 0

  /* sticky 轨真实高度（含窄屏换行）写进 --wf-rail-h，
     .wf-phase-head 的 top 用它，不写死。 */
  useEffect(() => {
    const rail = railRef.current, wrap = wrapRef.current
    if (!rail || !wrap || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => {
      wrap.style.setProperty('--wf-rail-h', `${rail.offsetHeight}px`)
    })
    ro.observe(rail)
    return () => ro.disconnect()
  }, [started])
  const endRef = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const [wide, setWide] = useState(false)
  const [openSig, setOpenSig] = useState<Record<string, number>>({})

  // 当前大环节：有 running 步骤的那个；没有就落到最后一个非空环节
  const activeKey =
    phases.find((p) => p.steps.some((s) => s.state === 'running'))?.key
    ?? phases[phases.length - 1]?.key
  const order: readonly string[] = mode === 'fast' ? FAST_ORDER : ORDER
  const activeIdx = order.indexOf(activeKey ?? '')
  // 「写作中」状态只该出现在真的写作环节——done/flow 里不能拿
  // 历史最后一个事件的 node 去猜（收尾前最后一条往往也是 write）
  const writingNow = running && activeKey === '写作'
  const fdata = useMemo(
    () => deriveFlow(progress, writingNow), [progress, writingNow])

  // 两列布局按**容器实测宽度**分（主区侧栏展开/折叠会挤压可用宽，
  // 视口媒体查询量不到这件事）。稿子断点 ~1000px。
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const ro = new ResizeObserver(() => setWide(el.clientWidth >= 1000))
    ro.observe(el)
    setWide(el.clientWidth >= 1000)
    return () => ro.disconnect()
  }, [])

  const jumpToPhase = (key: string) => {
    setOpenSig((o) => ({ ...o, [key]: Date.now() }))
    requestAnimationFrame(() => {
      const el = document.getElementById(`wfphase-${key}`)
      const smooth = !window.matchMedia('(prefers-reduced-motion: reduce)').matches
      el?.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: 'start' })
    })
  }

  useEffect(() => {
    const el = endRef.current?.parentElement
    if (!el) return
    const onScroll = () => {
      stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [])

  useEffect(() => {
    if (!stick.current) return
    endRef.current?.scrollIntoView({ block: 'end', behavior: 'smooth' })
  }, [phases])

  const writeFrac = fdata.planTasks && fdata.planTasks > 0
    ? Math.min(1, fdata.doneTasks / fdata.planTasks) : 0

  /* ---- 空态（started 提前声明，RO 依赖它） ---- */
  if (!started) {
    if (histLoaded && !running) {
      // 老会话（事件历史不存在）：明示空态，不能冒充「正在启动」
      return (
        <div ref={wrapRef} className="view-in mx-auto w-full max-w-[1080px] px-6 py-7 md:px-10">
          <PageHead running={running} flow={flow} question={question} onOpenReport={onOpenReport} mode={mode} />
          <div className="card mt-8 px-6 py-10 text-sm text-faint">
            该会话没有事件历史
          </div>
        </div>
      )
    }
    return (
      <div ref={wrapRef} className="view-in mx-auto w-full max-w-[1080px] px-6 py-7 md:px-10">
        <PageHead running={running} flow={flow} question={question} onOpenReport={onOpenReport} mode={mode} />
        <div className="card relative mt-8 overflow-hidden px-6 py-10">
          {running && (
            <>
              <div className="paper-grid" />
              {/* 空心步进器：预告三段（还没数据，只画壳）*/}
              <div className="relative mb-6 flex items-center gap-3">
                {(mode === 'fast' ? ['规划', '写作', '收尾'] : ['规划', '审批', '写作', '收尾']).map((k, i) => (
                  <span key={k} className="flex items-center gap-3">
                    {i > 0 && <span className="h-px w-10 bg-line" />}
                    <span className="h-3 w-3 rounded-full border-[1.5px] border-black/25" />
                    <span className="text-2xs text-faint">{phaseLabel(k, mode)}</span>
                  </span>
                ))}
              </div>
            </>
          )}
          <div className="relative text-sm text-muted">
            {running ? '正在启动…' : '载入中…'}
          </div>
          {running && (
            <p className="mt-2 max-w-prose text-2xs leading-6 text-faint">
              接下来会先做一轮轻量检索和深读，摸清这个题目有多少可用材料；
              然后定下报告框架，再进入多轮「检索 → 抽取 → 评估」。
              哪一步拿不准、或者有材料缺口，这里会标出来。
            </p>
          )}
        </div>
      </div>
    )
  }

  /* ---- 指标条：每一项都要有真实事件来源，全无则整条不渲染 ---- */
  const metrics: { k: string; v: number; unit?: string; fmt?: (n: number) => string }[] = []
  if (fdata.sources !== null) {
    metrics.push({ k: '已读来源', v: fdata.sources, unit: '个' })
  }
  if (fdata.segments !== null) {
    metrics.push({ k: '可用段落', v: fdata.segments, unit: '段' })
  }
  if (fdata.written !== null) {
    metrics.push({
      k: '已写入',
      v: fdata.written,
      unit: fdata.written >= 10000 ? '万字' : '字',
      fmt: (n) => n >= 10000 ? (n / 10000).toFixed(1) : String(Math.round(n)),
    })
  }
  // 「已用 token」从页脚并入指标条；cost=0（回放/老库）不出格子，不编。
  if (cost !== null && cost > 0) {
    metrics.push({
      k: '已用', v: cost, unit: 'token',
      fmt: (n) => n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(Math.round(n)),
    })
  }
  // 用时：running 用「现在 - 会话创建」；done/flow 没有可靠的结束时刻
  // 可算（/events 不回时间戳），整条省略，不编。
  const showElapsed = running && !!startedAt

  const maxN = Math.max(0, ...fdata.sections.map((s) => s.n))

  return (
    <div ref={wrapRef} className="view-in mx-auto w-full max-w-[1080px] px-6 pb-4 pt-7 md:px-10">
      <PageHead running={running} flow={flow} question={question} onOpenReport={onOpenReport} />

      {/* L0 刻度轨：sticky——长流程往下滚也要一眼看到走到哪了 */}
      {/* 底色跟页面 paper 走：纯白会浮出一条和底 #FBFAF9 有色差的带子 */}
      <div ref={railRef} className="sticky top-0 z-20 -mx-2 bg-paper/95 px-2 pb-3 pt-1 backdrop-blur">
        <TrackRail
          phases={phases}
          activeIdx={activeIdx}
          order={order}
          doneAll={flow}
          writeFrac={writeFrac}
          writeDone={fdata.doneTasks}
          writeTotal={fdata.planTasks}
          onJump={jumpToPhase}
          mode={mode}
        />
      </div>

      {truncated && (
        <div className="mt-2 text-2xs text-faint">较早的事件已省略</div>
      )}

      {(metrics.length > 0 || showElapsed) && (
        <div className="card mt-4 reveal">
          <div className="metrics">
            {metrics.map((m) => (
              <div key={m.k}>
                <div className="k">{m.k}</div>
                <div className="big tabular-nums">
                  <CountUp v={m.v} fmt={m.fmt} />{m.unit && <small> {m.unit}</small>}
                </div>
              </div>
            ))}
            {showElapsed && (
              <div>
                <div className="k">用时</div>
                <div className="big tabular-nums">
                  <ElapsedSince iso={startedAt!} />
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      <div className={wide && fdata.sections.length > 0
        ? 'stagger mt-5 grid grid-cols-[minmax(0,1fr)_minmax(0,330px)] gap-4'
        : 'mt-5 space-y-4'}>
        {fdata.sections.length > 0 && (
          <div className="card self-start p-1.5">
            {fdata.sections.map((sec, i) => (
              <SectionRow key={sec.id} i={i} sec={sec} maxN={maxN} />
            ))}
          </div>
        )}
        <div>
          <div className="card overflow-hidden">
            <div className="px-4 pb-1 pt-3.5 text-[12.5px] font-medium">执行记录</div>
            {phases.map((p, i) => (
              <PhaseBlock
                key={p.key}
                phase={p}
                num={i + 1}
                mode={mode}
                defaultOpen={running && i === phases.length - 1}
                isLast={i === phases.length - 1}
                runningSince={runningSince}
                openSignal={openSig[p.key] ?? 0}
                active={running && i === phases.length - 1}
              />
            ))}

          </div>
        </div>
      </div>
      <div ref={endRef} />
    </div>
  )
}

/** 页头：进行中「报告正在成形」/回看「研究流程」+ 原问题。 */
function PageHead({
  running, flow, question, onOpenReport, mode,
}: {
  running: boolean; flow: boolean
  question?: string; onOpenReport?: () => void; mode?: string
}) {
  return (
    <div className="flex items-end justify-between gap-4">
      <div className="min-w-0">
        <h1 className="flex items-center gap-2.5 text-[32px] font-semibold tracking-[-.02em]">
          {running ? '报告正在成形' : '研究流程'}
          {running && (
            <span className="pill mt-1.5 inline-flex items-center gap-1 text-live">
              <span className="dot live" />进行中
            </span>
          )}
          {mode === 'fast' && <span className="tag-fast mt-1.5">快速</span>}
        </h1>
        {question && (
          <div className="mt-1 truncate text-[13px] text-faint">{question}</div>
        )}
      </div>
      {flow && onOpenReport && (
        <button className="btn-ghost shrink-0" onClick={onOpenReport}>
          查看报告 →
        </button>
      )}
    </div>
  )
}

/** 「现在 - 创建时刻」的秒表，给指标条的「用时」用。 */
function ElapsedSince({ iso }: { iso: string }) {
  const [, tick] = useState(0)
  useEffect(() => {
    const t = window.setInterval(() => tick((x) => x + 1), 1000)
    return () => window.clearInterval(t)
  }, [])
  const t0 = Date.parse(iso)
  const s = Number.isFinite(t0) ? Math.max(0, Math.floor((Date.now() - t0) / 1000)) : 0
  return <span className="tabular-nums">{fmtDur(s)}</span>
}

/** 指标格里的滚动数字：hooks 规则要求无条件调用，
 *  所以做成子组件——每格自己挂一份 hook。 */
function CountUp({ v, fmt }: { v: number; fmt?: (n: number) => string }) {
  const n = useCountUp(v)
  return <>{fmt ? fmt(n) : Math.round(n)}</>
}
