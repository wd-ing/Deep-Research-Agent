import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../useSession'
import type { Folder } from '../types'
import { Menu, MenuItem, MenuSep } from './Menu'
import { ConfirmDialog } from './ConfirmDialog'

export interface FolderNode {
  /** null = 「未分类」。虚拟节点，数据库里没有这一行。 */
  id: string | null
  name: string
  parent_id: string | null
  sort_order: number
  /** 直接归属的会话数（后端给的原值） */
  sessions: number
  /** 自身 + 全部子孙。UI 显示这个，`sessions` 只在需要区分时用。 */
  total: number
  children: FolderNode[]
}

/** 扁平列表 → 树。
 *
 *  ⚠️ **不能靠后端的顺序**（`ORDER BY sort_order, created_at`）——
 *  嵌套之后子节点的 sort_order 可能比父节点小，直接按数组顺序渲染会错位。
 *  必须按 `parent_id` 自己挂。
 *
 *  ⚠️ `total` 必须**自底向上**算：后端只给直接数，
 *  按数组顺序累加会漏掉「子节点排在父节点之前」的那部分。
 */
export function buildTree(folders: Folder[]): FolderNode[] {
  const byId = new Map<string, FolderNode>()
  const roots: FolderNode[] = []
  const loose: FolderNode[] = []

  for (const f of folders) {
    if (f.id === null) {
      loose.push({ ...f, total: f.sessions, children: [] })
    } else {
      byId.set(f.id, { ...f, total: f.sessions, children: [] })
    }
  }

  for (const f of folders) {
    if (f.id === null) continue
    const n = byId.get(f.id)!
    const p = f.parent_id ? byId.get(f.parent_id) : undefined
    // 父节点不存在的当根处理：宁可显示在顶层，也不要**整支子树静默消失**
    if (p) p.children.push(n)
    else roots.push(n)
  }

  const sum = (n: FolderNode): number =>
    n.sessions + n.children.reduce((a, c) => a + sum(c), 0)
  const walk = (n: FolderNode) => {
    n.total = sum(n)
    n.children.forEach(walk)
  }
  roots.forEach(walk)

  // 「未分类」永远是顶层且排在最后：它是兜底，不该和用户建的文件夹混排
  return [...roots, ...loose]
}

/** 某节点及其全部子孙的 id。选中文件夹时用它判定「这场会话算不算在里面」。 */
export function subtreeIds(n: FolderNode): Set<string | null> {
  const out = new Set<string | null>([n.id])
  const walk = (x: FolderNode) => x.children.forEach((c) => { out.add(c.id); walk(c) })
  walk(n)
  return out
}

export function findNodeById(nodes: FolderNode[], id: string | null): FolderNode | null {
  for (const n of nodes) {
    if (n.id === id) return n
    const hit = findNodeById(n.children, id)
    if (hit) return hit
  }
  return null
}

/** 树里所有节点（深度优先）。菜单里列候选项要用。 */
function flatten(nodes: FolderNode[], depth = 0): { n: FolderNode; depth: number }[] {
  return nodes.flatMap((n) => [{ n, depth }, ...flatten(n.children, depth + 1)])
}

/** 改名/新建用的行内输入框。回车提交、Esc 取消、失焦提交。 */
function NameInput({
  initial, placeholder, depth, onSubmit, onCancel,
}: {
  initial: string
  placeholder: string
  depth: number
  onSubmit: (v: string) => void
  onCancel: () => void
}) {
  const [v, setV] = useState(initial)
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => { ref.current?.focus(); ref.current?.select() }, [])

  const commit = () => {
    const t = v.trim()
    if (t) onSubmit(t)
    else onCancel()
  }

  return (
    <div className="flex items-center gap-1" style={{ paddingLeft: depth * 12 + 6 }}>
      <input
        ref={ref}
        value={v}
        placeholder={placeholder}
        onChange={(e) => setV(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') commit()
          if (e.key === 'Escape') onCancel()
        }}
        className="min-w-0 flex-1 rounded border border-accent bg-white px-1.5 py-0.5 text-xs outline-none"
      />
      <button
        onMouseDown={(e) => e.preventDefault()}
        onClick={commit}
        className="shrink-0 rounded px-1 text-[10px] text-accent hover:bg-gray-100"
      >确定</button>
      <button
        onMouseDown={(e) => e.preventDefault()}
        onClick={onCancel}
        className="shrink-0 rounded px-1 text-[10px] text-muted hover:bg-gray-100"
      >取消</button>
    </div>
  )
}

export function FolderTree({
  folders, selected, onSelect, onChanged,
}: {
  folders: Folder[]
  /** undefined = 不按文件夹筛选（「全部」） */
  selected: string | null | undefined
  onSelect: (id: string | null | undefined) => void
  onChanged: () => void
}) {
  const tree = useMemo(() => buildTree(folders), [folders])
  const all = useMemo(() => flatten(tree), [tree])

  // 默认展开（「没被显式收起」就是展开）——否则树看着是平的，
  // 用户根本看不出这玩意儿支持嵌套。
  const [closed, setClosed] = useState<Set<string>>(new Set())
  const [menuAt, setMenuAt] = useState<{ id: string | null; anchor: HTMLElement } | null>(null)
  const [renaming, setRenaming] = useState<string | null>(null)
  const [creating, setCreating] = useState<{ parent: string | null } | null>(null)
  const [confirmDel, setConfirmDel] = useState<FolderNode | null>(null)
  const [err, setErr] = useState('')

  // 报错了就把提示清掉——否则一条红字会一直挂在那儿
  useEffect(() => { if (err) { const t = setTimeout(() => setErr(''), 5000); return () => clearTimeout(t) } },
    [err])

  const run = async (fn: () => Promise<unknown>) => {
    setErr('')
    try {
      await fn()
      onChanged()
      return true
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
      return false
    }
  }

  const toggle = (id: string) =>
    setClosed((prev) => {
      const n = new Set(prev)
      n.has(id) ? n.delete(id) : n.add(id)
      return n
    })

  const rows = (nodes: FolderNode[], depth: number): React.ReactNode[] =>
    nodes.flatMap((n) => {
      const isLoose = n.id === null
      const hasKids = n.children.length > 0
      const isOpen = hasKids && !closed.has(n.id!)
      const isSel = selected !== undefined && n.id === selected
      const pad = depth * 12

      const out: React.ReactNode[] = []

      // ⚠️ 「未分类」的 `id` 就是 `null`，和 `renaming`/`creating.parent`
      // 的「没在操作」空值撞车。初值是 null 时 `renaming === n.id` 直接成立，
      // 会给「未分类」渲染一个预填着「未分类」的改名框——
      // 它的行整个被输入框顶掉，看起来像 bug。
      // 所以：**未分类这一行永远不参与新建/改名分支**，
      // 而且它是虚拟节点，本来就没有可改的东西。
      if (creating && !isLoose && creating.parent === n.id) {
        out.push(
          <NameInput
            key="new" depth={depth} initial="" placeholder="文件夹名"
            onCancel={() => setCreating(null)}
            onSubmit={async (name) => {
              const ok = await run(() => api.jpost('/api/folders',
                { name, parent_id: n.id }))
              if (ok) setCreating(null)
            }}
          />,
        )
      }

      if (renaming !== null && !isLoose && renaming === n.id) {
        out.push(
          <NameInput
            key="rn" depth={depth} initial={n.name} placeholder="文件夹名"
            onCancel={() => setRenaming(null)}
            onSubmit={async (name) => {
              const ok = await run(() => api.jpatch(`/api/folders/${n.id}`, { name }))
              if (ok) setRenaming(null)
            }}
          />,
        )
      } else {
        out.push(
          <div
            key={n.id ?? 'none'}
            className={[
              'group/row flex items-center rounded pr-1 transition-colors',
              isSel ? 'bg-white text-ink shadow-sm' : 'text-muted hover:bg-white',
            ].join(' ')}
          >
            {/* 折叠箭头是**独立按钮**。第一版把整行做成折叠开关，
                结果点文件夹名去筛选时顺手把子树收了——
                「点名字=筛选」和「点箭头=展开」必须是两件事。 */}
            {hasKids ? (
              <button
                onClick={() => toggle(n.id!)}
                title={isOpen ? '折叠' : '展开'}
                style={{ paddingLeft: pad + 3 }}
                className="shrink-0 py-1 pr-0.5 text-[9px] leading-none text-muted hover:text-ink"
              >{isOpen ? '▼' : '▶'}</button>
            ) : (
              <span style={{ width: pad + 13 }} className="shrink-0" />
            )}

            <button
              onClick={() => onSelect(isSel ? undefined : n.id)}
              className="flex min-w-0 flex-1 items-center gap-1 py-1 text-left"
              title={isSel ? '取消筛选' : `只看「${n.name}」里的会话（含子文件夹）`}
            >
              <span className="shrink-0 text-[11px]">{isLoose ? '🗂' : '📁'}</span>
              <span className={[
                'min-w-0 flex-1 truncate text-xs',
                isSel ? 'font-medium' : '',
              ].join(' ')}>{n.name}</span>
              <span className="shrink-0 text-[10px] opacity-60">
                {n.total > 0 ? n.total : ''}
              </span>
            </button>

            {/* 「未分类」是虚拟节点：没有 id 就没有可改的东西，
                所以不给它菜单，否则会让人以为能改名/删它 */}
            {!isLoose && (
              <button
                title="文件夹操作"
                onClick={(e) => setMenuAt(
                  menuAt?.id === n.id ? null : { id: n.id, anchor: e.currentTarget })}
                className="hidden shrink-0 rounded px-1 text-[11px] text-muted hover:bg-gray-100 hover:text-ink group-hover/row:block group-focus-within/row:block"
              >⋯</button>
            )}
          </div>,
        )
      }

      if (isOpen) out.push(...rows(n.children, depth + 1))
      return out
    })

  return (
    <div className="px-2 pb-2">
      <div className="mb-0.5 flex items-center justify-between px-1">
        <span className="text-[11px] text-muted">文件夹</span>
        <button
          onClick={() => setCreating({ parent: null })}
          className="rounded px-1 text-[11px] text-muted hover:bg-white hover:text-ink"
          title="新建顶层文件夹"
        >+ 新建</button>
      </div>

      {/* 「全部」放在树外面——它不是文件夹 */}
      <button
        onClick={() => onSelect(undefined)}
        className={[
          'mb-0.5 block w-full rounded px-2 py-1 text-left text-xs transition-colors',
          selected === undefined ? 'bg-white font-medium text-ink shadow-sm' : 'text-muted hover:bg-white',
        ].join(' ')}
      >全部会话</button>

      {creating && creating.parent === null && (
        <NameInput
          depth={0} initial="" placeholder="文件夹名"
          onCancel={() => setCreating(null)}
          onSubmit={async (name) => {
            const ok = await run(() => api.jpost('/api/folders', { name, parent_id: null }))
            if (ok) setCreating(null)
          }}
        />
      )}

      <div className="mt-0.5">{rows(tree, 0)}</div>

      {err && (
        <div className="mt-1 rounded bg-red-50 px-2 py-1 text-[10px] text-red-600">{err}</div>
      )}

      {menuAt && menuAt.id !== null && (() => {
        const self = findNodeById(tree, menuAt.id)
        if (!self) return null
        // 不能移进自己的子孙——后端会拒（400），这里直接不列出来
        const banned = subtreeIds(self)
        const targets = all.filter((x) => !banned.has(x.n.id))
        return (
          <Menu anchor={menuAt.anchor} onClose={() => setMenuAt(null)}>
            <MenuItem onClick={() => { setRenaming(self.id); setMenuAt(null) }}>重命名</MenuItem>
            <MenuItem onClick={() => { setCreating({ parent: self.id }); setMenuAt(null) }}>
              新建子文件夹
            </MenuItem>
            <MenuSep />
            {targets.map(({ n, depth }) => (
              <MenuItem key={n.id ?? 'none'}
                onClick={() => {
                  void run(() => api.jpatch(`/api/folders/${self.id}`,
                    { parent_id: n.id }))
                  setMenuAt(null)
                }}>
                {/* 这里的「未分类」是 parent_id=null，也就是**顶层**。
                    直接写「未分类」会让人以为文件夹里的会话进了未分类——
                    实际是这文件夹自己升到顶层。换个说法。 */}
                <span style={{ paddingLeft: depth * 10 }}>
                  {n.id === null ? '移到顶层' : `${depth > 0 ? '└ ' : ''}${n.name}`}
                </span>
              </MenuItem>
            ))}
            <MenuSep />
            <MenuItem danger onClick={() => { setConfirmDel(self); setMenuAt(null) }}>
              删除文件夹
            </MenuItem>
          </Menu>
        )
      })()}

      {confirmDel && (
        <ConfirmDialog
          title={`删除文件夹「${confirmDel.name}」？`}
          lines={[
            '里面的会话会移到「未分类」，不会删除',
            confirmDel.children.length > 0
              ? `子文件夹（${confirmDel.children.map((c) => c.name).join('、')}）会一并删除`
              : '',
          ].filter(Boolean)}
          confirmText="删除文件夹"
          onCancel={() => setConfirmDel(null)}
          onConfirm={async () => {
            const id = confirmDel.id
            setConfirmDel(null)
            await run(() => api.jdel(`/api/folders/${id}`))
          }}
        />
      )}
    </div>
  )
}

/** 会话「移到文件夹」菜单要用的候选列表（带缩进）。 */
export function folderChoices(tree: FolderNode[]): { id: string | null; label: string; depth: number }[] {
  return flatten(tree).map(({ n, depth }) => ({
    id: n.id,
    label: n.name,
    depth: n.id === null ? 0 : depth,
  }))
}
