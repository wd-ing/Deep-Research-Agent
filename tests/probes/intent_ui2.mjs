// Playwright 不是项目依赖：全局装一份，或 PLAYWRIGHT_MODULE=<…>/node_modules/playwright/index.js 指向 npx 缓存里的那份
const pkg = (await import(process.env.PLAYWRIGHT_MODULE || 'playwright')).default
const { chromium } = pkg
const B='http://localhost:5181',S='/tmp/ck8/shots',R={}
const sessCount=async(pg)=>pg.evaluate(async()=>(await (await fetch('/api/sessions')).json()).length)
const b=await chromium.launch({args:['--no-proxy-server']})
const pg=await b.newPage({viewport:{width:1440,height:900}})
await pg.route('**/api/sessions/*/run',(r)=>r.fulfill({json:{ok:true}}))
// 记录 create 请求/响应
await pg.route('**/api/sessions',async r=>{
  if(r.request().method()!=='POST')return r.continue()
  const resp=await r.fetch()
  const body=await resp.text()
  console.log('CREATE req:',r.request().postData(),'→',resp.status(),body.slice(0,160))
  await r.fulfill({response:resp,body})
})
// ② off_scope→卡→force→应建
await pg.route('**/api/intent',(r)=>r.fulfill({json:{intent:'off_scope',source:'llm',
  reply:'这个请求更像写作、翻译、编程或日常问答。',
  suggestions:['固态电池产业化进展与主要技术路线','Rust 与 Go 在后端服务中的对比','GLP-1 减重药物的长期安全性证据']}}))
await pg.goto(B);await pg.waitForTimeout(1500)
await pg.locator('textarea').first().fill('帮我写一首诗')
await pg.locator('button:has-text("开始研究")').first().click();await pg.waitForTimeout(1200)
R['2_card']=await pg.locator('[role="status"]').count()
const n1=await sessCount(pg)
await pg.locator('button:has-text("仍然作为调研提交")').click();await pg.waitForTimeout(1800)
R['2_force_creates']=(await sessCount(pg))===n1+1
// ③ intent→500 fail-open，用真 research 题（后端自判 research 才建，烧 1 次 mini 可接受）
await pg.route('**/api/intent',(r)=>r.fulfill({status:500,body:'x'}))
await pg.goto(B);await pg.waitForTimeout(1500)
const n2=await sessCount(pg)
await pg.locator('textarea').first().fill('固态电池最新产业化进展')
await pg.locator('button:has-text("开始研究")').first().click();await pg.waitForTimeout(2500)
R['3_500_creates']=(await sessCount(pg))===n2+1
R['3_no_card']=(await pg.locator('[role="status"]').count())===0
// abort 同上
await pg.route('**/api/intent',(r)=>r.abort())
await pg.goto(B);await pg.waitForTimeout(1500)
const n3=await sessCount(pg)
await pg.locator('textarea').first().fill('固态电池技术路线分歧')
await pg.locator('button:has-text("开始研究")').first().click();await pg.waitForTimeout(2500)
R['3_abort_creates']=(await sessCount(pg))===n3+1
console.log(JSON.stringify(R,null,1))
await b.close()
