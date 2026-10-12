import { useEffect, useRef, useState } from 'react'
import { api } from '../useSession'
import type { Folder, SessionSummary } from '../types'

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

/** 确认对话框。不是 `window.confirm`——
 *  删一场研究连带删报告/来源/段，得说清楚，而且要能恢复。 */
function ConfirmDialog({
  title, lines, confirmText, onCancel, onConfirm,
}: {
  title: string
  lines: string[]
  confirmText: string
  onCancel: () => void
  onConfirm: () => void
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onCancel() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onCancel])
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
      <div className="w-full max-w-sm rounded-lg bg-white p-5 shadow-xl">
        <h3 className="text-sm font-medium text-ink">{title}</h3>
        <ul className="mt-3 space-y-1 text-xs text-muted">
          {lines.map((l, i) => <li key={i}>· {l}</li>)}
        </ul>
        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={onCancel}
            className="rounded border border-line px-3 py-1.5 text-xs text-muted hover:bg-gray-50"
          >取消</button>
          <button
            onClick={onConfirm}
            className="rounded bg-red-600 px-3 py-1.5 text-xs text-white hover:bg-red-700"
          >{confirmText}</button>
        </div>
      </div>
    </div>
  )
}

/** 一条会话。**hover 才出操作按钮**，平时干净。 */
function SessionRow({
  s, active, onOpen, onRename, onArchive, onDelete,
}: {
  s: SessionSummary
  active: boolean
  onOpen: () => void
  onRename: (title: string) => void
  onArchive: (v: boolean) => void
  onDelete: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(s.title ?? '')
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

  // 回收站的数据不在 `list` 里，必须换源
  const src = filter === 'trash' ? trash : list
  const shown = src.filter((s) => {
    if (q && !(`${s.title ?? ''}${s.question}`).toLowerCase().includes(q.toLowerCase()))
      return false
    if (folderId !== undefined && s.folder_id !== folderId) return false
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

      {/* 文件夹筛选（阶段 2 会换成真正的树） */}
      {folders.length > 0 && (
        <div className="border-b border-line px-3 pb-2">
          <div className="mb-1 text-[11px] text-muted">文件夹</div>
          <div className="flex flex-wrap gap-1">
            <button
              onClick={() => setFolderId(undefined)}
              className={[
                'rounded px-1.5 py-0.5 text-[11px]',
                folderId === undefined ? 'bg-white text-ink shadow-sm' : 'text-muted hover:bg-white',
              ].join(' ')}
            >全部</button>
            {folders.map((f) => (
              <button
                key={f.id ?? 'none'}
                onClick={() => setFolderId(f.id)}
                className={[
                  'rounded px-1.5 py-0.5 text-[11px]',
                  folderId === f.id ? 'bg-white text-ink shadow-sm' : 'text-muted hover:bg-white',
                ].join(' ')}
              >{f.name} <span className="opacity-60">{f.sessions}</span></button>
            ))}
          </div>
        </div>
      )}

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
                  <SessionRow key={s.id} s={s} active={s.id === cur}
                    onOpen={() => onPick(s.id)}
                    onRename={(t) => act(() => api.jpatch(`/api/sessions/${s.id}`, { title: t }))}
                    onArchive={(v) => act(() => api.jpatch(`/api/sessions/${s.id}`, { archived: v }))}
                    onDelete={() => setConfirm({ s, purge: false })} />
                ))}
              </>
            )}
            {rest.map((s) => (
              <SessionRow key={s.id} s={s} active={s.id === cur}
                onOpen={() => onPick(s.id)}
                onRename={(t) => act(() => api.jpatch(`/api/sessions/${s.id}`, { title: t }))}
                onArchive={(v) => act(() => api.jpatch(`/api/sessions/${s.id}`, { archived: v }))}
                onDelete={() => setConfirm({ s, purge: false })} />
            ))}
            {!shown.length && (
              <div className="px-1 py-3 text-xs text-muted">
                {q ? `没有匹配「${q}」的会话` : '还没有会话'}
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
