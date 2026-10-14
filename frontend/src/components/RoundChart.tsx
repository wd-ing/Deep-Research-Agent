import { useEffect, useMemo, useState } from 'react'
import { api } from '../useSession'
import type { SessionTrace, TraceRound } from '../types'

/* 检索轮次图表：每一轮检索拿到了多少东西、材料存量怎么长起来的。
 *
 * ## 为什么是这三样，不多不少
 *
 *   命中 hits      —— 搜索返回多少条。**看检索面在不在变窄**。
 *   采用 accepted  —— 通过筛选真正收进来的来源。看漏斗漏在哪。
 *   累计段 seg     —— 材料存量。看的是斜率：涨得快说明还有油水，平了说明该停。
 *
 * `new_segments` / `round_gain` / `gaps_by_urgency` 这些不放进图里，
 * 全在下面的文字行——图上放三个同量纲的「个数」就够了，
 * 再多就变成看图不看字，或者看字不看图。
 *
 * ## 无动画
 *
 * 这是一张事后回看的静态图，动它没有任何信息量。
 * 用户系统里若关了动效，这里本来也没东西要关。
 */

const W = 640
const H = 168
const PAD = { l: 34, r: 12, t: 14, b: 24 }
const PLOT_W = W - PAD.l - PAD.r
const PLOT_H = H - PAD.t - PAD.b

const C_HIT = '#93c5fd'
const C_ACCEPT = '#2563eb'
const C_TOTAL = '#f59e0b'

function fmt(n: number): string {
  return n >= 10000 ? `${(n / 1000).toFixed(0)}k` : String(n)
}

const Chart = ({ rounds }: { rounds: TraceRound[] }) => {
  const max = useMemo(() => {
    const vals = rounds.flatMap((r) => [r.hits, r.accepted, r.segments_total])
      .filter((v): v is number => typeof v === 'number')
    return Math.max(1, ...vals) * 1.12
  }, [rounds])

  const y = (v: number) => PAD.t + PLOT_H * (1 - v / max)
  const groupW = PLOT_W / Math.max(rounds.length, 1)
  const barW = Math.min(22, groupW * 0.2)
  const gap = 5
  const cx = (i: number) => PAD.l + groupW * (i + 0.5)

  // 累计段折线。**某轮没这个数就断开**，不把两端硬连——
  // 连起来会画出一条根本没发生过的增长。
  const pts = rounds
    .map((r, i) => (typeof r.segments_total === 'number'
      ? { x: cx(i), y: y(r.segments_total) } : null))
  const lineD = pts.reduce((acc, p, i) => {
    if (!p) return acc + 'M' + (pts[i - 1]?.x ?? 0) + ' ' + (pts[i - 1]?.y ?? 0) + ' '
    return acc + (i === 0 || !pts[i - 1] ? 'M' : 'L') + p.x + ' ' + p.y + ' '
  }, '')

  const gridY = [0, 0.5, 1].map((f) => PAD.t + PLOT_H * (1 - f))
  // gridY 是从**下往上**排的（值 0 在最下面），标签必须跟着从 0 排到 max。
  // ⚠️ 这里曾经写反成 [1, 0.5, 0]，画出来是「0 在顶、160 在底」——
  // 柱子明明朝上长，刻度却往下走，正是靠截图才发现的（DOM 里看不出反没反）。
  const gridLabel = [0, 0.5, 1].map((f) => Math.round(max * f))
  // viewBox 坐标 → 百分比。SVG 只画几何，文字全交给 HTML（见下方注释）
  const py = (yv: number) => `${(yv / H) * 100}%`
  const px = (xv: number) => `${(xv / W) * 100}%`

  return (
    // ⚠️ 刻度文字**不能画在 SVG 里**。SVG 按 viewBox 等比缩放，面板窄的时候
    // （实测 368px 宽 → 缩放 0.575）9px 的字实际只有 5.2px，根本看不清。
    // 所以：SVG 只画会被缩放的图形，刻度/轮次标签用同一坐标系做 HTML 绝对定位，
    // 字号永远是 CSS 像素，跟面板宽度无关。
    <div
      className="relative w-full"
      style={{ aspectRatio: `${W} / ${H}` }}
      role="img"
      aria-label={rounds
        .map((r) => `第 ${r.round} 轮：命中 ${r.hits ?? '?'}，采用 ${r.accepted ?? '?'}，累计 ${r.segments_total ?? '?'} 段`)
        .join('；')}
    >
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        aria-hidden
        className="absolute inset-0 h-full w-full"
      >
      {gridY.map((gy, i) => (
        <line
          key={gy}
          x1={PAD.l} x2={W - PAD.r} y1={gy} y2={gy}
          stroke="#e5e7eb" strokeWidth={1}
          strokeDasharray={i === 0 ? undefined : '2 3'}
        />
      ))}

      {rounds.map((r, i) => {
        const c = cx(i)
        return (
          <g key={r.round}>
            {typeof r.hits === 'number' && (
              <rect
                x={c - barW - gap / 2} y={y(r.hits)} width={barW}
                height={Math.max(PAD.t + PLOT_H - y(r.hits), 0)}
                fill={C_HIT} rx={2}
              />
            )}
            {typeof r.accepted === 'number' && (
              <rect
                x={c + gap / 2} y={y(r.accepted)} width={barW}
                height={Math.max(PAD.t + PLOT_H - y(r.accepted), 0)}
                fill={C_ACCEPT} rx={2}
              />
            )}
          </g>
        )
      })}

      {lineD && (
        <>
          <path d={lineD} fill="none" stroke={C_TOTAL} strokeWidth={1.5} />
          {pts.map((p, i) => p && (
            <circle key={i} cx={p.x} cy={p.y} r={2.5} fill={C_TOTAL} />
          ))}
        </>
      )}
      </svg>

      {/* 纵轴刻度：右对齐到绘图区左边缘 */}
      {gridY.map((gy, i) => (
        <span
          key={gy}
          className="absolute -translate-y-1/2 text-2xs tabular-nums text-gray-400"
          style={{ top: py(gy), left: 0, width: px(PAD.l - 6), textAlign: 'right' }}
        >{fmt(gridLabel[i])}</span>
      ))}

      {/* 横轴轮次 */}
      {rounds.map((r, i) => (
        <span
          key={r.round}
          className="absolute -translate-x-1/2 -translate-y-1/2 text-2xs text-muted"
          style={{ top: py(H - 8), left: px(cx(i)) }}
        >第 {r.round} 轮</span>
      ))}
    </div>
  )
}

const Legend = () => (
  <div className="flex flex-wrap items-center gap-x-3.5 gap-y-1 text-2xs text-muted">
    <span className="inline-flex items-center gap-1">
      <span className="h-2 w-2 rounded-[1px]" style={{ background: C_HIT }} />命中
    </span>
    <span className="inline-flex items-center gap-1">
      <span className="h-2 w-2 rounded-[1px]" style={{ background: C_ACCEPT }} />采用
    </span>
    <span className="inline-flex items-center gap-1">
      <span className="h-[2px] w-3 rounded" style={{ background: C_TOTAL }} />累计段
    </span>
  </div>
)

/**
 * 折叠态一行，展开态一图 + 一表。
 *
 * **默认折叠**：跑完的报告动辄两万字，事后回看时最先要回答的是
 * 「为什么只查到这些材料」，一眼能扫到的那一行就够了，
 * 整张图是第二眼才要看的东西。
 *
 * 数据来自 `run_trace` 而不是 SSE 事件——会话跑完后前端根本不会重连
 * （`useSession` 在 `status === 'done'` 时直接 return，不开 EventSource），
 * 只从事件推的话刷新一次这张图就永远空了。
 */
export function RoundChart({ sid, refreshKey }: { sid: string; refreshKey: number }) {
  const [trace, setTrace] = useState<SessionTrace | null>(null)
  const [open, setOpen] = useState(false)

  useEffect(() => {
    let alive = true
    // 跑的过程中 progress 每来一条就 +1，不防抖会连着打十几个请求
    const t = setTimeout(() => {
      api.jget<SessionTrace>(`/api/sessions/${sid}/trace`)
        .then((r) => { if (alive) setTrace(r) })
        .catch(() => { if (alive) setTrace(null) })
    }, 400)
    return () => { alive = false; clearTimeout(t) }
  }, [sid, refreshKey])

  const rounds = trace?.rounds ?? []
  // 没有轮次就不占位——空数据不画框
  if (!rounds.length) return null

  const sum = (k: 'hits' | 'accepted') =>
    rounds.reduce((a, r) => a + (typeof r[k] === 'number' ? r[k]! : 0), 0)
  const last = rounds[rounds.length - 1]
  const stop = rounds.find((r) => r.stop_would_be)

  return (
    <div className="border-b border-line bg-white/70 px-6">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2.5 py-2 text-left"
      >
        <span
          className={[
            'shrink-0 text-2xs text-muted transition-transform duration-200',
            open ? 'rotate-90' : '',
          ].join(' ')}
        >▶</span>
        <span className="shrink-0 text-xs text-ink">检索轮次</span>
        <span className="min-w-0 flex-1 truncate text-xs text-muted">
          {rounds.length} 轮 · 命中 {sum('hits')} · 采用 {sum('accepted')} 来源 · 累计{' '}
          {last.segments_total ?? '?'} 段
        </span>
      </button>

      {open && (
        <div className="pb-3 pl-6">
          <Chart rounds={rounds} />
          <div className="mt-1 flex items-start justify-between gap-4">
            <Legend />
          </div>

          {/* 图给形状，这几行给准确数——图上读不出 77 和 58 的区别 */}
          <table className="mt-2 w-full text-2xs text-muted">
            <thead>
              <tr className="text-left">
                <th className="w-14 font-normal">轮次</th>
                <th className="w-14 text-right font-normal">命中</th>
                <th className="w-14 text-right font-normal">采用</th>
                <th className="w-16 text-right font-normal">新增段</th>
                <th className="w-16 text-right font-normal">累计段</th>
                <th className="w-16 text-right font-normal">缺口</th>
              </tr>
            </thead>
            <tbody>
              {rounds.map((r) => (
                <tr key={r.round} className="border-t border-line/70">
                  <td>第 {r.round} 轮</td>
                  <td className="text-right tabular-nums">{r.hits ?? '—'}</td>
                  <td className="text-right tabular-nums">{r.accepted ?? '—'}</td>
                  <td className="text-right tabular-nums">{r.new_segments ?? '—'}</td>
                  <td className="text-right tabular-nums">{r.segments_total ?? '—'}</td>
                  <td className="text-right tabular-nums">
                    {typeof r.gaps_by_urgency === 'object' && r.gaps_by_urgency
                      ? Object.values(r.gaps_by_urgency).reduce((a, b) => a + b, 0) || 0
                      : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {/* 停止判据是**解释**，不是要用户决策，所以不用 ⚠️ */}
          {stop && (
            <p className="mt-2 text-2xs leading-5 text-muted">
              停在第 {stop.round} 轮：{stop.stop_would_be}
              {stop.stop_layer && <span className="opacity-70">（判据来自 {stop.stop_layer} 层）</span>}
            </p>
          )}
        </div>
      )}
    </div>
  )
}