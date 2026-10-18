/** hash 路由：URL 是唯一真相源。
 *
 *  设计决定（2026-10-18，和用户对齐过）：
 *  · 用 `#/s/…` 而不是真路径 `/s/…`——部署时不需要服务端 SPA fallback，
 *    静态文件丢哪儿都能跑；也不引 react-router（依赖洁癖同项目风格）。
 *  · 组件**不自己写状态再同步到 URL**，而是 `nav()` 改 hash、
 *    `hashchange` 回流驱动 setState——单向数据流，前进/后退/深链
 *    /刷新恢复四个行为全部是免费的。
 *
 *  ⚠️ `?debug=1` 这类参数在 `location.search` 里，与 hash 无关，
 *    改 hash 不会丢它们。
 */
import { useSyncExternalStore } from 'react'

export type Route =
  | { name: 'session'; sid: string }
  | { name: 'sources'; srcId?: string }
  | { name: 'settings' }
  | { name: 'home' }

export function parseHash(hash: string): Route {
  // 容忍 `#s/x`、`#/s/x`、`#/s/x/` 三种写法
  const h = hash.replace(/^#\/?/, '').replace(/\/+$/, '')
  if (!h) return { name: 'home' }
  const i = h.indexOf('/')
  const head = i < 0 ? h : h.slice(0, i)
  const tail = i < 0 ? '' : h.slice(i + 1)
  if (head === 's' && tail) {
    return { name: 'session', sid: decodeURIComponent(tail) }
  }
  if (head === 'sources') {
    return { name: 'sources', srcId: tail ? decodeURIComponent(tail) : undefined }
  }
  if (head === 'settings') return { name: 'settings' }
  return { name: 'home' }
}

export function toHash(r: Route): string {
  switch (r.name) {
    case 'session': return `#/s/${encodeURIComponent(r.sid)}`
    case 'sources': return `#/sources${r.srcId ? `/${encodeURIComponent(r.srcId)}` : ''}`
    case 'settings': return '#/settings'
    case 'home': return '#/'
  }
}

/** 导航 = 写 hash。hashchange 监听器再把它推回 React 状态。 */
export function nav(r: Route) {
  const h = toHash(r)
  // 相同 hash 赋值不触发 hashchange——那本来就意味着「就在这儿」，不用动
  if (window.location.hash !== h) window.location.hash = h
}

function subscribe(cb: () => void): () => void {
  window.addEventListener('hashchange', cb)
  return () => window.removeEventListener('hashchange', cb)
}

export function useRoute(): Route {
  const hash = useSyncExternalStore(subscribe, () => window.location.hash)
  return parseHash(hash)
}
