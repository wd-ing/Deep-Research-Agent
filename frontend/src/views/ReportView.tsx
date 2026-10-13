import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { IgnoredCitation, SegPreview, SegPreviews } from '../types'
import { NatureBadge } from '../components/SegDrawer'

const REF_RE = /\[(S\d+·\d+)\]/g

/** 气泡多久之后出现。鼠标扫过一段文字会连着划过好几个引用，
 *  即时弹出就是一串闪烁的方块——等一小会儿，只为「停下来看」的意图服务。 */
const HOVER_DELAY = 250

/** 引用标记 + hover 预览。
 *
 *  ⚠️ 气泡用 **portal + `position: fixed`**，两个原因缺一不可：
 *  ① 报告在 `overflow-auto` 容器里，`absolute` 的气泡会被裁掉
 *     （贴着视口上下边缘时尤其明显）；fixed 不受祖先 overflow 影响，
 *     前提是祖先链上没有 `transform`（已核对，App.tsx 里没有）。
 *  ② 引用在 markdown 里是段落内联元素，气泡跟着它渲染就成了
 *     `<div>` 塞进 `<p>`——React 的 `validateDOMNesting` 会报警。
 *     portal 到 `document.body` 两个问题一起没有。
 *
 *  **原生 `title` 必须去掉**：留着会和气泡同时出现，
 *  等于给同一句话两个提示，其中一个还要等一秒。
 */
function RefChip({
  ref: segRef, seg, onClick,
}: {
  ref: string
  seg: SegPreview | undefined
  onClick: () => void
}) {
  const [show, setShow] = useState(false)
  const [pos, setPos] = useState({ left: 0, top: 0, below: true })
  const timer = useRef<number | null>(null)

  const open = (e: React.MouseEvent) => {
    const r = e.currentTarget.getBoundingClientRect()
    // 高度先按估算给（3 行文字 + 头部），真放不下就翻到上方
    const h = seg?.概括 ? 190 : 150
    const below = window.innerHeight - r.bottom > h + 12
    setPos({
      left: Math.max(8, Math.min(r.left, window.innerWidth - 400)),
      top: below ? r.bottom + 6 : r.top - 6,
      below,
    })
    timer.current = window.setTimeout(() => setShow(true), HOVER_DELAY)
  }
  const close = () => {
    if (timer.current) { clearTimeout(timer.current); timer.current = null }
    setShow(false)
  }
  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current)
  }, [])

  return (
    <>
      <button
        onMouseEnter={open}
        onMouseLeave={close}
        onClick={onClick}
        // 报告里经常出现 `[S2·1][S21·1]` 这种相邻引用，
        // 不留左右间距会粘成一串 `S2·1S21·1`，看着像乱码。
        className="mx-[1px] rounded bg-blue-50 px-1 text-[0.85em] text-accent underline decoration-dotted hover:bg-blue-100"
      >{segRef}</button>
      {show && seg && createPortal(
        <div
          // `pointer-events-none`：鼠标移向气泡时不会因为进入它而触发 leave，
          // 也不会挡住底下的文字选中。
          className="pointer-events-none fixed z-50 w-[380px] max-w-[calc(100vw-16px)] rounded-md border border-line bg-white px-3 py-2 text-left text-ink shadow-lg"
          style={
            pos.below
              ? { left: pos.left, top: pos.top }
              : { left: pos.left, top: pos.top, transform: 'translateY(-100%)' }
          }
        >
          <div className="flex items-center gap-1.5 text-[11px] text-muted">
            <span className="shrink-0 font-medium text-ink">{segRef}</span>
            {seg.域名 && <span className="truncate">{seg.域名}</span>}
            <NatureBadge nature={seg.性质 || null} />
            <span className="ml-auto shrink-0 tabular-nums">{seg.token} token</span>
          </div>
          {seg.标题 && (
            <div className="mt-1 truncate text-[11px] text-muted">{seg.标题}</div>
          )}
          {seg.概括 ? (
            <p className="mt-1 text-xs leading-5">{seg.概括}</p>
          ) : (
            <p className="mt-1 line-clamp-3 text-xs leading-5 text-muted">{seg.摘录}</p>
          )}
          {seg.已排除 && (
            <div className="mt-1.5 rounded bg-amber-50 px-1.5 py-1 text-[11px] leading-4 text-amber-800">
              ⚠ 这个来源已被你标记为无用
            </div>
          )}
          <div className="mt-1 text-[10px] text-muted">点击看原文</div>
        </div>,
        document.body,
      )}
    </>
  )
}

/** 报告里 `[S1·5]` 是纯文本，要做成可点就得在渲染层拦截。
 *  这里不引 remark 插件——用 components 拦截文本节点自己切，
 *  少一个依赖，也更好控制。 */
function withRefs(
  children: ReactNode,
  onRef: (ref: string) => void,
  previews: SegPreviews,
): ReactNode {
  if (typeof children === 'string') {
    const out: ReactNode[] = []
    let last = 0
    for (const m of children.matchAll(REF_RE)) {
      const i = m.index!
      if (i > last) out.push(children.slice(last, i))
      const ref = m[1]
      out.push(
        <RefChip key={`${ref}-${i}`} ref={ref} seg={previews[ref]}
                 onClick={() => onRef(ref)} />
      )
      last = i + m[0].length
    }
    if (last < children.length) out.push(children.slice(last))
    return out.length ? out : children
  }
  if (Array.isArray(children)) {
    return children.map((c, i) => <span key={i}>{withRefs(c, onRef, previews)}</span>)
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
  markdown, onRef, ignoredCitations = [], onOpenSource, previews = {},
}: {
  markdown: string
  onRef: (ref: string) => void
  ignoredCitations?: IgnoredCitation[]
  onOpenSource?: (id: string) => void
  /** 段号 → 预览。缺了引用就只是没有气泡，引用仍可点开抽屉。 */
  previews?: SegPreviews
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
            h1: ({ children }) => <h1 className="mb-4 text-xl font-semibold">{withRefs(children, onRef, previews)}</h1>,
            h2: ({ children }) => <h2 className="mb-3 mt-8 border-b border-line pb-1 text-base font-semibold">{withRefs(children, onRef, previews)}</h2>,
            h3: ({ children }) => <h3 className="mb-2 mt-5 text-sm font-semibold">{withRefs(children, onRef, previews)}</h3>,
            p: ({ children }) => <p className="my-3">{withRefs(children, onRef, previews)}</p>,
            li: ({ children }) => <li>{withRefs(children, onRef, previews)}</li>,
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
