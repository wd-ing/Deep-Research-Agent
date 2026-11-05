/** 输入意图门（坑 98）：提交前判一次意图，非调研就地给回复卡。
 *
 *  流程：submit → POST /api/intent → research 直接建会话；
 *  非 research → 回复卡（reply + 示例 chips + 「仍然作为调研提交」）；
 *  /api/intent 网络错误/非 2xx → fail-open 直接建（不弹错）；
 *  POST /api/sessions 回 409 not_research（服务端兜底）→ 同样出回复卡。
 *  后端有 10min 缓存——前端只管顺序调，不用担心重复判定。
 *  ⚠️ 建会话不走 s.create/jpost：jpost 的错误只有字符串、拿不到
 *  409 的 detail 对象，这里用原始 fetch 自己解析。
 */
import { useCallback, useState } from 'react'
import { quotaMessage } from './auth'
import type { ResearchMode } from './mode'

export interface IntentReply {
  intent: string
  reply: string | null
  suggestions: string[]
  /** 'notice' = 额度/忙碌提示卡：无 chips、无「仍然提交」。 */
  kind?: 'intent' | 'notice'
}

export type GateOutcome = 'created' | 'blocked'

async function createRaw(question: string, mode: ResearchMode,
                         force: boolean): Promise<string> {
  const r = await fetch('/api/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, mode, force_research: force || undefined }),
  })
  if (r.ok) {
    const j = await r.json() as { session_id: string }
    return j.session_id
  }
  let detail: { code?: string; intent?: string; reply?: string;
                suggestions?: string[]; reason?: string; message?: string } = {}
  try { detail = (await r.json()).detail ?? {} } catch { /* 非 JSON 错误体 */ }
  throw Object.assign(new Error(detail.message ?? `HTTP ${r.status}`),
                      { detail, status: r.status })
}

export function useIntentGate(
  /** 创建成功后由调用方启动会话（App 里 = s.start(id)）。 */
  onCreated: (sessionId: string) => void,
) {
  const [checking, setChecking] = useState(false)
  const [reply, setReply] = useState<IntentReply | null>(null)
  /** 用户改输入就消卡——gate 只对「提交那一刻的文本」负责。 */
  const dismiss = useCallback(() => setReply(null), [])

  const submit = useCallback(async (
      question: string, mode: ResearchMode): Promise<GateOutcome> => {
    setChecking(true)
    setReply(null)
    try {
      let blocked: IntentReply | null = null
      try {
        const r = await fetch('/api/intent', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ question }),
        })
        if (r.ok) {
          const j = await r.json() as IntentReply & { source: string }
          if (j.intent !== 'research') blocked = j
        }
        // 非 2xx 也放行——意图服务挂了不能挡住真调研（fail-open）
      } catch { /* 网络错误同样 fail-open */ }
      if (blocked) { setReply(blocked); return 'blocked' }
      try {
        onCreated(await createRaw(question, mode, false))
        return 'created'
      } catch (e) {
        const e2 = e as { detail?: IntentReply & { code?: string };
                          status?: number }
        const d = e2.detail
        if (d?.code === 'not_research') {
          setReply({ intent: d.intent ?? 'unclear',
                     reply: d.reply ?? null,
                     suggestions: d.suggestions ?? [] })
          return 'blocked'
        }
        // 限额/并发——同一条回复卡通路，但无 chips/force（不是意图问题）
        const q = quotaMessage(e2.status ?? 0, d)
        if (q) {
          setReply({ intent: 'quota', reply: q, suggestions: [],
                     kind: 'notice' })
          return 'blocked'
        }
        throw e
      }
    } finally {
      setChecking(false)
    }
  }, [onCreated])

  /** 「仍然作为调研提交」：看过回复卡后的明示选择，跳过判定直接建。 */
  const forceSubmit = useCallback(async (
      question: string, mode: ResearchMode): Promise<void> => {
    setReply(null)
    setChecking(true)
    try { onCreated(await createRaw(question, mode, true)) }
    finally { setChecking(false) }
  }, [onCreated])

  return { checking, reply, submit, forceSubmit, dismiss }
}
