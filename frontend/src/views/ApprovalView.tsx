import type { ApprovalPayload } from '../types'
import { Icon } from '../components/Icon'

/** 框架可信度是用户批准与否的唯一硬依据，必须比什么都显眼。
 *  satisfied = 材料撑得住；其它 = 撞了上限，只是尽力而为。 */
function Credibility({ a }: { a: ApprovalPayload }) {
  const c = a.框架可信度
  if (!c) return null
  const ok = c.种类 === 'satisfied'
  return (
    <div className="card px-4 py-3.5">
      <div className="flex items-center gap-2">
        {/* 够了 = 进行中语义的绿点；撞上限 = 需要判断的告警 → 朱红 seal。
            全屏只有这一处大结论，胶囊比整卡染色安静也够醒目。 */}
        {ok
          ? <span className="pill"><span className="dot live" />材料已经够了</span>
          : <span className="seal"><Icon name="alert" size={11} />材料未必够</span>}
        {!ok && (
          <span className="text-sm font-medium text-ink">这份框架只是尽力而为</span>
        )}
      </div>
      <div className="mt-1.5 text-xs leading-relaxed text-muted">
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
        <div className="mt-3 flex gap-2">
          <span className="pill font-mono">来源 {mat.来源数}</span>
          <span className="pill font-mono">段落 {mat.段落数}</span>
          {mat.已抽取段数 ? <span className="pill font-mono">已抽取 {mat.已抽取段数}</span> : null}
          {a.已修订 && a.修订记录?.length ? (
            <span className="pill font-mono">已校准 {a.修订记录.length} 处</span>
          ) : null}
        </div>
      )}

      <div className="mt-6 space-y-2.5">
        {a.outline.map((s, i) => (
          <div key={s.id} className="card flex gap-3 px-4 py-3.5">
            <span className="shrink-0 pt-0.5 font-mono text-xs text-faint">
              {String(i + 1).padStart(2, '0')}
            </span>
            <div className="min-w-0 flex-1">
              <div className="text-sm font-medium text-ink">{s.title}</div>
              {s.question && (
                <div className="mt-1 text-xs leading-relaxed text-muted">要回答：{s.question}</div>
              )}
              {s.修订说明 && (
                /* 「按材料校准」是说明性注记不是告警——灰底注记 + 墨色标签，
                   朱红只留给需要复核的事（撞上限那条在顶上）。 */
                <div className="mt-2 rounded-lg bg-black/[.04] px-2.5 py-1.5 text-xs leading-relaxed text-muted">
                  <span className="font-medium text-ink">按材料校准：</span>{s.修订说明}
                </div>
              )}
            </div>
          </div>
        ))}
      </div>

      {/* 驳回按钮当前是空转的（见 DESIGN_R9_FRONTEND_UI.md §8），
          所以这一轮只给「通过」。要真驳回得先动后端。 */}
      <div className="mt-8 flex justify-end">
        <button
          onClick={onApprove}
          className="btn-primary px-5 py-2 text-sm"
        >
          通过，开始写作
        </button>
      </div>
    </div>
  )
}
