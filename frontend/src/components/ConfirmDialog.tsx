import { useEffect } from 'react'

/** 确认对话框。不是 `window.confirm`——
 *  删一场研究连带删报告/来源/段，得说清楚，而且要能恢复。 */
export function ConfirmDialog({
  title, lines, confirmText, onCancel, onConfirm, danger = true,
}: {
  title: string
  lines: string[]
  confirmText: string
  onCancel: () => void
  onConfirm: () => void
  /** 破坏性操作才用红底。整理类操作（移动/改名）不该吓用户。 */
  danger?: boolean
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onCancel() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onCancel])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
      <div className="w-full max-w-sm rounded-lg bg-white p-5 shadow-xl">
        <h3 className="text-sm font-medium text-ink">{title}</h3>
        {lines.length > 0 && (
          <ul className="mt-3 space-y-1 text-xs text-muted">
            {lines.map((l, i) => <li key={i}>· {l}</li>)}
          </ul>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={onCancel}
            className="rounded border border-line px-3 py-1.5 text-xs text-muted hover:bg-gray-50"
          >取消</button>
          <button
            autoFocus
            onClick={onConfirm}
            className={[
              'rounded px-3 py-1.5 text-xs text-white',
              danger ? 'bg-red-600 hover:bg-red-700' : 'bg-accent hover:bg-blue-700',
            ].join(' ')}
          >{confirmText}</button>
        </div>
      </div>
    </div>
  )
}
