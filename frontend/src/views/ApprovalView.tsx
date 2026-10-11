import type { ApprovalPayload } from '../types'

/** 框架可信度是用户批准与否的唯一硬依据，必须比什么都显眼。
 *  satisfied = 材料撑得住；其它 = 撞了上限，只是尽力而为。 */
function Credibility({ a }: { a: ApprovalPayload }) {
  const c = a.框架可信度
  if (!c) return null
  const ok = c.种类 === 'satisfied'
  return (
    <div className={[
      'rounded-lg border px-4 py-3',
      ok ? 'border-green-200 bg-green-50' : 'border-amber-300 bg-amber-50',
    ].join(' ')}>
      <div className={['text-sm font-medium', ok ? 'text-green-900' : 'text-amber-900'].join(' ')}>
        {ok ? '材料已经够了' : '材料未必够 —— 这份框架只是尽力而为'}
      </div>
      <div className="mt-1 text-xs text-muted">
        {c.说明}
        {c.停止原因 ? `（停止原因：${c.停止原因}）` : ''}
      </div>
    </div>
  )
}

export function ApprovalView({
  approval, onApprove,
}: {
  approval: ApprovalPayload
  onApprove: () => void
}) {
  const a = approval
  const mat = a.当前材料
  return (
    <div className="mx-auto max-w-3xl px-6 py-8">
      <h2 className="text-lg font-medium text-ink">研究框架待你批准</h2>
      <p className="mt-1 text-sm text-muted">
        这一轮框架已经按手上的材料校准过。下面是最终版本。
      </p>

      <div className="mt-4"><Credibility a={a} /></div>

      {mat && (
        <div className="mt-3 flex gap-6 text-xs text-muted">
          <span>来源 {mat.来源数}</span>
          <span>段落 {mat.段落数}</span>
          {mat.已抽取段数 ? <span>已抽取 {mat.已抽取段数}</span> : null}
          {a.已修订 && a.修订记录?.length ? (
            <span>已校准 {a.修订记录.length} 处</span>
          ) : null}
        </div>
      )}

      <div className="mt-6 space-y-3">
        {a.outline.map((s) => (
          <div key={s.id} className="rounded-lg border border-line bg-white px-4 py-3">
            <div className="text-sm font-medium text-ink">{s.title}</div>
            {s.question && (
              <div className="mt-1 text-xs text-muted">要回答：{s.question}</div>
            )}
            {s.修订说明 && (
              <div className="mt-2 rounded bg-amber-50 px-2 py-1 text-xs text-amber-900">
                <span className="font-medium">按材料校准：</span>{s.修订说明}
              </div>
            )}
          </div>
        ))}
      </div>

      {/* 驳回按钮当前是空转的（见 DESIGN_R9_FRONTEND_UI.md §8），
          所以这一轮只给「通过」。要真驳回得先动后端。 */}
      <div className="mt-8 flex justify-end">
        <button
          onClick={onApprove}
          className="rounded bg-accent px-5 py-2 text-sm text-white hover:bg-blue-700"
        >
          通过，开始写作
        </button>
      </div>
    </div>
  )
}
