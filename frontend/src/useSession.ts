import { useCallback, useEffect, useRef, useState } from 'react'
import type {
  ApprovalPayload, ProgressEvent, SegmentRow, SessionDetail,
  SseEvent, View,
} from './types'

async function jget<T>(url: string): Promise<T> {
  const r = await fetch(url)
  if (!r.ok) throw new Error(`${r.status} ${url}`)
  return r.json() as Promise<T>
}

async function jpost<T>(url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!r.ok) throw new Error(`${r.status} ${url}`)
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
  const [thinking, setThinking] = useState<{ stage: string; text: string } | null>(null)
  const [seg, setSeg] = useState<SegmentRow | null>(null)

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

  useEffect(() => {
    if (!sid) {
      setView('idle'); setDetail(null); setProgress([])
      setNode(null); setApproval(null); setError(null)
      return
    }
    let alive = true

    void (async () => {
      try {
        const d = await loadDetail(sid)
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
            // 二轮先不展示，攒起来三轮用；只记最后一段，避免高频 setState
            setThinking({ stage: ev.stage, text: ev.delta })
            break
          case 'done':
            void (async () => {
              await loadDetail(sid).catch(() => {})
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
  }, [sid, loadApproval, loadDetail])

  const start = useCallback(async (id: string) => {
    setView('running'); setProgress([]); setError(null); setApproval(null)
    await jpost(`/api/sessions/${id}/run`)
  }, [])

  const approve = useCallback(async (id: string) => {
    setView('running'); setApproval(null)
    await jpost(`/api/sessions/${id}/approve`, { approved: true })
  }, [])

  const create = useCallback(async (question: string) => {
    const r = await jpost<{ session_id: string }>('/api/sessions', { question })
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
      setSeg(r.段落?.[0] ?? null)
    } catch (e) {
      setError(`读取 ${ref} 失败：${String(e)}`)
    }
  }, [])

  return {
    view, detail, progress, node, approval, error, thinking, seg,
    start, approve, create, archive, openSeg,
    setSeg, setView, setProgress, setNode, setApproval, setError, loadDetail,
  }
}

export type SessionApi = ReturnType<typeof useSession>

export const api = { jget, jpost }
