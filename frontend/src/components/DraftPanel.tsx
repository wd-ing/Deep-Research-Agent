import { useLayoutEffect, useRef } from 'react'

/**
 * 「正在写」面板：正文流式打字。
 *
 * **和思考面板刻意区分开**——
 * 思考是「AI 在想什么」（灰色、折叠、按 stage 分 tab），
 * 正文是「这节要交付的东西」（白底、始终展开、直接读）。
 * 混在一起用户就分不清哪块是成品。
 *
 * ⚠️ **不分小节**（2026-10-11 实测约束）：
 * `write_section` 是 4 并发，且子 agent 走 `run_tool_loop`——
 * 而它**拿不到 task_id**（签名里没有这个参数），所以正文增量
 * 无法归属到具体小节。要做「按小节分别打字」得先改后端透传，
 * 那是另一件事。
 */
export function DraftPanel({ text, active }: { text: string; active: boolean }) {
  const ref = useRef<HTMLPreElement>(null)

  // 自动滚到底，跟着最新的字走
  useLayoutEffect(() => {
    const el = ref.current
    if (el) el.scrollTop = el.scrollHeight
  }, [text])

  if (!text && !active) return null

  return (
    <div className="border-b border-line bg-white">
      <div className="flex items-center gap-2 px-6 pt-2 pb-1">
        <span className="text-xs font-medium text-ink">正在写</span>
        {active && (
          <span className="inline-flex items-center gap-1 text-[11px] text-accent">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
            实时
          </span>
        )}
        <span className="ml-auto text-[11px] text-muted">
          {text.length} 字{text.length > 4000 && '（滚动看全文）'}
        </span>
      </div>
      <pre
        ref={ref}
        className="max-h-72 overflow-auto whitespace-pre-wrap px-6 pb-3 text-sm leading-7 text-ink"
      >
        {text || '（等待模型开始输出…）'}
        {active && <span className="ml-0.5 inline-block w-1.5 animate-pulse bg-accent align-middle">▍</span>}
      </pre>
    </div>
  )
}
