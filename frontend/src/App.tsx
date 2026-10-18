import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSession, api } from './useSession'
import { nav, useRoute } from './router'
import type { Folder, SessionSummary, SourceLibrary, SourceDetailRow, SegPreviews } from './types'
import { ApprovalView } from './views/ApprovalView'
import { ReportView } from './views/ReportView'
import { WorkflowView } from './views/WorkflowView'
import { buildPhases } from './views/workflow'
import { SegDrawer, NatureBadge } from './components/SegDrawer'
import { DebugPanel } from './components/DebugPanel'
import { RoundChart } from './components/RoundChart'
import { Sidebar, type Pane } from './components/Sidebar'
import { SourceDetail } from './components/SourceDetail'
import { SettingsView } from './views/SettingsView'
import { NewResearchDialog } from './components/NewResearchDialog'
import { Icon } from './components/Icon'
import { ago, tokens } from './lib/format'

export default function App() {
  // ⚠️ sid / pane / srcCur 不是 state——它们由 URL hash 派生（router.ts）。
  //  想切换就 nav()，hashchange 会回流驱动这里的重渲染。
  //  这样刷新/前进后退/深链/分享链接全部免费。
  const route = useRoute()
  const sid = route.name === 'session' ? route.sid : null
  const pane: Pane = route.name === 'sources' ? 'source' : 'session'
  const srcId = route.name === 'sources' ? route.srcId ?? null : null
  const [list, setList] = useState<SessionSummary[]>([])
  const [trash, setTrash] = useState<SessionSummary[]>([])
  const [folders, setFolders] = useState<Folder[]>([])
  const s = useSession(sid)

  // ---- 来源库（产品化阶段 4）----
  const [lib, setLib] = useState<SourceLibrary | null>(null)
  /** 失败原因单独存。`lib === null` 既表示「加载中」也表示「加载失败」，
   *  只靠它判断的话来源库列表会永远停在「来源库加载中…」。 */
  const [libErr, setLibErr] = useState<string | null>(null)
  const [srcQ, setSrcQ] = useState('')
  const [srcDetail, setSrcDetail] = useState<SourceDetailRow | null>(null)

  // ---- 引用 hover 预览（seg_preview）----
  // ⚠️ state 声明可以放前面，**取数的 effect 必须放在 `const d = s.detail` 之后**——
  // `d` 在下面才声明，提前引用会踩到暂时性死区（tsc 查不出来，它不报 TDZ）。
  const [previews, setPreviews] = useState<SegPreviews>({})

  // ---- 新建研究对话框（替掉原生 prompt）----
  const [newOpen, setNewOpen] = useState(false)
  // 侧栏折叠（E2 专注阅读）：内存态即可——不进 URL，
  // 「我暂时想宽点看报告」不该变成分享链接的一部分。
  const [sideCollapsed, setSideCollapsed] = useState(false)
  // 回放模式探测：/api/replay/state 只在回放服务器上存在。
  // null=还没探完/真后端（按钮不显示）；false=探测失败。
  const [replayOn, setReplayOn] = useState(false)
  useEffect(() => {
    api.jget('/api/replay/state').then(() => setReplayOn(true)).catch(() => {})
  }, [])

  /** 「演示回放」：建回放会话 + 启动 + 跳过去。只出现在回放模式。
   *  speed=0.6：fixture 时间轴是压缩过的（19s+29s），1.0 会闪完，
   *  0.6 全程约 80 秒——够看清每个环节在干什么。 */
  async function runReplay() {
    try {
      const r = await api.jpost<{ session_id: string }>('/api/replay/start?speed=0.6')
      await refresh()
      nav({ name: 'session', sid: r.session_id })
      void s.start(r.session_id).catch((e: unknown) =>
        s.notify(`回放会话已建好，但启动失败：${(e as Error).message}`))
    } catch (e) {
      s.notify(`起不了回放：${(e as Error).message}`)
    }
  }

  // 报告正文的滚动容器。目录跳转、阅读进度、当前节高亮全靠它——
  // 滚动的是这个容器，不是报告自己（报告是里面一块静态内容）。
  const reportScrollRef = useRef<HTMLDivElement>(null)

  // ⚠️ `/api/sessions` 里的 `deleted_at IS NULL` 是**硬过滤**（server.py:114），
  // 回收站的会话根本不在这一份里。必须单独打 `/api/trash`，
  // 否则侧栏「回收站」档永远只能拿到空数组。
  const refresh = useCallback(async () => {
    let failed = 0
    try { setList(await api.jget<SessionSummary[]>('/api/sessions')) } catch { failed++ }
    try { setTrash(await api.jget<SessionSummary[]>('/api/trash')) } catch { failed++ }
    try { setFolders(await api.jget<Folder[]>('/api/folders')) } catch { failed++ }
    // **静默失败在这里最坏**：服务没起来时侧栏就是个空列表，用户会理解成
    // 「我没有历史会话」而不是「服务挂了」——那会让他去找根本不存在的 bug。
    // 三条全挂 = 服务不可达；只挂一条 = 局部问题，都得说。
    if (failed === 3) s.notify('连不上后端服务。侧栏是空的——那是拿不到数据，不是不存在数据。')
    else if (failed > 0) s.notify(`侧栏有 ${failed} 项没加载出来，其余正常`)
  }, [s.notify])

  /** 来源库**只在切到来源视图时才拉**——它要按 session 建段号索引算引用数，
   *  比会话列表重得多；不看见就别付这个成本。 */
  const refreshLib = useCallback(async (q: string) => {
    try {
      setLib(await api.jget<SourceLibrary>(
        `/api/sources${q ? `?q=${encodeURIComponent(q)}` : ''}`))
      setLibErr(null)
    } catch (e) {
      setLib(null)
      const msg = (e as Error).message
      setLibErr(msg)
      s.notify(`来源库加载失败：${msg}`)
    }
  }, [s.notify])

  useEffect(() => { void refresh() }, [refresh, s.view])

  // 搜索词防抖：每敲一个字都重跑一遍「建全部场次的段号索引」太浪费
  useEffect(() => {
    if (pane !== 'source') return
    const t = setTimeout(() => void refreshLib(srcQ), 250)
    return () => clearTimeout(t)
  }, [pane, srcQ, refreshLib])

  const pickSource = useCallback(async (id: string) => {
    try { setSrcDetail(await api.jget<SourceDetailRow>(`/api/sources/${id}`)) }
    catch (e) {
      setSrcDetail(null)
      // 详情页会把 null 渲染成「左侧来源库里点一条」——但列表里其实
      // 已经有选中态了，两边对不上，不说明原因用户会一直点。
      s.notify(`打不开这个来源：${(e as Error).message}`)
    }
  }, [s.notify])

  // URL 是来源选中态的真相源：进入 #/sources/{id} 就取详情，
  // 离开来源视图或 hash 没带 id 就清掉——深链进来和点列表走同一条路。
  useEffect(() => {
    if (!srcId) { setSrcDetail(null); return }
    let alive = true
    void api.jget<SourceDetailRow>(`/api/sources/${srcId}`)
      .then((d) => { if (alive) setSrcDetail(d) })
      .catch((e) => {
        if (!alive) return
        setSrcDetail(null)
        s.notify(`打不开这个来源：${(e as Error).message}`)
      })
    return () => { alive = false }
  }, [srcId, s.notify])

  /** 标记 / 取消排除。三处视图都读这个状态，必须一起刷新：
   *  来源库列表、当前打开的来源详情、以及**当前会话的报告提示条**
   *  —— 少刷一个就会出现「标记成功了但报告顶部还是老样子」。 */
  const ignoreSource = useCallback(
    async (id: string, ignore: boolean, reason: string) => {
      try {
        await api.jpost(`/api/sources/${id}/${ignore ? 'ignore' : 'unignore'}`, { reason })
      } catch (e) {
        // ⚠️ 这条**必须**提示，不能像以前那样「下次刷新会露出真值」。
        // 「标记排除」是用户对报告可信度的判断：以为标上了、其实没存，
        // 等于用户以为某条证据已作废，它却还留在报告结论里——这比不标更糟。
        s.notify(`标记失败，这个来源仍是原状态：${(e as Error).message}`)
        return
      }
      await refreshLib(srcQ)
      // 按**刚改的那条**判断要不要刷详情，不是按「当前选中的那条」。
      // 两者可以不是一回事：在列表里标记 A，右边打开的却是 B。
      if (srcDetail?.id === id) await pickSource(id)
      if (sid) await s.loadDetail(sid)
    },
    [refreshLib, srcQ, srcDetail, pickSource, sid, s.loadDetail, s.notify],
  )

  /** 报告提示条上的来源名 → 跳来源库并选中它（兑现「可在来源库中查看」）。
   *  跳转走 nav()——hash 变化会驱动 pane/srcId 一起换，详情由上面的
   *  srcId effect 取，不用在这里手动 pickSource。 */
  const openSourceFromReport = useCallback((id: string) => {
    nav({ name: 'sources', srcId: id })
    setSrcQ('')
    void refreshLib('')
  }, [refreshLib])

  async function start(question: string) {
    // ⚠️ 建会话失败**不能**让对话框自己关掉、更不能默默 reject。
    // 原来 `onSubmit={(q) => void start(q)}` + `await s.create()` 无 catch，
    // 网络一抖就是 unhandled rejection：对面上卡在「正在创建…」，
    // 控制台才有线索（项目里也没有 unhandledrejection 兜底）。
    // 现在失败就**留在原地 + 说原因**，用户改个问题重试即可。
    let id: string
    try {
      id = await s.create(question)
    } catch (e) {
      s.notify(`建不了这场研究：${(e as Error).message}`)
      return
    }
    setNewOpen(false)
    nav({ name: 'session', sid: id })
    await refresh()
    // 启动是**另一个**请求，它失败时会议话已经建好了——
    // 提示要说清「会话已建好，只是没启动」，否则用户会去重复建。
    void s.start(id).catch((e: unknown) =>
      s.notify(`会话已建好，但启动失败：${(e as Error).message}`))
  }

  const d = s.detail
  const report = d?.report ?? ''

  // `buildPhases` 是纯函数，但每次渲染都重算会造出一个**新数组**——
  // `WorkflowView` 的 `useEffect([phases])` 于是每次渲染都触发一次
  // `scrollIntoView`，用户正在往上翻历史也会被拽回底部。
  const phases = useMemo(
    () => buildPhases(s.progress, s.node, s.view),
    [s.progress, s.node, s.view]
  )

  /** 切会话的过渡态：`sid` 有了、`detail` 还没到（`useSession` 里会先置空）。
   *  没有它的话这个窗口里主区域什么都不渲染——看着像点了没反应。 */
  const switching = !!sid && s.view === 'idle' && !d

  // B4 debug 后门：?debug=1 时挂载 thinking/draft 原始流面板。
  // 在 location.search 里，与 hash 路由互不干扰。
  const debug = ['1', 'true', 'yes'].includes(
    new URLSearchParams(window.location.search).get('debug') ?? '')

  // ---- B3 离开友好：标签页标题就是状态 ----
  // 一场研究 20+ 分钟，正确姿势包括「你不用盯着」——切走之后
  // 标签页标题要替界面说话。节点名翻译成用户语言，不暴露内部名。
  useEffect(() => {
    const NODE_LABEL: Record<string, string> = {
      probe: '探测', outline: '定框架', retrieve: '检索', extract: '抽取',
      gate: '证据闸门', assess: '评估', apply_revision: '校准框架',
      await_approval: '待审批', write: '写作', finalize: '收尾', summary: '收尾',
    }
    const t = d?.title || d?.question || '深度研究'
    if (route.name === 'sources') {
      document.title = '来源库 · 深度研究'
    } else if (!sid) {
      document.title = '深度研究'
    } else if (s.view === 'running') {
      document.title = `⏳ ${(s.node && NODE_LABEL[s.node]) || s.node || '启动中'} · ${t}`
    } else if (s.view === 'awaiting') {
      document.title = `✋ 待审批 · ${t}`
    } else if (s.view === 'done') {
      document.title = `✓ ${t}`
    } else if (s.view === 'error') {
      document.title = `✗ 出错 · ${t}`
    } else {
      document.title = t
    }
  }, [route.name, sid, s.view, s.node, d?.title, d?.question])

  /** 引用 hover 预览。**只在有报告时取**：没有报告就没有引用可 hover。
   *  换会话时先清空——留着上一场的预览，新报告里的段号
   *  恰好同名就会显示别人的内容（段号是按 rowid 编的，
   *  两个会话里 S1·1 指的是完全不同的段）。 */
  useEffect(() => {
    setPreviews({})
    if (!sid || !d?.report) return
    let alive = true
    api.jget<SegPreviews>(`/api/sessions/${sid}/seg_preview`)
      .then((r) => { if (alive) setPreviews(r) })
      .catch(() => { if (alive) setPreviews({}) })
    return () => { alive = false }
  }, [sid, d?.report])

  return (
    <div className="flex h-screen text-ink">
      <Sidebar
        list={list}
        trash={trash}
        cur={sid}
        folders={folders}
        onPick={(id) => nav({ name: 'session', sid: id })}
        onChanged={refresh}
        onNew={() => setNewOpen(true)}
        pane={pane}
        // 切换视图走 nav()：来源→会话回到正在看的会话（没有就回首页），
        // 会话→来源进来源库首页。hash 是唯一真相源，详情清理由
        // srcId effect 的「!srcId → 清空」分支兜住。
        onPane={(p) => nav(
          p === 'source' ? { name: 'sources' }
          : sid ? { name: 'session', sid } : { name: 'home' })}
        lib={lib}
        libErr={libErr}
        onRetryLib={() => void refreshLib(srcQ)}
        onNotify={s.notify}
        srcQuery={srcQ}
        onSrcQuery={setSrcQ}
        srcCur={srcId}
        onPickSource={(id) => nav({ name: 'sources', srcId: id })}
        onIgnoreSource={(id, ignore, reason) => void ignoreSource(id, ignore, reason)}
        onSettings={() => nav({ name: 'settings' })}
        onReplay={replayOn ? runReplay : null}
        collapsed={sideCollapsed}
        onToggleCollapse={() => setSideCollapsed((v) => !v)}
      />

      {/* 来源视图占满整个主区域：报告有 2 万字，来源详情是主内容，
          没必要再套一层会话的头部。设置页同理——整页编辑器。 */}
      {route.name === 'settings' ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-auto">
          <SettingsView onNotify={s.notify} />
        </main>
      ) : pane === 'source' ? (
        <main className="flex min-w-0 flex-1 flex-col">
          <SourceDetail
            src={srcDetail}
            onOpenSession={(id) => nav({ name: 'session', sid: id })}
            onToggleIgnore={(id, ignored) => void ignoreSource(id, !ignored, '')}
          />
        </main>
      ) : (
      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-line bg-white px-6 py-3 shadow-[0_1px_2px_rgba(0,0,0,0.03)] print:hidden">
          <div className="min-w-0">
            <div className={[
              'truncate text-sm font-medium',
              report && s.view === 'done' ? 'font-serif text-base' : '',
            ].join(' ')}>
              {d?.title || d?.question || '深度研究'}
            </div>
            {d && (
              <div className="mt-0.5 text-xs text-muted">
                <span className="inline-flex items-center gap-1.5">
                <span className={[
                  'inline-block h-1.5 w-1.5 rounded-full',
                  d.status === 'running' ? 'wf-running-dot bg-live' : 'bg-gray-300',
                ].join(' ')} />
                {d.status}
              </span>
                {d.degraded && (
                  <span
                    className="rounded bg-amber-100 px-1.5 py-0.5 text-2xs text-amber-800"
                    title={d.degraded}
                  >降级产出</span>
                )}
                {d.cost_tokens > 0 && ` · ${d.cost_tokens.toLocaleString()} token`}
                {d.sources.length > 0 && ` · ${d.sources.length} 来源`}
              </div>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {d && s.view === 'done' && report && (
              <>
                {/* 报告是终点不是终点——流程还在后面一页能回看 */}
                <button className="btn-ghost" onClick={() => s.setView('flow')}>
                  <Icon name="chevrons-left" size={13} />回看流程
                </button>
                {/* C1 导出：复制 / 下载 / 打印（打印即 PDF）。
                    clipboard 需要 secure context——localhost 与 https 都是，
                    file:// 直开才会落空，落空就明说。 */}
                <button
                  onClick={() => void navigator.clipboard.writeText(report)
                    .then(() => s.notify('已复制报告 Markdown', 'ok'))
                    .catch(() => s.notify('复制失败：浏览器没给剪贴板权限'))}
                  className="btn-ghost"
                >
                  <Icon name="copy" size={13} />复制 MD
                </button>
                <button
                  onClick={() => {
                    const name = `${(d.title || '报告').replace(/[\\/:*?"<>|]/g, '')}.md`
                    const url = URL.createObjectURL(
                      new Blob([report], { type: 'text/markdown;charset=utf-8' }))
                    const a = document.createElement('a')
                    a.href = url; a.download = name; a.click()
                    URL.revokeObjectURL(url)
                  }}
                  className="btn-ghost"
                >
                  <Icon name="download" size={13} />下载 .md
                </button>
                <button
                  onClick={() => window.print()}
                  className="btn-ghost"
                >
                  <Icon name="printer" size={13} />打印 / PDF
                </button>
              </>
            )}
            {d && !d.archived_at && d.status === 'done' && (
              <button
                onClick={() => void s.archive(d.id).then(refresh).catch(
                    (e: unknown) => s.notify(`归档失败：${(e as Error).message}`))}
                className="btn-ghost"
              >
                归档（不出现在默认列表）
              </button>
            )}
          </div>
        </header>

        {/* 检索轮次图。**运行中不显示**——那时候工作流列表正在逐轮叙述
            （「检索（第 N 轮）· 累计 X 段」），图上是重复信息；
            要看的是停下来之后回头看：为什么停在这、这几轮各捞到什么。 */}
        {sid && s.view !== 'running' && (
          <RoundChart sid={sid} refreshKey={s.progress.length} />
        )}

        {/* 欢迎面板（E3）：首屏不该只有一行提示。
            三条信息：这是什么、入口在哪、以前在做什么（最近会话卡片）。 */}
        {s.view === 'idle' && !sid && (
          <div className="flex flex-1 items-start justify-center overflow-auto px-6 py-14">
            <div className="w-full max-w-xl">
              {/* 衬线标题是「文档感」的签名件——首页先立住这个气质 */}
              <h1 className="font-serif text-2xl font-medium text-ink">深度研究</h1>
              <p className="mt-2 max-w-md text-sm leading-6 text-muted">
                提出一个问题，agent 会做多轮检索和评估，产出带逐句引用的长报告。
                一场研究约 20–30 分钟，过程中随时可以离开，完成后回来看结果。
              </p>
              <button
                onClick={() => setNewOpen(true)}
                className="btn-primary mt-6 px-4 py-2 text-sm"
              >
                <Icon name="plus" size={14} />新建研究
              </button>
              {list.filter((x) => !x.archived_at).length > 0 && (
                <>
                  <div className="mt-10 text-xs font-medium text-muted">继续之前的</div>
                  <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
                    {list.filter((x) => !x.archived_at).slice(0, 6).map((x) => (
                      <button
                        key={x.id}
                        onClick={() => nav({ name: 'session', sid: x.id })}
                        className="rounded-md border border-line bg-white px-3 py-2.5 text-left transition-shadow hover:shadow-sm"
                      >
                        <div className="truncate text-sm text-ink">
                          {x.title || x.question}
                        </div>
                        <div className="mt-1 flex items-center gap-1.5 text-2xs text-muted">
                          <span className={[
                            'inline-block h-1.5 w-1.5 rounded-full',
                            x.status === 'running' ? 'wf-running-dot bg-live' : 'bg-gray-300',
                          ].join(' ')} />
                          <span>{x.status === 'running' ? '进行中' : x.status === 'done' ? '已完成' : x.status}</span>
                          <span>· {ago(x.created_at)}</span>
                          {x.cost_tokens > 0 && <span>· {tokens(x.cost_tokens)} token</span>}
                        </div>
                      </button>
                    ))}
                  </div>
                </>
              )}
            </div>
          </div>
        )}

        {switching && (
          <div className="flex flex-1 flex-col items-center justify-center gap-2 text-muted">
            <div className="h-4 w-4 animate-spin rounded-full border-2 border-line border-t-accent" />
            <p className="text-sm">正在打开…</p>
          </div>
        )}

        {/* 待审批时**只显示审批视图**——规划环节已经跑完，
            审批页本身就是结论，不需要再叠一层工作流 */}
        {(s.view === 'running' || s.view === 'flow'
          || (s.view === 'idle' && sid && !report)) && (
          <div className="flex-1 overflow-auto">
            {/* flow = done 之后回来看流程：顶部留一个回去的口 */}
            {s.view === 'flow' && (
              <div className="sticky top-0 z-30 flex items-center justify-between
                              border-b border-line bg-white/95 px-6 py-2 backdrop-blur">
                <span className="text-xs text-muted">研究流程（已完成）</span>
                {report && (
                  <button className="btn-ghost" onClick={() => s.setView('done')}>
                    查看报告 →
                  </button>
                )}
              </div>
            )}
            <WorkflowView
              phases={phases}
              running={s.view === 'running'}
              runningSince={s.nodeAt}
              cost={s.cost}
            />
            {debug && <DebugPanel thinking={s.thinking} draft={s.draft} />}
          </div>
        )}

        {s.view === 'awaiting' && s.approval && (
          <div className="flex-1 overflow-auto">
            <ApprovalView approval={s.approval} onApprove={() => void s.approve(sid!).catch(
              (e: unknown) => s.notify(`批准失败：${(e as Error).message}`))} />
            {/* 批准之前想看一眼裁判在想什么——debug 面板挂这儿正好 */}
            {debug && <DebugPanel thinking={s.thinking} draft={s.draft} />}
          </div>
        )}

        {s.view === 'done' && report && (
          /* 抽屉打开时锁住这个容器——滚的是它，不是 body。
             锁 body 没用：它压根不滚，报告会从抽屉底下划过去。 */
          <div ref={reportScrollRef} className={s.seg ? 'flex-1 overflow-hidden print:overflow-visible' : 'flex-1 overflow-auto print:overflow-visible'}>
            <ReportView
              scrollRef={reportScrollRef}
              markdown={report}
              previews={previews}
              ignoredCitations={d?.ignored_citations ?? []}
              onOpenSource={openSourceFromReport}
              onRef={(r) => { void s.openSeg(sid ?? '', r) }}
            />
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
          <div className="border-t border-line px-6 py-3 text-xs text-muted print:hidden">
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

      {/* 全局忙碌指示：任何请求在飞时，顶部一条 2px 的不定长进度线。
          `busy` 是计数器不是布尔值（并发请求时布尔值会被先完成的那次清掉）。
          它覆盖的是「点了之后有一小段没反应」的那些请求——建会话、启动、
          审批、切换会话取详情。 */}
      {s.busy && (
        <div className="fixed inset-x-0 top-0 z-40 h-0.5 overflow-hidden bg-line" aria-hidden>
          <div className="h-full w-1/3 animate-pulse bg-accent" />
        </div>
      )}

      {pane === 'session' && <SegDrawer seg={s.seg} onClose={() => s.setSeg(null)} />}

      {/* 新建研究。`list` 已经在手上了，「最近问过」不用额外请求。
          只给未归档的——归档是「我不想再看了」，摆进新建框里是干扰。 */}
      {newOpen && (
        <NewResearchDialog
          busy={s.busy}
          recent={list.filter((x) => !x.archived_at).slice(0, 4)
            .map((x) => ({ id: x.id, question: x.question }))}
          onCancel={() => setNewOpen(false)}
          onSubmit={(q) => void start(q)}
        />
      )}

      {/* 一次操作没成的提示（2026-10-13 加）。
          `role="status" + aria-live`：屏幕阅读器能听到——项目里已有
          aria 用得很少，这条是最需要「机器也能感知」的一处（静默失败正是
          无障碍问题的一部分 sighted 用户能看见、盲用户连「没反应」都感知不到）。
          `⚠` 只在这里出现：它表示「某件事没成功，需要你知道」。 */}
      {s.notice && (
        <div
          role="status"
          aria-live="polite"
          className={[
            'fixed bottom-5 left-1/2 z-50 flex max-w-[min(600px,calc(100vw-32px))]',
            '-translate-x-1/2 items-start gap-2 rounded-md border bg-white',
            'px-3.5 py-2.5 shadow-lg print:hidden',
            s.notice.kind === 'err' ? 'border-red-200' : 'border-line',
          ].join(' ')}
        >
          <span
            className={s.notice.kind === 'err'
              ? 'mt-px shrink-0 text-red-500' : 'mt-px shrink-0 text-live'}
            aria-hidden
          >
            <Icon name={s.notice.kind === 'err' ? 'alert' : 'check'} size={14} strokeWidth={2} />
          </span>
          <span className="min-w-0 flex-1 text-xs leading-relaxed text-ink">{s.notice.msg}</span>
          <button
            onClick={() => s.setNotice(null)}
            aria-label="关闭提示"
            className="shrink-0 text-muted hover:text-ink"
          ><Icon name="x" size={13} /></button>
        </div>
      )}
    </div>
  )
}
