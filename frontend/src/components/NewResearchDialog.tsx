import { useEffect, useState } from 'react'

/** 新建研究。不是 `window.prompt()`——
 *  原生弹窗没法显示历史问题、不能多行、样式不可控，移动端更是难用。
 *  而「新建一次研究」往往是顺着上一次的问题继续问，把最近问过的摆出来
 *  能省掉重新打字。 */
export function NewResearchDialog({
  recent, busy, onCancel, onSubmit,
}: {
  recent: { id: string; question: string }[]
  /** 创建请求在飞：禁掉按钮，防止连点建出两条一样的会话 */
  busy: boolean
  onCancel: () => void
  onSubmit: (question: string) => void
}) {
  const [q, setQ] = useState('')

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onCancel() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onCancel])

  const text = q.trim()
  const ok = text.length > 0 && !busy
  const submit = () => { if (ok) onSubmit(text) }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/30 p-4 pt-[10vh]">
      <div
        className="w-full max-w-lg rounded-lg bg-white p-5 shadow-xl"
        role="dialog"
        aria-modal="true"
        aria-label="新建研究"
      >
        <h3 className="text-sm font-medium text-ink">要研究什么问题？</h3>

        <textarea
          autoFocus
          rows={3}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          // Enter 换行、⌘/Ctrl+Enter 才提交：问题常常要写好几行，
          // 单行输入框会逼着人把话挤成一句
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); submit() }
          }}
          placeholder="例：固态电池的产业化进展，以及各家技术路线的分歧点"
          className="mt-3 w-full resize-y rounded-md border border-line bg-white px-2.5 py-2
                     text-sm leading-relaxed outline-none focus:border-accent"
        />

        {recent.length > 0 && (
          <div className="mt-3">
            <div className="text-2xs text-muted">最近问过</div>
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {recent.map((r) => (
                <button
                  key={r.id}
                  onClick={() => setQ(r.question)}
                  title={r.question}
                  className="max-w-full truncate rounded-full border border-line px-2.5 py-1
                             text-2xs text-muted hover:bg-gray-50 hover:text-ink"
                >{r.question}</button>
              ))}
            </div>
          </div>
        )}

        <div className="mt-5 flex items-center justify-between">
          <span className="text-2xs text-muted">⌘ / Ctrl + Enter 开始</span>
          <div className="flex gap-2">
            <button
              onClick={onCancel}
              className="rounded border border-line px-3 py-1.5 text-xs text-muted hover:bg-gray-50"
            >取消</button>
            <button
              onClick={submit}
              disabled={!ok}
              className="rounded bg-accent px-3 py-1.5 text-xs text-white
                         hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-40"
            >{busy ? '正在创建…' : '开始研究'}</button>
          </div>
        </div>
      </div>
    </div>
  )
}
