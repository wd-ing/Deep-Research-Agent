import { useMemo, useState } from 'react'
import type { SourceLibrary as Lib } from '../types'
import { NatureBadge } from './SegDrawer'

/** 来源库列表（侧栏）。
 *
 *  **按性质分组是这个页的第一价值**——一手 / 研究 / 媒体 / 社区 / 聚合
 *  的可信度完全不同，混在一个按时间排的列表里等于没有分类。
 *  顺序按 `nature_order`（后端给的规范序），不按数量——数量会变，
 *  「先看一手再看社区」这个顺序不该变。
 */
export function SourceList({
  lib, q, onQuery, cur, onPick,
}: {
  lib: Lib | null
  q: string
  onQuery: (v: string) => void
  cur: string | null
  onPick: (id: string) => void
}) {
  const [closed, setClosed] = useState<Set<string>>(new Set())

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
    return <div className="px-3 py-4 text-xs text-muted">来源库加载中…</div>
  }

  const shown = groups.filter((g) => g.items.length > 0)

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="space-y-2 p-3 pb-2">
        <input
          value={q}
          onChange={(e) => onQuery(e.target.value)}
          placeholder="搜索标题 / 域名…"
          className="w-full rounded-md border border-line bg-white px-2.5 py-1.5 text-sm outline-none focus:border-accent"
        />
        <div className="flex gap-2 text-[11px] text-muted">
          <span>{lib.total} 个来源</span>
          <span>{lib.segments} 段</span>
          <span>{lib.citations} 处引用</span>
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
                className="flex w-full items-center gap-1 rounded px-2 py-1 text-left text-[11px] text-muted hover:bg-white"
              >
                <span className="w-2 shrink-0 text-[9px] leading-none">
                  {isClosed ? '▶' : '▼'}
                </span>
                {/* NatureBadge 自己就渲染中文标签，别再补一遍文字 */}
                <NatureBadge nature={g.nature} />
                <span className="ml-auto opacity-60">{g.items.length}</span>
              </button>

              {!isClosed && g.items.map((s) => (
                <button
                  key={s.id}
                  onClick={() => onPick(s.id)}
                  className={[
                    'group/src mb-0.5 block w-full rounded-md px-2 py-1.5 text-left transition-colors',
                    s.id === cur ? 'bg-white shadow-sm ring-1 ring-line'
                                  : 'hover:bg-white',
                  ].join(' ')}
                >
                  <div className="line-clamp-2 text-[12px] leading-snug text-ink">
                    {s.title || '(无标题)'}
                  </div>
                  <div className="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-[10px] text-muted">
                    <span className="max-w-[9rem] truncate">{s.domain}</span>
                    {s.source_date && <span>{s.source_date.slice(0, 10)}</span>}
                    <span>{s.segments} 段</span>
                    {s.citations > 0
                      ? <span className="text-accent">{s.citations} 处引用</span>
                      : <span className="text-gray-400">未被引用</span>}
                  </div>
                  {/* 不在题的来源要标出来——它留了库但没进报告，
                      「为什么列表里有它却没被引用」这个疑问得有个答案 */}
                  {!s.on_topic && (
                    <div className="mt-0.5 text-[10px] text-amber-600">
                      ⚠ 相关性判定为不在题，未参与报告
                    </div>
                  )}
                </button>
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
    </div>
  )
}
