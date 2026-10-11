import { useMemo, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

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

export function ReportView({
  markdown, onRef,
}: {
  markdown: string
  onRef: (ref: string) => void
}) {
  const parts = useMemo(() => {
    // 「存疑与未完成」是系统主动声明的弱点，要和正文视觉区分开
    const i = markdown.indexOf('## 存疑与未完成')
    if (i < 0) return { body: markdown, doubt: '' }
    return { body: markdown.slice(0, i), doubt: markdown.slice(i) }
  }, [markdown])

  return (
    <div className="mx-auto max-w-3xl px-6 py-8">
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
