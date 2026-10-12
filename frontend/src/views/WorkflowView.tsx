import { useState } from 'react'
import type { Phase, Step } from './workflow'

/** 一个子环节。**运行中永远展开**（正在干，不能藏起来）；完成后摘要常显、明细可收。 */
function StepRow({ step }: { step: Step }) {
  const [open, setOpen] = useState(false)
  const running = step.state === 'running'
  const hasDetail = !!step.detail?.length

  return (
    <li className="py-1.5">
      <div className="flex items-baseline gap-2">
        <span className={[
          'text-sm',
          step.warn ? 'font-medium text-amber-700' : 'text-ink',
          running ? 'text-accent' : '',
        ].join(' ')}>
          {step.warn ? '⚠ ' : ''}{step.label}
        </span>
        <span className="flex-1 truncate text-xs text-muted">{step.summary}</span>
        {running && (
          <span className="inline-block h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-accent" />
        )}
        {hasDetail && !running && (
          <button
            onClick={() => setOpen((o) => !o)}
            className="shrink-0 text-[11px] text-muted hover:text-ink"
          >
            {open ? '收起' : '展开'}
          </button>
        )}
      </div>
      {/* ⚠️ 有警告时详情默认展开——那是要用户判断的东西，不该藏 */}
      {hasDetail && (open || step.warn) && (
        <ul className="mt-1 space-y-0.5 pl-4">
          {step.detail!.map((d, i) => (
            <li key={i} className="text-xs leading-5 text-muted">· {d}</li>
          ))}
        </ul>
      )}
    </li>
  )
}

/** 一个大环节。**折叠后仍能看到摘要**，所以往上翻也能知道这组干了什么。 */
function PhaseBlock({ phase, defaultOpen }: { phase: Phase; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  const running = phase.steps.some((s) => s.state === 'running')

  return (
    <section className="border-t border-line first:border-t-0">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-6 py-2.5 text-left hover:bg-gray-50"
      >
        <span className="text-xs text-muted">{open ? '▾' : '▸'}</span>
        <span className={[
          'text-sm font-medium',
          running ? 'text-accent' : 'text-ink',
        ].join(' ')}>
          {phase.label}环节
        </span>
        {running && (
          <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
        )}
        <span className="flex-1 truncate text-xs text-muted">{phase.summary}</span>
      </button>
      {open && (
        <ul className="space-y-0 px-6 pb-3">
          {phase.steps.map((s) => <StepRow key={s.id} step={s} />)}
        </ul>
      )}
    </section>
  )
}

/**
 * 工作流视图：对话式的一条流，往上翻即可。
 *
 * **三层**：大环节（折叠但摘要常显）→ 子环节（摘要常显）→ 明细（收起）。
 * 摘要永不折叠——那是「知道在干什么」的底线。
 */
export function WorkflowView({
  phases, running,
}: {
  phases: Phase[]
  running: boolean
}) {
  if (!phases.length) {
    return <div className="px-6 py-10 text-sm text-muted">正在启动…</div>
  }
  return (
    <div>
      {phases.map((p, i) => (
        <PhaseBlock
          key={p.key}
          phase={p}
          // 当前大环节默认展开，之前的折起来；跑完的最后一个保持展开方便看结果
          defaultOpen={running ? i === phases.length - 1 : true}
        />
      ))}
    </div>
  )
}
