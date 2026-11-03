import { useEffect, useState } from 'react'
import type { ApprovalPayload } from '../types'
import { Icon } from '../components/Icon'
import { CountUp } from '../components/CountUp'

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

/** 材料读数（真实字段，有就显示）：数字滚一下。 */
function MatReadout({ a }: { a: ApprovalPayload }) {
  const mat = a.当前材料
  const items: { k: string; v: number }[] = []
  if (mat?.来源数 != null) items.push({ k: '来源', v: mat.来源数 })
  if (mat?.段落数 != null) items.push({ k: '段落', v: mat.段落数 })
  if (mat?.已抽取段数) items.push({ k: '已抽取', v: mat.已抽取段数 })
  if (a.已修订 && a.修订记录?.length) items.push({ k: '已校准', v: a.修订记录.length })
  if (!items.length) return null
  return (
    <div className="mt-4 flex gap-8 border-y border-line2 py-3">
      {items.map((it) => (
        <div key={it.k} className="flex items-baseline gap-1.5">
          <b className="text-[24px] font-medium leading-none tabular-nums">
            <CountUp v={it.v} />
          </b>
          <span className="text-2xs text-faint">{it.k}</span>
        </div>
      ))}
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
  /* 防连点：点击后按钮置灰 3 秒自然解除——onApprove 走网络，
     approval 一变这条视图就卸载了，锁本身只是防双击的短锁。 */
  const [pending, setPending] = useState(false)
  useEffect(() => {
    if (!pending) return
    const t = window.setTimeout(() => setPending(false), 3000)
    return () => window.clearTimeout(t)
  }, [pending])

  return (
    <div className="mx-auto w-full max-w-3xl px-6 pb-24 pt-8">
      {/* eyebrow：审批节点的近黑语义（这一步是你的） */}
      <div className="flex items-center gap-2 font-mono text-2xs tracking-[.14em] text-faint">
        <span className="h-2 w-2 rounded-full bg-ink" />
        STEP · 审批
      </div>
      <h2 className="mt-2 text-[30px] font-semibold tracking-[-.02em] text-ink">
        这一步留给你
      </h2>
      <p className="mt-1.5 text-sm text-muted">
        研究框架待你批准——它已经按手上的材料校准过。下面是最终版本。
      </p>

      <div className="mt-5"><Credibility a={a} /></div>
      <MatReadout a={a} />

      <div className="stagger mt-6 space-y-2.5">
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

      {/* 吸附操作条：sticky 贴容器底，不遮末行（pb-24 留量）。
          驳回按钮当前是空转的（见 DESIGN_R9_FRONTEND_UI.md §8），
          所以这一轮只给「通过」。要真驳回得先动后端。 */}
      <div className="sticky bottom-0 -mx-6 mt-8 flex justify-end border-t border-line2 bg-white/95 px-6 py-3.5 backdrop-blur">
        <button
          onClick={() => { if (!pending) { setPending(true); onApprove() } }}
          disabled={pending}
          className="appr-cta relative w-full rounded-xl bg-ink px-5 py-3 text-[15px] font-medium text-paper transition-opacity disabled:opacity-60 sm:w-auto sm:px-8"
        >
          {pending ? '已提交…' : '通过，开始写作'}
        </button>
      </div>
    </div>
  )
}
