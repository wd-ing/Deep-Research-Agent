import { useEffect, useLayoutEffect, useRef, useState } from 'react'

/** 图节点名 → 对应的 LLM stage 前缀。
 *
 * ⚠️ **两套命名不一样**（2026-10-11 实测踩到）：
 * - 图节点：`probe` / `outline` / `retrieve` / `extract` / `assess` / `write` / `finalize`
 * - LLM stage：`probe_outline` / `outline` / `assess` / `write_plan` / `write_audit` /
 *   `write_section` / `write_revise`（来自 `config/models.yaml` 的 `stages`）
 * 直接拿节点名去 `thinking[node]` 查，永远查不到 → 面板一直显示「没有思考内容」。
 */
const NODE_TO_STAGE_PREFIX: Record<string, string[]> = {
  probe: ['probe_', ''],          // probe_light / probe_outline / probe_degraded
  outline: ['outline'],
  assess: ['assess'],             // apply_revision 也走 assess（它只是应用 assess 的结果）
  write: ['write_'],
}

/** stage → 中文名。覆盖 `config/models.yaml` 里 `stages` 的全部条目。 */
const STAGE_LABEL: Record<string, string> = {
  probe_light: '轻扫', probe_outline: '轮廓', probe_degraded: '探针降级',
  outline: '框架', assess: '评估',
  write_plan: '写作规划', write_audit: '审查',
  write_section: '写作', write_revise: '返修',
  extract: '抽取', relevance: '闸门', summarize: '概括',
  dedupe: '去重', assign: '分派',
}

function human(stage: string): string {
  return STAGE_LABEL[stage] || stage
}

/**
 * 思考过程面板。
 *
 * **为什么要按 stage 分开而不是合成一大坨**：实测 `assess` 一轮思考有 7,009 字，
 * 里面是对**逐个小节**的材料充足度判断（“sec2 沾边2段、最高吻合度1，属于 high，
 * 因为可能通过检索获得”）——这正是用户最该看到的决策依据，
 * 但混在一整屏里会被 `probe_outline` 的内容淹没。
 */
export function ThinkingPanel({
  thinking, activeStage,
}: {
  thinking: Record<string, string>
  activeStage: string | null
}) {
  const stages = Object.keys(thinking)
  // 默认展开当前阶段；没有思考时整块折叠
  const [open, setOpen] = useState<string | null>(activeStage)
  const boxRef = useRef<HTMLDivElement>(null)
  const stickRef = useRef(true)   // 用户是否还在底部

  // 当前节点变了就跟着切 tab（用户手动点过之后不再抢——用 openByUser 记）
  const [openByUser, setOpenByUser] = useState(false)
  useEffect(() => {
    if (!activeStage || openByUser) return
    const prefixes = NODE_TO_STAGE_PREFIX[activeStage]
    if (!prefixes) return
    const hit = stages.find((s) => prefixes.some((p) => s.startsWith(p)))
    if (hit) setOpen(hit)
  }, [activeStage, stages, openByUser])

  // 自动跟随到底部，但**用户一上滑就停**——
  // 正在读思考内容时被人一直拽到底部是最烦人的一种「自作主张」。
  const onScroll = () => {
    const el = boxRef.current
    if (!el) return
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
  }

  useLayoutEffect(() => {
    const el = boxRef.current
    if (el && stickRef.current) el.scrollTop = el.scrollHeight
  }, [thinking, open])

  // 当前该显示哪一段：优先用户点的 tab，否则跟当前节点匹配的那一组。
  // **不包 useMemo** —— 第一版包了，而它又在 `if (!stages.length) return null`
  // 之后，于是「有思考 / 没思考」两种情况下 Hook 数量不同，React 直接崩
  // （Rendered more hooks than during the previous render）。
  // 规则很简单：**所有 Hook 必须无条件执行**，早退只影响渲染、不影响调用。
  let current = open && thinking[open] ? open : (stages[stages.length - 1] || '')
  if (!(open && thinking[open]) && activeStage) {
    const prefixes = NODE_TO_STAGE_PREFIX[activeStage]
    if (prefixes) {
      const hit = stages.find((s) => prefixes.some((p) => s.startsWith(p)))
      if (hit) current = hit
    } else if (thinking[activeStage]) {
      current = activeStage
    }
  }
  const text = thinking[current] || ''

  if (!stages.length) return null

  const chars = Object.values(thinking).reduce((a, b) => a + b.length, 0)

  return (
    <div className="border-b border-line bg-gray-50/60">
      <div className="flex items-center gap-1 px-6 pt-2">
        {stages.map((s) => (
          <button
            key={s}
            onClick={() => { setOpen(s); setOpenByUser(true); stickRef.current = true }}
            className={[
              'rounded px-2 py-0.5 text-xs',
              s === current
                ? 'bg-white font-medium text-ink shadow-sm ring-1 ring-line'
                : 'text-muted hover:bg-white',
            ].join(' ')}
          >
            {human(s)}
            <span className="ml-1 text-[10px] opacity-60">
              {Math.round(thinking[s].length / 100) / 10}k
            </span>
          </button>
        ))}
        <span className="ml-auto text-[11px] text-muted">
          共 {chars} 字
          {!stickRef.current && ' · 已暂停跟随'}
        </span>
      </div>

      <div
        ref={boxRef}
        onScroll={onScroll}
        className="max-h-56 overflow-auto px-6 py-2 text-xs leading-6 text-muted"
      >
        <pre className="whitespace-pre-wrap font-sans">
          {text || '（这一阶段还没有思考内容）'}
          {/* 实时光标：让人一眼看出这是「正在想」而不是静态文本 */}
          {activeStage === current && (
            <span className="ml-0.5 inline-block w-1.5 animate-pulse bg-accent align-middle">▍</span>
          )}
        </pre>
      </div>
    </div>
  )
}
