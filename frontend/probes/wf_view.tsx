/**
 * 工作流视图探针 —— 用**真实录制的 SSE fixture** 跑 `buildPhases`，
 * 并把 `<WorkflowView>` SSR 成一张 HTML 页面。
 *
 * ## 为什么需要它
 *
 * 坑 25：SSE 的 progress 事件**不落库**，会话跑完 `useSession` 直接 return、
 * 不开 EventSource，`progress` 恒空。所以工作流视图**只在真实运行的那几分钟
 * 里有数据**，回放服务又只测得到审批为止 —— 这个视图实际上从没被前端测过。
 *
 * 这个探针把「真实事件 → 真实组件 → 真的能看见」这条链补上，
 * 以后改工作流视图不用再靠脑补。
 *
 * ## 跑法
 *
 *   cd frontend
 *   npx esbuild probes/wf_view.tsx --bundle --platform=node --format=esm \
 *     --outfile=/tmp/wfview/probe.mjs --jsx=automatic \
 *     --external:react --external:react-dom \
 *     --loader:.css=empty && node /tmp/wfview/probe.mjs
 *
 * 然后 `python3 -m http.server 5199 --directory /tmp/wfview`，
 * 浏览器打开 http://127.0.0.1:5199/view.html
 *
 * ⚠️ 页面里的样式是 **真产物 CSS**（从 dist 拷过来），不是另写一套，
 * 所以看到的排版就是线上排版。
 */
import { readFileSync, writeFileSync, copyFileSync, readdirSync } from 'node:fs'
import { renderToStaticMarkup } from 'react-dom/server'
import { buildPhases, type Phase } from '../src/views/workflow'
import { WorkflowView } from '../src/views/WorkflowView'
import type { ProgressEntry } from '../src/types'

const OUT = '/tmp/wfview'
// 从 frontend/ 跑（见下方跑法），所以用 cwd 拼路径——
// CJS 输出下 import.meta.url 不可用
const ROOT = process.cwd()
const FIX = `${ROOT}/../tests/fixtures/`
const DIST = `${ROOT}/dist/assets/`

/** fixture 的形状是 `{at, event:{kind:'progress', node, event:{...}}}`，
 *  而组件吃的是 `{...内层, node}` —— 和 `useSession` 存的形状一致。 */
function loadProgress(file: string): ProgressEntry[] {
  const raw = JSON.parse(readFileSync(FIX + file, 'utf8')) as {
    events: { event: { kind: string; node?: string; event?: Record<string, unknown> } }[]
  }
  return raw.events
    .filter((e) => e.event.kind === 'progress' && e.event.event)
    .map((e) => ({ ...(e.event.event as object), node: e.event.node as string }) as ProgressEntry)
}

const before = loadProgress('run_to_approval.json')          // 10 条，跑到审批
const after = loadProgress('run_after_approval.json')       // 7 条，审批后到写完
const whole = [...before, ...after]                         // 17 条，整轮

// ── 统计：先量清楚再动手 ────────────────────────────────────────────
function stats(name: string, ps: Phase[]) {
  console.log(`\n===== ${name} =====`)
  console.log(`环节数 ${ps.length}｜步骤总数 ${ps.reduce((a, p) => a + p.steps.length, 0)}`)
  let warn = 0, detail = 0, running = 0, rows = 0
  for (const p of ps) {
    const w = p.steps.filter((s) => s.warn).length
    const d = p.steps.filter((s) => s.detail?.length).length
    const r = p.steps.filter((s) => s.state === 'running').length
    warn += w; detail += d; running += r
    rows += p.steps.reduce((a, s) => a + (s.detail?.length ?? 0), 0)
    console.log(`【${p.label}】${p.summary}`)
    for (const s of p.steps) {
      console.log(
        `    ${s.warn ? '⚠️ ' : ''}${s.label.padEnd(14)} ${s.summary}` +
        (s.detail?.length ? `   [明细 ${s.detail.length} 行]` : '') +
        (s.state === 'running' ? '   <<进行中>>' : '')
      )
    }
  }
  console.log(`—— 汇总：⚠️ ${warn} 处｜可展开明细 ${detail} 项（展开后共 ${rows} 行）｜进行中 ${running}`)
}

stats('跑到审批（view=awaiting，node=null）', buildPhases(before, null, 'awaiting'))
stats('整轮跑完（view=done，node=null）', buildPhases(whole, null, 'done'))
stats('正在写作（view=running，node=write）', buildPhases(before, 'write', 'running'))
stats('**空状态**（0 个事件，view=running）', buildPhases([], 'probe', 'running'))

// ── SSR 成能看的页面 ───────────────────────────────────────────────
const cases: [string, Phase[], boolean][] = [
  ['① 正在跑（进行中）—— 真实 run_to_approval + node=write',
    buildPhases(before, 'write', 'running'), true],
  ['② 整轮跑完 —— 真实 run_to_approval + run_after_approval',
    buildPhases(whole, null, 'done'), false],
  ['③ 空状态 —— 0 个事件',
    buildPhases([], 'probe', 'running'), true],
]

// 先定 CSS 名再写页面：之前是反过来——从 OUT 里「已存在的」css 反推名字，
// 第一次跑（OUT 是空的）就静默生成 href="./"，整页无样式还看不出所以然。
const css = readdirSync(DIST).find((f) => f.endsWith('.css'))
if (!css) throw new Error(`${DIST} 下没有 .css，先跑 npx vite build`)
copyFileSync(`${DIST}${css}`, `${OUT}/${css}`)

const body = cases
  .map(([title, phases, running]) => `
    <section class="mb-14">
      <h2 class="mb-3 border-b border-gray-300 pb-2 text-sm font-semibold text-gray-500">
        ${title}
      </h2>
      <div class="border border-line bg-white">${renderToStaticMarkup(
        <WorkflowView phases={phases} running={running} />
      )}</div>
    </section>`)
  .join('')

writeFileSync(
  `${OUT}/view.html`,
  `<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>工作流视图 · 真实数据</title>
<link rel="stylesheet" href="./${css}">
<style>body{background:#f9fafb;padding:24px;max-width:900px;margin:0 auto;
font-family:ui-sans-serif,system-ui,"PingFang SC",sans-serif}</style>
</head><body>${body}</body></html>`,
  'utf8'
)
console.log(`\n已写出 ${OUT}/view.html（${body.length} 字节）已拷入 CSS: ${css}`)