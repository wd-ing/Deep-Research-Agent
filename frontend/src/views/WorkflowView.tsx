import { useEffect, useRef, useState } from 'react'
import type { Phase, Step } from './workflow'

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

/** 一个子环节。**运行中不折叠**（正在干，不能藏）；完成后明细可收。 */
function StepRow({ step, labelW }: { step: Step; labelW: string }) {
  const [open, setOpen] = useState(false)
  const running = step.state === 'running'
  const hasDetail = !!step.detail?.length
  // ⚠️ 的明细默认展开——那是给用户判断的，不该藏
  const show = hasDetail && (open || step.warn)

  return (
    <li className="wf-step relative flex gap-3 py-[5px] pl-1">
      <Dot state={step.state} warn={step.warn} />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span
            className={[
              'shrink-0 truncate text-sm',
              step.warn ? 'font-medium text-amber-700' : 'text-ink',
              running ? 'text-live' : '',
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
            <span className="shrink-0 text-2xs text-live">进行中</span>
          )}
          {hasDetail && !running && (
            <button
              onClick={() => setOpen((o) => !o)}
              className={[
                'shrink-0 rounded px-1 text-2xs transition-colors',
                step.warn
                  ? 'text-amber-600 hover:bg-amber-50'
                  : 'text-muted hover:bg-gray-100 hover:text-ink',
              ].join(' ')}
            >
              {open ? '收起' : '展开'}
            </button>
          )}
        </div>
        {show && (
          <ul className="wf-detail mt-1 space-y-1 rounded-md bg-gray-50/80 px-2.5 py-1.5">
            {step.detail!.map((d, i) => (
              <li key={i} className="text-xs leading-5 text-muted">· {d}</li>
            ))}
          </ul>
        )}
      </div>
    </li>
  )
}

/** 一个大环节。**标题吸顶**——滚过长规划时仍知道自己在哪一段。 */
function PhaseBlock({
  phase, defaultOpen, isLast,
}: {
  phase: Phase
  defaultOpen: boolean
  isLast: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  const running = phase.steps.some((s) => s.state === 'running')
  const warns = phase.steps.filter((s) => s.warn && s.state !== 'running').length
  const labelW = labelColWidth(phase.steps)

  return (
    <section className="border-b border-line last:border-b-0">
      <button
        onClick={() => setOpen((o) => !o)}
        className="wf-phase-head flex w-full items-center gap-2.5 bg-gray-50/90 px-5 py-2.5 text-left transition-colors hover:bg-gray-100/80"
      >
        {/* 箭头用 CSS 过渡而不是换字符，展开时才有「转」的动画 */}
        <span
          className={[
            'shrink-0 text-2xs text-muted transition-transform duration-200',
            open ? 'rotate-90' : '',
          ].join(' ')}
        >
          ▶
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
              <StepRow key={s.id} step={s} labelW={labelW} />
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
  phases, running,
}: {
  phases: Phase[]
  running: boolean
}) {
  const endRef = useRef<HTMLDivElement>(null)
  const stick = useRef(true)

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
      {phases.map((p, i) => (
        <PhaseBlock
          key={p.key}
          phase={p}
          // 运行中：当前大环节展开；跑完后全部展开，往上翻即可
          defaultOpen={running ? i === phases.length - 1 : true}
          isLast={i === phases.length - 1}
        />
      ))}
      <div ref={endRef} />
    </div>
  )
}
