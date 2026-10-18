import { useCallback, useEffect, useRef, useState } from 'react'
import type {
  ApprovalPayload, ProgressEntry, SegmentRow, SessionDetail,
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

/** 系统通知（B3 离开友好）。只在「用户不盯着这个标签页」时发——
 *  正看着时还弹通知只是吵。不支持/没授权就静默跳过。 */
function sysNotify(title: string, body: string): void {
  try {
    if (!('Notification' in window) || Notification.permission !== 'granted') return
    if (document.hasFocus()) return
    new Notification(title, { body })
  } catch { /* 平台不支持就当没这功能 */ }
}

/** 在用户**点了启动/批准**这种主动作时才申请通知权限——
 *  时机和意图相关，接受率比页面加载就弹高得多。 */
export function askNotifyPermission(): void {
  try {
    if ('Notification' in window && Notification.permission === 'default') {
      void Notification.requestPermission()
    }
  } catch { /* 同上 */ }
}

/** `done` 之后图还会停在审批点（interrupt），所以收到 done 不能就断定结束。
 *  轮询 approval，拿到 payload 才切到 awaiting。 */
export function useSession(sid: string | null) {
  const [view, setView] = useState<View>('idle')
  const [detail, setDetail] = useState<SessionDetail | null>(null)
  const [progress, setProgress] = useState<ProgressEntry[]>([])
  const [node, setNode] = useState<string | null>(null)
  // 当前节点开始时刻（B2 活性信号：环节秒表）
  const [nodeAt, setNodeAt] = useState<number | null>(null)
  // 累计 token（B2：10s 轮询便宜端点，数字在涨就是活着）
  const [cost, setCost] = useState<number | null>(null)
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
  /** 提示分两级（2026-10-18 视觉轮）：err=某次操作没成（红，7s）；
   *  ok=操作成了（中性，2.5s 短一点——成功的确认不需要读那么久）。
   *  原来只有 err 一档：成功和失败长成一个样，红色被用成了通用色，
   *  真报错时反而失去警示力。 */
  const [notice, setNotice] = useState<{ msg: string; kind: 'err' | 'ok' } | null>(null)
  const noticeTimer = useRef<number | null>(null)

  /** 弹一条会自己消失的提示。重复调用只延长显示时间，不叠加。 */
  const notify = useCallback((msg: string, kind: 'err' | 'ok' = 'err') => {
    setNotice({ msg, kind })
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current)
    noticeTimer.current = window.setTimeout(() => setNotice(null), kind === 'ok' ? 2500 : 7000)
  }, [])
  useEffect(() => () => {
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current)
  }, [])

  const esRef = useRef<EventSource | null>(null)
  const timerRef = useRef<number | null>(null)
  // 轮询回调里读 view 需要最新值：effect 不随 view 重跑，留个 ref
  const viewRef = useRef(view)
  useEffect(() => {
    viewRef.current = view
  }, [view])

  /** 返回后端看到的会话状态——SSE 断线时的兜底轮询靠它判断该不该停。
   *  拉 `/api/sessions/{id}` 是 110 KB，每 4 秒一次太重，所以走 approval 这个
   *  便宜端点（后端在那里顺带回了 status 和 cost_tokens）。
   *  `waiting` 一并回：done 事件后要靠它分辨「停在了审批点」还是「真跑完了」。 */
  const loadApproval = useCallback(async (id: string) => {
    try {
      const r = await jget<{
        waiting: boolean; approval?: ApprovalPayload; status?: string | null
        cost_tokens?: number | null
      }>(`/api/sessions/${id}/approval`)
      if (typeof r.cost_tokens === 'number') setCost(r.cost_tokens)
      if (r.waiting && r.approval) {
        setApproval(r.approval)
        setView('awaiting')
      } else {
        setApproval(null)
      }
      return { status: r.status ?? null, waiting: !!(r.waiting && r.approval) }
    } catch {
      // 轮询失败不是致命错误：网络抖一下不该把界面搞崩
      return { status: null, waiting: false }
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
      setNode(null); setNodeAt(null); setCost(null)
      setApproval(null); setError(null)
      setThinking({}); setDraft('')
      return
    }
    let alive = true
    // 活性信号轮询的定时器句柄：在 IIFE 里赋值，在 cleanup 里清，
    // 所以必须声明在 effect 作用域（放 IIFE 里 cleanup 拿不到）。
    let costTimer: number | null = null

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
    setNodeAt(null)
    setCost(null)

    void (async () => {
      try {
        const d = await track(loadDetail(sid))
        if (!alive) return
        setCost(d.cost_tokens ?? null)
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
            setNodeAt(Date.now())
            break
          case 'progress':
            // ⚠️ **`node` 必须一起存**（`server.py:832` 发的是 `{kind,node,event}`，
            // node 在外层）。原来只 push `ev.event`，把 node 丢了——
            // `buildPhases` 拿不到事件出自哪个节点，只能拿「当前节点」去猜，
            // 结果写作阶段整个「规划」环节消失。端到端复现见 DECISIONS.md。
            setProgress((p) => [...p, { ...ev.event, node: ev.node }])
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
              const d = await track(loadDetail(sid)).catch(() => null)
              if (d) setCost(d.cost_tokens ?? null)
              // done 之后可能还停在审批点（interrupt），所以要问一次。
              // 两种结束语义不同，通知文案也要分开：
              // 停在审批点 = 「等你批准」，跑完 = 「报告好了」。
              const r = await loadApproval(sid)
              const t = d?.title || d?.question || sid
              sysNotify(
                r.waiting ? '研究框架待审批' : '研究完成',
                r.waiting ? `「${t}」的框架已校准好，等你批准开始写作`
                          : `「${t}」的报告已生成`,
              )
            })()
            break
          case 'error':
            setError(ev.error)
            setView('error')
            sysNotify('研究出错', (ev.error ?? '').slice(0, 120))
            break
        }
      }

      // 活性信号（B2）：SSE 正常时这部分一个事件都不来，
      // 每 10s 问一次便宜端点拿累计 token——数字在涨就是活着。
      // 终态（done/error）自停，别打到天荒地老。
      costTimer = window.setInterval(() => {
        const v = viewRef.current
        if (v === 'done' || v === 'error') {
          if (costTimer !== null) window.clearInterval(costTimer)
          return
        }
        void jget<{ cost_tokens?: number | null }>(`/api/sessions/${sid}/approval`)
          .then((r) => {
            if (typeof r.cost_tokens === 'number') setCost(r.cost_tokens)
          })
          .catch(() => {})
      }, 10_000)

      es.onerror = () => {
        // EventSource 自带重连，但断线期间的事件会丢。
        // 用轮询兜底：会话数据本来就在 DB 里，重启 server 也不丢。
        if (!alive) return
        es.close()
        void loadApproval(sid)
        // ⚠️ 原来这里有三个问题叠在一起：
        // ① `onerror` 会连着来好几次，每次都 `setInterval` 覆盖 `timerRef.current`
        //    ——**前一个 interval 变成没人管的孤儿，永远停不下来**；
        // ② 轮询只在组件卸载时才停。可 SSE 已经断了，前端也就再也不会知道
        //    「这场跑完了」，于是每 4 秒打一次接口、每次带一次 setState，
        //    一场已经结束的研究也会这么转到天荒地老；
        // ③ 拉详情来判状态是 110 KB，太重。
        // 现在：已有一个就别再开；跑完了就地自停（后端在 approval 里回了 status）。
        if (timerRef.current !== null) return
        timerRef.current = window.setInterval(() => {
          void loadApproval(sid).then((r) => {
            if (r.status !== 'done') return
            if (timerRef.current !== null) {
              window.clearInterval(timerRef.current)
              timerRef.current = null
            }
          })
        }, 4000)
      }
    })()

    return () => {
      alive = false
      esRef.current?.close()
      // 清完要把 ref 置回 null，否则下次 onerror 看到它非空就不开新轮询了
      if (timerRef.current !== null) {
        window.clearInterval(timerRef.current)
        timerRef.current = null
      }
      if (costTimer !== null) window.clearInterval(costTimer)
    }
  }, [sid, loadApproval, loadDetail, track])

  const start = useCallback(async (id: string) => {
    askNotifyPermission()
    setView('running'); setProgress([]); setError(null)
    setApproval(null); setThinking({}); setDraft('')
    setNodeAt(Date.now())
    await track(jpost(`/api/sessions/${id}/run`))
  }, [])

  const approve = useCallback(async (id: string) => {
    askNotifyPermission()
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
    view, detail, progress, node, nodeAt, cost, approval, error,
    thinking, draft, seg, busy,
    notice, notify, setNotice,
    start, approve, create, archive, openSeg,
    setSeg, setView, setProgress, setNode, setApproval, setError, loadDetail,
  }
}

export type SessionApi = ReturnType<typeof useSession>

/** ⚠️ 这两个也**必须**走 `errText`。它们原本拼的是 `${r.status} ${url}`，
 *  是 P0 之前的老格式——而它们恰好是「改名 / 归档 / 移文件夹 / 删除」全部
 *  写操作的入口：用户点「移到回收站」失败，看到的是
 * `500 /api/sessions/s_xxx`，既不知道哪一步失败也不知道原因。
 * 同文件里的 `jget`/`jpost` 早就修好了，这两个漏了。 */
async function jpatch<T>(url: string, body: unknown): Promise<T> {
  const r = await fetch(url, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok) throw new Error(await errText(r, url))
  return r.json() as Promise<T>
}

async function jput<T>(url: string, body: unknown): Promise<T> {
  const r = await fetch(url, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok) throw new Error(await errText(r, url))
  return r.json() as Promise<T>
}

async function jdel<T>(url: string): Promise<T> {
  const r = await fetch(url, { method: 'DELETE' })
  if (!r.ok) throw new Error(await errText(r, url))
  return r.json() as Promise<T>
}

export const api = { jget, jpost, jpatch, jdel, jput }
