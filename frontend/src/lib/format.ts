/** 相对时间。列表里比绝对时间好用（「2 小时前」比「2026-10-11T05:39」）。
 *
 *  ⚠️ 渲染期纯函数、读 `Date.now()`——不重渲染它就永远是那个值。
 *  侧栏条目有自己的 useTick 心跳（Sidebar.tsx），别处用时注意同一件事。 */
export function ago(iso: string): string {
  const t = new Date(iso).getTime()
  if (!t) return ''
  const s = (Date.now() - t) / 1000
  if (s < 60) return '刚刚'
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} 天前`
  return new Date(t).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })
}

/** token 数缩写：231769 → 232k。0/负数一律不显示（没记账≠便宜）。 */
export function tokens(n: number): string {
  if (!n) return ''
  return n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`
}
