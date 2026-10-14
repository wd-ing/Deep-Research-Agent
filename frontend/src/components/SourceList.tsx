import { useMemo, useState } from 'react'
import type { SourceLibrary as Lib, SourceLibItem } from '../types'
import { NatureBadge } from './SegDrawer'
import { ConfirmDialog } from './ConfirmDialog'

/** 来源库列表（侧栏）。
 *
 *  **按性质分组是这个页的第一价值**——一手 / 研究 / 媒体 / 社区 / 聚合
 *  的可信度完全不同，混在一个按时间排的列表里等于没有分类。
 *  顺序按 `nature_order`（后端给的规范序），不按数量——数量会变，
 *  「先看一手再看社区」这个顺序不该变。
 */
export function SourceList({
  lib, libErr, onRetryLib, q, onQuery, cur, onPick, onIgnore, onUnignore,
}: {
  lib: Lib | null
  /** 加载失败的原因。`lib === null` 分不清「加载中」和「加载失败」，
   *  所以失败必须显式传进来（见下面 `if (!lib)` 处的说明）。 */
  libErr: string | null
  onRetryLib: () => void
  q: string
  onQuery: (v: string) => void
  cur: string | null
  onPick: (id: string) => void
  onIgnore: (id: string, reason: string) => void
  onUnignore: (id: string) => void
}) {
  const [closed, setClosed] = useState<Set<string>>(new Set())
  const [marking, setMarking] = useState<SourceLibItem | null>(null)
  const [reason, setReason] = useState('')

  const groups = useMemo(() => {
    if (!lib) return []
    const by = new Map<string, typeof lib.sources>()
    for (const s of lib.sources) {
      const k = s.nature
      if (!by.has(k)) by.set(k, [])
      by.get(k)!.push(s)
    }
    // 服务端给的规范序在前；词表外的值兜底排最后
    return lib.nature_order
      .filter((k) => by.has(k))
      .map((k) => ({ nature: k, label: lib.nature_labels[k] ?? k, items: by.get(k)! }))
      .concat(
        [...by.entries()]
          .filter(([k]) => !lib.nature_order.includes(k))
          .map(([k, items]) => ({ nature: k, label: k, items })),
      )
  }, [lib])

  if (!lib) {
    // ⚠️ 这里原来只有一句「来源库加载中…」。但 `lib === null` **同时**表示
    // 「还在加载」和「加载失败」——请求失败时 App 把 lib 置回 null，
    // 于是列表永远停在「加载中…」。2026-10-13 实测：后端停掉后侧栏就卡在这句上，
    // 而底部提示条同时在说「来源库加载失败」，**两边自相矛盾**。
    // 所以失败必须有自己的状态，不能靠 `lib` 猜。
    if (libErr) {
      return (
        <div className="space-y-2 px-3 py-4 text-xs text-muted">
          <div className="text-red-600">来源库没加载出来</div>
          <div className="leading-relaxed">{libErr}</div>
          <button
            onClick={onRetryLib}
            className="rounded border border-line px-2 py-1 text-2xs text-ink hover:bg-gray-50"
          >
            重试
          </button>
        </div>
      )
    }
    return <div className="px-3 py-4 text-xs text-muted">来源库加载中…</div>
  }

  const shown = groups.filter((g) => g.items.length > 0)
  const nIgnored = lib.sources.filter((s) => s.ignored).length

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="space-y-2 p-3 pb-2">
        <input
          value={q}
          onChange={(e) => onQuery(e.target.value)}
          placeholder="搜索标题 / 域名…"
          className="w-full rounded-md border border-line bg-white px-2.5 py-1.5 text-sm outline-none focus:border-accent"
        />
        <div className="flex gap-2 text-2xs text-muted">
          <span>{lib.total} 个来源</span>
          <span>{lib.segments} 段</span>
          <span>{lib.citations} 处引用</span>
          {/* 没有就完全不出现——`⚠ 只在需要用户判断时出现` */}
          {nIgnored > 0 && <span className="text-amber-600">{nIgnored} 个已排除</span>}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto px-2 pb-4">
        {shown.map((g) => {
          const isClosed = closed.has(g.nature)
          return (
            <div key={g.nature} className="mb-1">
              <button
                onClick={() => setClosed((prev) => {
                  const n = new Set(prev)
                  n.has(g.nature) ? n.delete(g.nature) : n.add(g.nature)
                  return n
                })}
                className="flex w-full items-center gap-1 rounded px-2 py-1 text-left text-2xs text-muted hover:bg-white"
              >
                <span className="w-2 shrink-0 text-2xs leading-none">
                  {isClosed ? '▶' : '▼'}
                </span>
                {/* NatureBadge 自己就渲染中文标签，别再补一遍文字 */}
                <NatureBadge nature={g.nature} />
                <span className="ml-auto opacity-60">{g.items.length}</span>
              </button>

              {!isClosed && g.items.map((s) => (
                // ⚠️ 行容器不能是 <button>：里面还要放「标记排除 / 恢复」按钮，
                // <button> 套 <button> 是非法 HTML，浏览器会把内层拆出来导致布局错乱。
                <div
                  key={s.id}
                  className={[
                    'group/src relative mb-0.5 rounded-md transition-colors',
                    s.ignored ? 'bg-gray-50 opacity-60' : '',
                    s.id === cur ? 'sel' : 'hover:bg-white',
                  ].join(' ')}
                >
                  <button
                    onClick={() => onPick(s.id)}
                    className="block w-full px-2 py-1.5 pr-7 text-left"
                  >
                    <div className="line-clamp-2 text-xs leading-snug text-ink">
                      {s.title || '(无标题)'}
                    </div>
                    <div className="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-2xs text-muted">
                      <span className="max-w-[9rem] truncate">{s.domain}</span>
                      {s.source_date && <span>{s.source_date.slice(0, 10)}</span>}
                      <span>{s.segments} 段</span>
                      {s.citations > 0
                        ? <span className="text-accent">{s.citations} 处引用</span>
                        : <span className="text-gray-400">未被引用</span>}
                    </div>
                    {/* 已排除要说清**为什么**，以及排除到底意味着什么——
                        否则用户以为「标记一下报告里的它就没了」 */}
                    {s.ignored && (
                      <div className="mt-0.5 text-2xs text-amber-700">
                        ⊘ 已排除{s.ignore_reason ? `：${s.ignore_reason}` : ''}
                        {s.citations > 0 && '（报告已引用，不会被改写）'}
                      </div>
                    )}
                    {/* 不在题的来源要标出来——它留了库但没进报告，
                        「为什么列表里有它却没被引用」这个疑问得有个答案 */}
                    {!s.on_topic && !s.ignored && (
                      <div className="mt-0.5 text-2xs text-amber-600">
                        ⚠ 相关性判定为不在题，未参与报告
                      </div>
                    )}
                  </button>
                  <button
                    onClick={() => {
                      if (s.ignored) onUnignore(s.id)
                      else { setReason(''); setMarking(s) }
                    }}
                    title={s.ignored ? '取消排除' : '标记为无用来源'}
                    aria-label={s.ignored ? `取消排除 ${s.domain}` : `标记 ${s.domain} 为无用来源`}
                    // ⚠️ 原来只有 `group-hover/src:block`，**没有 focus-within**——
                    // 显示靠 `hidden`（display:none），所以键盘用户根本 Tab 不到它，
                    // 「标记排除」对他们等于不存在。SessionRow / FolderTree
                    // 两处同类按钮都有 focus-within，这里是漏的。
                    className="absolute right-1 top-1.5 hidden rounded px-1 text-2xs text-muted hover:bg-gray-200 hover:text-ink group-hover/src:block group-focus-within/src:block"
                  >
                    {s.ignored ? '↺' : '⊘'}
                  </button>
                </div>
              ))}
            </div>
          )
        })}

        {!lib.total && (
          <div className="px-1 py-3 text-xs text-muted">
            {q ? `没有匹配「${q}」的来源` : '还没有收集到来源'}
          </div>
        )}
      </div>

      {marking && (
        <ConfirmDialog
          title="标记为无用来源"
          lines={[
            `来源：${marking.domain}`,
            // 只在真的会影响已完成报告时才提——这才是需要用户知道的
            marking.citations > 0
              ? `⚠ 报告已经引用了它 ${marking.citations} 处。排除不会改写报告，报告顶部会出现提示条。`
              : '它没有被任何报告引用，排除不影响已完成的研究。',
            '来源和段落都会保留，随时可以恢复。',
          ]}
          input={{
            placeholder: '排除原因（可选）',
            value: reason,
            onChange: setReason,
          }}
          confirmText="标记排除"
          danger={marking.citations > 0}
          onCancel={() => setMarking(null)}
          onConfirm={() => { onIgnore(marking.id, reason.trim()); setMarking(null) }}
        />
      )}
    </div>
  )
}
