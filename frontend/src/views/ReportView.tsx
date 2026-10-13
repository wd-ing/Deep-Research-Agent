import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
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
    // ⚠️ **只给文本节点套 span。** 数组里可能混着块级元素——比如嵌套列表的
    // `li > ul`，给 `<ul>` 套 span 就是 `<span><ul>`，React 会报
    // validateDOMNesting。元素原样放行（`withRefs` 对元素直接返回它本身）。
    return children.map((c, i) => {
      if (typeof c === 'string' || typeof c === 'number') {
        return <span key={i}>{withRefs(c, onRef, previews)}</span>
      }
      return <Fragment key={i}>{withRefs(c, onRef, previews)}</Fragment>
    })
  }
  return children
}

/** 报告里出现的 markdown 元素的统一样式。
 *
 *  ⚠️ 原来 `<article>` 上写的是 `prose-sm`，但 `@tailwindcss/typography`
 *  **从来没安装过**——`node_modules/@tailwindcss/` 目录都不存在，构建产物里
 *  一条 `.prose*` 规则都没生成。那个类一直在骗人：看着像有排版，实际是空的。
 *
 *  不装依赖、改手写的理由：项目只需要表格和列表两类，而 typography 会带进
 *  一整套 `.prose-*` 工具类，跟现有 Tailwind 配置打架。
 *
 *  v5 真实报告实测：5 行表格、14 个无序项、5 个有序项、1 个引用块、128 处加粗。
 *  之前 `table/th/td` 全都没被接管，而 Tailwind preflight 对表格只给了
 *  `table{border-collapse:collapse}`——**没有边框、没有单元格内边距**，
 *  那张表渲染成一坨挤在一起的文字。 */
function mdComponents(
  onRef: (ref: string) => void,
  previews: SegPreviews,
  compact = false,
) {
  const R = (c: ReactNode) => withRefs(c, onRef, previews)
  return {
    h1: ({ children }: { children?: ReactNode }) => (
      <h1 className={compact ? 'mb-2 text-base font-semibold' : 'mb-4 text-xl font-semibold'}>
        {R(children)}
      </h1>
    ),
    h2: ({ children }: { children?: ReactNode }) => (
      <h2 className={
        compact
          ? 'mb-2 mt-3 text-sm font-semibold'
          // 大环节标题下压一条线：43 个标题滚过去时靠这条线分段
          : 'mb-3 mt-8 border-b border-line pb-1 text-base font-semibold'
      }>{R(children)}</h2>
    ),
    h3: ({ children }: { children?: ReactNode }) => (
      <h3 className={compact ? 'text-sm font-medium' : 'mb-2 mt-5 text-sm font-semibold'}>
        {R(children)}
      </h3>
    ),
    p: ({ children }: { children?: ReactNode }) => (
      <p className={compact ? 'my-2' : 'my-3'}>{R(children)}</p>
    ),
    ul: ({ children }: { children?: ReactNode }) => (
      <ul className={compact ? 'my-2 list-disc space-y-0.5 pl-5' : 'my-3 list-disc space-y-1 pl-5'}>
        {children}
      </ul>
    ),
    ol: ({ children }: { children?: ReactNode }) => (
      <ol className={compact ? 'my-2 list-decimal space-y-0.5 pl-5' : 'my-3 list-decimal space-y-1 pl-5'}>
        {children}
      </ol>
    ),
    li: ({ children }: { children?: ReactNode }) => <li>{R(children)}</li>,
    blockquote: ({ children }: { children?: ReactNode }) => (
      <blockquote className="my-3 border-l-2 border-line pl-3 text-muted">{children}</blockquote>
    ),
    code: ({ children }: { children?: ReactNode }) => (
      <code className="rounded bg-gray-100 px-1 py-0.5 text-[0.9em]">{children}</code>
    ),
    /** 外链一律新标签页打开并断 `opener`——报告是长文，来回切标签很容易，
     *  不带 `rel="noreferrer"` 还会把来源页面的 window 句柄交出去。 */
    a: ({ children, href }: { children?: ReactNode; href?: string }) => (
      <a href={href} target="_blank" rel="noreferrer"
         className="text-accent underline decoration-dotted underline-offset-2">
        {children}
      </a>
    ),
    strong: ({ children }: { children?: ReactNode }) => (
      <strong className="font-semibold">{children}</strong>
    ),
    hr: () => <hr className="my-6 border-line" />,
    /** 表格：preflight 不给任何边框和内边距，这四个必须成套接管。
     *  外层 `overflow-x-auto`——报告正文是 `max-w-3xl`，宽表硬撑会把
     *  整篇的排版挤变形。 */
    table: ({ children }: { children?: ReactNode }) => (
      <div className="my-4 overflow-x-auto">
        <table className="w-full border-collapse text-sm">{children}</table>
      </div>
    ),
    thead: ({ children }: { children?: ReactNode }) => <thead>{children}</thead>,
    tr: ({ children }: { children?: ReactNode }) => <tr>{children}</tr>,
    th: ({ children }: { children?: ReactNode }) => (
      <th className="border border-line bg-gray-50 px-3 py-2 text-left font-medium">{children}</th>
    ),
    td: ({ children }: { children?: ReactNode }) => (
      <td className="border border-line px-3 py-2 align-top">{R(children)}</td>
    ),
  }
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

  // ⚠️ `compact` 只改标题/段落的尺寸，**引用拦截两段都用**。
  //  原来存疑节那一整套 components 是单独写的、没套 `withRefs`——
  //  那节里的 `[S12·3]` 全是死文本。而它恰恰是全报告最长的一节
  //  （v5 实测 5,221 字符），引用密度不低。
  const bodyMd = useMemo(() => mdComponents(onRef, previews), [onRef, previews])
  const doubtMd = useMemo(() => mdComponents(onRef, previews, true), [onRef, previews])

  return (
    <div className="mx-auto max-w-3xl px-6 py-8">
      <IgnoredBanner items={ignoredCitations} onOpenSource={onOpenSource ?? (() => {})} />
      {/* ⚠️ 这里原来写的是 `prose-sm`——那个类从未生效，见 mdComponents 的说明 */}
      <article className="leading-7 text-ink">
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={bodyMd}>
          {parts.body}
        </ReactMarkdown>
      </article>

      {parts.doubt && (
        <section className="mt-10 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3">
          <div className="mb-2 text-xs font-medium text-amber-900">
            以下是系统自己声明的存疑与未完成，和上面的正文可信度不同
          </div>
          <article className="text-sm leading-6 text-amber-950">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={doubtMd}>
              {parts.doubt}
            </ReactMarkdown>
          </article>
        </section>
      )}
    </div>
  )
}
