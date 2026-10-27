/** 动效工具（P1）。无依赖：rAF + prefers-reduced-motion。 */
import { useEffect, useRef, useState } from 'react'

const reduced = () =>
  typeof matchMedia !== 'undefined' &&
  matchMedia('(prefers-reduced-motion: reduce)').matches

/** 数字滚动：从上一值（首次 0）缓到 target。reduced-motion 下直接给最终值。 */
export function useCountUp(target: number, duration = 900): number {
  const [v, setV] = useState(reduced() ? target : 0)
  // 记录最新显示值：动画途中目标再变（轮询刷新）时从当前显示值出发，
  // 不回跳到上一个目标。
  const cur = useRef(reduced() ? target : 0)
  useEffect(() => {
    if (reduced()) { setV(target); cur.current = target; return }
    const from = cur.current
    const t0 = performance.now()
    let raf = 0
    const step = (t: number) => {
      const p = Math.min(1, (t - t0) / duration)
      const e = 1 - Math.pow(1 - p, 3)
      const now = from + (target - from) * e
      cur.current = now
      setV(now)
      if (p < 1) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [target, duration])
  return v
}
