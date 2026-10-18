import { useEffect, useRef, useState } from 'react'
import type { Phase, Step } from './workflow'
import { Icon } from '../components/Icon'

/** 状态点的形状：完成=实心圆，进行中=呼吸环，需注意=菱形。
 *  **形状**比颜色更能区分——色弱用户也能读出来。
 *
 *  完成态原来是 `bg-gray-300`，一列 8 个浅灰点看不出「这 8 步确实走完了」。
 *  加深到 `gray-400`（2026-10-14 用户定：保持圆点，只加深，不换成 ✓——
 *  勾会和左侧时间轴的圆点语言打架）。 */
function Dot({ state, warn }: { state?: Step['state']; warn?: boolean }) {
  if (warn) {
    return (
      <span
        className="mt-1.5 h-2.5 w-2.5 shrink-0 rotate-45 rounded-[2px] bg-amber-400"
        title="需要注意"
      />
    )
  }
  if (state === 'running') {
    return (
      <span className="mt-1 flex h-3 w-3 shrink-0 items-center justify-center">
        <span className="wf-running-dot h-2 w-2 rounded-full bg-live" />
      </span>
    )
  }
  return <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-gray-400" />
}

/** 粗略字宽：CJK/全角记 1em，其余 0.55em。差一点也不影响，只是列宽略有出入。 */
function labelEm(t: string): number {
  let w = 0
  for (const ch of t) w += /[\u2e80-\uffef]/.test(ch) ? 1 : 0.55
  return w
}

/** 标签列宽：按**本环节**最长的标签算，让这个环节里所有摘要的左边缘对齐成列。
 *
 *  原来标签是 `shrink-0`，摘要紧跟其后——标签长短不一（`深读` 2 字 vs
 *  `检索（第 1 轮）` 7 字），于是每行摘要的起点都不同，横着扫「命中/采用」
 *  那几列数字时对不齐。
 *
 *  ⚠️ **不用全局固定宽度**：规划环节有 7 字标签，写作环节最长的只有 4 字
 *  （`装配成文`），写死会让写作那边白空一大块。按环节各算各的。 */
function labelColWidth(steps: Step[]): string {
  const w = Math.max(0, ...steps.map((s) => labelEm(s.label)))
  return `${(w + 0.5).toFixed(2)}em`   // 0.5em 余量，别让最长的标签贴着摘要
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
  return (
    <span className="tabular-nums">
      {' '}{Math.floor(s / 60)}:{String(s % 60).padStart(2, '0')}
    </span>
  )
}

/** 环节组里的一条明细（三级）：比父行轻一档，⚠️ 用琥珀色透出来。 */
function ChildRow({ c }: { c: Step }) {
  return (
    <li className="text-xs leading-5">
      <div className="flex items-baseline gap-2">
        <span className={[
          'shrink-0', c.warn ? 'font-medium text-amber-700' : 'text-muted/80',
        ].join(' ')}>{c.label}</span>
        <span className={[
          'min-w-0 flex-1 truncate',
          c.warn ? 'text-amber-700' : 'text-muted',
        ].join(' ')}>{c.summary}</span>
      </div>
      {c.detail?.map((d, i) => (
        <div key={i} className="pl-4 text-2xs leading-5 text-muted/80">· {d}</div>
      ))}
    </li>
  )
}

/** 一个子环节。**运行中不折叠**（正在干，不能藏）；完成后明细可收。
 *  `live` = 本环节组的孩子还在往里流（它是当前阶段的最后一个
 *  非 running 步骤）——此时保持展开看流；不再是最后一个时自动收拢。 */
function StepRow({
  step, labelW, runningSince, live,
}: {
  step: Step; labelW: string; runningSince: number | null; live: boolean
}) {
  const kids = step.children ?? []
  const hasKids = kids.length > 0
  const running = step.state === 'running'
  const hasDetail = !!step.detail?.length
  const auto = running || (live && hasKids)
  const [open, setOpen] = useState(auto)
  // 完成一刻自动收拢（live 变 false）；用户之后手动展开不再被顶回去，
  // 因为 auto 不再变化、effect 不会再触发。
  useEffect(() => { setOpen(auto) }, [auto])
  // ⚠️ 的明细默认展开——那是给用户判断的，不该藏
  const show = hasDetail && (open || step.warn)
  const kidWarns = kids.filter((k) => k.warn).length
  const collapsible = hasKids || hasDetail

  return (
    <li className="wf-step relative flex gap-3 py-[5px] pl-1">
      {/* live = 本环节最新产出行：流式期间它就是「当前在跑的点」，
          给绿呼吸点——原来只有阶段头和收尾合成行是绿的，
          中途的活跃组全是灰点，看不出在动。 */}
      <Dot state={running || live ? 'running' : undefined} warn={step.warn || kidWarns > 0} />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          {/* 可收起的行先给一个 chevron——原来只有尾部的「N 条」chip，
              行首看不出「这行能点开」。 */}
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
              'shrink-0 truncate text-sm',
              step.warn ? 'font-medium text-amber-700' : 'text-ink',
              running || live ? 'text-live' : '',
            ].join(' ')}
            style={{ width: labelW }}
            title={step.label}
          >
            {step.label}
          </span>
          <span className="min-w-0 flex-1 truncate text-xs leading-5 text-muted/90">
            {step.summary}
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
                open ? 'text-muted hover:bg-gray-100 hover:text-ink'
                : step.warn || kidWarns > 0
                  ? 'bg-amber-50 text-amber-700 hover:bg-amber-100'
                  : 'bg-gray-100/80 text-muted hover:bg-gray-200/80 hover:text-ink',
              ].join(' ')}
            >
              {open ? '收起' : hasKids ? `${kids.length} 条` : '展开'}
              {!open && kidWarns > 0 ? ` · ⚠${kidWarns}` : ''}
            </button>
          )}
        </div>
        {hasKids && open && (
          /* L3 明细：缩进 + 左侧引导竖线，明确「属于上一行的环节」。
             墨系 border-line 不用蓝——层级是结构不是链接。 */
          <ul className="wf-detail ml-1 mt-1 space-y-1 rounded-r-md border-l-2 border-line bg-gray-50/60 py-1.5 pl-3 pr-2.5">
            {kids.map((c) => <ChildRow key={c.id} c={c} />)}
          </ul>
        )}
        {show && (
          <ul className="wf-detail ml-1 mt-1 space-y-1 rounded-r-md border-l-2 border-line bg-gray-50/60 py-1.5 pl-3 pr-2.5">
            {step.detail!.map((d, i) => (
              <li key={i} className="text-xs leading-5 text-muted">· {d}</li>
            ))}
          </ul>
        )}
      </div>
    </li>
  )
}

/** 四阶段进度轨（E4）：长列表的「一眼定位」——
 *  原来想知道走到哪一步得滚回顶部找吸顶标题。
 *  固定四格（规划→审批→写作→收尾）：跑过的打勾、在跑的呼吸、
 *  没跑的灰着。点击=展开该环节并滚过去。 */
function PhaseRail({
  phases, onJump,
}: {
  phases: Phase[]
  onJump: (key: string) => void
}) {
  const ORDER = ['规划', '审批', '写作', '收尾']
  // 当前环节：有 running 步骤的那个；没有就是最后一个非空环节
  const activeKey =
    phases.find((p) => p.steps.some((s) => s.state === 'running'))?.key
    ?? phases[phases.length - 1]?.key
  const activeIdx = ORDER.indexOf(activeKey ?? '')

  return (
    <div className="sticky top-0 z-20 border-b border-line bg-white/95 backdrop-blur">
      <div className="mx-auto flex max-w-3xl items-center px-6 py-2">
        {ORDER.map((k, i) => {
          const p = phases.find((x) => x.key === k)
          const done = !!p && i < activeIdx
          const active = k === activeKey
          const warns = p ? p.steps.reduce(
            (a, s) => a + (s.warn ? 1 : 0)
              + (s.children?.filter((c) => c.warn).length ?? 0), 0) : 0
          return (
            <div key={k} className="flex min-w-0 flex-1 items-center">
              {i > 0 && (
                <span className={[
                  'mx-1 h-px flex-1 min-w-2',
                  i <= activeIdx ? 'bg-live/50' : 'bg-line',
                ].join(' ')} />
              )}
              <button
                onClick={() => p && onJump(k)}
                disabled={!p}
                title={warns ? `${p!.steps.filter((s) => s.warn).length} 处需注意` : undefined}
                className={[
                  'flex shrink-0 items-center gap-1.5 rounded-full px-2 py-1 text-xs transition-colors',
                  p ? 'hover:bg-gray-100' : 'cursor-default',
                ].join(' ')}
              >
                <span className={[
                  'flex h-4 w-4 items-center justify-center rounded-full text-[9px] font-bold',
                  done ? 'bg-live/15 text-live'
                    : active ? 'wf-running-dot bg-live text-white'
                    : 'bg-gray-200 text-gray-400',
                ].join(' ')}>
                  {done ? <Icon name="check" size={10} strokeWidth={2.5} /> : i + 1}
                </span>
                <span className={[
                  'truncate',
                  active ? 'font-medium text-ink'
                    : done ? 'text-muted' : 'text-muted/60',
                ].join(' ')}>
                  {k}
                </span>
                {warns > 0 && (
                  <span className="rounded bg-amber-100 px-1 text-2xs text-amber-800">{warns}</span>
                )}
              </button>
            </div>
          )
        })}
      </div>
    </div>
  )
}

/** 一个大环节。**标题吸顶**——滚过长规划时仍知道自己在哪一段。 */
function PhaseBlock({
  phase, defaultOpen, isLast, runningSince, openSignal,
}: {
  phase: Phase
  defaultOpen: boolean
  isLast: boolean
  runningSince: number | null
  /** 进度轨点了「跳过来」：变化时强制展开（展开后再滚，顺序不能反） */
  openSignal: number
}) {
  const [open, setOpen] = useState(defaultOpen)
  const running = phase.steps.some((s) => s.state === 'running')
  // ⚠️ 计数要把收进组里的明细行也算上——它们折起来了不代表不存在
  const warns = phase.steps.reduce(
    (a, s) => a + (s.warn && s.state !== 'running' ? 1 : 0)
      + (s.children?.filter((c) => c.warn).length ?? 0),
    0,
  )
  const labelW = labelColWidth(phase.steps)
  // 「当前活跃组」= 本阶段最后一个非 running 步骤：write_dispatch 这类
  // 组长落地后孩子还在流，保持展开看流；下一步落地就不再是最后一个，自动收拢。
  const lastReal = [...phase.steps].reverse().find((s) => s.state !== 'running')

  useEffect(() => {
    if (openSignal > 0) setOpen(true)
  }, [openSignal])

  // 本阶段跑完（不再是最后一个大环节）就收成一行概要；
  // 用户手动展开后 defaultOpen 不再变，不会被顶回去。
  useEffect(() => {
    if (!defaultOpen) setOpen(false)
  }, [defaultOpen])

  return (
    <section id={`wfphase-${phase.key}`}
      className="border-b border-line last:border-b-0">
      <button
        onClick={() => setOpen((o) => !o)}
        className="wf-phase-head flex w-full items-center gap-2.5 bg-gray-50/90 px-5 py-2.5 text-left transition-colors hover:bg-gray-100/80"
      >
        {/* 箭头用 CSS 过渡而不是换字符，展开时才有「转」的动画 */}
        <span
          className={[
            'shrink-0 text-muted transition-transform duration-200',
            open ? 'rotate-90' : '',
          ].join(' ')}
        >
          <Icon name="chevron-right" size={11} />
        </span>
        <span
          className={[
            'shrink-0 text-sm font-medium transition-colors',
            running ? 'text-live' : 'text-ink',
          ].join(' ')}
        >
          {phase.label}环节
        </span>
        {running && (
          <span className="wf-running-dot h-1.5 w-1.5 shrink-0 rounded-full bg-live" />
        )}
        <span className="min-w-0 flex-1 truncate text-xs leading-5 text-muted/90">
          {phase.summary}
        </span>
        {warns > 0 && (
          <span className="shrink-0 rounded bg-amber-100 px-1.5 py-0.5 text-2xs text-amber-800">
            {warns} 处需注意
          </span>
        )}
      </button>

      {open && (
        <div className="relative px-5 pb-4 pt-2">
          {/* 时间轴竖线：给一串步骤一条纵向节奏线 */}
          {!isLast && (
            <span
              className="pointer-events-none absolute bottom-0 left-[13.5px] top-3 w-px bg-line"
              aria-hidden
            />
          )}
          <ul className="space-y-0.5">
            {phase.steps.map((s) => (
              <StepRow key={s.id} step={s} labelW={labelW}
                runningSince={runningSince}
                live={running && s.id === lastReal?.id} />
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

/**
 * 工作流视图：对话式的一条流，往上翻即可。
 *
 * **动画克制**：只有三处动效——新条目淡入上移（0.28s）、
 * 进行中状态点呼吸、展开箭头旋转。其余一律不动。
 * 理由：这是长时间盯着看的界面，动得越多越累，
 * 而「有东西在推进」这件事，一个呼吸点就够表达了。
 */
export function WorkflowView({
  phases, running, runningSince, cost,
}: {
  phases: Phase[]
  running: boolean
  /** 当前节点开始时刻（ms），驱动「进行中 1:23」秒表 */
  runningSince: number | null
  /** 累计 token（10s 轮询的活性信号） */
  cost: number | null
}) {
  const endRef = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  // 进度轨跳转信号：记录每个环节最近一次「被点名」的时刻，
  // PhaseBlock 看到变化就展开——展开必须先于滚动，否则滚不到。
  const [openSig, setOpenSig] = useState<Record<string, number>>({})
  const jumpToPhase = (key: string) => {
    setOpenSig((o) => ({ ...o, [key]: Date.now() }))
    // 展开是重渲染，滚动要等 DOM 长出高度——一帧之后再滚
    requestAnimationFrame(() => {
      const el = document.getElementById(`wfphase-${key}`)
      const smooth = !window.matchMedia('(prefers-reduced-motion: reduce)').matches
      el?.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: 'start' })
    })
  }

  // 自动滚到底——但**用户一上滑就停**（正在读时被人拽走最烦人）
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
    // ⚠️ 这里原来有一句 `if (!stick.current) { force(n => n + 1); return }`——
    // 那个 `force` 触发一次谁也不读的重渲染，纯空转（2026-10-14 清掉）。
    // 用户上滑之后本来就什么都不用做：不滚就是了。
    if (!stick.current) return
    endRef.current?.scrollIntoView({ block: 'end', behavior: 'smooth' })
  }, [phases])

  // 什么都没发生的时候（刚点启动，第一个 SSE 事件还没到）：原来只回一句
  // 「正在启动…」，用户不知道在等什么、是正常等待还是卡住了。
  // 补一句**接下来会做什么**——这句话必须和 `graph/` 的真实顺序对得上：
  // probe → outline → (retrieve/extract/assess)×N → 审批 → write → finalize。
  // 不写「大概几分钟」：真实一轮 1591s，但这个数会随题材变，报不准不如不报。
  const started = phases.some((p) => p.steps.some((s) => s.state !== 'running'))
  if (!started) {
    return (
      <div className="px-6 py-10">
        <div className="text-sm text-muted">正在启动…</div>
        <p className="mt-2 max-w-prose text-xs leading-6 text-muted/80">
          接下来会先做一轮轻量检索和深读，摸清这个题目有多少可用材料；
          然后定下报告框架，再进入多轮「检索 → 抽取 → 评估」。
          哪一步拿不准、或者有材料缺口，这里会标出来。
        </p>
      </div>
    )
  }

  return (
    <div className="pb-4">
      <PhaseRail phases={phases} onJump={jumpToPhase} />
      {phases.map((p, i) => (
        <PhaseBlock
          key={p.key}
          phase={p}
          // 运行中：只展开当前大环节；跑完后**默认全收**——
          // 完成的环节留一行概要，往上翻时点标题展开（2026-10-18 用户定）。
          defaultOpen={running && i === phases.length - 1}
          isLast={i === phases.length - 1}
          runningSince={runningSince}
          openSignal={openSig[p.key] ?? 0}
        />
      ))}
      {/* 活性信号：累计 token。SSE 不推这个（频率不值一条事件），
          每 10s 轮询便宜端点——对「花着钱在跑」的心理感知特别好。 */}
      {cost !== null && (
        <div className="px-6 pt-3 text-2xs text-muted/70 tabular-nums">
          已用约 {cost >= 1000 ? `${(cost / 1000).toFixed(1)}k` : cost} token
        </div>
      )}
      <div ref={endRef} />
    </div>
  )
}
