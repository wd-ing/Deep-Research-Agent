import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { IgnoredCitation, ReviewPatch, ReviewResp, SegPreview, SegPreviews, SourceRow } from '../types'
import { NatureBadge } from '../components/SegDrawer'
import { ProgressBar, TocBar, scanHeadings, type TocItem } from '../components/Toc'
import { Icon } from '../components/Icon'

const REF_RE = /\[(S\d+·\d+)\]/g

// 修订痕迹的字符串哨兵（私用区字符）：markdown 解析器把它们当普通
// 文本原样穿过，又绝不会和真实报告内容撞车。del/ins 的标记区间
// 在 `withRefs` 里转成元素，引用 chip 的切分不受影响。
const M_DEL = '\uE000', M_INS = '\uE001', M_END = '\uE002', M_SEP = '\uE003'
const MARK_RE = /\uE000(\d+)\uE003([\s\S]*?)\uE002|\uE001(\d+)\uE003([\s\S]*?)\uE002/g

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
/** 剥掉写作子图泄漏进报告的装配痕迹：
 *  - `# secN` 任务标题行（是内部任务名，不是报告内容）
 *  - 包着 `# secN` 的 ```markdown 围栏对（剥后内容恢复正常渲染，
 *    否则整节显示成一大坨等宽代码块）
 *  存量报告（v13 及以前）带这个残留；后端 assemble 已修但旧数据
 *  不会重写，渲染层兜一道。 */
function cleanReportMd(md: string): string {
  const SEC_HDR = /^#\s*sec\d/
  const lines = md.split('\n')
  const out: string[] = []
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i]
    if (/^```(?:markdown)?\s*$/.test(l)) {
      let j = i + 1
      while (j < lines.length && !/^```\s*$/.test(lines[j])) j++
      const block = lines.slice(i + 1, j)
      if (j < lines.length && block.some((b) => SEC_HDR.test(b))) {
        out.push(...block.filter((b) => !SEC_HDR.test(b)))
        i = j
        continue
      }
    }
    if (SEC_HDR.test(l)) continue
    out.push(l)
  }
  return out.join('\n')
}

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
  if (n >= 4) return 'cite t3'
  if (n >= 2) return 'cite t2'
  return 'cite'
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
  ref: segRef, seg, count, onClick, rail, onRail, onRailLeave,
}: {
  ref: string
  seg: SegPreview | undefined
  /** 本报告引用这一段的次数。用来分档 + 在气泡里说明。 */
  count: number
  onClick: () => void
  /** rail 显示时（主区够宽）悬停预览不进浮层，改投到右侧栏。
   *  窄屏 rail 不存在 → 两个回调为空，走原来的气泡。 */
  rail?: boolean
  onRail?: (rect: DOMRect, seg: SegPreview, ref: string) => void
  onRailLeave?: () => void
}) {
  const [show, setShow] = useState(false)
  const [pos, setPos] = useState({ left: 0, top: 0, below: true })
  const timer = useRef<number | null>(null)

  // ⚠️ 参数是 `SyntheticEvent` 不是 `MouseEvent`：键盘 Tab 过来的
  //  `onFocus` 也要算同一个位置，`FocusEvent` 同样有 `currentTarget`。
  const open = (e: React.SyntheticEvent, delay = HOVER_DELAY) => {
    const r = e.currentTarget.getBoundingClientRect()
    // rail 模式：延迟一样保留（扫过一排引用不该闪卡），只是终点不同
    if (rail && seg && onRail) {
      timer.current = window.setTimeout(() => onRail(r, seg, segRef), delay)
      return
    }
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
    if (rail) onRailLeave?.()
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
        className={tierClass(count)}
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
            <div className="mt-1.5 rounded bg-seal/10 px-1.5 py-1 text-2xs leading-4 text-seal">
              <Icon name="alert" size={10} className="mb-px mr-0.5 inline-block" />这个来源已被你标记为无用
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

/** 修订痕迹标记的上下文。rail 显示时 ins 不用 title 兜底
 *  （问题写在朱批卡里）；窄屏没有 rail → title=problem。 */
interface ChipCtx {
  rail: boolean
  problems: string[]
  onRail?: (rect: DOMRect, seg: SegPreview, ref: string) => void
  onRailLeave?: () => void
}

/** 一段纯文本 → [文字, RefChip, …]。抽出是因为正文和 del/ins 里
 *  的哨兵内容都要走同一套切分（del 里旧句的 [S..] 也保留引用语义）。 */
function refSplit(
  s: string, keyBase: string, onRef: (ref: string) => void,
  previews: SegPreviews, counts: Map<string, number>, chip: ChipCtx,
): ReactNode[] {
  const out: ReactNode[] = []
  let last = 0
  for (const m of s.matchAll(REF_RE)) {
    const i = m.index!
    if (i > last) out.push(s.slice(last, i))
    const ref = m[1]
    out.push(
      <RefChip key={`${keyBase}-${i}`} ref={ref} seg={previews[ref]}
               count={counts.get(ref) ?? 1} onClick={() => onRef(ref)}
               rail={chip.rail} onRail={chip.onRail}
               onRailLeave={chip.onRailLeave} />
    )
    last = i + m[0].length
  }
  if (last < s.length) out.push(s.slice(last))
  return out
}

/** 报告里 `[S1·5]` 是纯文本，要做成可点就得在渲染层拦截。
 *  这里不引 remark 插件——用 components 拦截文本节点自己切，
 *  少一个依赖，也更好控制。哨兵（M_DEL/M_INS 区间）同层处理：
 *  先按哨兵切段，段内再走引用切分。 */
function withRefs(
  children: ReactNode,
  onRef: (ref: string) => void,
  previews: SegPreviews,
  counts: Map<string, number>,
  chip: ChipCtx,
): ReactNode {
  if (typeof children === 'string') {
    const s = children
    const out: ReactNode[] = []
    let last = 0
    for (const m of s.matchAll(MARK_RE)) {
      const i = m.index!
      if (i > last) out.push(...refSplit(s.slice(last, i), `t${i}`, onRef, previews, counts, chip))
      const isDel = m[1] !== undefined
      const idx = Number(isDel ? m[1] : m[3])
      const inner = refSplit(isDel ? m[2] : m[4], `m${idx}`, onRef, previews, counts, chip)
      out.push(isDel
        ? <del key={`d${i}`} data-mark={idx} className="rpt-del">{inner}</del>
        // rail 显示时朱批卡写了 problem，title 就只给窄屏兜底
        : <ins key={`i${i}`} data-mark={idx} className="rpt-ins"
               title={chip.rail ? undefined : chip.problems[idx]}>{inner}</ins>)
      last = i + m[0].length
    }
    if (last < s.length) out.push(...refSplit(s.slice(last), 'e', onRef, previews, counts, chip))
    return out.length ? out : children
  }
  if (Array.isArray(children)) {
    // ⚠️ **只给文本节点套 span。** 数组里可能混着块级元素——比如嵌套列表的
    // `li > ul`，给 `<ul>` 套 span 就是 `<span><ul>`，React 会报
    // validateDOMNesting。元素原样放行（`withRefs` 对元素直接返回它本身）。
    return children.map((c, i) => {
      if (typeof c === 'string' || typeof c === 'number') {
        return <span key={i}>{withRefs(c, onRef, previews, counts, chip)}</span>
      }
      return <Fragment key={i}>{withRefs(c, onRef, previews, counts, chip)}</Fragment>
    })
  }
  return children
}

/** 把补丁明细变成正文里的哨兵区间。
 *
 *  定位优先级：`before+new`（before 是 old 前 40 字，edit 应用后
 *  `before` 和 `new` 在定稿里是相邻的，组合锚最稳）→ 单独 `new`；
 *  delete 型在 `before` 锚点之后插 del。找不到就放弃行内标记——
 *  补丁在 markdown 里失配多半是标题重编号/二次补丁改了上下文，
 *  硬标会标错位置，不如进 rail 末尾「未能定位」。
 *  哨兵替换按位置**从后往前**插，索引不会互相错位。
 *  ⚠️ 哨兵区间里的 old/new 必须 **escape 全部 ASCII 标点**再放进
 *  markdown——不转义的话，补丁里若含 `**`/`[x](y)`/`` ` `` 之类，
 *  remark 会把这段拆成多个节点，哨兵首尾落在不同文本节点里，
 *  MARK_RE 匹配不上 → 标记静默消失（连「未能定位」都不报）。
 *  转义后区间保证是**一整段字面文本节点**。
 *  `[S2·1]` 不受影响：`\[` 渲染回字面 `[`，REF_RE 照旧切 chip。
 *  空白也压成单空格：payload 带 `\n` 时 remark 会生成 break 节点，
 *  同样把哨兵区间劈开——标记是视觉注释，丢换行无所谓，
 *  丢标记不行（会变成静默消失的「未能定位」）。 */
const escPunct = (s: string) =>
  s.replace(/[!"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~]/g, '\\$&')
   .replace(/\s+/g, ' ')

function applyMarks(
  body: string, patches: ReviewPatch[],
): { md: string; located: Set<number> } {
  const ops: { at: number; len: number; text: string }[] = []
  const located = new Set<number>()
  patches.forEach((p, i) => {
    if (p.action === 'delete' || !p.new) {
      let at = -1
      if (p.before) {
        const j = body.indexOf(p.before)
        if (j >= 0) at = j + p.before.length
      }
      // before 丢了就退到 old 原位——delete 的 old 本来就是正文里的文字
      if (at < 0 && p.old) at = body.indexOf(p.old)
      if (at >= 0) {
        ops.push({ at, len: 0, text: `${M_DEL}${i}${M_SEP}${escPunct(p.old)}${M_END}` })
        located.add(i)
      }
    } else {
      let at = -1
      if (p.before && p.new) {
        const j = body.indexOf(p.before + p.new)
        if (j >= 0) at = j + p.before.length
      }
      if (at < 0) at = body.indexOf(p.new)
      if (at >= 0) {
        ops.push({
          at, len: p.new.length,
          text: `${M_DEL}${i}${M_SEP}${escPunct(p.old)}${M_END}${M_INS}${i}${M_SEP}${escPunct(p.new)}${M_END}`,
        })
        located.add(i)
      }
    }
  })
  // 哨兵插入点落在另一条补丁的替换区间里时，后应用的替换会把它
  // 整个吞掉（交叉补丁/合成数据才会这样）——把纯插入挪到该区间
  // 末尾：del 紧挨着宿主 ins 之后，正好是旧文本原本相邻的位置。
  const repls = ops.filter((o) => o.len > 0)
  for (const o of ops) {
    if (o.len === 0) {
      const host = repls.find((b) => b.at <= o.at && o.at < b.at + b.len)
      if (host) o.at = host.at + host.len
    }
  }
  ops.sort((a, b) => b.at - a.at)
  let md = body
  for (const o of ops) md = md.slice(0, o.at) + o.text + md.slice(o.at + o.len)
  return { md, located }
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
  chip: ChipCtx,
  compact = false,
) {
  const R = (c: ReactNode) => withRefs(c, onRef, previews, counts, chip)
  // 标题 id：目录靠它跳转，所以正文里的 `[S2·1]` 也要去掉再比
  const idOf = (c: ReactNode) =>
    ids.get(textOf(c).replace(/\[S\d+·\d+\]/g, '').trim()) ?? undefined
  return {
    h1: ({ children }: { children?: ReactNode }) => (
      compact ? (
        <h1 className="mb-2 text-base font-semibold">{R(children)}</h1>
      ) : (
        // 稿子的报告题：30px/600/-0.01em + 题下刻度线。
        // 刻度线跟着 h1 走而不是写死在正文里——报告不一定总以 h1 开头。
        <div className="mb-6">
          <h1 className="text-[30px] font-semibold leading-[1.3] tracking-[-.01em]">
            {R(children)}
          </h1>
          <div className="ruler mt-3 w-[200px]" />
        </div>
      )
    ),
    h2: ({ children }: { children?: ReactNode }) => (
      <h2 id={idOf(children)} className={
        compact
          ? 'mb-2 mt-3 scroll-mt-24 text-sm font-semibold'
          // 稿子 h2=21px、无分割线——靠字距和上下空白分段，
          // 不再用旧式的压线。
          // `scroll-mt-24` 给顶部常驻区（进度条+目录条）让位。
          : 'mb-1 mt-[26px] scroll-mt-24 text-[21px] font-semibold tracking-[-.005em]'
      }>{R(children)}</h2>
    ),
    // 稿子 h3=16px/600，与 h2 靠尺寸和间距分层，不加左侧色条。
    h3: ({ children }: { children?: ReactNode }) => (
      <h3 id={idOf(children)} className={compact
        ? 'scroll-mt-24 text-sm font-medium'
        : 'mb-2.5 mt-2 scroll-mt-24 text-base font-semibold'
      }>{R(children)}</h3>
    ),
    p: ({ children }: { children?: ReactNode }) => (
      <p className={compact ? 'my-2' : 'mb-[18px]'}>{R(children)}</p>
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
    // 稿子 .lead 的规格就是给它的：2px 墨色竖线 + 18px 内边距，无底色——
    // 「本文主张」是自己的论点不是引文，底色只会把它压成注释。
    blockquote: ({ children }: { children?: ReactNode }) => (
      <blockquote className="my-[26px] border-l-2 border-ink py-0.5 pl-[18px] text-ink">{children}</blockquote>
    ),
    code: ({ children }: { children?: ReactNode }) => (
      <code className="rounded bg-paper2 px-1.5 py-0.5 text-[0.9em] text-ink/85">{children}</code>
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
  // 已排除来源提示 = 需要复核的告警，朱红一族（seal），不再用琥珀
  return (
    <div className="mb-2 rounded-md border border-seal/35 bg-seal/5 px-3.5 py-2.5">
      <div className="text-xs font-medium leading-relaxed text-seal">
        <Icon name="alert" size={12} className="mb-px mr-1 inline-block" />本报告引用了 {items.length} 个已排除的来源
        <span className="font-normal">（共 {total} 处引用）：</span>
        {items.map((it, i) => (
          <span key={it.id}>
            {i > 0 && '、'}
            <button
              onClick={() => onOpenSource(it.id)}
              title={it.reason ? `排除原因：${it.reason}` : '在来源库中查看'}
              className="underline decoration-dotted underline-offset-2 hover:text-seal"
            >
              {it.domain || '(无域名)'}
            </button>
          </span>
        ))}
      </div>
      <p className="mt-1 text-2xs leading-relaxed text-seal/80">
        排除发生在报告写完之后，<b>报告不会被改写</b>。这些材料你后来标为不可信，
        引用处的结论请自行复核；点来源名可直接查看。
      </p>
      {items.some((x) => x.reason) && (
        <ul className="mt-1 space-y-0.5 text-2xs text-seal/70">
          {items.filter((x) => x.reason).map((x) => (
            <li key={x.id}>· {x.domain}：{x.reason}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** 「句子带引用」占比的口径（稿子健康条第一项）：
 *  只数 `parts.body`（已剔除「存疑与未完成」段），再去掉代码围栏、
 *  标题行、表格行；按 `。！？；` 和换行切句，丢弃 <8 字的碎片
 *  （节标题残句、断开的小标题之类不是句子）；含 `[S..·..]` 引用
 *  标记的算「带引用」。分子分母都是确定性纯文本统计，不依赖模型。 */
function citedPercent(body: string): number | null {
  const text = body
    .replace(/```[\s\S]*?```/g, '')
    .split('\n')
    .filter((l) => !/^\s*(#{1,6}\s|\|)/.test(l))
    .join('\n')
  const sents = text.split(/[。！？；\n]/).map((s) => s.trim()).filter((s) => s.length >= 8)
  if (!sents.length) return null
  const cited = sents.filter((s) => /\[S\d+·\d+\]/.test(s)).length
  return Math.round((cited / sents.length) * 100)
}

/** 存疑段的条目数：该节是系统自己声明的弱点清单，一项一条 `- ` 列表。 */
function doubtCount(doubt: string): number {
  return doubt.split('\n').filter((l) => /^\s*[-*]\s/.test(l)).length
}

export function ReportView({
  markdown, onRef, ignoredCitations = [], onOpenSource, previews = {}, scrollRef,
  review, marksOn = false, sources,
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
  /** `/api/sessions/{sid}/review`：修订痕迹 + 运行状况。null=没数据
   *  （老会话/请求失败），各 UI 元素静默缺席。 */
  review?: ReviewResp | null
  /** 「修订痕迹」开关（顶栏按钮控制，默认关）。 */
  marksOn?: boolean
  /** 会话来源列表（健康条的「来源 · 一手」）。 */
  sources?: SourceRow[]
}) {
  const parts = useMemo(() => {
    const clean = cleanReportMd(markdown)
    // 「存疑与未完成」是系统主动声明的弱点，要和正文视觉区分开
    const i = clean.indexOf('## 存疑与未完成')
    if (i < 0) return { body: clean, doubt: '' }
    return { body: clean.slice(0, i), doubt: clean.slice(i) }
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

  // ---- 右侧批注栏（rail）：主区够宽才有 ----
  // 量的是滚动容器宽而不是视口——侧栏折叠会改变主区宽度，
  // 媒体查询看不到这个变化。
  const [wide, setWide] = useState(false)
  const [tick, setTick] = useState(0)
  // ⚠️ 不能用 useLayoutEffect：scrollRef 挂在父 div 上，ref 自底向上
  //  attach——子的 layout effect 先跑，此时 current 还是 null，
  //  会提前 return、RO 永远挂不上。useEffect 在整个 commit 之后，
  //  ref 一定已就位（下面滚动监听也是这个原因用 useEffect）。
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const check = () => setWide(el.clientWidth >= 1000)
    check()
    const ro = new ResizeObserver(() => { check(); setTick((t) => t + 1) })
    ro.observe(el)
    return () => ro.disconnect()
  }, [scrollRef])

  // ---- 修订痕迹：字符串哨兵 → 行内 del/ins + rail 朱批卡 ----
  const marked = useMemo(
    () => (marksOn && review?.patches?.length
      ? applyMarks(parts.body, review.patches) : null),
    [marksOn, review, parts.body])
  const bodySource = marked?.md ?? parts.body
  const problems = useMemo(
    () => (review?.patches ?? []).map((p) => p.problem), [review])

  const gridRef = useRef<HTMLDivElement>(null)
  const asideRef = useRef<HTMLElement>(null)
  const [tops, setTops] = useState<Record<number, number>>({})
  // 行内标记渲染出来后量它们相对网格的 y——朱批卡对齐的就是这条线。
  // tick 是 ResizeObserver 给的「宽度变了重算」信号。
  useLayoutEffect(() => {
    const g = gridRef.current
    if (!marked || !g) { setTops({}); return }
    const gTop = g.getBoundingClientRect().top
    const t: Record<number, number> = {}
    for (const i of marked.located) {
      const el = g.querySelector(`[data-mark="${i}"]`)
      if (el) t[i] = el.getBoundingClientRect().top - gTop
    }
    setTops(t)
  }, [marked, wide, tick, parts])

  // 朱批卡的最终 y：想落在对应标记的 y 上，但两条补丁挤在
  // 同一段时卡片会叠住——按标记 y 排序后逐张往下让位；
  // 「未能定位」的卡排在所有已定位卡之后。卡片高不可测就
  // 算不了「下一张从哪开始」，所以先渲染（hidden）量高再定位。
  const [notePos, setNotePos] = useState<Record<number, number>>({})
  useLayoutEffect(() => {
    const a = asideRef.current
    const patches = review?.patches ?? []
    if (!marked || !a || !patches.length) { setNotePos({}); return }
    const h: Record<number, number> = {}
    patches.forEach((_, i) => {
      h[i] = a.querySelector(`[data-note="${i}"]`)
        ?.getBoundingClientRect().height ?? 120
    })
    const order = patches
      .map((_, i) => i)
      .sort((x, y) => (tops[x] ?? Infinity) - (tops[y] ?? Infinity))
    const res: Record<number, number> = {}
    let cursor = 0
    for (const i of order) {
      const want = tops[i]
      res[i] = Math.max(want ?? cursor, cursor + (cursor ? 12 : 0))
      cursor = res[i] + h[i]
    }
    setNotePos(res)
  }, [marked, tops, review])

  // 悬停引用 → rail 来源卡（第 4 项）。chip 报自己的屏幕位置，
  // 换算成网格内偏移；窄屏 onRail 是 undefined，chip 自己回退气泡。
  const [hoverCard, setHoverCard] = useState<
    { seg: SegPreview; ref: string; top: number } | null>(null)
  const chipCtx = useMemo<ChipCtx>(() => ({
    rail: wide,
    problems,
    onRail: (rect, seg, ref) => {
      const g = gridRef.current
      if (!g) return
      setHoverCard({
        seg, ref,
        top: rect.top - g.getBoundingClientRect().top - 6,
      })
    },
    onRailLeave: () => setHoverCard(null),
  }), [wide, problems])

  const bodyMd = useMemo(
    () => mdComponents(onRef, previews, ids, counts, chipCtx),
    [onRef, previews, ids, counts, chipCtx])
  const doubtMd = useMemo(
    () => mdComponents(onRef, previews, ids, counts, chipCtx, true),
    [onRef, previews, ids, counts, chipCtx])

  // ---- 运行状况条：全部真数，没数据的项直接不渲染 ----
  const healthItems = useMemo(() => {
    const out: { k: string; v: string | number; seal?: boolean }[] = []
    const cPct = citedPercent(parts.body)
    if (cPct !== null) out.push({ k: '句子带引用', v: `${cPct}%` })
    if (sources?.length) {
      const first = sources.filter((x) => x.source_nature === 'first_party').length
      out.push({ k: first ? `来源 · 一手 ${first}` : '来源', v: sources.length })
    }
    const nPatch = review?.patches?.length ?? 0
    if (nPatch) out.push({ k: '审查修订', v: nPatch })
    const nDoubt = doubtCount(parts.doubt)
    if (nDoubt) out.push({ k: '存疑 · 已标注', v: nDoubt, seal: true })
    const h = review?.health
    if (h) {
      const uns = h.unsummarized_on_topic_chunks as
        { missing?: number } | undefined
      const degraded = Number(h.gate_degraded_chunks ?? 0)
        + Number(h.unjudged_sources ?? 0) + Number(h.failed_tasks ?? 0)
        + Number(uns?.missing ?? 0)
      out.push({ k: '降级环节', v: degraded, seal: degraded > 0 })
    }
    return out
  }, [parts, sources, review])

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
          ><Icon name="chevron-right" size={12} className="-rotate-90" /></button>
          <button
            onClick={() => matches.length && scrollToMatch(
              (cur + 1) % matches.length, matches)}
            className="rounded px-1 text-xs text-muted hover:bg-gray-100 hover:text-ink"
            title="下一个（⏎）"
          ><Icon name="chevron-down" size={12} /></button>
          <button
            onClick={() => setFindOpen(false)}
            className="rounded px-1 text-xs text-muted hover:bg-gray-100 hover:text-ink"
          ><Icon name="x" size={12} /></button>
        </div>
      )}

      {/* 稿子 .doc：640 版心 + 280 批注栏的两列网格。
          主区 ≥1000px 才排得下两列（ResizeObserver 量容器——侧栏
          折叠会改变主区宽，媒体查询看不见）；不够宽时 rail 不存在，
          行内标记和引用气泡照旧。 */}
      <div ref={gridRef}
           className={wide
             ? 'mx-auto grid w-fit grid-cols-[640px_280px] gap-8 px-6 pb-8 pt-4 print:block'
             : 'mx-auto max-w-[640px] px-6 pb-8 pt-4'}>
        <div ref={contentRef}>
          {/* 排除提示改成**一次性**：跟着正文流走，滚过去就没了。
              仍然保留是因为它是唯一一处告诉用户「这份报告引用了你后来
              标为不可信的材料」的地方，直接删掉等于让不可信结论悄悄留着。 */}
          <IgnoredBanner items={ignoredCitations} onOpenSource={onOpenSource ?? (() => {})} />

          {/* 运行状况条：真数据才列项，一项都没有整条不渲染。
              打印不出现——纸质件不需要运行指标。 */}
          {healthItems.length > 0 && (
            <div className="health mb-6 overflow-x-auto print:hidden">
              {healthItems.map((it) => (
                <div key={it.k} className="whitespace-nowrap">
                  <b className={it.seal ? 'text-seal' : ''}>{it.v}</b>
                  <span>{it.k}</span>
                </div>
              ))}
            </div>
          )}

          {/* ⚠️ 这里原来写的是 `prose-sm`——那个类从未生效，见 mdComponents 的说明 */}
          {/* 稿子正文档：15.5px / 1.95 行高 */}
          <article className="text-[15.5px] leading-[1.95] text-ink">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={bodyMd}>
              {bodySource}
            </ReactMarkdown>
          </article>

          {parts.doubt && (
            // 存疑段 = 系统自报的不可信区，朱红一族（seal）框起来，
            // 和正文的可信度在视觉上就区分开——不再用琥珀。
            <section className="mt-10 rounded-lg border border-seal/35 bg-seal/5 px-4 py-3">
              <div className="mb-2 text-xs font-medium text-seal">
                以下是系统自己声明的存疑与未完成，和上面的正文可信度不同
              </div>
              <article className="text-sm leading-6 text-seal/90">
                <ReactMarkdown remarkPlugins={[remarkGfm]} components={doubtMd}>
                  {parts.doubt}
                </ReactMarkdown>
              </article>
            </section>
          )}
        </div>

        {wide && (
          // 右侧批注栏：朱批卡纵向对齐到行内标记的 y。
          // 卡片全部绝对定位，最终 y 在测量过后写进 notePos——
          // 两条补丁挤在同一段时，后面的卡要往下让，不能叠住。
          <aside ref={asideRef} className="relative print:hidden">
            {marked && review?.patches.map((p, i) => (
              <div key={i} data-note={i}
                   className={`note red absolute inset-x-0 ${tops[i] === undefined ? 'border-dashed' : ''}`}
                   style={{
                     top: notePos[i] ?? -9999,
                     visibility: notePos[i] === undefined ? 'hidden' : 'visible',
                   }}>
                <div className="h">
                  <Icon name="pen" size={11} className="text-seal" />
                  <b>朱批 · 已修订</b>
                  <span className="seal ml-auto">改</span>
                </div>
                <p className="text-ink/85">{p.problem || '（无问题说明）'}</p>
                <div className="src">
                  {p.section_title || p.section || p.task} · 第 {p.round ?? '?'} 轮
                  {tops[i] === undefined && ' · 未能定位'}
                </div>
              </div>
            ))}
            {/* 悬停引用 → 来源卡（同 .note 语言）。浮在朱批卡之上：
                悬停是瞬时的，不该推开已固定的批注卡。 */}
            {hoverCard && (
              <div className="note absolute inset-x-0 z-10 shadow-md"
                   style={{ top: hoverCard.top }}>
                <div className="h">
                  <span className="cite">{hoverCard.ref}</span>
                  <NatureBadge nature={hoverCard.seg.性质 || null} />
                  <span className="ml-auto tabular-nums text-faint">
                    {hoverCard.seg.token} token
                  </span>
                </div>
                <p className="text-ink/85">
                  {hoverCard.seg.标题 || hoverCard.seg.名称 || hoverCard.ref}
                </p>
                {hoverCard.seg.摘录 && <p className="mt-1">{hoverCard.seg.摘录}</p>}
                {hoverCard.seg.已排除 && (
                  <p className="mt-1 text-seal">· 该来源已被标记为无用</p>
                )}
                <div className="src">
                  {[hoverCard.seg.域名, hoverCard.seg.名称]
                    .filter(Boolean).join(' · ')}
                </div>
              </div>
            )}
          </aside>
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
      ><Icon name="arrow-up" size={16} /></button>
    </>
  )
}
