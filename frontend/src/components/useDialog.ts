import { useEffect, useRef, type RefObject } from 'react'

/** 模态对话框的共同部分：Esc 关闭、Tab 焦点陷阱、背景锁滚、关闭后归还焦点。
 *
 *  三个对话框（`NewResearchDialog` / `ConfirmDialog` / `SegDrawer`）
 *  原来各自抄了一份 Esc 处理，且**三个都没有焦点陷阱**——
 *  Tab 会一路跑到背后的页面上，键盘用户按 Tab 回不来；
 *  Esc 关掉之后焦点掉回 `<body>`，要重新 Tab 几轮才回得到原来的按钮。
 *
 *  ⚠️ **`onCancel` 必须走 ref，effect 依赖只能是 `[]`**。
 *  它们都是从父组件传进来的内联箭头函数（`() => setX(false)`），
 *  每次父组件重渲染就是**新函数**。如果直接进依赖列表，
 *  effect 会跟着重跑 → 「打开时把焦点放进面板」那段又执行一遍
 *  → 用户正打在输入框里，焦点被抢回第一个按钮，输入直接断掉。
 */
export function useDialog<T extends HTMLElement>(
  ref: RefObject<T | null>,
  onCancel: () => void,
  /** 默认 true。抽屉这类「组件一直挂着、只是内部 null」的要传 false，
   *  否则打开的那一下会重新接管键盘、关掉的时候又急着把焦点塞回去。 */
  enabled = true,
) {
  const cancelRef = useRef(onCancel)
  cancelRef.current = onCancel

  useEffect(() => {
    if (!enabled) return
    const before = document.activeElement as HTMLElement | null

    const focusables = (): HTMLElement[] => {
      const box = ref.current
      if (!box) return []
      return Array.from(box.querySelectorAll<HTMLElement>(
        'a[href], button:not(:disabled), input:not(:disabled),' +
        ' select:not(:disabled), textarea:not(:disabled),' +
        ' [tabindex]:not([tabindex="-1"])'))
    }

    // 打开时把焦点放进面板：一个可聚焦元素都没有就落在面板本身
    const first = focusables()
    if (first.length) first[0].focus()
    else ref.current?.focus()

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { cancelRef.current?.(); return }
      if (e.key !== 'Tab') return
      const list = focusables()
      if (!list.length) { e.preventDefault(); return }
      const i = list.indexOf(document.activeElement as HTMLElement)
      // 循环：末尾再 Tab 回到开头，开头再 Shift+Tab 回到末尾
      const next = e.shiftKey
        ? (i <= 0 ? list.length - 1 : i - 1)
        : (i < 0 || i === list.length - 1 ? 0 : i + 1)
      e.preventDefault()
      list[next]?.focus()
    }
    window.addEventListener('keydown', onKey)

    // 背景锁滚：模态开着的时候背后不该能滚
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = prevOverflow
      before?.focus?.()
    }
    // ⚠️ 依赖只能是 `[enabled]`：见上面关于 onCancel 内联函数的说明。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled])
}
