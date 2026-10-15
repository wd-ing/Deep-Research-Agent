import { useEffect, useRef } from 'react'

/** 菜单里的可聚焦项。`disabled` 的 button 本来就不可聚焦，不用另外过滤。 */
function items(box: HTMLDivElement | null): HTMLButtonElement[] {
  if (!box) return []
  return Array.from(box.querySelectorAll<HTMLButtonElement>('button:not(:disabled)'))
}

/** 浮层菜单。点外面 / 按 Esc / 一滚动就关掉。
 *
 *  自绘而不是用原生 select：菜单项要显示**缩进的层级**和当前选中标记，
 *  原生 select 在 Windows 上还会渲染成系统菜单，完全不认这些。
 *
 *  键盘：↑↓ 移动、Home/End 跳首尾、Esc 关闭并把焦点还给打开它的那个按钮。
 *  ⚠️ 原来只声明了 `role="menu"` / `role="menuitem"` 却没有任何键盘导航——
 *  屏幕里看得到、键盘用不到。现在所有项 `tabIndex=-1`（对 menu 角色来说
 *  **Tab 应该整个跳过菜单**，而不是在项间跳），焦点用 `.focus()` 游走。
 *  不走 state：方向键只改焦点、不改渲染，用 state 只会让每次移动都触发重渲染。
 */
export function Menu({
  anchor, onClose, children, align = 'left',
}: {
  /** 相对定位锚点。菜单用 fixed 定位，滚出视口也不需要跟着重算。 */
  anchor: HTMLElement | null
  onClose: () => void
  children: React.ReactNode
  align?: 'left' | 'right'
}) {
  const box = useRef<HTMLDivElement>(null)

  // ⚠️ **`onClose` 必须走 ref，且不能进下面的依赖列表**。
  // 它是从父组件传进来的内联箭头函数（`() => setMenuAt(null)`），
  // 每次父组件重渲染都是**新函数**（侧栏在轮询，一直在重渲染）。
  // 直接进依赖 → effect 跟着重跑 → 下面「记下打开时焦点在哪」那行
  // 会把 `before` **改写成当前聚焦的菜单项**。
  // Esc 关闭时菜单已经卸载，往一个脱离 DOM 的节点调 focus() 是**静默无效**的，
  // `parentElement` 兜底链也跟着断 —— 焦点就掉回 <body>，看起来像没修。
  const closeRef = useRef(onClose)
  closeRef.current = onClose

  // 同理，这两个必须跨 effect 重跑存活，所以放 ref 而不是 effect 局部变量。
  const beforeRef = useRef<HTMLElement | null>(null)
  const grabbed = useRef(false)
  // 只在**键盘**关闭时归还焦点。点外面关闭时用户已经把焦点放到别处了，
  // 这时再抢回来反而是干扰。
  const byKeyboard = useRef(false)

  useEffect(() => {
    // 打开之前焦点在哪，Esc 关闭之后还回哪去。没有这个的话焦点掉回 <body>，
    // 键盘用户得从头 Tab 一遍才回得到原来的按钮。
    // 只在**第一次**跑时记：之后 focus 已经进了菜单，再记就记成了菜单项。
    if (!grabbed.current) {
      grabbed.current = true
      beforeRef.current = document.activeElement as HTMLElement | null
    }
    const before = beforeRef.current

    const giveBack = () => {
      if (!before) return
      before.focus?.()
      if (document.activeElement === before) return
      // ⚠️ 对 `display:none` 的元素调 `focus()` 是**静默无效**的。
      // 文件夹那个 ⋯ 按钮恰恰是 `hidden` + `group-hover/row:block`——
      // 用键盘打开菜单时（Tab 到 ⋯ 按回车），Esc 那一刻焦点已经在菜单里，
      // `group-focus-within` 不再生效、按钮重新变 `display:none`，焦点就还不了。
      // 退到同一行里第一个可聚焦的元素（文件夹名按钮），保证焦点没丢。
      let p: HTMLElement | null = before.parentElement
      for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
        const f = p.querySelector<HTMLElement>(
          'button:not(:disabled), a[href], [tabindex]:not([tabindex="-1"])')
        if (f) { f.focus(); return }
      }
    }

    const onDown = (e: MouseEvent) => {
      if (!box.current?.contains(e.target as Node)) closeRef.current()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { byKeyboard.current = true; closeRef.current(); return }
      const list = items(box.current)
      if (!list.length) return
      const at = list.indexOf(document.activeElement as HTMLButtonElement)
      let next = -1
      if (e.key === 'ArrowDown') next = at < 0 ? 0 : (at + 1) % list.length
      else if (e.key === 'ArrowUp') next = at < 0 ? list.length - 1
        : (at - 1 + list.length) % list.length
      else if (e.key === 'Home') next = 0
      else if (e.key === 'End') next = list.length - 1
      else return
      e.preventDefault()
      list[next]?.focus()
    }
    // capture：点击触发菜单的那个按钮时，本轮事件还没冒泡完，
    // 不 capture 的话 onDown 会把刚打开的菜单立刻关掉
    document.addEventListener('mousedown', onDown, true)
    document.addEventListener('keydown', onKey)
    window.addEventListener('resize', closeRef.current)
    // ⚠️ 滚动必须也关掉。菜单是 `position: fixed`，页面或侧栏一滚，
    // 锚点跟着走、菜单原地不动——用户会点到一个和标签脱节的位置上。
    // `scroll` **不冒泡**（只在捕获阶段派发），所以这里必须 capture:true，
    // 否则侧栏那个 `.overflow-auto` 里滚动根本收不到。
    window.addEventListener('scroll', closeRef.current, true)
    // 打开后焦点落在第一项，否则键盘用户还得先 Tab 进来才用得上 ↑↓
    items(box.current)[0]?.focus()
    return () => {
      document.removeEventListener('mousedown', onDown, true)
      document.removeEventListener('keydown', onKey)
      window.removeEventListener('resize', closeRef.current)
      window.removeEventListener('scroll', closeRef.current, true)
      if (byKeyboard.current) giveBack()
    }
    // ⚠️ `onClose` 故意不在依赖里（见上面 closeRef 那段）。
    // `anchor` 必须在：首次渲染时它可能还是 null（下面 return null），
    // 那个 effect 已经空跑过一次了。少这一项，菜单就永远进不去焦点。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [anchor])

  // ⚠️ 锚点没量到之前**先不渲染**，否则会以 (0,0) 闪一下
  if (!anchor) return null
  const r = anchor.getBoundingClientRect()
  const left = align === 'right' ? r.right : r.left
  // 底部空间不够就翻到上方——文件夹树通常靠下，翻转比被裁掉好
  const below = window.innerHeight - r.bottom
  const flip = below < 180 && r.top > below

  return (
    <div
      ref={box}
      role="menu"
      style={{
        position: 'fixed',
        left: Math.min(left, window.innerWidth - 200),
        top: flip ? undefined : r.bottom + 4,
        bottom: flip ? window.innerHeight - r.top + 4 : undefined,
        zIndex: 60,
      }}
      className="max-h-72 min-w-[9rem] overflow-auto rounded-md border border-line bg-white py-1 shadow-lg"
    >
      {children}
    </div>
  )
}

export function MenuItem({
  onClick, children, active, disabled, danger,
}: {
  onClick: () => void
  children: React.ReactNode
  /** 当前选中项打勾 */
  active?: boolean
  disabled?: boolean
  danger?: boolean
}) {
  return (
    <button
      role="menuitem"
      // -1：菜单整体对 Tab 不可达，焦点只在项之间用 ↑↓ 游走（见 Menu 的说明）
      tabIndex={-1}
      disabled={disabled}
      onClick={onClick}
      className={[
        'flex w-full items-center gap-1.5 whitespace-nowrap px-2.5 py-1 text-left text-xs',
        disabled
          ? 'cursor-not-allowed text-gray-300'
          : danger
            ? 'text-red-600 hover:bg-red-50'
            : 'text-ink hover:bg-gray-100',
      ].join(' ')}
    >
      <span className="w-3 shrink-0 text-2xs">{active ? '✓' : ''}</span>
      <span className="min-w-0 flex-1 truncate">{children}</span>
    </button>
  )
}

/** 菜单里的分隔线。 */
export function MenuSep() {
  return <div className="my-1 border-t border-line" />
}
