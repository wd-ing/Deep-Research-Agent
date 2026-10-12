import { useEffect, useRef } from 'react'

/** 浮层菜单。点外面 / 按 Esc 关掉。
 *
 *  自绘而不是用原生 select：菜单项要显示**缩进的层级**和当前选中标记，
 *  原生 select 在 Windows 上还会渲染成系统菜单，完全不认这些。
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

  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (!box.current?.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    // capture：点击触发菜单的那个按钮时，本轮事件还没冒泡完，
    // 不 capture 的话 onDown 会把刚打开的菜单立刻关掉
    document.addEventListener('mousedown', onDown, true)
    document.addEventListener('keydown', onKey)
    window.addEventListener('resize', onClose)
    return () => {
      document.removeEventListener('mousedown', onDown, true)
      document.removeEventListener('keydown', onKey)
      window.removeEventListener('resize', onClose)
    }
  }, [onClose])

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
      <span className="w-3 shrink-0 text-[10px]">{active ? '✓' : ''}</span>
      <span className="min-w-0 flex-1 truncate">{children}</span>
    </button>
  )
}

/** 菜单里的分隔线。 */
export function MenuSep() {
  return <div className="my-1 border-t border-line" />
}
