// 回归：普通 research 题两个入口各提交一次 → 跳会话页 + /run 被调一次
// Playwright 不是项目依赖：全局装一份，或 PLAYWRIGHT_MODULE=<…>/node_modules/playwright/index.js 指向 npx 缓存里的那份
const pkg = (await import(process.env.PLAYWRIGHT_MODULE || 'playwright')).default
const { chromium } = pkg
const B='http://localhost:5181',R={}
const b=await chromium.launch({args:['--no-proxy-server']})
const pg=await b.newPage({viewport:{width:1440,height:900}})
let runs=0
await pg.route('**/api/sessions/*/run',(r)=>{runs++;r.fulfill({json:{ok:true}})})
// 首页入口
await pg.goto(B);await pg.waitForTimeout(1500)
await pg.locator('textarea').first().fill('量子计算最新进展')
await pg.locator('button:has-text("开始研究")').first().click()
await pg.waitForTimeout(2500)
R['home_nav']=pg.url().includes('/s/')
R['home_runs']=runs
// 对话框入口
await pg.goto(B);await pg.waitForTimeout(1500)
await pg.locator('button:has-text("新建研究")').first().click();await pg.waitForTimeout(600)
await pg.locator('[role="dialog"] textarea').first().fill('人形机器人量产瓶颈')
await pg.locator('[role="dialog"] button:has-text("开始研究")').click()
await pg.waitForTimeout(2500)
R['dialog_nav']=pg.url().includes('/s/')
R['dialog_runs_total']=runs
console.log(JSON.stringify(R))
await b.close()
