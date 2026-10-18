import { useRef } from 'react'
import { useDialog } from './useDialog'

/** 确认对话框。不是 `window.confirm`——
 *  删一场研究连带删报告/来源/段，得说清楚，而且要能恢复。 */
export function ConfirmDialog({
  title, lines, confirmText, onCancel, onConfirm, danger = true, input,
}: {
  title: string
  lines: string[]
  confirmText: string
  onCancel: () => void
  onConfirm: () => void
  /** 破坏性操作才用红底。整理类操作（移动/改名）不该吓用户。 */
  danger?: boolean
  /** 有这个字段就渲染一个输入框，值由调用方持有。 */
  input?: {
    placeholder: string
    value: string
    onChange: (v: string) => void
  }
}) {
  const panel = useRef<HTMLDivElement>(null)
  useDialog(panel, onCancel)

  return (
    // 遮罩点击关闭。SegDrawer / NewResearchDialog 现在都有，这里是唯一不一致的一处。
    // ⚠️ 面板必须 `stopPropagation`——它就在遮罩里面，不拦住的话点面板等于点遮罩。
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-gray-900/40 p-4 backdrop-blur-[2px]"
      onClick={onCancel}
    >
      <div
        ref={panel}
        tabIndex={-1}
        className="w-full max-w-sm rounded-xl bg-white p-5 shadow-xl outline-none"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <h3 className="text-sm font-medium text-ink">{title}</h3>
        {lines.length > 0 && (
          <ul className="mt-3 space-y-1 text-xs text-muted">
            {lines.map((l, i) => <li key={i}>· {l}</li>)}
          </ul>
        )}
        {input && (
          <input
            autoFocus
            value={input.value}
            onChange={(e) => input.onChange(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') onConfirm() }}
            placeholder={input.placeholder}
            className="mt-3 w-full rounded-md border border-line bg-white px-2.5 py-1.5 text-sm outline-none focus:border-accent"
          />
        )}
        <div className="mt-5 flex justify-end gap-2">
          <button onClick={onCancel} className="btn-ghost">取消</button>
          <button
            autoFocus={!input}
            onClick={onConfirm}
            className={danger ? 'btn-danger-solid' : 'btn-primary'}
          >{confirmText}</button>
        </div>
      </div>
    </div>
  )
}
