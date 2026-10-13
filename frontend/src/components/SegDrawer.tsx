import { useEffect } from 'react'
import type { SegmentRow, SourceMeta } from '../types'

/** 引用详情用右侧抽屉而不是弹窗——弹窗会挡住读者正在看的位置。 */
export function SegDrawer({
  seg, onClose,
}: {
  seg: SegmentRow | null
  onClose: () => void
}) {
  // ⚠️ effect 必须写在 `if (!seg) return null` **之前**——提前 return 会让
  //  这次渲染少调一个 hook，React 直接报「hook 调用次数不对」。
  useEffect(() => {
    if (!seg) return
    // Esc 关闭。跟 ConfirmDialog / Menu 同一套：抽屉是这一层最深的浮层，
    // 用户按 Esc 的预期是「收起眼前这个东西」。
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [seg, onClose])

  if (!seg) return null
  // 来源元数据由后端拼好（段落里的 `来源号` 是显示编号 S2，
  // 和 source.id 不是一回事，前端自己比对必然匹配不上）。
  const src: SourceMeta | undefined = seg.来源
  return (
    <>
      {/* 遮罩。z-30 压得住报告顶部的常驻条（那里面 `sticky top-0 z-20`），
          不然点它不会关抽屉、而且上半屏看着没被遮住。 */}
      <div className="fixed inset-0 z-30 bg-black/20" onClick={onClose} aria-hidden />
      <aside
        role="dialog"
        aria-label={`段落 ${seg.段号}`}
        className="fixed right-0 top-0 z-40 flex h-full w-[420px] flex-col border-l border-line bg-white shadow-lg"
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
                <div className="rounded bg-amber-50 px-2 py-1.5 text-[11px] leading-relaxed text-amber-800">
                  ⚠ 你已把这个来源标记为无用
                  {src.ignore_reason && `：${src.ignore_reason}`}
                  <div className="mt-0.5 text-amber-700">
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
          <div className="border-b border-line bg-blue-50 px-4 py-2 text-xs text-blue-900">
            <span className="font-medium">概括：</span>{seg.概括}
          </div>
        )}
        <div className="flex-1 overflow-auto px-4 py-3 text-sm leading-6 text-ink whitespace-pre-wrap">
          {seg.全文}
        </div>
      </aside>
    </>
  )
}

/** 来源性质要标出来——community / media 的可信度不同于 research。 */
export function NatureBadge({ nature }: { nature: string | null }) {
  const map: Record<string, [string, string]> = {
    first_party: ['一手', 'bg-green-100 text-green-800'],
    research: ['研究', 'bg-purple-100 text-purple-800'],
    media: ['媒体', 'bg-blue-100 text-blue-800'],
    community: ['社区', 'bg-amber-100 text-amber-800'],
    aggregator: ['聚合', 'bg-gray-100 text-gray-700'],
  }
  const [label, cls] = map[nature ?? ''] ?? ['未判', 'bg-gray-100 text-gray-500']
  return <span className={`rounded px-1.5 py-0.5 text-[11px] ${cls}`}>{label}</span>
}
