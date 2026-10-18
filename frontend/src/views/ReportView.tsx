import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { IgnoredCitation, SegPreview, SegPreviews } from '../types'
import { NatureBadge } from '../components/SegDrawer'
import { ProgressBar, TocBar, scanHeadings, type TocItem } from '../components/Toc'

const REF_RE = /\[(S\d+·\d+)\]/g

/** 气泡多久之后出现。鼠标扫过一段文字会连着划过好几个引用，
 *  即时弹出就是一串闪烁的方块——等一小会儿，只为「停下来看」的意图服务。 */
const HOVER_DELAY = 250

/** 段号 → 本报告引用了几次。
 *
 *  ⚠️ 数的是**原文里的 `[S..·..]`**，不是数渲染出来的 chip。两者只有一种情况会差：
 *  代码块——`code` 组件不过 `withRefs`，里面的 `[S2·1]` 会保持纯文本。
 *  所以先把围栏代码块剔掉。行内反引号没剔（react-markdown 的 `code` 同样不转换），
 *  v5 真实报告实测围栏 0 / 反引号 0，这里对得上。
 */
function refCounts(markdown: string): Map<string, number> {
  const m = new Map<string, number>()
  for (const x of markdown.replace(/```[\s\S]*?```/g, '').matchAll(REF_RE)) {
    m.set(x[1], (m.get(x[1]) ?? 0) + 1)
  }
  return m
}

/** 引用被复用的次数 → 三档外观。
 *
 *  v5 真实报告实测：162 处引用 / 93 个去重段号，分布是
 *  **1 次 51 段、2 次 24、3 次 12、4 次 5、7 次 1**。
 *
 *  所以「只出现一次」占 55%，它是**基准**，不需要任何强调；
 *  值得跳出来的是被反复引用的那 6 个段——报告的承重墙。
 *  之前 162 处全是同一种蓝底虚线，等于完全没有信息：
 *  一句话是靠一堆材料堆出来的、还是孤证，全看不出来。 */
function tierClass(n: number): string {
  if (n >= 4) return 'bg-blue-100 font-medium text-blue-900 underline decoration-solid decoration-blue-400 hover:bg-blue-200'
  if (n >= 2) return 'bg-blue-50 text-accent underline decoration-dotted hover:bg-blue-100'
  return 'text-accent underline decoration-dotted hover:bg-blue-50'
}


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
  ref: segRef, seg, count, onClick,
}: {
  ref: string
  seg: SegPreview | undefined
  /** 本报告引用这一段的次数。用来分档 + 在气泡里说明。 */
  count: number
  onClick: () => void
}) {
  const [show, setShow] = useState(false)
  const [pos, setPos] = useState({ left: 0, top: 0, below: true })
  const timer = useRef<number | null>(null)

  // ⚠️ 参数是 `SyntheticEvent` 不是 `MouseEvent`：键盘 Tab 过来的
  //  `onFocus` 也要算同一个位置，`FocusEvent` 同样有 `currentTarget`。
  const open = (e: React.SyntheticEvent, delay = HOVER_DELAY) => {
    const r = e.currentTarget.getBoundingClientRect()
    // 高度先按估算给（3 行文字 + 头部），真放不下就翻到上方
    const h = seg?.概括 ? 190 : 150
    const below = window.innerHeight - r.bottom > h + 12
    setPos({
      left: Math.max(8, Math.min(r.left, window.innerWidth - 400)),
      top: below ? r.bottom + 6 : r.top - 6,
      below,
    })
    timer.current = window.setTimeout(() => setShow(true), delay)
  }
  const close = () => {
    if (timer.current) { clearTimeout(timer.current); timer.current = null }
    setShow(false)
  }
  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current)
  }, [])

  /** 无障碍标签只说「这是什么、指向哪儿、复用了几次」，**不复制正文**——
   *  全文和概括在抽屉里，回车就能读到；塞进 aria-label 只会让屏幕阅读器
   *  把同一段话读两遍。气泡本身不进 a11y 树（`pointer-events-none` 的
   *  portal，和按钮没有 `aria-describedby` 关联），所以标签必须自足。 */
  const ariaLabel = [
    `引用 ${segRef}`,
    seg?.域名 && `来自 ${seg.域名}`,
    count > 1 && `本报告引用 ${count} 次`,
  ].filter(Boolean).join('，')

  return (
    <>
      <button
        onMouseEnter={open}
        onMouseLeave={close}
        // 键盘可达：Tab 到引用就该出气泡，和 hover 同一份逻辑。
        // **焦点不给延迟**——hover 的延迟是为了躲开「扫过」，而 Tab 是明确的停留。
        onFocus={(e) => open(e, 0)}
        onBlur={close}
        // 点击前先收气泡：抽屉一开，气泡(portal 到 body, z-50)会浮在
        // 抽屉(z-40)上面，而鼠标还压在这个引用上，它不会自己消失。
        onClick={() => { close(); onClick() }}
        aria-label={ariaLabel}
        // 报告里经常出现 `[S2·1][S21·1]` 这种相邻引用，
        // 不留左右间距会粘成一串 `S2·1S21·1`，看着像乱码。
        className={`mx-[1px] rounded px-1 text-[0.85em] ${tierClass(count)}`}
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
          <div className="flex items-center gap-1.5 text-2xs text-muted">
            <span className="shrink-0 font-medium text-ink">{segRef}</span>
            {seg.域名 && <span className="truncate">{seg.域名}</span>}
            <NatureBadge nature={seg.性质 || null} />
            <span className="ml-auto shrink-0 tabular-nums">{seg.token} token</span>
          </div>
          {seg.标题 && (
            <div className="mt-1 truncate text-2xs text-muted">{seg.标题}</div>
          )}
          {seg.概括 ? (
            <p className="mt-1 text-xs leading-5">{seg.概括}</p>
          ) : (
            <p className="mt-1 line-clamp-3 text-xs leading-5 text-muted">{seg.摘录}</p>
          )}
          {seg.已排除 && (
            <div className="mt-1.5 rounded bg-amber-50 px-1.5 py-1 text-2xs leading-4 text-amber-800">
              ⚠ 这个来源已被你标记为无用
            </div>
          )}
          <div className="mt-1 flex items-center justify-between gap-2 text-2xs text-muted">
            {/* 只在复用时说——被引一次是常态，标出来反而是噪音 */}
            {count > 1 && <span>本报告引用 {count} 次</span>}
            <span className="ml-auto">点击看原文</span>
          </div>
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
  counts: Map<string, number>,
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
                 count={counts.get(ref) ?? 1} onClick={() => onRef(ref)} />
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
        return <span key={i}>{withRefs(c, onRef, previews, counts)}</span>
      }
      return <Fragment key={i}>{withRefs(c, onRef, previews, counts)}</Fragment>
    })
  }
  return children
}

/** ReactNode 里所有文字，递归取出来（跳过结构）。
 *
 *  用于给标题算 id：markdown 组件拿到的 `children` 可能是一段纯文本，
 *  也可能是 `[文本, <RefChip/>]` 这样的数组，两种都要能取出可比的文本。
 */
function textOf(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (node && typeof node === 'object' && 'props' in node) {
    const p = (node as { props?: { children?: ReactNode } }).props
    return p?.children ? textOf(p.children) : ''
  }
  return ''
}

/** 标题文本 → 目录 id。
 *
 *  ⚠️ **必须渲染时就带上 id，不能事后用 useEffect + setAttribute 补。**
 *  补过一次确实对，但 `previews` 到位后 `mdComponents` 会被重算，
 *  `h2`/`h3` 就成了**新的组件类型**——React 会把整棵标题树卸载重建，
 *  刚补好的 id 全部消失，而那个 effect 的依赖是 `toc`（没变）、不会重跑。
 *  结果就是标题永远没有 id、目录点了没反应。
 *
 *  查表是纯函数，重渲多少次结果都一样，StrictMode 跑两遍也没事。
 */
function headingIds(toc: TocItem[]): Map<string, string> {
  const m = new Map<string, string>()
  for (const t of toc) {
    // 清洗后撞名的标题只记第一个：目录跳到哪一个都说得过去，
    // 但**不能**让后一个覆盖前面的——那会让目录里两项指向同一节。
    if (!m.has(t.text)) m.set(t.text, t.id)
  }
  return m
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
  ids: Map<string, string>,
  counts: Map<string, number>,
  compact = false,
) {
  const R = (c: ReactNode) => withRefs(c, onRef, previews, counts)
  // 标题 id：目录靠它跳转，所以正文里的 `[S2·1]` 也要去掉再比
  const idOf = (c: ReactNode) =>
    ids.get(textOf(c).replace(/\[S\d+·\d+\]/g, '').trim()) ?? undefined
  return {
    h1: ({ children }: { children?: ReactNode }) => (
      <h1 className={compact ? 'mb-2 text-base font-semibold' : 'mb-4 font-serif text-2xl font-medium'}>
        {R(children)}
      </h1>
    ),
    h2: ({ children }: { children?: ReactNode }) => (
      <h2 id={idOf(children)} className={
        compact
          ? 'mb-2 mt-3 scroll-mt-24 text-sm font-semibold'
          // 大环节标题下压一条线：43 个标题滚过去时靠这条线分段。
          // `scroll-mt-24` 是给顶部常驻区（进度条+目录条）让位——
          // 不加的话点目录跳过来，标题正好藏在常驻条底下。
          // font-serif：报告的节标题用衬线是「文档感」的签名件。
          : 'mb-3 mt-9 scroll-mt-24 border-b border-line pb-1.5 font-serif text-xl font-medium'
      }>{R(children)}</h2>
    ),
    // ⚠️ **层级原来是倒置的**：正文 16px，而 h3 只有 14px——小节标题
    // 比它管辖的正文还小。h2 的 16px 又跟正文同号，只靠字重区分，
    // 43 个标题滚过去全糊成一片。
    // 现在：h2 = 18px + 下边线，h3 = 16px 半粗 + 左侧色条。
    // 色条用 `-ml-3 pl-3` **往外借位**——正文左边缘保持对齐，
    // 只把色条放进 `max-w-3xl px-6` 的内边距里（不会溢出、不用改容器）。
    h3: ({ children }: { children?: ReactNode }) => (
      <h3 id={idOf(children)} className={compact
        ? 'scroll-mt-24 text-sm font-medium'
        : '-ml-3 mb-2 mt-6 scroll-mt-24 border-l-2 border-accent/40 pl-3 text-base font-semibold'
      }>{R(children)}</h3>
    ),
    p: ({ children }: { children?: ReactNode }) => (
      <p className={compact ? 'my-2' : 'my-3'}>{R(children)}</p>
    ),
    // `[&::marker]` 把项目符号压到灰：marker 默认继承正文墨色，
    // 一屏 14 个黑点会盖过它们标注的句子。
    ul: ({ children }: { children?: ReactNode }) => (
      <ul className={[compact ? 'my-2 space-y-0.5 pl-5' : 'my-3 space-y-1.5 pl-5',
                      'list-disc [&::marker]:text-gray-400'].join(' ')}>
        {children}
      </ul>
    ),
    ol: ({ children }: { children?: ReactNode }) => (
      <ol className={[compact ? 'my-2 space-y-0.5 pl-5' : 'my-3 space-y-1.5 pl-5',
                      'list-decimal [&::marker]:text-gray-400 [&::marker]:text-xs'].join(' ')}>
        {children}
      </ol>
    ),
    li: ({ children }: { children?: ReactNode }) => <li>{R(children)}</li>,
    // 引用块不是「引别人的话」，是作者自己的主张/前提（v5 里是「本文主张：…」）。
    // 原来 `border-line + text-muted` 把它压成了脚注，这里给回存在感：
    // 蓝色左规 + 极淡的底色 + 正文墨色。
    // 底色用 `bg-accent/5` 而**不是** `bg-accent/[0.035]`——后者 Tailwind
    // 没生成（任意透明度解析不了带前导零的值），构建不报错、样式直接失效。
    blockquote: ({ children }: { children?: ReactNode }) => (
      <blockquote className="my-4 border-l-2 border-accent/50 bg-accent/5
                          py-2.5 pl-4 pr-3 text-ink/90">{children}</blockquote>
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
    // 四边的格子样式在 `index.css` 的 `.rpt-table` 里，检索轮次图共用。
    table: ({ children }: { children?: ReactNode }) => (
      <div className="my-5 overflow-x-auto">
        <table className="rpt-table">{children}</table>
      </div>
    ),
    thead: ({ children }: { children?: ReactNode }) => <thead>{children}</thead>,
    tr: ({ children }: { children?: ReactNode }) => <tr>{children}</tr>,
    th: ({ children }: { children?: ReactNode }) => <th>{children}</th>,
    td: ({ children }: { children?: ReactNode }) => <td>{R(children)}</td>,
  }
}

/** 报告顶部提示条：这份报告引用了已被你标记排除的来源。
 *
 *  **这是整个「标记排除」功能最要紧的一处。** 排除发生在报告写完之后，
 *  报告不会被改写——所以用户必须知道，报告里有一部分材料的来源
 *  他后来标成了不可信。不提示就是让不可信结论悄悄留在那里。
 *
 *  ⚠️ **2026-10-14 改成一次性，不再常驻**（用户定）。
 *  原来它住在常驻区（和进度条、目录条同一个 `sticky` 容器），
 *  理由是「报告有 2 万字，滚到中段就看不见了」。
 *  但实测它有 6~7 行高，在窄窗口下吃掉**约 35% 的阅读高度**——
 *  为一条不常出现的提示，长期让出三成屏幕不划算。
 *  现在跟着正文流走，**滚过就没了**：打开报告时该看见的看见了，
 *  真正在读正文时它不占地方。
 *  要彻底删掉的话，就是把这个组件从 `ReportView` 里拿掉。
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
    <div className="mb-2 rounded-md border border-amber-300 bg-amber-50 px-3.5 py-2.5">
      <div className="text-xs font-medium leading-relaxed text-amber-900">
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
      <p className="mt-1 text-2xs leading-relaxed text-amber-800">
        排除发生在报告写完之后，<b>报告不会被改写</b>。这些材料你后来标为不可信，
        引用处的结论请自行复核；点来源名可直接查看。
      </p>
      {items.some((x) => x.reason) && (
        <ul className="mt-1 space-y-0.5 text-2xs text-amber-700">
          {items.filter((x) => x.reason).map((x) => (
            <li key={x.id}>· {x.domain}：{x.reason}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function ReportView({
  markdown, onRef, ignoredCitations = [], onOpenSource, previews = {}, scrollRef,
}: {
  markdown: string
  onRef: (ref: string) => void
  ignoredCitations?: IgnoredCitation[]
  onOpenSource?: (id: string) => void
  /** 段号 → 预览。缺了引用就只是没有气泡，引用仍可点开抽屉。 */
  previews?: SegPreviews
  /** 外层那个 `overflow-auto` 容器。目录跳转、进度条、当前节高亮
   *  都要拿它算——报告正文不是滚动它自己，是滚动那个容器。 */
  scrollRef: React.RefObject<HTMLDivElement | null>
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
  // ---- 目录 / 进度 / 当前节 ----
  const toc = useMemo(() => scanHeadings(parts.body, parts.doubt), [parts])
  const ids = useMemo(() => headingIds(toc), [toc])
  /** 数的是**整篇**（含存疑节）。分档看的是「这个段被引用几次」，
   *  跨正文和存疑节才是读者心里的那个数。 */
  const counts = useMemo(() => refCounts(markdown), [markdown])
  const [openToc, setOpenToc] = useState(false)
  const [activeId, setActiveId] = useState('')
  const [pct, setPct] = useState(0)

  const bodyMd = useMemo(
    () => mdComponents(onRef, previews, ids, counts), [onRef, previews, ids, counts])
  const doubtMd = useMemo(
    () => mdComponents(onRef, previews, ids, counts, true), [onRef, previews, ids, counts])

  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const onScroll = () => {
      const max = el.scrollHeight - el.clientHeight
      setPct(max <= 0 ? 0 : Math.min(100, Math.round((el.scrollTop / max) * 100)))
      // 当前节 = 最后一个越过「距容器顶 96px」这条线的标题。
      // 96px 是给常驻的进度条+目录条留的位置，标题不能算在它底下。
      const top = el.getBoundingClientRect().top + 96
      let cur = ''
      for (const t of toc) {
        const h = el.querySelector(`#${t.id}`)
        if (h && h.getBoundingClientRect().top <= top) cur = t.id
      }
      setActiveId(cur)
    }
    onScroll()
    el.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('resize', onScroll)
    return () => {
      el.removeEventListener('scroll', onScroll)
      window.removeEventListener('resize', onScroll)
    }
  }, [scrollRef, toc])

  // ---- 查找条（C2）----
  // 用 CSS Custom Highlight API，不去 React 渲染的 DOM 里插 <mark>——
  // 动了那棵树，React 下次重渲会对不上。Range + ::highlight 是纯叠加层。
  const [findOpen, setFindOpen] = useState(false)
  const [findQ, setFindQ] = useState('')
  const [matches, setMatches] = useState<Range[]>([])
  const [cur, setCur] = useState(0)
  const contentRef = useRef<HTMLDivElement>(null)

  const clearFind = () => {
    const hi = (CSS as unknown as { highlights?: Map<string, unknown> }).highlights
    hi?.delete('report-find'); hi?.delete('report-find-cur')
  }

  const scrollToMatch = (i: number, all: Range[]) => {
    const r = all[i]
    if (!r) return
    setCur(i)
    const hi = (CSS as unknown as { highlights?: Map<string, unknown> }).highlights
    const Hl = (window as unknown as { Highlight?: new (...r: Range[]) => unknown }).Highlight
    if (hi && Hl) hi.set('report-find-cur', new Hl(r))
    const el = r.startContainer.parentElement
    el?.scrollIntoView({ block: 'center' })
  }

  useEffect(() => {
    if (!findOpen || !findQ.trim()) {
      clearFind(); setMatches([]); setCur(0)
      return
    }
    const root = contentRef.current
    if (!root) return
    const q = findQ.toLowerCase()
    const ranges: Range[] = []
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = (node.textContent ?? '').toLowerCase()
      for (let i = text.indexOf(q); i >= 0; i = text.indexOf(q, i + q.length)) {
        const r = new Range()
        r.setStart(node, i); r.setEnd(node, i + q.length)
        ranges.push(r)
      }
    }
    setMatches(ranges)
    const hi = (CSS as unknown as { highlights?: Map<string, unknown> }).highlights
    const Hl = (window as unknown as { Highlight?: new (...r: Range[]) => unknown }).Highlight
    if (hi && Hl && ranges.length) hi.set('report-find', new Hl(...ranges))
    if (ranges.length) scrollToMatch(0, ranges)
    return clearFind
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [findQ, findOpen])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === 'f' || e.key === 'F')) {
        // 报告在自己的滚动容器里，浏览器查找条管不到它的视觉反馈
        e.preventDefault()
        setFindOpen(true)
      }
      if (e.key === 'Escape') setFindOpen(false)
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      clearFind()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const jump = (id: string) => {
    const h = scrollRef.current?.querySelector(`#${id}`)
    if (!h) return
    // 用户在系统里关了动效就别滑——全局 CSS 也压了 transition，
    // 但 scroll-behavior 不在那条规则里，得显式判断
    const smooth = !window.matchMedia('(prefers-reduced-motion: reduce)').matches
    h.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: 'start' })
    setOpenToc(false)
  }

  return (
    <>
      {/* 进度条 + 目录，合成**一个**常驻区。
          原来 IgnoredBanner 也在里面——那是「常驻」的由来：
          它有 6~7 行高，在窄窗口下吃掉约 35% 的阅读高度，
          而报告有 2 万字、要读很久。2026-10-14 用户定：提示不常驻。 */}
      <div className="sticky top-0 z-20 bg-white/95 backdrop-blur print:hidden">
        <ProgressBar pct={pct} />
        <div className="mx-auto flex max-w-3xl items-center px-6">
          <div className="min-w-0 flex-1">
            <TocBar
              toc={toc} active={activeId} open={openToc}
              onToggle={() => setOpenToc((v) => !v)} onJump={jump}
            />
          </div>
          <button
            onClick={() => setFindOpen(true)}
            className="ml-2 shrink-0 rounded px-1.5 py-0.5 text-2xs text-muted hover:bg-gray-100 hover:text-ink"
            title="在报告中查找（⌘F / Ctrl+F）"
          >
            查找
          </button>
        </div>
      </div>

      {/* 查找条（C2）：浮动在右上，不进正文流。带计数和上一个/下一个，
          匹配高亮走 CSS.highlights 叠加层，不碰 React 的 DOM。 */}
      {findOpen && (
        <div className="fixed right-6 top-[72px] z-30 flex items-center gap-1 rounded-md border border-line bg-white px-2 py-1 shadow-md print:hidden">
          <input
            autoFocus
            value={findQ}
            onChange={(e) => setFindQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && matches.length) {
                scrollToMatch(
                  e.shiftKey ? (cur - 1 + matches.length) % matches.length
                             : (cur + 1) % matches.length, matches)
              }
              if (e.key === 'Escape') setFindOpen(false)
            }}
            placeholder="在报告中查找"
            className="w-44 bg-transparent text-xs outline-none"
          />
          <span className="shrink-0 text-2xs tabular-nums text-muted">
            {findQ.trim() ? (matches.length ? `${cur + 1}/${matches.length}` : '无匹配') : ''}
          </span>
          <button
            onClick={() => matches.length && scrollToMatch(
              (cur - 1 + matches.length) % matches.length, matches)}
            className="rounded px-1 text-xs text-muted hover:bg-gray-100 hover:text-ink"
            title="上一个（⇧⏎）"
          >↑</button>
          <button
            onClick={() => matches.length && scrollToMatch(
              (cur + 1) % matches.length, matches)}
            className="rounded px-1 text-xs text-muted hover:bg-gray-100 hover:text-ink"
            title="下一个（⏎）"
          >↓</button>
          <button
            onClick={() => setFindOpen(false)}
            className="rounded px-1 text-xs text-muted hover:bg-gray-100 hover:text-ink"
          >×</button>
        </div>
      )}

      <div ref={contentRef} className="mx-auto max-w-3xl px-6 pb-8 pt-4">
        {/* 排除提示改成**一次性**：跟着正文流走，滚过去就没了。
            仍然保留是因为它是唯一一处告诉用户「这份报告引用了你后来
            标为不可信的材料」的地方，直接删掉等于让不可信结论悄悄留着。 */}
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

      {/* 回到顶部（E5）：报告常到 4 万字，读到底要回目录只能硬滚。
          滚过 ~10% 才出现——刚打开时屏幕上没有它需要挡的东西。
          滚的是外层容器（scrollRef），不是 window。 */}
      <button
        onClick={() => {
          const el = scrollRef.current
          if (!el) return
          const smooth = !window.matchMedia('(prefers-reduced-motion: reduce)').matches
          el.scrollTo({ top: 0, behavior: smooth ? 'smooth' : 'auto' })
        }}
        aria-hidden={pct < 10}
        tabIndex={pct < 10 ? -1 : 0}
        className={[
          'fixed bottom-6 right-6 z-30 flex h-9 w-9 items-center justify-center',
          'rounded-full border border-line bg-white text-muted shadow-md',
          'transition-all duration-200 hover:text-ink print:hidden',
          pct < 10 ? 'pointer-events-none translate-y-2 opacity-0' : 'opacity-100',
        ].join(' ')}
        title="回到顶部"
      >↑</button>
    </>
  )
}
