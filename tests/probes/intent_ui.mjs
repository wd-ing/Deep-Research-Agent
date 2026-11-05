// 意图门前端验收（坑 98·阶段2）。用法：node tests/probes/intent_ui.mjs
// 前提：后端 8012 (RA_INTENT=1+临时库) + vite 5181 代理 8012。
// Playwright 不是项目依赖：全局装一份，或 PLAYWRIGHT_MODULE=<…>/node_modules/playwright/index.js 指向 npx 缓存里的那份
const pkg = (await import(process.env.PLAYWRIGHT_MODULE || 'playwright')).default
const { chromium } = pkg
const B = 'http://localhost:5181'
const S = '/tmp/ck8/shots'
const R = {}
const sessCount = async (pg) =>
  pg.evaluate(async () => (await (await fetch('/api/sessions')).json()).length)

const b = await chromium.launch({ args: ['--no-proxy-server'] })
const pg = await b.newPage({ viewport: { width: 1440, height: 900 } })
// 所有 /run 一律拦截——会话可以真建（查列表数），研究绝不许真跑
await pg.route('**/api/sessions/*/run', (r) => r.fulfill({ json: { ok: true } }))

// ① 首页输入卡：「你好」→ 回复卡 + 会话数不增
await pg.goto(B); await pg.waitForTimeout(1800)
const n0 = await sessCount(pg)
await pg.locator('textarea').first().fill('你好')
await pg.locator('button:has-text("开始研究")').first().click()
await pg.waitForTimeout(1500)
R['1a_home_card'] = await pg.locator('text=专做深度调研').count()
R['1a_chips'] = await pg.locator('[role="status"] button:has-text("固态电池")').count()
R['1a_sess_unchanged'] = (await sessCount(pg)) === n0
await pg.screenshot({ path: S + '/home_chitchat.png' })
// 改输入 → 卡消失（⑤）
await pg.locator('textarea').first().fill('固态电池产业化进展')
await pg.waitForTimeout(400)
R['5_dismiss_on_edit'] = (await pg.locator('text=专做深度调研').count()) === 0

// ① 新建对话框：「你是谁」→ 回复卡
await pg.locator('button:has-text("新建研究"), [aria-label*="新建"]').first().click()
await pg.waitForTimeout(700)
await pg.locator('[role="dialog"] textarea').first().fill('你是谁')
await pg.locator('[role="dialog"] button:has-text("开始研究")').click()
await pg.waitForTimeout(1500)
R['1b_dialog_card'] = await pg.locator('text=多轮检索').count()
R['1b_sess_unchanged'] = (await sessCount(pg)) === n0
await pg.screenshot({ path: S + '/dialog_about.png' })
await pg.keyboard.press('Escape'); await pg.waitForTimeout(400)

// ② 拦截 /api/intent → off_scope：chips 填入 + 「仍然提交」才建
await pg.route('**/api/intent', (r) => r.fulfill({ json: {
  intent: 'off_scope', source: 'llm',
  reply: '这个请求更像写作、翻译、编程或日常问答。',
  suggestions: ['固态电池产业化进展与主要技术路线', 'Rust 与 Go 在后端服务中的对比', 'GLP-1 减重药物的长期安全性证据'] } }))
await pg.goto(B); await pg.waitForTimeout(1500)
await pg.locator('textarea').first().fill('帮我写一首诗')
await pg.locator('button:has-text("开始研究")').first().click()
await pg.waitForTimeout(1200)
R['2_card'] = await pg.locator('[role="status"]').count()
// 先点 force（卡还在时）：创建会话
const n1 = await sessCount(pg)
await pg.locator('button:has-text("仍然作为调研提交")').click()
await pg.waitForTimeout(1500)
R['2_force_creates'] = (await sessCount(pg)) === n1 + 1
// chip 填入验证：重新触发卡片再点 chip
await pg.goto(B); await pg.waitForTimeout(1500)
await pg.locator('textarea').first().fill('再帮我润色一封邮件')
await pg.locator('button:has-text("开始研究")').first().click()
await pg.waitForTimeout(1200)
await pg.locator('[role="status"] button:has-text("Rust 与 Go")').click()
await pg.waitForTimeout(300)
R['2_chip_fills'] = (await pg.locator('textarea').first().inputValue()).includes('Rust 与 Go')
R['2_chip_closes'] = (await pg.locator('[role="status"]').count()) === 0

// ③ /api/intent → 500 → fail-open 直接建
await pg.route('**/api/intent', (r) => r.fulfill({ status: 500, body: 'x' }))
await pg.goto(B); await pg.waitForTimeout(1500)
const n2 = await sessCount(pg)
await pg.locator('textarea').first().fill('固态电池最新产业化进展')
await pg.locator('button:has-text("开始研究")').first().click()
await pg.waitForTimeout(1500)
R['3_500_creates'] = (await sessCount(pg)) === n2 + 1
R['3_no_card'] = (await pg.locator('text=专做深度调研').count()) === 0
// 断网(abort) → 同上
await pg.route('**/api/intent', (r) => r.abort())
await pg.goto(B); await pg.waitForTimeout(1500)
const n3 = await sessCount(pg)
await pg.locator('textarea').first().fill('固态电池技术路线分歧')
await pg.locator('button:has-text("开始研究")').first().click()
await pg.waitForTimeout(1500)
R['3_abort_creates'] = (await sessCount(pg)) === n3 + 1

// ④ 拦截 POST /api/sessions → 409 not_research
await pg.unroute('**/api/intent')
await pg.route('**/api/sessions', (r) => {
  if (r.request().method() === 'POST')
    return r.fulfill({ status: 409, json: { detail: { code: 'not_research',
      intent: 'chitchat', reply: '你好！我是 Deep Research Agent。',
      suggestions: ['示例一', '示例二', '示例三'] } } })
  return r.continue()
})
await pg.goto(B); await pg.waitForTimeout(1500)
await pg.locator('textarea').first().fill('讲个笑话吧')
await pg.locator('button:has-text("开始研究")').first().click()
await pg.waitForTimeout(1200)
R['4_409_card'] = await pg.locator('[role="status"]').count()
await pg.unroute('**/api/sessions')

// ⑥ 390px + 回复卡下无横向溢出
await pg.setViewportSize({ width: 390, height: 844 })
await pg.goto(B); await pg.waitForTimeout(1500)
await pg.locator('textarea').first().fill('你好')
await pg.locator('button:has-text("开始研究")').first().click()
await pg.waitForTimeout(1200)
R['6_scrollW'] = await pg.evaluate(() => document.documentElement.scrollWidth)
await pg.screenshot({ path: S + '/m390_card.png' })
console.log(JSON.stringify(R, null, 1))
await b.close()
