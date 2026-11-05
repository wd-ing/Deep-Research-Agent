import { useRef, useState } from 'react'
import { useDialog } from './useDialog'
import { MODES, loadDefaultMode, type ResearchMode } from '../lib/mode'
import { IntentReply } from './IntentReply'
import { useIntentGate } from '../lib/useIntentGate'

/** 新建研究。不是 `window.prompt()`——
 *  原生弹窗没法显示历史问题、不能多行、样式不可控，移动端更是难用。
 *  而「新建一次研究」往往是顺着上一次的问题继续问，把最近问过的摆出来
 *  能省掉重新打字。 */
export function NewResearchDialog({
  recent, busy, onCancel, onGatedCreated, onError,
}: {
  recent: { id: string; question: string }[]
  /** 创建请求在飞：禁掉按钮，防止连点建出两条一样的会话 */
  busy: boolean
  onCancel: () => void
  /** gate 建好会话后的启动回调（App: startById）。 */
  onGatedCreated: (sessionId: string) => void
  /** 非 409 的错误上浮（App: notify）。 */
  onError?: (msg: string) => void
}) {
  const [q, setQ] = useState('')
  // 初始选中 = 设置页存的默认档；这次选择只影响本次，
  // 不在这里写回默认值（默认值归设置页管）。
  const [mode, setMode] = useState<ResearchMode>(() => loadDefaultMode())

  // Esc / Tab 陷阱 / 背景锁滚 / 关闭后归还焦点——三个对话框共用一套
  const panel = useRef<HTMLDivElement>(null)
  useDialog(panel, onCancel)

  const text = q.trim()
  const gate = useIntentGate(onGatedCreated)
  const ok = text.length > 0 && !busy && !gate.checking
  const submit = () => {
    if (ok) void gate.submit(text, mode).catch((e: unknown) =>
      onError?.(`建不了这场研究：${(e as Error).message}`))
  }
  const taRef = useRef<HTMLTextAreaElement>(null)

  return (
    // 遮罩点击关闭（和 SegDrawer / ConfirmDialog 统一）。
    // 面板 stopPropagation —— 它是遮罩的子节点，不拦住就等于点了遮罩。
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-gray-900/40 p-4 pt-[10vh] backdrop-blur-[2px]"
      onClick={onCancel}
    >
      <div
        ref={panel}
        // tabIndex=-1：面板本身要能被聚焦——万一里面一个可聚焦元素都没有，
        // 焦点陷阱需要一个落点
        tabIndex={-1}
        className="dialog-in w-full max-w-lg rounded-xl bg-white p-5 shadow-float outline-none"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="新建研究"
      >
        <h3 className="text-sm font-medium text-ink">要研究什么问题？</h3>

        <textarea
          ref={taRef}
          rows={3}
          value={q}
          onChange={(e) => { setQ(e.target.value); gate.dismiss() }}
          // Enter 换行、⌘/Ctrl+Enter 才提交：问题常常要写好几行，
          // 单行输入框会逼着人把话挤成一句
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); submit() }
          }}
          placeholder="例：固态电池的产业化进展，以及各家技术路线的分歧点"
          className="mt-3 w-full resize-y rounded-md border border-line bg-white px-2.5 py-2
                     text-sm leading-relaxed outline-none focus:border-accent"
        />

        {/* 档位卡：标准=完整流程，快速=检索+一次成文简报。
            role=radiogroup + ←/→ 切换，与分段控件同一套键盘语义。 */}
        <div className="mt-3" role="radiogroup" aria-label="研究档位">
          <div className="text-2xs text-muted">档位</div>
          <div className="mt-1.5 grid grid-cols-2 gap-2 max-sm:grid-cols-1">
            {MODES.map((m) => {
              const on = mode === m.key
              return (
                <button
                  key={m.key}
                  type="button"
                  role="radio"
                  aria-checked={on}
                  tabIndex={on ? 0 : -1}
                  onClick={() => setMode(m.key)}
                  onKeyDown={(e) => {
                    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
                      e.preventDefault()
                      const i = MODES.findIndex((x) => x.key === mode)
                      const nxt = MODES[(i + (e.key === 'ArrowRight' ? 1 : MODES.length - 1)) % MODES.length]
                      setMode(nxt.key)
                      ;(e.currentTarget.parentElement?.querySelector(
                        `[data-mode="${nxt.key}"]`) as HTMLElement | null)?.focus()
                    }
                  }}
                  data-mode={m.key}
                  className={[
                    'rounded-lg border px-3 py-2.5 text-left outline-none transition-colors',
                    on
                      ? 'border-accent bg-accent/5 text-ink focus-visible:ring-2 focus-visible:ring-accent/40'
                      : 'border-line bg-white text-ink hover:bg-gray-50 focus-visible:ring-2 focus-visible:ring-accent/40',
                  ].join(' ')}
                >
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="text-sm font-medium">
                      {m.label}
                      <span className="ml-1.5 text-2xs font-normal text-muted">{m.tagline}</span>
                    </span>
                    <span className={['shrink-0 text-2xs tabular-nums', on ? 'text-accent' : 'text-faint'].join(' ')}>
                      {m.eta}
                    </span>
                  </div>
                  <div className="mt-1 text-2xs leading-relaxed text-muted">{m.bullets}</div>
                </button>
              )
            })}
          </div>
        </div>

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

        {gate.reply && (
          <IntentReply
            notice={gate.reply.kind === 'notice'}
            reply={gate.reply.reply}
            suggestions={gate.reply.suggestions}
            onPick={(t) => { setQ(t); gate.dismiss(); taRef.current?.focus() }}
            onForce={() => void gate.forceSubmit(text, mode)
              .catch((e: unknown) =>
                onError?.(`建不了这场研究：${(e as Error).message}`))}
            onDismiss={gate.dismiss}
          />
        )}

        <div className="mt-5 flex items-center justify-between">
          <span className="text-2xs text-muted">⌘ / Ctrl + Enter 开始</span>
          <div className="flex gap-2">
            <button onClick={onCancel} className="btn-ghost">取消</button>
            <button onClick={submit} disabled={!ok} className="btn-primary">
              {gate.checking ? '判断中…' : busy ? '正在创建…' : '开始研究'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
