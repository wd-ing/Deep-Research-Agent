import type { SourceDetailRow } from '../types'
import { NatureBadge } from './SegDrawer'

/** 来源详情（主区域）。点来源库里的某一条看它到底说了什么。
 *
 *  段号（`S37·5`）由后端 EvidenceTools 生成，**与报告正文里的编号一致**——
 *  所以这里显示的段号可以直接拿去报告里搜，能定位到同一段。
 */
export function SourceDetail({
  src, onOpenSession, onToggleIgnore,
}: {
  src: SourceDetailRow | null
  onOpenSession: (id: string) => void
  onToggleIgnore: (id: string, ignored: boolean) => void
}) {
  if (!src) {
    return (
      <div className="flex flex-1 items-center justify-center text-sm text-muted">
        左侧来源库里点一条，看它说了什么
      </div>
    )
  }

  return (
    <div className="flex-1 overflow-auto">
      <header className="border-b border-line bg-white px-6 py-4">
        <h2 className="text-base font-medium leading-snug text-ink">
          {src.title || '(无标题)'}
        </h2>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted">
          <NatureBadge nature={src.nature} />
          <span>{src.domain}</span>
          <span>{src.source_date ? src.source_date.slice(0, 10) : '日期未知'}</span>
          <span>{src.segments.length} 段</span>
          {!src.on_topic && (
            <span className="rounded bg-amber-100 px-1.5 py-0.5 text-amber-800">
              ⚠ 判定为不在题，未参与报告
            </span>
          )}
          {src.ignored && (
            <span className="rounded bg-amber-100 px-1.5 py-0.5 text-amber-800">
              ⊘ 已标记为无用{src.ignore_reason ? `：${src.ignore_reason}` : ''}
            </span>
          )}
        </div>
        {src.nature_reason && (
          <p className="mt-2 text-xs leading-relaxed text-muted">
            性质判据：{src.nature_reason}
          </p>
        )}
        <div className="mt-2 flex items-center gap-3 text-xs">
          <a href={src.url} target="_blank" rel="noreferrer"
             className="truncate text-accent underline decoration-dotted">
            {src.url}
          </a>
        </div>
        <button
          onClick={() => onOpenSession(src.session_id)}
          className="mt-2 text-xs text-muted underline decoration-dotted hover:text-ink"
        >
          ← 回到这场研究：{src.session_title.slice(0, 30)}
        </button>
        {/* 在详情页也能改判——用户常常是点进来看了正文才决定「这条确实是垃圾」 */}
        <button
          onClick={() => onToggleIgnore(src.id, src.ignored)}
          className="mt-2 ml-3 text-xs text-muted underline decoration-dotted hover:text-ink"
        >
          {src.ignored ? '↺ 取消排除' : '⊘ 标记为无用来源'}
        </button>
      </header>

      <div className="px-6 py-4">
        <div className="mb-3 text-xs text-muted">
          以下段落来自这篇文章。段号与报告正文里的引用标记一致，可直接对照。
        </div>
        {src.segments.map((s) => (
          <article key={s.id} className="mb-4 border-l-2 border-line pl-3">
            <div className="flex items-center gap-2 text-2xs">
              <code className="rounded bg-gray-100 px-1 py-0.5 font-mono text-ink">
                {s.ref}
              </code>
              {s.heading_path && (
                <span className="truncate text-muted">{s.heading_path}</span>
              )}
              {s.token_count != null && (
                <span className="ml-auto shrink-0 text-muted">{s.token_count} token</span>
              )}
            </div>
            {s.summary && (
              <p className="mt-1 text-xs leading-relaxed text-ink">{s.summary}</p>
            )}
            <p className="mt-1.5 text-xs leading-6 whitespace-pre-wrap text-muted">
              {s.content}
            </p>
          </article>
        ))}
      </div>
    </div>
  )
}
