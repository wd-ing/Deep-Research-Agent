import { useEffect, useMemo, useRef, useState } from 'react'
import type { SourceLibrary as Lib, SourceLibItem, SourceDetailRow } from '../types'
import { natureMeta, NatureBadge } from '../components/SegDrawer'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { CountUp } from '../components/CountUp'
import { Icon } from '../components/Icon'
import { Empty } from '../components/Empty'

/** 「仅看已排除」筛选的特殊值——它不是一种性质，不进 nature_order。 */
export const IGNORED_ONLY = '__ignored'

type SortKey = 'citations' | 'recent' | 'segments'
const SORTS: [SortKey, string][] = [['citations', '引用数'], ['recent', '最近'], ['segments', '段数']]

/** 来源库主区（P4）：原来整个库挤在 227px 侧栏里。
 *  现在是「左栏列表 | 右栏详情」双栏，页头带读数和性质构成。
 *
 *  分组结构沿用旧侧栏列表的教训：
 *  - 按性质分组 + nature_order 规范序（价值在分类，不在数量排序）；
 *  - 行容器不能是 <button>（内层还有 ⊘/↺ 按钮，嵌套非法）；
 *  - 悬停钮要 group-hover + focus-within 双通道，否则键盘用户 Tab 不到；
 *  - lib===null 分不清加载中/失败，失败走 libErr 单独渲染。
 */
export function SourcesView({
  lib, libErr, onRetryLib, q, onQuery, cur, onPick,
  onIgnore, onUnignore, srcDetail, onOpenSession, onToggleIgnore,
  natureFilter, onNatureFilter, onBack,
}: {
  lib: Lib | null
  libErr: string | null
  onRetryLib: () => void
  q: string
  onQuery: (v: string) => void
  cur: string | null
  onPick: (id: string) => void
  onIgnore: (id: string, reason: string) => void
  onUnignore: (id: string) => void
  srcDetail: SourceDetailRow | null
  onOpenSession: (id: string) => void
  onToggleIgnore: (id: string, ignored: boolean) => void
  natureFilter: string | null
  onNatureFilter: (v: string | null) => void
  onBack: () => void
}) {
  const rootRef = useRef<HTMLDivElement>(null)
  const [wide, setWide] = useState(true)
  useEffect(() => {
    const el = rootRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const check = () => setWide(el.clientWidth >= 900)
    check()
    const ro = new ResizeObserver(check)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const [sort, setSort] = useState<SortKey>('citations')
  const [closed, setClosed] = useState<Set<string>>(new Set())
  const [marking, setMarking] = useState<SourceLibItem | null>(null)
  const [reason, setReason] = useState('')

  /* ---- 筛选 + 分组 + 排序 ---- */
  const groups = useMemo(() => {
    if (!lib) return []
    let items = lib.sources
    if (natureFilter === IGNORED_ONLY) items = items.filter((s) => s.ignored)
    else if (natureFilter) items = items.filter((s) => s.nature === natureFilter)
    const cmp = (a: SourceLibItem, b: SourceLibItem) =>
      sort === 'citations' ? b.citations - a.citations
      : sort === 'segments' ? b.segments - a.segments
      : b.fetched_at.localeCompare(a.fetched_at)
    const by = new Map<string, SourceLibItem[]>()
    for (const s of items) {
      if (!by.has(s.nature)) by.set(s.nature, [])
      by.get(s.nature)!.push(s)
    }
    for (const arr of by.values()) arr.sort(cmp)
    return lib.nature_order
      .filter((k) => by.has(k))
      .map((k) => ({ nature: k, label: lib.nature_labels[k] ?? k, items: by.get(k)! }))
      .concat(
        [...by.entries()]
          .filter(([k]) => !lib.nature_order.includes(k))
          .map(([k, items]) => ({ nature: k, label: k, items })),
      )
  }, [lib, natureFilter, sort])

  const nIgnored = lib?.sources.filter((s) => s.ignored).length ?? 0
  const flatList = useMemo(() => groups.flatMap((g) => g.items), [groups])

  /* 深链进来时把选中行滚进视口 */
  const selRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (cur) selRef.current?.scrollIntoView({ block: 'nearest' })
  }, [cur])

  /* ---- 渲染 ---- */
  if (!lib) {
    return (
      <div ref={rootRef} className="flex min-h-0 flex-1 flex-col px-10 py-7">
        {libErr ? (
          <div className="card max-w-md space-y-2 px-5 py-4 text-xs text-muted">
            <div className="text-red-600">来源库没加载出来</div>
            <div className="leading-relaxed">{libErr}</div>
            <button
              onClick={onRetryLib}
              className="rounded border border-line px-2 py-1 text-2xs text-ink hover:bg-gray-50"
            >重试</button>
          </div>
        ) : (
          <div aria-busy="true" aria-label="来源库加载中">
            <div className="skel h-8 w-40" />
            <div className="mt-4 flex gap-3">
              {Array.from({ length: 4 }).map((_, i) => (
                <div key={i} className="skel h-12 w-32" />
              ))}
            </div>
            <div className="mt-6 flex min-h-0 flex-1 gap-6">
              <div className="w-[380px] shrink-0 space-y-2.5">
                {Array.from({ length: 8 }).map((_, i) => (
                  <div key={i} className="space-y-1.5">
                    <div className="skel h-3.5 w-4/5" />
                    <div className="skel h-2.5 w-2/5" />
                  </div>
                ))}
              </div>
              <div className="min-w-0 flex-1 space-y-3">
                <div className="skel h-6 w-3/5" />
                <div className="skel h-4 w-2/5" />
                <div className="skel h-28 w-full" />
              </div>
            </div>
          </div>
        )}
      </div>
    )
  }

  const showList = wide || !cur
  const showDetail = wide || !!cur

  return (
    <div ref={rootRef} className="flex min-h-0 flex-1 flex-col">
      {/* 页头：标题 + 读数行 + 性质构成色带（全真数据） */}
      <header className={`reveal shrink-0 border-b border-line2 ${wide ? 'px-8 pb-4 pt-6' : 'px-4 pb-2.5 pt-4'}`}>
        <div className="flex items-end justify-between gap-4">
          <h1 className={`${wide ? 'text-[32px]' : 'text-[26px]'} font-semibold tracking-[-.02em]`}>来源库</h1>
        </div>
        <div className={`${wide ? 'mt-3' : 'mt-2'} flex flex-wrap items-center gap-x-6 gap-y-1`}>
          <div className="flex items-baseline gap-1.5">
            <b className={`${wide ? 'text-[26px]' : 'text-[20px]'} font-medium leading-none tabular-nums`}><CountUp v={lib.total} /></b>
            <span className="text-2xs text-faint">来源</span>
          </div>
          <div className="flex items-baseline gap-1.5">
            <b className={`${wide ? 'text-[26px]' : 'text-[20px]'} font-medium leading-none tabular-nums`}><CountUp v={lib.segments} /></b>
            <span className="text-2xs text-faint">段</span>
          </div>
          <div className="flex items-baseline gap-1.5">
            <b className={`${wide ? 'text-[26px]' : 'text-[20px]'} font-medium leading-none tabular-nums`}><CountUp v={lib.citations} /></b>
            <span className="text-2xs text-faint">处引用</span>
          </div>
          {nIgnored > 0 && (
            <div className="flex items-baseline gap-1.5">
              <b className={`${wide ? 'text-[26px]' : 'text-[20px]'} font-medium leading-none tabular-nums text-seal`}><CountUp v={nIgnored} /></b>
              <span className="text-2xs text-seal">已排除</span>
            </div>
          )}
        </div>
        {/* 性质构成色带：宽度=真实占比，颜色=NATURE_META（性质色不是状态色） */}
        {lib.total > 0 && (
          <div className={`mix ${wide ? 'mt-3.5' : 'mt-2'} flex h-1.5 w-full overflow-hidden rounded-full`}>
            {lib.nature_order.map((k) => {
              const n = lib.by_nature[k] ?? 0
              if (!n) return null
              return (
                <i key={k} title={`${lib.nature_labels[k] ?? k} ${n}`}
                   style={{ width: `${(n / lib.total) * 100}%`, background: natureMeta(k).dot }} />
              )
            })}
          </div>
        )}

        {/* 工具行：搜索 + 性质 chips + 排序 */}
        <div className={`${wide ? 'mt-4' : 'mt-2.5'} flex flex-wrap items-center gap-2`}>
          <div className="relative min-w-[200px] flex-1 sm:max-w-[300px]">
            <Icon name="search" size={13}
              className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" />
            <input
              value={q}
              onChange={(e) => onQuery(e.target.value)}
              placeholder="搜索标题 / 域名…"
              className="w-full rounded-[10px] border border-line bg-white py-[7px] pl-8 pr-2.5 text-[13px] outline-none placeholder:text-faint focus:border-accent"
            />
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            <FilterChip on={natureFilter === null}
              onClick={() => onNatureFilter(null)}>全部</FilterChip>
            {lib.nature_order.map((k) => {
              const n = lib.by_nature[k] ?? 0
              if (!n) return null
              const on = natureFilter === k
              return (
                <FilterChip key={k} on={on}
                  onClick={() => onNatureFilter(on ? null : k)}
                  dot={natureMeta(k).dot}>
                  {lib.nature_labels[k] ?? k} {n}
                </FilterChip>
              )
            })}
            {nIgnored > 0 && (
              <FilterChip on={natureFilter === IGNORED_ONLY}
                onClick={() => onNatureFilter(natureFilter === IGNORED_ONLY ? null : IGNORED_ONLY)}
                seal>
                已排除 {nIgnored}
              </FilterChip>
            )}
          </div>
          <div className="seg ml-auto">
            {SORTS.map(([k, label]) => (
              <button key={k} className={sort === k ? 'on' : ''}
                onClick={() => setSort(k)}>{label}</button>
            ))}
          </div>
        </div>
      </header>

      {/* 双栏 */}
      <div className="flex min-h-0 flex-1">
        {showList && (
          <div className={wide
            ? 'flex w-[380px] shrink-0 flex-col overflow-y-auto border-r border-line2 px-3 py-3'
            : 'flex min-w-0 flex-1 flex-col overflow-y-auto px-3 py-3'}>
            {lib.total === 0 ? (
              <Empty icon="book" text={q ? `没有匹配「${q}」的来源` : '还没有收集到来源'} />
            ) : flatList.length === 0 ? (
              <div className="px-3 py-8 text-center text-xs text-muted">
                {natureFilter === IGNORED_ONLY ? '没有已排除的来源' : '该性质下没有来源'}
                {natureFilter && (
                  <button onClick={() => onNatureFilter(null)}
                    className="ml-1.5 text-accent hover:underline">清除筛选</button>
                )}
              </div>
            ) : groups.map((g) => {
              const isClosed = closed.has(g.nature)
              return (
                <div key={g.nature} className="mb-1">
                  <button
                    onClick={() => setClosed((prev) => {
                      const n = new Set(prev)
                      n.has(g.nature) ? n.delete(g.nature) : n.add(g.nature)
                      return n
                    })}
                    className="flex w-full items-center gap-1.5 rounded px-2 py-1 text-left transition-colors hover:bg-gray-50"
                  >
                    <span className="w-2 shrink-0 text-muted">
                      <Icon name={isClosed ? 'chevron-right' : 'chevron-down'} size={10} />
                    </span>
                    <span className="h-1.5 w-1.5 shrink-0 rounded-full"
                      style={{ background: natureMeta(g.nature).dot }} />
                    <span className="min-w-0 flex-1 truncate text-2xs font-medium tracking-[.04em] text-faint">
                      {g.label}
                    </span>
                    <span className="shrink-0 text-2xs tabular-nums text-faint">{g.items.length}</span>
                  </button>

                  {!isClosed && g.items.map((s) => (
                    <SrcRow key={s.id} s={s} sel={s.id === cur}
                      refEl={s.id === cur ? selRef : undefined}
                      onPick={onPick}
                      onMark={() => { setReason(''); setMarking(s) }}
                      onUnignore={() => onUnignore(s.id)} />
                  ))}
                </div>
              )
            })}
          </div>
        )}

        {showDetail && (
          <div className="min-w-0 flex-1 overflow-y-auto">
            {!wide && cur && (
              <button onClick={onBack}
                className="sticky top-0 z-10 flex w-full items-center gap-1 border-b border-line2 bg-white/95 px-4 py-2 text-xs text-accent backdrop-blur">
                <Icon name="chevron-right" size={12} className="rotate-180" />返回来源列表
              </button>
            )}
            {cur && srcDetail ? (
              <DetailPanel key={srcDetail.id} src={srcDetail}
                onOpenSession={onOpenSession} onToggleIgnore={onToggleIgnore} />
            ) : cur ? (
              <div className="px-8 py-10 text-xs text-muted">载入中…</div>
            ) : (
              <Overview lib={lib} onPick={onPick} onNatureFilter={onNatureFilter} />
            )}
          </div>
        )}
      </div>

      {marking && (
        <ConfirmDialog
          title="标记为无用来源"
          lines={[
            `来源：${marking.domain}`,
            marking.citations > 0
              ? `⚠ 报告已经引用了它 ${marking.citations} 处。排除不会改写报告，报告顶部会出现提示条。`
              : '它没有被任何报告引用，排除不影响已完成的研究。',
            '来源和段落都会保留，随时可以恢复。',
          ]}
          input={{ placeholder: '排除原因（可选）', value: reason, onChange: setReason }}
          confirmText="标记排除"
          danger={marking.citations > 0}
          onCancel={() => setMarking(null)}
          onConfirm={() => { onIgnore(marking.id, reason.trim()); setMarking(null) }}
        />
      )}
    </div>
  )
}

/* ---------- 筛选 chip ---------- */
function FilterChip({
  on, onClick, dot, seal, children,
}: {
  on: boolean; onClick: () => void; dot?: string; seal?: boolean; children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={on}
      className={[
        'flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-2xs transition-colors',
        on ? 'border-accent/50 bg-accent/[.07] text-accent'
          : seal ? 'border-seal/40 text-seal hover:bg-seal/5'
          : 'border-line text-muted hover:border-black/20 hover:text-ink',
      ].join(' ')}
    >
      {dot && <span className="h-1.5 w-1.5 rounded-full" style={{ background: dot }} />}
      {children}
    </button>
  )
}

/* ---------- 列表行 ---------- */
function SrcRow({
  s, sel, refEl, onPick, onMark, onUnignore,
}: {
  s: SourceLibItem
  sel: boolean
  refEl?: React.RefObject<HTMLDivElement | null>
  onPick: (id: string) => void
  onMark: () => void
  onUnignore: () => void
}) {
  /* ↑/↓ 在相邻行间移焦点——只移焦点不触发选中，
     不然按住下键就把详情接口打穿了。 */
  const onKey = (e: React.KeyboardEvent<HTMLButtonElement>) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    e.preventDefault()
    const rows = [...document.querySelectorAll<HTMLElement>('[data-srcrow]')]
    const i = rows.indexOf(e.currentTarget)
    const nxt = rows[i + (e.key === 'ArrowDown' ? 1 : -1)]
    nxt?.focus()
  }
  return (
    <div ref={refEl} data-row
      className={[
        'group/src relative mb-0.5 rounded-md transition-colors',
        s.ignored ? 'bg-gray-50 opacity-60' : '',
        sel ? 'sel' : 'hover:bg-white',
      ].join(' ')}
      style={{ contentVisibility: 'auto', containIntrinsicSize: 'auto 64px' } as React.CSSProperties}
    >
      {sel && <span className="absolute inset-y-1 left-0 w-0.5 rounded bg-ink" aria-hidden />}
      <button data-srcrow
        onClick={() => onPick(s.id)}
        onKeyDown={onKey}
        className="block w-full px-2 py-1.5 pl-3 pr-7 text-left"
      >
        <div className="flex items-start gap-2">
          <div className="line-clamp-2 min-w-0 flex-1 text-xs leading-snug text-ink"
            title={s.title || '(无标题)'}>
            {s.title || '(无标题)'}
          </div>
          {s.citations > 0
            ? <span className="shrink-0 pt-px font-mono text-2xs tabular-nums text-accent">{s.citations} 引</span>
            : <span className="shrink-0 pt-px text-2xs text-faint">未引</span>}
        </div>
        <div className="mt-0.5 flex items-center gap-1.5 font-mono text-2xs text-muted">
          <span className="min-w-0 flex-1 truncate" title={s.domain ?? undefined}>
            {s.domain}{s.source_date ? ` · ${s.source_date.slice(0, 7)}` : ''}
          </span>
          <span className="shrink-0 tabular-nums">{s.segments} 段</span>
        </div>
        {s.ignored && (
          <div className="mt-0.5 text-2xs text-seal">
            ⊘ 已排除{s.ignore_reason ? `：${s.ignore_reason}` : ''}
            {s.citations > 0 && '（报告已引用，不会被改写）'}
          </div>
        )}
        {!s.on_topic && !s.ignored && (
          <div className="mt-0.5 text-2xs text-seal">
            <Icon name="alert" size={10} className="mb-px mr-0.5 inline-block" />相关性判定为不在题，未参与报告
          </div>
        )}
      </button>
      <button
        onClick={s.ignored ? onUnignore : onMark}
        title={s.ignored ? '取消排除' : '标记为无用来源'}
        aria-label={s.ignored ? `取消排除 ${s.domain}` : `标记 ${s.domain} 为无用来源`}
        className="absolute right-1 top-1.5 hidden rounded px-1 text-2xs text-muted hover:bg-gray-200 hover:text-ink group-hover/src:block group-focus-within/src:block"
      >
        {s.ignored ? '↺' : '⊘'}
      </button>
    </div>
  )
}

/* ---------- 未选中时的概览 ---------- */
function Overview({
  lib, onPick, onNatureFilter,
}: {
  lib: Lib
  onPick: (id: string) => void
  onNatureFilter: (v: string) => void
}) {
  const top = useMemo(
    () => [...lib.sources].filter((s) => s.citations > 0)
      .sort((a, b) => b.citations - a.citations).slice(0, 5),
    [lib])
  if (lib.total === 0) {
    return (
      <div className="flex flex-1 items-center justify-center py-16">
        <Empty icon="book" text="还没有收集到来源" />
      </div>
    )
  }
  return (
    <div className="view-in mx-auto max-w-[560px] px-8 py-8">
      <div className="lbl">概览</div>
      <div className="card mt-3 px-5 py-4">
        <div className="text-2xs text-faint">性质构成</div>
        <div className="mix mt-2.5 flex h-2 w-full overflow-hidden rounded-full">
          {lib.nature_order.map((k) => {
            const n = lib.by_nature[k] ?? 0
            if (!n) return null
            return <i key={k} style={{ width: `${(n / lib.total) * 100}%`, background: natureMeta(k).dot }} />
          })}
        </div>
        <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1.5">
          {lib.nature_order.map((k) => {
            const n = lib.by_nature[k] ?? 0
            if (!n) return null
            return (
              <button key={k} onClick={() => onNatureFilter(k)}
                className="flex items-center gap-1.5 text-2xs text-muted transition-colors hover:text-ink">
                <span className="h-1.5 w-1.5 rounded-full" style={{ background: natureMeta(k).dot }} />
                {lib.nature_labels[k] ?? k} <b className="tabular-nums text-ink">{n}</b>
              </button>
            )
          })}
        </div>
      </div>
      {top.length > 0 && (
        <div className="card mt-4 px-5 py-4">
          <div className="text-2xs text-faint">被引用最多</div>
          <ul className="mt-2.5 space-y-1">
            {top.map((s) => (
              <li key={s.id}>
                <button onClick={() => onPick(s.id)}
                  className="group flex w-full items-center gap-2.5 rounded px-1.5 py-1 text-left hover:bg-black/[.03]">
                  <span className="shrink-0 font-mono text-2xs tabular-nums text-accent">{s.citations} 引</span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-xs text-ink">{s.title || '(无标题)'}</span>
                    <span className="block truncate text-2xs text-faint">
                      {s.domain}{s.session_title ? ` · ${s.session_title}` : ''}
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

/* ---------- 详情面板 ---------- */
function DetailPanel({
  src, onOpenSession, onToggleIgnore,
}: {
  src: SourceDetailRow
  onOpenSession: (id: string) => void
  onToggleIgnore: (id: string, ignored: boolean) => void
}) {
  const [openSegs, setOpenSegs] = useState<Set<string>>(new Set())
  return (
    <div className="view-in">
      <header className="border-b border-line px-8 py-5">
        <h1 className="text-[22px] font-semibold leading-snug tracking-[-.01em] text-ink">
          {src.title || '(无标题)'}
        </h1>
        <div className="mt-2.5 flex flex-wrap items-center gap-1.5 text-2xs text-muted">
          <NatureBadge nature={src.nature} />
          <span className="pill font-mono">{src.domain}</span>
          <span className="pill font-mono">{src.source_date ? src.source_date.slice(0, 10) : '日期未知'}</span>
          <span className="pill">{src.segments.length} 段</span>
          {!src.on_topic && (
            <span className="pill bg-seal/10 text-seal">
              <Icon name="alert" size={10} />判定为不在题，未参与报告
            </span>
          )}
          {src.ignored && (
            <span className="pill bg-seal/10 text-seal">
              ⊘ 已标记为无用{src.ignore_reason ? `：${src.ignore_reason}` : ''}
            </span>
          )}
        </div>
        {src.nature_reason && (
          <p className="mt-2 text-xs leading-relaxed text-muted">
            性质判据：{src.nature_reason}
          </p>
        )}
        <div className="mt-2.5 flex flex-wrap items-center gap-3 text-xs">
          <a href={src.url} target="_blank" rel="noreferrer"
             className="btn-ghost">在原网页打开 ↗</a>
          <button onClick={() => onOpenSession(src.session_id)} className="btn-ghost">
            ← 回到这场研究
          </button>
          <button onClick={() => onToggleIgnore(src.id, src.ignored)} className="btn-ghost">
            {src.ignored ? '↺ 取消排除' : '⊘ 标记为无用来源'}
          </button>
        </div>
        <div className="mt-2 truncate font-mono text-2xs text-faint" title={src.url}>{src.url}</div>
      </header>

      <div className="px-8 py-4">
        <div className="mb-2 text-xs text-muted">
          以下段落来自这篇文章。段号与报告正文里的引用标记一致，可直接对照。
        </div>
        <div className="card">
          {src.segments.map((s, i) => {
            const open = openSegs.has(s.id)
            // 超过 ~6 行才给展开/收起——短段一个按钮都嫌多余
            const long = s.content.length > 380 || (s.content.match(/\n/g) ?? []).length > 5
            return (
              <article key={s.id} className={i > 0 ? 'border-t border-line' : ''}>
                <div className="flex items-center gap-2 px-4 pt-3 text-2xs">
                  <code className="rounded bg-black/[.04] px-1 py-0.5 font-mono text-ink">
                    {s.ref}
                  </code>
                  {s.heading_path && (
                    <span className="truncate text-muted">{s.heading_path}</span>
                  )}
                  {s.token_count != null && (
                    <span className="ml-auto shrink-0 font-mono text-faint">{s.token_count} token</span>
                  )}
                </div>
                {s.summary && (
                  <p className="mt-1.5 px-4 text-xs leading-relaxed text-ink">{s.summary}</p>
                )}
                <p className={[
                  'mx-4 my-3 whitespace-pre-wrap border-l-2 border-ink/25 pl-3 text-xs leading-6 text-muted',
                  open ? '' : 'line-clamp-6',
                ].join(' ')}>
                  {s.content}
                </p>
                {long && (
                  <button
                    onClick={() => setOpenSegs((prev) => {
                      const n = new Set(prev)
                      n.has(s.id) ? n.delete(s.id) : n.add(s.id)
                      return n
                    })}
                    className="mx-4 mb-2 text-2xs text-accent hover:underline"
                  >{open ? '收起' : '展开'}</button>
                )}
              </article>
            )
          })}
        </div>
      </div>
    </div>
  )
}
