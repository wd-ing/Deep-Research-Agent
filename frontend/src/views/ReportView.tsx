import { useMemo, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { IgnoredCitation } from '../types'

const REF_RE = /\[(S\d+·\d+)\]/g

/** 报告里 `[S1·5]` 是纯文本，要做成可点就得在渲染层拦截。
 *  这里不引 remark 插件——用 components 拦截文本节点自己切，
 *  少一个依赖，也更好控制。 */
function withRefs(children: ReactNode, onRef: (ref: string) => void): ReactNode {
  if (typeof children === 'string') {
    const out: ReactNode[] = []
    let last = 0
    for (const m of children.matchAll(REF_RE)) {
      const i = m.index!
      if (i > last) out.push(children.slice(last, i))
      const ref = m[1]
      out.push(
        <button
          key={`${ref}-${i}`}
          onClick={() => onRef(ref)}
          // 报告里经常出现 `[S2·1][S21·1]` 这种相邻引用，
          // 不留左右间距会粘成一串 `S2·1S21·1`，看着像乱码。
          className="mx-[1px] rounded bg-blue-50 px-1 text-[0.85em] text-accent underline decoration-dotted hover:bg-blue-100"
          title={`查看 ${ref} 的原文`}
        >
          {ref}
        </button>,
      )
      last = i + m[0].length
    }
    if (last < children.length) out.push(children.slice(last))
    return out.length ? out : children
  }
  if (Array.isArray(children)) {
    return children.map((c, i) => <span key={i}>{withRefs(c, onRef)}</span>)
  }
  return children
}

/** 报告顶部提示条：这份报告引用了已被你标记排除的来源。
 *
 *  **这是整个「标记排除」功能最要紧的一处。** 排除发生在报告写完之后，
 *  报告不会被改写——所以用户必须知道，报告里有一部分材料的来源
 *  他后来标成了不可信。不提示就是让不可信结论悄悄留在那里。
 *
 *  报告有 2 万字，提示条若只挂在正文最上方，用户滚到中段读到那些
 *  段落时就看不见了，所以 `sticky` 常驻在滚动容器顶部。
 */
function IgnoredBanner({
  items, onOpenSource,
}: {
  items: IgnoredCitation[]
  onOpenSource: (id: string) => void
}) {
  if (!items.length) return null
  const total = items.reduce((a, x) => a + x.citations, 0)
  return (
    <div className="sticky top-0 z-10 mb-5 rounded-md border border-amber-300 bg-amber-50 px-3.5 py-2.5">
      <div className="text-[13px] font-medium leading-relaxed text-amber-900">
        ⚠ 本报告引用了 {items.length} 个已排除的来源
        <span className="font-normal">（共 {total} 处引用）：</span>
        {items.map((it, i) => (
          <span key={it.id}>
            {i > 0 && '、'}
            <button
              onClick={() => onOpenSource(it.id)}
              title={it.reason ? `排除原因：${it.reason}` : '在来源库中查看'}
              className="underline decoration-dotted underline-offset-2 hover:text-amber-700"
            >
              {it.domain || '(无域名)'}
            </button>
          </span>
        ))}
      </div>
      <p className="mt-1 text-[11px] leading-relaxed text-amber-800">
        排除发生在报告写完之后，<b>报告不会被改写</b>。这些材料你后来标为不可信，
        引用处的结论请自行复核；点来源名可直接查看。
      </p>
      {items.some((x) => x.reason) && (
        <ul className="mt-1 space-y-0.5 text-[11px] text-amber-700">
          {items.filter((x) => x.reason).map((x) => (
            <li key={x.id}>· {x.domain}：{x.reason}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function ReportView({
  markdown, onRef, ignoredCitations = [], onOpenSource,
}: {
  markdown: string
  onRef: (ref: string) => void
  ignoredCitations?: IgnoredCitation[]
  onOpenSource?: (id: string) => void
}) {
  const parts = useMemo(() => {
    // 「存疑与未完成」是系统主动声明的弱点，要和正文视觉区分开
    const i = markdown.indexOf('## 存疑与未完成')
    if (i < 0) return { body: markdown, doubt: '' }
    return { body: markdown.slice(0, i), doubt: markdown.slice(i) }
  }, [markdown])

  return (
    <div className="mx-auto max-w-3xl px-6 py-8">
      <IgnoredBanner items={ignoredCitations} onOpenSource={onOpenSource ?? (() => {})} />
      <article className="prose-sm leading-7 text-ink">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            h1: ({ children }) => <h1 className="mb-4 text-xl font-semibold">{withRefs(children, onRef)}</h1>,
            h2: ({ children }) => <h2 className="mb-3 mt-8 border-b border-line pb-1 text-base font-semibold">{withRefs(children, onRef)}</h2>,
            h3: ({ children }) => <h3 className="mb-2 mt-5 text-sm font-semibold">{withRefs(children, onRef)}</h3>,
            p: ({ children }) => <p className="my-3">{withRefs(children, onRef)}</p>,
            li: ({ children }) => <li>{withRefs(children, onRef)}</li>,
            blockquote: ({ children }) => (
              <blockquote className="my-3 border-l-2 border-line pl-3 text-muted">{children}</blockquote>
            ),
            code: ({ children }) => (
              <code className="rounded bg-gray-100 px-1 py-0.5 text-[0.9em]">{children}</code>
            ),
          }}
        >
          {parts.body}
        </ReactMarkdown>
      </article>

      {parts.doubt && (
        <section className="mt-10 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3">
          <div className="mb-2 text-xs font-medium text-amber-900">
            以下是系统自己声明的存疑与未完成，和上面的正文可信度不同
          </div>
          <article className="text-sm leading-6 text-amber-950">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                h2: ({ children }) => <h2 className="mb-2 text-sm font-semibold">{children}</h2>,
                h3: ({ children }) => <h3 className="text-sm font-medium">{children}</h3>,
                p: ({ children }) => <p className="my-2">{children}</p>,
                li: ({ children }) => <li>{children}</li>,
              }}
            >
              {parts.doubt}
            </ReactMarkdown>
          </article>
        </section>
      )}
    </div>
  )
}
