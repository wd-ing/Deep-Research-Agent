import { useRef } from 'react'
import { useDialog } from './useDialog'
import { Icon } from './Icon'
import type { SegmentRow, SourceMeta } from '../types'

/** 引用详情用右侧抽屉而不是弹窗——弹窗会挡住读者正在看的位置。 */
export function SegDrawer({
  seg, onClose,
}: {
  seg: SegmentRow | null
  onClose: () => void
}) {
  // ⚠️ hook 必须写在 `if (!seg) return null` **之前**——提前 return 会让
  //  这次渲染少调一个 hook，React 直接报「hook 调用次数不对」。
  // `useDialog` 里以 ref 取 `seg == null`：抽屉关着的时候不接管键盘，
  // 打开的那一刻才装上 Esc / Tab 陷阱 / 锁滚。
  const panel = useRef<HTMLElement>(null)
  const isOpen = !!seg
  useDialog(panel, onClose, isOpen)

  if (!seg) return null
  // 来源元数据由后端拼好（段落里的 `来源号` 是显示编号 S2，
  // 和 source.id 不是一回事，前端自己比对必然匹配不上）。
  const src: SourceMeta | undefined = seg.来源
  return (
    <>
      {/* 遮罩。z-30 压得住报告顶部的常驻条（那里面 `sticky top-0 z-20`），
          不然点它不会关抽屉、而且上半屏看着没被遮住。 */}
      <div className="fixed inset-0 z-30 bg-gray-900/25 backdrop-blur-[1px] print:hidden" onClick={onClose} aria-hidden />
      <aside
        ref={panel}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={`段落 ${seg.段号}`}
        className="fixed right-0 top-0 z-40 flex h-full w-[420px] flex-col border-l border-line bg-white shadow-lg outline-none print:hidden"
      >
        <div className="flex items-center justify-between border-b border-line px-4 py-3">
          <span className="text-sm font-medium text-ink">段落 {seg.段号}</span>
          <button onClick={onClose} className="text-sm text-muted hover:text-ink">关闭</button>
        </div>
        <div className="border-b border-line px-4 py-3 text-xs">
          {src ? (
            <div className="space-y-1">
              <div className="font-medium text-ink">{src.title || '(无标题)'}</div>
              <div className="text-muted">
                {src.domain} · {src.source_date || '日期未知'}
              </div>
              <div className="flex items-center gap-2">
                <NatureBadge nature={src.source_nature} />
                <span className="text-muted">{seg.token} token</span>
              </div>
              {/* 报告已经写完了，排除来源不会回头改写它。
                  用户点开这一段时，是最后一次提醒「这段材料你后来标为不可信」的时机 */}
              {src.ignored && (
                <div className="rounded bg-seal/10 px-2 py-1.5 text-2xs leading-relaxed text-seal">
                  <Icon name="alert" size={11} className="mb-px mr-0.5 inline-block" />你已把这个来源标记为无用
                  {src.ignore_reason && `：${src.ignore_reason}`}
                  <div className="mt-0.5 opacity-80">
                    报告已写完，不会因此改写——用到这一段的结论请自行复核。
                  </div>
                </div>
              )}
              <a href={src.url} target="_blank" rel="noreferrer"
                 className="block truncate text-accent underline decoration-dotted">
                {src.url}
              </a>
            </div>
          ) : (
            <div className="text-muted">来源信息缺失</div>
          )}
        </div>
        {seg.概括 && (
          <div className="border-b border-line bg-paper2 px-4 py-2 text-xs text-muted">
            <span className="font-medium">概括：</span>{seg.概括}
          </div>
        )}
        {/* 正文排版对齐报告正文（15px 级 / 行高 1.8+）；左 2px 墨线 = 稿子里
            「原文引用块」的语言，标出这段是引用材料不是界面文字。 */}
        <div className="flex-1 overflow-auto px-4 py-3">
          <div className="whitespace-pre-wrap border-l-2 border-ink pl-3 text-[15px] leading-[1.85] text-ink">
            {seg.全文}
          </div>
        </div>
      </aside>
    </>
  )
}

/** 来源性质要标出来——community / media 的可信度不同于 research。 */
/** 来源性质 → 标签与颜色。**唯一一份**。
 *
 *  `NatureBadge`（详情里的色块）和 `SourceList` 的分组标题（一个小圆点）
 *  都从这里取色——两处各写一份的话，早晚漂移成两种紫。
 *  `chip` 是文字色块的类，`dot` 是同色系的小圆点（分组标题用，比色块安静）。 */
export const NATURE_META: Record<string, { label: string; chip: string; dot: string }> = {
  // chip 取 index.css 的 .tg1–.tg5（稿子原值）；dot 是同色系实色（分组标题用）。
  first_party: { label: '一手', chip: 'tg1', dot: '#2f7440' },
  research: { label: '研究', chip: 'tg4', dot: '#6a4c96' },
  media: { label: '媒体', chip: 'tg2', dot: '#2E50C8' },
  community: { label: '社区', chip: 'tg5', dot: '#9a6a17' },
  aggregator: { label: '聚合', chip: 'tg3', dot: 'rgb(0 0 0 / .42)' },
}
const NATURE_UNKNOWN = { label: '未判', chip: 'tg3', dot: 'rgb(0 0 0 / .3)' }

export function natureMeta(nature: string | null | undefined) {
  return NATURE_META[nature ?? ''] ?? NATURE_UNKNOWN
}

export function NatureBadge({ nature }: { nature: string | null }) {
  const m = natureMeta(nature)
  return <span className={`rounded px-1.5 py-0.5 text-2xs ${m.chip}`}>{m.label}</span>
}
