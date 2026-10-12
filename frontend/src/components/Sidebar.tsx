import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../useSession'
import type { Folder, SessionSummary } from '../types'
import { FolderTree, buildTree, findNodeById, subtreeIds, folderChoices } from './FolderTree'
import { Menu, MenuItem } from './Menu'
import { ConfirmDialog } from './ConfirmDialog'

type Filter = 'all' | 'running' | 'archived' | 'trash'

/** 相对时间。列表里比绝对时间好用（「2 小时前」比「2026-10-11T05:39」）。 */
function ago(iso: string): string {
  const t = new Date(iso).getTime()
  if (!t) return ''
  const s = (Date.now() - t) / 1000
  if (s < 60) return '刚刚'
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} 天前`
  return new Date(t).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })
}

function tokens(n: number): string {
  if (!n) return ''
  return n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`
}

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
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(s.title ?? '')
  const [moveAt, setMoveAt] = useState<HTMLElement | null>(null)
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
        active ? 'bg-white shadow-sm ring-1 ring-line' : 'hover:bg-white',
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
              <span className="wf-running-dot mt-1.5 inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-accent" />
            )}
            {s.archived_at && (
              <span className="mt-0.5 shrink-0 text-[11px] text-muted">📌</span>
            )}
            <span className="min-w-0 flex-1 truncate text-sm text-ink">
              {s.title || s.question}
            </span>
          </div>
          <div className="mt-0.5 flex items-center gap-2 pl-3 text-[11px] text-muted">
            {s.status !== 'done' && <span>{s.status}</span>}
            {s.cost_tokens > 0 && <span>{tokens(s.cost_tokens)} token</span>}
            <span>{ago(s.created_at)}</span>
          </div>
        </button>
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
            <div className="px-2.5 py-1.5 text-[11px] text-muted">
              还没有文件夹，在左下角「+ 新建」建一个。
            </div>
          )}
        </Menu>
      )}
    </div>
  )
}

export function Sidebar({
  list, trash, cur, folders, onPick, onChanged, onNew,
}: {
  list: SessionSummary[]
  /** 回收站内容。来自 `/api/trash`——不在 `list` 里（那边硬过滤 deleted_at）。 */
  trash: SessionSummary[]
  cur: string | null
  folders: Folder[]
  onPick: (id: string) => void
  onChanged: () => void
  onNew: () => void
}) {
  const [filter, setFilter] = useState<Filter>('all')
  const [q, setQ] = useState('')
  const [folderId, setFolderId] = useState<string | null | undefined>(undefined)
  // 待确认的删除操作。purge=true 是「彻底删除」，false 是「移到回收站」——
  // 两个入口共用一个对话框，但**文案和后果完全不同**，不能混。
  const [confirm, setConfirm] = useState<
    { s: SessionSummary; purge: boolean } | null
 >(null)

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

  const act = async (fn: () => Promise<unknown>) => {
    await fn()
    onChanged()
  }

  return (
    <nav className="flex w-72 shrink-0 flex-col border-r border-line bg-gray-50">
      <div className="space-y-2 p-3">
        <button
          onClick={onNew}
          className="w-full rounded-md bg-accent px-3 py-2 text-sm text-white shadow-sm transition-all hover:bg-blue-700 hover:shadow active:scale-[.98]"
        >新建研究</button>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="搜索标题或问题…"
          className="w-full rounded-md border border-line bg-white px-2.5 py-1.5 text-sm outline-none focus:border-accent"
        />
        <div className="flex gap-1">
          {([['all', '全部'], ['running', '进行中'], ['archived', '已归档'], ['trash', '回收站']] as const)
            .map(([k, label]) => (
              <button
                key={k}
                onClick={() => setFilter(k)}
                className={[
                  'flex-1 rounded px-1 py-1 text-[11px] transition-colors',
                  filter === k ? 'bg-white font-medium text-ink shadow-sm' : 'text-muted hover:bg-white',
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

      <div className="flex-1 overflow-auto px-2 pb-4">
        {filter === 'trash' ? (
          <>
            <div className="px-1 py-1.5 text-[11px] text-muted">
              回收站里的会话数据完整保留，可随时恢复。
            </div>
            {shown.map((s) => (
              <div key={s.id} className="group mb-0.5 rounded-md px-3 py-2 hover:bg-white">
                <div className="truncate text-sm text-muted line-through">
                  {s.title || s.question}
                </div>
                <div className="mt-1 flex gap-2">
                  <button
                    onClick={() => act(() => api.jpost(`/api/sessions/${s.id}/restore`))}
                    className="rounded border border-line px-2 py-0.5 text-[11px] text-muted hover:bg-white hover:text-ink"
                  >恢复</button>
                  <button
                    onClick={() => setConfirm({ s, purge: true })}
                    className="rounded border border-line px-2 py-0.5 text-[11px] text-muted hover:border-red-300 hover:text-red-600"
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
                <div className="px-1 py-1 text-[11px] text-muted">已归档</div>
                {archived.map((s) => (
                  <SessionRow key={s.id} s={s} active={s.id === cur} choices={choices}
                    onOpen={() => onPick(s.id)}
                    onRename={(t) => act(() => api.jpatch(`/api/sessions/${s.id}`, { title: t }))}
                    onArchive={(v) => act(() => api.jpatch(`/api/sessions/${s.id}`, { archived: v }))}
                    onDelete={() => setConfirm({ s, purge: false })}
                    onMove={(fid) => act(() => api.jpatch(`/api/sessions/${s.id}`, { folder_id: fid }))} />
                ))}
              </>
            )}
            {rest.map((s) => (
              <SessionRow key={s.id} s={s} active={s.id === cur} choices={choices}
                onOpen={() => onPick(s.id)}
                onRename={(t) => act(() => api.jpatch(`/api/sessions/${s.id}`, { title: t }))}
                onArchive={(v) => act(() => api.jpatch(`/api/sessions/${s.id}`, { archived: v }))}
                onDelete={() => setConfirm({ s, purge: false })}
                onMove={(fid) => act(() => api.jpatch(`/api/sessions/${s.id}`, { folder_id: fid }))} />
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

      {confirm && (
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
            await act(() => purge
              ? api.jdel(`/api/sessions/${s.id}/purge`)
              : api.jdel(`/api/sessions/${s.id}`))
          }}
        />
      )}
    </nav>
  )
}
