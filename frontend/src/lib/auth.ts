/** 鉴权前端小件（坑 99·段 4）：
 *  - 401 全局信号：任何请求层撞到 401 调 reportUnauth()，
 *    App 置 unauth → 整页空态、停轮询（jget/jpost 短路不再发请求）。
 *  - /api/auth/me 的形参与 429/409 人话映射。 */
export interface MeInfo {
  id: string; name: string; admin: boolean
  limits: { standard: number; fast: number } | null
  used: { standard: number; fast: number }
  resets_at: number
}

let _unauth = false
const _subs = new Set<() => void>()

export function isUnauth() { return _unauth }
export function reportUnauth() {
  if (_unauth) return
  _unauth = true
  _subs.forEach((f) => f())
}
export function subUnauth(fn: () => void): () => void {
  _subs.add(fn)
  return () => _subs.delete(fn)
}

export async function fetchMe(): Promise<MeInfo | null> {
  const r = await fetch('/api/auth/me')
  if (r.status === 401) { reportUnauth(); return null }
  if (!r.ok) return null
  return r.json()
}

/** 429/409 detail → 人话。返回 null = 不是已知码，走原兜底。 */
export function quotaMessage(status: number, detail: unknown): string | null {
  const d = detail as { code?: string; kind?: string;
                        limit?: number; used?: number; resets_at?: number }
  if (status === 429 && d?.code === 'quota_exceeded') {
    const kind = d.kind === 'fast' ? '快速档' : '标准档'
    const t = d.resets_at
      ? new Date(d.resets_at * 1000).toLocaleTimeString('zh-CN',
          { hour: '2-digit', minute: '2-digit' })
      : '明天'
    return `今日${kind}额度已用完（${d.used}/${d.limit}），将在 ${t} 重置。`
  }
  if (status === 429 && d?.code === 'busy')
    return '当前研究任务较多，请稍后再试。'
  if (status === 409 && d?.code === 'already_running')
    return '这场研究已经在运行。'
  return null
}
