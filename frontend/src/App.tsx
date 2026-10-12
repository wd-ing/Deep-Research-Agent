import { useCallback, useEffect, useState } from 'react'
import { useSession, api } from './useSession'
import type { Folder, SessionSummary, SourceLibrary, SourceDetailRow } from './types'
import { ApprovalView } from './views/ApprovalView'
import { ReportView } from './views/ReportView'
import { WorkflowView } from './views/WorkflowView'
import { buildPhases } from './views/workflow'
import { SegDrawer, NatureBadge } from './components/SegDrawer'
import { Sidebar, type Pane } from './components/Sidebar'
import { SourceDetail } from './components/SourceDetail'

export default function App() {
  const [sid, setSid] = useState<string | null>(null)
  const [list, setList] = useState<SessionSummary[]>([])
  const [trash, setTrash] = useState<SessionSummary[]>([])
  const [folders, setFolders] = useState<Folder[]>([])
  const s = useSession(sid)

  // ---- 来源库（产品化阶段 4）----
  const [pane, setPane] = useState<Pane>('session')
  const [lib, setLib] = useState<SourceLibrary | null>(null)
  const [srcQ, setSrcQ] = useState('')
  const [srcCur, setSrcCur] = useState<string | null>(null)
  const [srcDetail, setSrcDetail] = useState<SourceDetailRow | null>(null)

  // ⚠️ `/api/sessions` 里的 `deleted_at IS NULL` 是**硬过滤**（server.py:114），
  // 回收站的会话根本不在这一份里。必须单独打 `/api/trash`，
  // 否则侧栏「回收站」档永远只能拿到空数组。
  const refresh = useCallback(async () => {
    try { setList(await api.jget<SessionSummary[]>('/api/sessions')) } catch { /* 忽略 */ }
    try { setTrash(await api.jget<SessionSummary[]>('/api/trash')) } catch { /* 忽略 */ }
    try { setFolders(await api.jget<Folder[]>('/api/folders')) } catch { /* 忽略 */ }
  }, [])

  /** 来源库**只在切到来源视图时才拉**——它要按 session 建段号索引算引用数，
   *  比会话列表重得多；不看见就别付这个成本。 */
  const refreshLib = useCallback(async (q: string) => {
    try {
      setLib(await api.jget<SourceLibrary>(
        `/api/sources${q ? `?q=${encodeURIComponent(q)}` : ''}`))
    } catch { /* 忽略 */ }
  }, [])

  useEffect(() => { void refresh() }, [refresh, s.view])

  // 搜索词防抖：每敲一个字都重跑一遍「建全部场次的段号索引」太浪费
  useEffect(() => {
    if (pane !== 'source') return
    const t = setTimeout(() => void refreshLib(srcQ), 250)
    return () => clearTimeout(t)
  }, [pane, srcQ, refreshLib])

  const pickSource = useCallback(async (id: string) => {
    setSrcCur(id)
    try { setSrcDetail(await api.jget<SourceDetailRow>(`/api/sources/${id}`)) }
    catch { setSrcDetail(null) }
  }, [])

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
        trash={trash}
        cur={sid}
        folders={folders}
        onPick={setSid}
        onChanged={refresh}
        onNew={() => {
          const q = prompt('要研究什么问题？')
          if (q) void start(q)
        }}
        pane={pane}
        onPane={(p) => { setPane(p); if (p === 'session') setSrcCur(null) }}
        lib={lib}
        srcQuery={srcQ}
        onSrcQuery={setSrcQ}
        srcCur={srcCur}
        onPickSource={(id) => void pickSource(id)}
      />

      {/* 来源视图占满整个主区域：报告有 2 万字，来源详情是主内容，
          没必要再套一层会话的头部 */}
      {pane === 'source' ? (
        <main className="flex min-w-0 flex-1 flex-col">
          <SourceDetail
            src={srcDetail}
            onOpenSession={(id) => { setPane('session'); setSid(id) }}
          />
        </main>
      ) : (
      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-line bg-white px-6 py-3 shadow-[0_1px_2px_rgba(0,0,0,0.03)]">
          <div className="min-w-0">
            <div className="truncate text-sm font-medium">
              {d?.title || d?.question || '深度研究'}
            </div>
            {d && (
              <div className="mt-0.5 text-xs text-muted">
                <span className="inline-flex items-center gap-1.5">
                <span className={[
                  'inline-block h-1.5 w-1.5 rounded-full',
                  d.status === 'running' ? 'wf-running-dot bg-accent' : 'bg-gray-300',
                ].join(' ')} />
                {d.status}
              </span>
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
              归档（不出现在默认列表）
            </button>
          )}
        </header>

        {s.view === 'idle' && !sid && (
          <div className="flex flex-1 flex-col items-center justify-center text-muted">
            <p className="text-sm">左侧「新建研究」，或点一条历史会话</p>
          </div>
        )}

        {/* 待审批时**只显示审批视图**——规划环节已经跑完，
            审批页本身就是结论，不需要再叠一层工作流 */}
        {(s.view === 'running'
          || (s.view === 'idle' && sid && !report)) && (
          <div className="flex-1 overflow-auto">
            <WorkflowView
              phases={buildPhases(s.progress, s.node, s.view)}
              running={s.view === 'running'}
            />
          </div>
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
      )}

      {pane === 'session' && <SegDrawer seg={s.seg} onClose={() => s.setSeg(null)} />}
    </div>
  )
}
