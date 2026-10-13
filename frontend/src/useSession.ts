import { useCallback, useEffect, useRef, useState } from 'react'
import type {
  ApprovalPayload, ProgressEvent, SegmentRow, SessionDetail,
  SseEvent, View,
} from './types'

/** 把后端的错误体翻成人能读的一句话。
 *
 *  FastAPI 的错误体是 `{"detail": "段号 S12·3 不存在"}`。以前只拼
 *  `${r.status} ${url}`，用户看到的是 `404 /api/sessions/s_x/seg/S12%C2%B73`——
 *  状态码和 URL 对他没有任何意义。**报错要说清「哪一步失败、为什么」。** */
async function errText(r: Response, url: string): Promise<string> {
  try {
    const j = await r.json() as { detail?: unknown }
    if (typeof j?.detail === 'string' && j.detail) return j.detail
  } catch {
    // 错误体不是 JSON（比如反向代理返回的 HTML 502 页），退回状态码
  }
  return `${r.status} ${url}`
}

async function jget<T>(url: string): Promise<T> {
  const r = await fetch(url)
  if (!r.ok) throw new Error(await errText(r, url))
  return r.json() as Promise<T>
}

async function jpost<T>(url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!r.ok) throw new Error(await errText(r, url))
  return r.json() as Promise<T>
}

/** `done` 之后图还会停在审批点（interrupt），所以收到 done 不能就断定结束。
 *  轮询 approval，拿到 payload 才切到 awaiting。 */
export function useSession(sid: string | null) {
  const [view, setView] = useState<View>('idle')
  const [detail, setDetail] = useState<SessionDetail | null>(null)
  const [progress, setProgress] = useState<ProgressEvent[]>([])
  const [node, setNode] = useState<string | null>(null)
  const [approval, setApproval] = useState<ApprovalPayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [thinking, setThinking] = useState<Record<string, string>>({})
  // 正文增量不分小节（write_section 是 4 并发、run_tool_loop 拿不到 task_id，
  // 增量无法归属到具体小节——这是实测确认的约束，不是偷懒）。
  const [draft, setDraft] = useState('')
  const [seg, setSeg] = useState<SegmentRow | null>(null)

  // ---- 一次操作没成时的提示（2026-10-13 加）----
  // ⚠️ **不要复用 `error`。** 那个字段驱动的是**整屏错误态**（图跑挂了、
  // 会话读不出来），而这里报的是**某一次操作没成**：点引用没反应、
  // 标记排除没保存、连不上服务。共用一个字段的后果正是 v5 之前的 bug——
  // `openSeg` 失败只 `setError`，而错误区只在 `view === 'error'` 时渲染，
  // 那句「读取 S12·3 失败」**永远不会有人看见**。
  const [notice, setNotice] = useState<string | null>(null)
  const noticeTimer = useRef<number | null>(null)

  /** 弹一条会自己消失的提示。重复调用只延长显示时间，不叠加。 */
  const notify = useCallback((msg: string) => {
    setNotice(msg)
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current)
    noticeTimer.current = window.setTimeout(() => setNotice(null), 7000)
  }, [])
  useEffect(() => () => {
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current)
  }, [])

  const esRef = useRef<EventSource | null>(null)
  const timerRef = useRef<number | null>(null)

  const loadApproval = useCallback(async (id: string) => {
    try {
      const r = await jget<{ waiting: boolean; approval?: ApprovalPayload }>(
        `/api/sessions/${id}/approval`)
      if (r.waiting && r.approval) {
        setApproval(r.approval)
        setView('awaiting')
      } else {
        setApproval(null)
      }
    } catch {
      // 轮询失败不是致命错误：网络抖一下不该把界面搞崩
    }
  }, [])

  const loadDetail = useCallback(async (id: string) => {
    const d = await jget<SessionDetail>(`/api/sessions/${id}`)
    setDetail(d)
    return d
  }, [])

  // ---- 「正在取数据」标记（2026-10-13 加）----
  // 全项目原来零加载态：点侧栏换会话，那几百毫秒里界面**显示的是上一场的报告**
  // （detail 要等新请求回来才被替换）。看着像已经切过去了，内容却是错的——
  // 比空白更糟，因为人会照着那份旧报告找东西。
  // 用计数器而不是布尔值：loadDetail 会被并发调用（切会话 + 刷新详情 + 审批回来），
  // 布尔值会被先完成的那次提前清掉。
  const inflight = useRef(0)
  const [busy, setBusy] = useState(false)
  const track = useCallback(<T,>(p: Promise<T>): Promise<T> => {
    inflight.current += 1
    setBusy(true)
    return p.finally(() => {
      inflight.current -= 1
      if (inflight.current <= 0) setBusy(false)
    })
  }, [])

  useEffect(() => {
    if (!sid) {
      setView('idle'); setDetail(null); setProgress([])
      setNode(null); setApproval(null); setError(null)
      setThinking({}); setDraft('')
      return
    }
    let alive = true

    // ⚠️ 换会话时**先清掉 detail 和 view**。不清的话界面会一直挂着上一场的报告，
    // 直到新请求回来才替换——那几百毫秒里看着像已经切过去了，内容却是错的。
    // `view` 也必须回到 idle：从一条**已完成**的会话切过来时它还是 'done'，
    // 而主区域是按 view 分支渲染的，`done` + 空报告会命中
    // 「这次研究没有产出报告正文」——那个空状态是在说谎。
    setDetail(null)
    setView('idle')
    setProgress([])
    setApproval(null)
    setError(null)

    void (async () => {
      try {
        const d = await track(loadDetail(sid))
        if (!alive) return
        // 已在跑的会话（刷新页面后重连）不该显示成 idle
        setView(d.status === 'done' ? 'done' : 'running')
        if (d.status === 'done') { void loadApproval(sid); return }
      } catch (e) {
        if (alive) { setView('error'); setError(String(e)) }
        return
      }

      const es = new EventSource(`/api/stream/${sid}`)
      esRef.current = es

      es.onmessage = (m) => {
        if (!alive) return
        let ev: SseEvent
        try { ev = JSON.parse(m.data) as SseEvent } catch { return }

        switch (ev.kind) {
          case 'node':
            setNode(ev.node)
            break
          case 'progress':
            setProgress((p) => [...p, ev.event])
            break
          case 'thinking':
            // 思考增量按 stage 累积（不是只留最后一段）。
            // 后端同一 stage 会分批推，最后一批带 `final:true`。
            // 累积量可能到几千字（实测 assess 一轮 7,009 字），
            // 所以拼接用「取尾部」而不是整段重赋——否则每 0.25 秒重渲整棵子树。
            setThinking((prev) => {
              const cur = prev[ev.stage] || ''
              return { ...prev, [ev.stage]: cur + ev.delta }
            })
            break
          case 'draft':
            setDraft((d) => d + ev.delta)
            break
          case 'done':
            void (async () => {
              await track(loadDetail(sid)).catch(() => {})
              // done 之后可能还停在审批点（interrupt），所以要问一次
              await loadApproval(sid)
            })()
            break
          case 'error':
            setError(ev.error)
            setView('error')
            break
        }
      }

      es.onerror = () => {
        // EventSource 自带重连，但断线期间的事件会丢。
        // 用轮询兜底：会话数据本来就在 DB 里，重启 server 也不丢。
        if (!alive) return
        es.close()
        void loadApproval(sid)
        timerRef.current = window.setInterval(() => {
          void loadApproval(sid)
        }, 4000)
      }
    })()

    return () => {
      alive = false
      esRef.current?.close()
      if (timerRef.current) window.clearInterval(timerRef.current)
    }
  }, [sid, loadApproval, loadDetail, track])

  const start = useCallback(async (id: string) => {
    setView('running'); setProgress([]); setError(null)
    setApproval(null); setThinking({}); setDraft('')
    await track(jpost(`/api/sessions/${id}/run`))
  }, [])

  const approve = useCallback(async (id: string) => {
    setView('running'); setApproval(null)
    await track(jpost(`/api/sessions/${id}/approve`, { approved: true }))
  }, [])

  const create = useCallback(async (question: string) => {
    const r = await track(jpost<{ session_id: string }>('/api/sessions', { question }))
    return r.session_id
  }, [])

  const archive = useCallback(async (id: string) => {
    await jpost(`/api/sessions/${id}/archive`)
    await loadDetail(id)
  }, [loadDetail])

  const openSeg = useCallback(async (id: string, ref: string) => {
    const enc = encodeURIComponent(ref)
    try {
      const r = await jget<{ 段落?: SegmentRow[]; 没找到?: string[] }>(
        `/api/sessions/${id}/seg/${enc}`)
      // 后端查无此段时抛 404；真拿到空数组（字典非空但没段）也要兜住，
      // 否则 `setSeg(null)` 让抽屉不开，用户只看到「点了没反应」。
      if (!r.段落?.length) {
        notify(`报告里引用的 ${ref} 在库里找不到${r.没找到?.length ? `：${r.没找到.join('、')}` : ''}`)
        return
      }
      setSeg(r.段落[0])
    } catch (e) {
      notify(`打不开 ${ref}：${(e as Error).message}`)
    }
  }, [notify])

  return {
    view, detail, progress, node, approval, error, thinking, draft, seg, busy,
    notice, notify, setNotice,
    start, approve, create, archive, openSeg,
    setSeg, setView, setProgress, setNode, setApproval, setError, loadDetail,
  }
}

export type SessionApi = ReturnType<typeof useSession>

async function jpatch<T>(url: string, body: unknown): Promise<T> {
  const r = await fetch(url, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok) throw new Error(`${r.status} ${url}`)
  return r.json() as Promise<T>
}

async function jdel<T>(url: string): Promise<T> {
  const r = await fetch(url, { method: 'DELETE' })
  if (!r.ok) throw new Error(`${r.status} ${url}`)
  return r.json() as Promise<T>
}

export const api = { jget, jpost, jpatch, jdel }
