import { Icon } from './Icon'

export interface TocItem {
  id: string
  /** 2 = `##`，3 = `###` */
  level: number
  text: string
}

/** 扫出报告里的 `##` / `###`，给每一节一个 id。
 *
 *  v5 报告实测 **44 个标题**（8 个 `##` + 36 个 `###`）、23,130 字符——
 *  靠滚动找节是不可行的，这个列表就是给「按位置找」用的。
 *
 *  ⚠️ **正文和存疑节必须分开扫、再拼接**：`doubt` 是从 markdown 中段切出来的，
 *  单独扫它得到的是从 0 开始的序号，跟 DOM 里它在全部标题中的实际位置对不上。
 *  两段拼起来才是 DOM 顺序，id 也才和下面的 `setAttribute` 对得上。
 *
 *  ⚠️ 标题里的 `[S2·1]` 要去掉：正文渲染时它是可点芯片，目录里出现
 *  一串引用号只会碍眼，而且点击行为也不该跟着跳。
 */
export function scanHeadings(body: string, doubt: string): TocItem[] {
  const out: TocItem[] = []
  const scan = (text: string) => {
    // 每次新建：matchAll 不会重置 lastIndex，跨字符串复用同一个 regex 会漏匹配
    const re = /^(#{2,3})\s+(.+?)\s*$/gm
    for (const m of text.matchAll(re)) {
      out.push({
        id: `rh-${out.length}`,
        level: m[1].length,
        text: m[2].replace(/\[S\d+·\d+\]/g, '').trim(),
      })
    }
  }
  scan(body)
  scan(doubt)
  return out
}

/** 目录条。收起时只占一行，显示「当前读到哪一节」，展开是完整大纲。
 *
 *  **为什么不做成侧边浮窗**：报告正文是 `max-w-3xl` 居中，窗口窄的时候
 *  两侧本来就空，可一旦报告变成宽表或大屏，浮窗就会压到正文上。
 *  常驻顶部这一行不抢地方，展开时才占空间。
 */
export function TocBar({
  toc, active, open, onToggle, onJump,
}: {
  toc: TocItem[]
  active: string
  open: boolean
  onToggle: () => void
  onJump: (id: string) => void
}) {
  if (toc.length < 2) return null
  const cur = toc.find((t) => t.id === active) ?? toc[0]
  return (
    <div className="border-b border-line pb-1.5">
      <button
        onClick={onToggle}
        aria-expanded={open}
        className="flex w-full items-center gap-2 rounded px-1 py-1 text-left text-xs hover:bg-gray-50"
      >
        <span className="shrink-0 text-muted">目录</span>
        <span className="min-w-0 flex-1 truncate text-ink">
          {cur ? cur.text || '（无标题）' : ''}
        </span>
        <span className="shrink-0 tabular-nums text-muted">{toc.length} 节</span>
        <span className="shrink-0 text-muted" aria-hidden>
          <Icon name="chevron-down" size={12}
            className={`transition-transform duration-150 ${open ? 'rotate-180' : ''}`} />
        </span>
      </button>

      {open && (
        <ul className="mt-1 max-h-[45vh] overflow-auto border-t border-line pt-1">
          {toc.map((t) => (
            <li key={t.id}>
              <button
                onClick={() => onJump(t.id)}
                className={[
                  'block w-full truncate rounded px-2 py-1 text-left text-xs',
                  t.level === 3 ? 'pl-5 text-muted' : 'font-medium text-ink',
                  t.id === active
                    ? 'bg-black/[.055] text-ink'
                    : 'hover:bg-black/[.03]',
                ].join(' ')}
              >{t.text || '（无标题）'}</button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** 顶部 2px 阅读进度条。23,130 字符的文档，滚动到哪儿全靠这个指示。 */
export function ProgressBar({ pct }: { pct: number }) {
  return (
    <div className="h-0.5 w-full bg-line" aria-hidden>
      <div
        className="h-full bg-accent transition-[width] duration-150 ease-out"
        style={{ width: `${pct}%` }}
      />
    </div>
  )
}
