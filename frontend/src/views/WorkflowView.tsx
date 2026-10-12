import { useEffect, useRef, useState } from 'react'
import type { Phase, Step } from './workflow'

/** 状态点的形状：完成=实心圆，进行中=呼吸环，未开始=空心圈。
 *  **形状**比颜色更能区分——色弱用户也能读出来。 */
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
        <span className="wf-running-dot h-2 w-2 rounded-full bg-accent" />
      </span>
    )
  }
  return <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-gray-300" />
}

/** 一个子环节。**运行中不折叠**（正在干，不能藏）；完成后明细可收。 */
function StepRow({ step }: { step: Step }) {
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
              'shrink-0 text-sm',
              step.warn ? 'font-medium text-amber-700' : 'text-ink',
              running ? 'text-accent' : '',
            ].join(' ')}
          >
            {step.label}
          </span>
          <span className="min-w-0 flex-1 truncate text-xs leading-5 text-muted/90">
            {step.summary}
          </span>
          {running && (
            <span className="shrink-0 text-[11px] text-accent">进行中</span>
          )}
          {hasDetail && !running && (
            <button
              onClick={() => setOpen((o) => !o)}
              className={[
                'shrink-0 rounded px-1 text-[11px] transition-colors',
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

  return (
    <section className="border-b border-line last:border-b-0">
      <button
        onClick={() => setOpen((o) => !o)}
        className="wf-phase-head flex w-full items-center gap-2.5 bg-gray-50/90 px-5 py-2.5 text-left transition-colors hover:bg-gray-100/80"
      >
        {/* 箭头用 CSS 过渡而不是换字符，展开时才有「转」的动画 */}
        <span
          className={[
            'shrink-0 text-[10px] text-muted transition-transform duration-200',
            open ? 'rotate-90' : '',
          ].join(' ')}
        >
          ▶
        </span>
        <span
          className={[
            'shrink-0 text-sm font-medium transition-colors',
            running ? 'text-accent' : 'text-ink',
          ].join(' ')}
        >
          {phase.label}环节
        </span>
        {running && (
          <span className="wf-running-dot h-1.5 w-1.5 shrink-0 rounded-full bg-accent" />
        )}
        <span className="min-w-0 flex-1 truncate text-xs leading-5 text-muted/90">
          {phase.summary}
        </span>
        {warns > 0 && (
          <span className="shrink-0 rounded bg-amber-100 px-1.5 py-0.5 text-[11px] text-amber-800">
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
              <StepRow key={s.id} step={s} />
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
  const [, force] = useState(0)

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
    if (!stick.current) { force((n) => n + 1); return }
    endRef.current?.scrollIntoView({ block: 'end', behavior: 'smooth' })
  }, [phases])

  if (!phases.length) {
    return <div className="px-6 py-10 text-sm text-muted">正在启动…</div>
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
