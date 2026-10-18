import { useRef, useState } from 'react'
import { useDialog } from './useDialog'

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

  // Esc / Tab 陷阱 / 背景锁滚 / 关闭后归还焦点——三个对话框共用一套
  const panel = useRef<HTMLDivElement>(null)
  useDialog(panel, onCancel)

  const text = q.trim()
  const ok = text.length > 0 && !busy
  const submit = () => { if (ok) onSubmit(text) }

  return (
    // 遮罩点击关闭（和 SegDrawer / ConfirmDialog 统一）。
    // 面板 stopPropagation —— 它是遮罩的子节点，不拦住就等于点了遮罩。
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/30 p-4 pt-[10vh]"
      onClick={onCancel}
    >
      <div
        ref={panel}
        // tabIndex=-1：面板本身要能被聚焦——万一里面一个可聚焦元素都没有，
        // 焦点陷阱需要一个落点
        tabIndex={-1}
        className="w-full max-w-lg rounded-lg bg-white p-5 shadow-xl outline-none"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="新建研究"
      >
        <h3 className="text-sm font-medium text-ink">要研究什么问题？</h3>

        <textarea
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
            <button onClick={onCancel} className="btn-ghost">取消</button>
            <button onClick={submit} disabled={!ok} className="btn-primary">
              {busy ? '正在创建…' : '开始研究'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
