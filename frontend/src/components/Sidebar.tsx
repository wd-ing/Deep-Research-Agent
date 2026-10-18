import { useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { api } from '../useSession'
import type { Folder, SearchHit, SessionSummary, SourceLibrary } from '../types'
import { FolderTree, buildTree, findNodeById, subtreeIds, folderChoices } from './FolderTree'
import { ago, tokens } from '../lib/format'
import { Menu, MenuItem } from './Menu'
import { ConfirmDialog } from './ConfirmDialog'
import { SourceList } from './SourceList'

type Filter = 'all' | 'running' | 'archived' | 'trash'
export type Pane = 'session' | 'source'

/** 每 30 秒重渲染一次，让相对时间自己往前走。
 *
 *  `ago()` 是渲染期纯函数、读 `Date.now()`——不重渲染它就永远是那个值。
 *  于是「59 分钟前」能卡上一整天，直到别的事件碰巧触发一次重渲染。
 *  30 秒是「相对时间基本够新」和「别白烧」之间的折中：
 *  一分钟级的显示精度，代价是每 30 秒重画一个侧栏。
 *
 *  ⚠️ 和 `WorkflowView` 里清掉的那个 `force` 空转**不是一回事**：
 *  那个触发一次谁也不读的重渲染，这里是真的要把新时间画出来。 */
function useTick(ms = 30_000) {
  const [, tick] = useReducer((x: number) => x + 1, 0)
  useEffect(() => {
    const t = window.setInterval(tick, ms)
    return () => window.clearInterval(t)
  }, [ms])
}

// ago/tokens 提到 lib/format——欢迎面板（E3）也要用同一套写法。

/** 一条会话。**hover 才出操作按钮**，平时干净。 */
function SessionRow({
  s, active, choices, onOpen, onRename, onArchive, onDelete, onMove,
}: {
  s: SessionSummary
  active: boolean
  /** 「移到文件夹」菜单的候选项（带缩进）。没建过文件夹就为空数组。 */
  choices: { id: string | null; label: string; depth: number }[]
  onOpen: () => void
  onRename: (title: string) => void
  onArchive: (v: boolean) => void
  onDelete: () => void
  onMove: (folderId: string | null) => void
}) {
  // 心跳挂在**行**上而不是 Sidebar 上：只有行里有相对时间要刷新，
  // 让整个侧栏每 30 秒重画一遍没有意义。
  useTick()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(s.title ?? '')
  const [moveAt, setMoveAt] = useState<HTMLElement | null>(null)
  /** 摘要的要点/缺口是否展开。默认收起——列表要能扫。 */
  const [showSum, setShowSum] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (editing) { inputRef.current?.focus(); inputRef.current?.select() }
  }, [editing])

  const commit = () => {
    const t = draft.trim()
    if (t && t !== s.title) onRename(t)
    setEditing(false)
  }

  return (
    <div
      className={[
        'group relative mb-0.5 rounded-md px-3 py-2 transition-colors',
        active ? 'sel' : 'hover:bg-white',
      ].join(' ')}
    >
      {editing ? (
        <input
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === 'Enter') commit()
            if (e.key === 'Escape') setEditing(false)
          }}
          className="w-full rounded border border-accent px-1.5 py-0.5 text-sm outline-none"
        />
      ) : (
        <button onClick={onOpen} className="block w-full text-left">
          <div className="flex items-start gap-1.5">
            {s.status === 'running' && (
              <span className="wf-running-dot mt-1.5 inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-live" />
            )}
            {s.archived_at && (
              <span className="mt-0.5 shrink-0 text-2xs text-muted">📌</span>
            )}
            <span className="min-w-0 flex-1 truncate text-sm text-ink">
              {s.title || s.question}
            </span>
          </div>
          <div className="mt-0.5 flex items-center gap-2 pl-3 text-2xs text-muted">
            {s.degraded && (
              <span
                className="rounded bg-amber-100 px-1 text-amber-800"
                title={s.degraded}
              >降级产出</span>
            )}
            {s.status !== 'done' && <span>{s.status}</span>}
            {s.cost_tokens > 0 && <span>{tokens(s.cost_tokens)} token</span>}
            <span>{ago(s.created_at)}</span>
          </div>
        </button>
      )}

      {/* 摘要：这一条就是「不用点进报告就知道讲了什么」的兑现处。
          ⚠️ 展开按钮是外层按钮的**兄弟**而不是子节点——
          塞进去就成了 <button> 套 <button>，非法 HTML，点击行为不可靠。
          只显示 `summary` 一行；要点和缺口点了才展开——列表要能扫。 */}
      {!editing && s.summary?.summary && (
        <div className="pl-3">
          <button
            onClick={() => setShowSum((v) => !v)}
            className="block w-full text-left text-2xs leading-snug text-muted hover:text-ink"
            title={s.summary.summary}
          >
            <span className="mr-1 inline-block w-2 align-middle text-gray-400">
              {showSum ? '▾' : '▸'}
            </span>
            {s.summary.summary}
          </button>
          {showSum && (
            <div className="mt-1 space-y-1 border-l border-line pl-2 text-2xs leading-snug">
              {(s.summary.key_points ?? []).map((p, i) => (
                <div key={i} className="flex gap-1 text-muted">
                  <span className="shrink-0 text-gray-400">·</span>
                  <span className="min-w-0">{p}</span>
                </div>
              ))}
              {(s.summary.gaps ?? []).length > 0 && (
                <div className="mt-1.5 border-t border-line pt-1">
                  <div className="mb-0.5 text-2xs text-gray-400">没答上的</div>
                  {(s.summary.gaps ?? []).map((g, i) => (
                    <div key={i} className="flex gap-1 text-amber-700">
                      <span className="shrink-0">?</span>
                      <span className="min-w-0">{g}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* 操作按钮：hover / 聚焦时才出现，键盘用户用 focus-within 也能拿到 */}
      {!editing && (
        <div className="absolute right-1.5 top-1.5 hidden gap-0.5 group-hover:flex group-focus-within:flex">
          <button
            title="重命名"
            onClick={() => { setDraft(s.title ?? ''); setEditing(true) }}
            className="rounded px-1 text-xs text-muted hover:bg-gray-100 hover:text-ink"
          >✏️</button>
          <button
            title="移到文件夹"
            onClick={(e) => setMoveAt(moveAt ? null : e.currentTarget)}
            className="rounded px-1 text-xs text-muted hover:bg-gray-100 hover:text-ink"
          >📁</button>
          <button
            title={s.archived_at ? '取消归档' : '归档（值得留，不出现在默认列表）'}
            onClick={() => onArchive(!s.archived_at)}
            className="rounded px-1 text-xs text-muted hover:bg-gray-100 hover:text-ink"
          >{s.archived_at ? '📌' : '📥'}</button>
          <button
            title="移到回收站"
            onClick={onDelete}
            className="rounded px-1 text-xs text-muted hover:bg-red-50 hover:text-red-600"
          >🗑</button>
        </div>
      )}

      {moveAt && (
        <Menu anchor={moveAt} align="right" onClose={() => setMoveAt(null)}>
          {choices.map((c) => (
            <MenuItem key={c.id ?? 'none'} active={c.id === (s.folder_id ?? null)}
              onClick={() => { onMove(c.id); setMoveAt(null) }}>
              <span style={{ paddingLeft: c.depth * 10 }}>
                {c.depth > 0 ? '└ ' : ''}{c.label}
              </span>
            </MenuItem>
          ))}
          {!choices.length && (
            <div className="px-2.5 py-1.5 text-2xs text-muted">
              还没有文件夹，在左下角「+ 新建」建一个。
            </div>
          )}
        </Menu>
      )}
    </div>
  )
}

export function Sidebar({
  list, trash, cur, folders, onPick, onChanged, onNew, pane, onPane,
  lib, libErr, onRetryLib, srcQuery, onSrcQuery, srcCur, onPickSource, onIgnoreSource,
  onNotify, onSettings, collapsed, onToggleCollapse,
}: {
  list: SessionSummary[]
  /** 回收站内容。来自 `/api/trash`——不在 `list` 里（那边硬过滤 deleted_at）。 */
  trash: SessionSummary[]
  cur: string | null
  folders: Folder[]
  onPick: (id: string) => void
  onChanged: () => void
  onNew: () => void
  /** 侧栏当前是会话列表还是来源库 */
  pane: Pane
  onPane: (p: Pane) => void
  lib: SourceLibrary | null
  /** 来源库加载失败的原因。`lib === null` 同时表示「加载中」和「失败」，
   *  光看它分不出来——分不出来时列表会一直停在「加载中…」。 */
  libErr: string | null
  onRetryLib: () => void
  /** 写操作失败时弹提示。走 App 的 notice 通道——
   *  侧栏自己的红字条（FolderTree 里那个 `err`）在这里不合适：
   *  那是文件夹树的局部状态，而这里失败的是会话。 */
  onNotify: (msg: string) => void
  srcQuery: string
  onSrcQuery: (v: string) => void
  srcCur: string | null
  onPickSource: (id: string) => void
  onIgnoreSource: (id: string, ignore: boolean, reason: string) => void
  /** 设置页入口（D1）。挂在侧栏底部——路由跳转的事不归侧栏管，
   *  这里只负责把「点了」报上去。 */
  onSettings: () => void
  /** 折叠态（视觉轮 E2）：侧栏收成 44px 图标轨，读报告时让出宽度。
   *  状态在 App 持有——不进 URL：折叠是临时偏好，不该污染可分享链接。 */
  collapsed: boolean
  onToggleCollapse: () => void
}) {
  const [filter, setFilter] = useState<Filter>('all')
  const [q, setQ] = useState('')
  const [folderId, setFolderId] = useState<string | null | undefined>(undefined)
  // 待确认的删除操作。purge=true 是「彻底删除」，false 是「移到回收站」——
  // 两个入口共用一个对话框，但**文案和后果完全不同**，不能混。
  const [confirm, setConfirm] = useState<
    { s: SessionSummary; purge: boolean } | null
 >(null)
  // 跨会话全文搜索（D2）：本地 q 只做标题/问题的即时过滤，
  // 输入停下来 300ms 后顺手查一次服务端（标题+问题+报告正文）。
  // null = 没在搜索 / 请求失败（退回纯本地过滤，搜索坏了不挡路）。
  const [hits, setHits] = useState<SearchHit[] | null>(null)
  useEffect(() => {
    const v = q.trim()
    if (!v || filter === 'trash') { setHits(null); return }
    const t = window.setTimeout(() => {
      api.jget<{ hits: SearchHit[] }>(
        `/api/sessions/search?q=${encodeURIComponent(v)}`)
        .then((r) => setHits(r.hits))
        .catch(() => setHits(null))
    }, 300)
    return () => window.clearTimeout(t)
  }, [q, filter])

  const tree = useMemo(() => buildTree(folders), [folders])
  const choices = useMemo(() => folderChoices(tree), [tree])
  const selNode = folderId !== undefined ? findNodeById(tree, folderId) : null
  // 选中父文件夹时，**子文件夹里的会话也要能看到**——
  // 选了「固态电池」却看不到它下面「2026」里的研究，说不通。
  const selIds = selNode ? subtreeIds(selNode) : null

  // 选中的文件夹被删掉之后，别留一个永远筛不出东西的死条件
  useEffect(() => {
    if (folderId !== undefined && !selNode) setFolderId(undefined)
  }, [folderId, selNode])

  // 回收站的数据不在 `list` 里，必须换源
  const src = filter === 'trash' ? trash : list
  const shown = src.filter((s) => {
    if (q && !(`${s.title ?? ''}${s.question}`).toLowerCase().includes(q.toLowerCase()))
      return false
    if (selIds && !selIds.has(s.folder_id ?? null)) return false
    if (filter === 'running') return s.status === 'running'
    if (filter === 'archived') return !!s.archived_at
    return true
  })

  const archived = shown.filter((s) => s.archived_at)
  const rest = shown.filter((s) => !s.archived_at)

  /** 一次写操作。**失败必须说出来**——
   *
   *  ⚠️ 原来这里是裸的 `await fn(); onChanged()`，没有任何 catch。
   *  重命名 / 归档 / 移到文件夹 / 恢复 / 彻底删除全走它，请求一失败
   *  **界面什么都不发生**，而确认框早在请求发出**之前**就关掉了——
   *  用户以为「删掉了」，其实研究还在列表里。这是「以为标上了其实没存」
   *  那一类，比不操作更糟。同一个项目里 `FolderTree.run()` 是有
   *  try/catch + 红字的，这里是遗漏不是风格。
   *
   *  带上操作名：光说「操作失败」用户还得回忆刚点的是哪个按钮。 */
  const act = async (what: string, fn: () => Promise<unknown>) => {
    try {
      await fn()
      onChanged()
    } catch (e) {
      onNotify(`${what}失败，状态没变：${(e as Error).message}`)
    }
  }

  // 折叠轨：保留四个最常用的动作（视图切换 / 新建 / 设置 / 展开）。
  // 视图图标点一下=展开并切到那个视图——图标本身不做完整导航，
  // 没有列表的「会话视图」没有内容可看。
  if (collapsed) {
    return (
      <nav className="flex w-11 shrink-0 flex-col items-center border-r border-line bg-gray-50 py-2 print:hidden">
        <button
          onClick={onToggleCollapse}
          title="展开侧栏"
          className="mb-2 flex h-8 w-8 items-center justify-center rounded text-muted transition-colors hover:bg-white hover:text-ink"
        >»</button>
        <button
          onClick={() => { onPane('session'); onToggleCollapse() }}
          title="会话"
          aria-pressed={pane === 'session'}
          className={[
            'mb-1 flex h-8 w-8 items-center justify-center rounded text-sm transition-colors',
            pane === 'session' ? 'sel text-ink' : 'text-muted hover:bg-white hover:text-ink',
          ].join(' ')}
        >💬</button>
        <button
          onClick={() => { onPane('source'); onToggleCollapse() }}
          title="来源库"
          aria-pressed={pane === 'source'}
          className={[
            'mb-1 flex h-8 w-8 items-center justify-center rounded text-sm transition-colors',
            pane === 'source' ? 'sel text-ink' : 'text-muted hover:bg-white hover:text-ink',
          ].join(' ')}
        >📚</button>
        <button
          onClick={onNew}
          title="新建研究"
          className="mt-1 flex h-8 w-8 items-center justify-center rounded text-sm text-accent transition-colors hover:bg-white"
        >＋</button>
        <button
          onClick={onSettings}
          title="设置"
          className="mt-auto flex h-8 w-8 items-center justify-center rounded text-sm text-muted transition-colors hover:bg-white hover:text-ink"
        >⚙</button>
      </nav>
    )
  }

  return (
    <nav className="flex w-72 shrink-0 flex-col border-r border-line bg-gray-50 print:hidden">
      {/* 会话 / 来源 两个视图。同一批数据两种组织方式：
          会话按时间流找「上周那场」，来源按性质找「哪些材料值得信」。 */}
      <div className="flex gap-1 border-b border-line p-2">
        {([['session', '对话'], ['source', '来源库']] as const).map(([k, label]) => (
          <button
            key={k}
            onClick={() => onPane(k)}
            // 原来只靠底色表示选中，读屏用户完全听不出当前在哪个视图
            aria-pressed={pane === k}
            className={[
              'flex-1 rounded px-2 py-1 text-xs transition-colors',
              pane === k ? 'sel font-medium'
                         : 'text-muted hover:bg-white',
            ].join(' ')}
          >{label}</button>
        ))}
      </div>

      {pane === 'source' ? (
        <SourceList
          lib={lib} libErr={libErr} onRetryLib={onRetryLib}
          q={srcQuery} onQuery={onSrcQuery}
          cur={srcCur} onPick={onPickSource}
          onIgnore={(id, reason) => onIgnoreSource(id, true, reason)}
          onUnignore={(id) => onIgnoreSource(id, false, '')}
        />
      ) : (
        <>
      <div className="space-y-2 p-3">
        <button
          onClick={onNew}
          // ⚠️ 原来是**满屏最重的元素**（实心蓝 + 阴影 + 按下缩放），
          // 比报告里任何一个标题都重。这个按钮并不常按（一场研究十几分钟才开一次），
          // 而内容才是要看很久的东西——所以降成描边：还找得到，但不抢。
          className="w-full rounded-md border border-accent/40 bg-white px-3 py-2 text-sm
                     font-medium text-accent transition-colors
                     hover:border-accent hover:bg-accent/5"
        >新建研究</button>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="搜索标题、问题、报告正文…"
          className="w-full rounded-md border border-line bg-white px-2.5 py-1.5 text-sm outline-none focus:border-accent"
        />
        <div className="flex gap-1">
          {([['all', '全部'], ['running', '进行中'], ['archived', '已归档'], ['trash', '回收站']] as const)
            .map(([k, label]) => (
              <button
                key={k}
                onClick={() => setFilter(k)}
                aria-pressed={filter === k}
                className={[
                  'flex-1 rounded px-1 py-1 text-2xs transition-colors',
                  filter === k ? 'sel font-medium' : 'text-muted hover:bg-white',
                ].join(' ')}
              >{label}</button>
            ))}
        </div>
      </div>

      {/* 文件夹树：选中即筛选（含整棵子树的会话） */}
      <div className="border-b border-line pb-1">
        <FolderTree
          folders={folders}
          selected={folderId}
          onSelect={(id) => setFolderId(id)}
          onChanged={onChanged}
        />
      </div>
        </>
      )}

      {pane === 'session' && (
      <div className="flex-1 overflow-auto px-2 pb-4">
        {hits !== null ? (
          /* 搜索中：服务端命中结果顶替会话列表。
             正文命中带定位片段——「在哪句话里命中的」直接可见。 */
          <>
            <div className="px-1 py-1.5 text-2xs text-muted">
              {hits.length ? `命中 ${hits.length} 场研究` : '没有命中的会话'}
            </div>
            {hits.map((h) => (
              <button
                key={h.id}
                onClick={() => onPick(h.id)}
                className={[
                  'mb-0.5 w-full rounded-md px-3 py-2 text-left hover:bg-white',
                  h.id === cur ? 'sel' : '',
                ].join(' ')}
              >
                <div className="flex items-center gap-1.5">
                  <span className="min-w-0 flex-1 truncate text-sm text-ink">
                    {h.title}
                  </span>
                  <span className="shrink-0 rounded bg-gray-100 px-1 py-0.5 text-2xs text-muted">
                    {h.matched_in === 'title' ? '标题'
                     : h.matched_in === 'question' ? '问题' : '正文'}
                  </span>
                </div>
                {h.snippet && (
                  <div className="mt-0.5 line-clamp-2 text-2xs leading-4 text-muted">
                    {h.snippet}
                  </div>
                )}
              </button>
            ))}
          </>
        ) : filter === 'trash' ? (
          <>
            <div className="px-1 py-1.5 text-2xs text-muted">
              回收站里的会话数据完整保留，可随时恢复。
            </div>
            {shown.map((s) => (
              <div key={s.id} className="group mb-0.5 rounded-md px-3 py-2 hover:bg-white">
                <div className="truncate text-sm text-muted line-through">
                  {s.title || s.question}
                </div>
                <div className="mt-1 flex gap-2">
                  <button
                    onClick={() => act('恢复', () => api.jpost(`/api/sessions/${s.id}/restore`))}
                    className="rounded border border-line px-2 py-0.5 text-2xs text-muted hover:bg-white hover:text-ink"
                  >恢复</button>
                  <button
                    onClick={() => setConfirm({ s, purge: true })}
                    className="rounded border border-line px-2 py-0.5 text-2xs text-muted hover:border-red-300 hover:text-red-600"
                  >彻底删除</button>
                </div>
              </div>
            ))}
            {!shown.length && <div className="px-1 py-3 text-xs text-muted">回收站是空的</div>}
          </>
        ) : (
          <>
            {archived.length > 0 && (
              <>
                <div className="px-1 py-1 text-2xs text-muted">已归档</div>
                {archived.map((s) => (
                  <SessionRow key={s.id} s={s} active={s.id === cur} choices={choices}
                    onOpen={() => onPick(s.id)}
                    onRename={(t) => act('重命名', () => api.jpatch(`/api/sessions/${s.id}`, { title: t }))}
                    onArchive={(v) => act(v ? '归档' : '取消归档', () => api.jpatch(`/api/sessions/${s.id}`, { archived: v }))}
                    onDelete={() => setConfirm({ s, purge: false })}
                    onMove={(fid) => act('移动到文件夹', () => api.jpatch(`/api/sessions/${s.id}`, { folder_id: fid }))} />
                ))}
              </>
            )}
            {rest.map((s) => (
              <SessionRow key={s.id} s={s} active={s.id === cur} choices={choices}
                onOpen={() => onPick(s.id)}
                onRename={(t) => act('重命名', () => api.jpatch(`/api/sessions/${s.id}`, { title: t }))}
                onArchive={(v) => act(v ? '归档' : '取消归档', () => api.jpatch(`/api/sessions/${s.id}`, { archived: v }))}
                onDelete={() => setConfirm({ s, purge: false })}
                onMove={(fid) => act('移动到文件夹', () => api.jpatch(`/api/sessions/${s.id}`, { folder_id: fid }))} />
            ))}
            {!shown.length && (
              <div className="px-1 py-3 text-xs text-muted">
                {selNode
                  ? `「${selNode.name}」里还没有会话`
                  : q ? `没有匹配「${q}」的会话` : '还没有会话'}
              </div>
            )}
          </>
        )}
      </div>
      )}

      {pane === 'session' && confirm && (
        <ConfirmDialog
          title={confirm.purge ? '彻底删除这场研究？' : '移到回收站？'}
          lines={confirm.purge
            ? ['报告、来源、段落会一并删除', '此操作不可撤销']
            : ['报告、来源、段落都会保留', '可在回收站中随时恢复']}
          confirmText={confirm.purge ? '彻底删除' : '移到回收站'}
          onCancel={() => setConfirm(null)}
          onConfirm={async () => {
            const { s, purge } = confirm
            setConfirm(null)
            await act(purge ? '彻底删除' : '移到回收站', () => purge
              ? api.jdel(`/api/sessions/${s.id}/purge`)
              : api.jdel(`/api/sessions/${s.id}`))
          }}
        />
      )}

      {/* 底部工具区：设置（D1）+ 折叠（E2）。不抢主列表的注意力，放最底下 */}
      <div className="mt-auto flex items-center gap-1 border-t border-line p-2">
        <button
          onClick={onSettings}
          className="flex-1 rounded px-2 py-1 text-left text-xs text-muted transition-colors hover:bg-white hover:text-ink"
        >
          ⚙ 设置
        </button>
        <button
          onClick={onToggleCollapse}
          title="收起侧栏（专注阅读）"
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded text-muted transition-colors hover:bg-white hover:text-ink"
        >«</button>
      </div>
    </nav>
  )
}
