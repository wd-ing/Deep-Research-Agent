import { useCallback, useEffect, useState } from 'react'
import { useSession, api } from './useSession'
import type { SessionSummary } from './types'
import { StageBar, Timeline } from './views/RunView'
import { ApprovalView } from './views/ApprovalView'
import { ReportView } from './views/ReportView'
import { SegDrawer, NatureBadge } from './components/SegDrawer'

function Sidebar({
  list, cur, onPick, onNew,
}: {
  list: SessionSummary[]
  cur: string | null
  onPick: (id: string) => void
  onNew: () => void
}) {
  return (
    <nav className="flex w-60 shrink-0 flex-col border-r border-line bg-gray-50">
      <div className="p-4">
        <button
          onClick={onNew}
          className="w-full rounded bg-accent px-3 py-2 text-sm text-white hover:bg-blue-700"
        >
          新建研究
        </button>
      </div>
      <div className="flex-1 overflow-auto px-2 pb-4">
        {list.map((s) => (
          <button
            key={s.id}
            onClick={() => onPick(s.id)}
            className={[
              'mb-1 block w-full rounded px-3 py-2 text-left text-sm',
              s.id === cur ? 'bg-white shadow-sm ring-1 ring-line' : 'hover:bg-white',
            ].join(' ')}
          >
            <div className="truncate font-medium text-ink">{s.title || s.question}</div>
            <div className="mt-0.5 flex items-center gap-2 text-[11px] text-muted">
              <span>{s.status}</span>
              {s.archived_at && <span className="rounded bg-gray-200 px-1">已归档</span>}
              {s.cost_tokens > 0 && <span>{Math.round(s.cost_tokens / 1000)}k token</span>}
            </div>
          </button>
        ))}
      </div>
    </nav>
  )
}

export default function App() {
  const [sid, setSid] = useState<string | null>(null)
  const [list, setList] = useState<SessionSummary[]>([])
  const s = useSession(sid)

  const refresh = useCallback(async () => {
    try { setList(await api.jget<SessionSummary[]>('/api/sessions')) } catch { /* 忽略 */ }
  }, [])

  useEffect(() => { void refresh() }, [refresh, s.view])

  async function start(question: string) {
    const id = await s.create(question)
    setSid(id)
    await refresh()
    void s.start(id)
  }

  const d = s.detail
  const report = d?.report ?? ''

  return (
    <div className="flex h-screen text-ink">
      <Sidebar
        list={list}
        cur={sid}
        onPick={setSid}
        onNew={() => {
          const q = prompt('要研究什么问题？')
          if (q) void start(q)
        }}
      />

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-line bg-white px-6 py-3">
          <div className="min-w-0">
            <div className="truncate text-sm font-medium">
              {d?.title || d?.question || '深度研究'}
            </div>
            {d && (
              <div className="mt-0.5 text-xs text-muted">
                {d.status}
                {d.cost_tokens > 0 && ` · ${d.cost_tokens.toLocaleString()} token`}
                {d.sources.length > 0 && ` · ${d.sources.length} 来源`}
              </div>
            )}
          </div>
          {d && !d.archived_at && d.status === 'done' && (
            <button
              onClick={() => void s.archive(d.id).then(refresh)}
              className="rounded border border-line px-3 py-1 text-xs text-muted hover:bg-gray-50"
            >
              归档（进入历史召回）
            </button>
          )}
        </header>

        {s.view === 'idle' && !sid && (
          <div className="flex flex-1 flex-col items-center justify-center text-muted">
            <p className="text-sm">左侧「新建研究」，或点一条历史会话</p>
          </div>
        )}

        {(s.view === 'running' || (s.view === 'idle' && sid && !report)) && (
          <>
            <StageBar current={s.node} />
            <div className="flex-1 overflow-auto">
              <Timeline progress={s.progress} />
            </div>
          </>
        )}

        {s.view === 'awaiting' && s.approval && (
          <div className="flex-1 overflow-auto">
            <ApprovalView approval={s.approval} onApprove={() => void s.approve(sid!)} />
          </div>
        )}

        {s.view === 'done' && report && (
          <div className="flex-1 overflow-auto">
            <ReportView markdown={report} onRef={(r) => {
              console.log('[ref-click]', r, sid)
              void s.openSeg(sid ?? '', r)
            }} />
          </div>
        )}

        {s.view === 'done' && !report && (
          <div className="flex flex-1 flex-col items-center justify-center gap-3 text-muted">
            <p className="text-sm">
              这次研究没有产出报告正文（回放模式下尤其常见）。
            </p>
            <p className="text-xs">
              左侧选一条已完成的会话，就能看到完整报告和引用。
            </p>
          </div>
        )}

        {s.view === 'error' && (
          <div className="px-6 py-4 text-sm text-red-600">
            出错了：{s.error}
            {d?.report ? (
              <button className="ml-3 underline" onClick={() => s.setView('done')}>看已产出的报告</button>
            ) : null}
          </div>
        )}

        {/* 报告的来源清单 */}
        {s.view === 'done' && d && d.sources.length > 0 && (
          <div className="border-t border-line px-6 py-3 text-xs text-muted">
            来源：{d.sources.slice(0, 12).map((x, i) => (
              <span key={x.id} className="mr-3 inline-flex items-center gap-1">
                <span>[S{i + 1}]</span><NatureBadge nature={x.source_nature} />
              </span>
            ))}
            {d.sources.length > 12 && ` …共 ${d.sources.length} 个`}
          </div>
        )}
      </main>

      <SegDrawer seg={s.seg} onClose={() => s.setSeg(null)} />
    </div>
  )
}
