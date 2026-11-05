// 坑99 段4 前端验收：RA_AUTH=1 真后端 8012 + vite 5181。
import pkg from '/Users/wd/.npm/_npx/e41f203b7505f1fb/node_modules/playwright/index.js'
const { chromium } = pkg
const B='http://localhost:5181', S='/tmp/ck10/shots', R={}
const TOK={amy:'6K71IsVmrpyh4Qch5lm0BDt-_U5PJtrZRo8ISnBrOhA',
           zed:'opHwXnt83O6pBQ78zT9pdZh4vydE96zoMsrbgcjgHKo',
           root:'m9qvRXVEy7d3g6eMkvE_42fyq2N2HbNKqiVwF22seSo'}
const b=await chromium.launch({args:['--no-proxy-server']})

const login=async(ctx,who)=>{
  const pg=await ctx.newPage()
  await pg.goto(`${B}/api/auth/invite?t=${TOK[who]}`,{waitUntil:'domcontentloaded'})
  await pg.waitForTimeout(2000)
  return pg
}
const sessCount=async(pg)=>pg.evaluate(
  async()=>(await (await fetch('/api/sessions')).json()).length)

// ① 未登录→整页空态 + 请求不增长
let pg=await b.newPage({viewport:{width:1440,height:900}})
let reqs=0
pg.on('request',r=>{if(r.url().includes('/api/'))reqs++})
await pg.goto(B);await pg.waitForTimeout(1500)
R['1_empty']=await pg.locator('text=需要邀请链接').count()
const n1=reqs;await pg.waitForTimeout(3000)
R['1_req_stopped']=reqs-n1===0

// ② amy 登录→首页+侧栏 amy+余量
const cxA=await b.newContext({viewport:{width:1440,height:900}})
pg=await login(cxA,'amy')
await pg.waitForTimeout(1200)
R['2_amy']=await pg.locator('text=Amy').count()
R['2_quota']=await pg.locator('text=今日 标准').count()
await pg.screenshot({path:S+'/amy_home.png'})

// ③ amy 建会话→root 列表看不到（admin 只见自己+NULL）
await pg.locator('textarea').first().waitFor({timeout:15000})
await pg.locator('textarea').first().fill('amy的私密研究题xyz')
await pg.locator('button:has-text("开始研究")').first().click()
await pg.waitForTimeout(2500)
const cxR=await b.newContext({viewport:{width:1440,height:900}})
const pgR=await login(cxR,'root')
await pgR.waitForTimeout(1200)
R['3_root_blind']=!(await pgR.locator('text=amy的私密研究题xyz').count())
R['3_amy_sees']=await pg.locator('text=amy的私密研究题xyz').count()>=0
await pgR.close()

// ④ zed（额度0/0）→回复卡无chips/force，行数不增
const cxZ=await b.newContext({viewport:{width:1440,height:900}})
const pgZ=await login(cxZ,'zed')
await pgZ.waitForTimeout(1200)
const n0=await sessCount(pgZ)
await pgZ.locator('textarea').first().waitFor({timeout:15000})
await pgZ.locator('textarea').first().fill('量子计算')
await pgZ.locator('button:has-text("开始研究")').first().click()
await pgZ.waitForTimeout(1500)
R['4_quota_card']=await pgZ.locator('text=额度已用完').count()
R['4_noforce']=await pgZ.locator('button:has-text("仍然作为调研提交")').count()===0
R['4_nochips']=await pgZ.locator('[role="status"] button:has-text("固态电池")').count()===0
R['4_count_static']=(await sessCount(pgZ))===n0
await pgZ.screenshot({path:S+'/zed_quota.png'})

// ⑤ /run 429 busy→人话 notify
await pg.route('**/api/sessions/*/run',r=>r.fulfill({status:429,
  json:{detail:{code:'busy'}}}))
await pg.locator('textarea').first().waitFor({timeout:15000})
await pg.locator('textarea').first().fill('新能源政策分析')
await pg.locator('button:has-text("开始研究")').first().click()
await pg.waitForTimeout(2000)
R['5_notify']=await pg.locator('text=当前研究任务较多').count()+
             await pg.locator('text=任务较多').count()
await pg.unroute('**/api/sessions/*/run')

// ⑥ amy 设置页无编辑区 / root 有
await pg.goto(`${B}/#/settings`);await pg.waitForTimeout(1200)
R['6_amy_403']=await pg.locator('text=模型配置仅管理员可见').count()
const pgR2=await cxR.newPage()
await pgR2.goto(`${B}/#/settings`);await pgR2.waitForTimeout(1500)
R['6_root_editor']=await pgR2.locator('textarea').count()
await pgR2.close()

// ⑦ amy 退出→空态+刷新仍空态
await pg.goto(B);await pg.waitForTimeout(1200)
await pg.locator('button:has-text("退出")').click()
await pg.waitForTimeout(1200)
R['7_logout']=await pg.locator('text=需要邀请链接').count()
await pg.reload();await pg.waitForTimeout(1200)
R['7_persist']=await pg.locator('text=需要邀请链接').count()

// ⑧ 截图 + 390px 溢出
await pg.setViewportSize({width:390,height:844})
await pgZ.bringToFront?.()
await pgZ.screenshot({path:S+'/m390.png'})
R['8_scrollW']=await pg.evaluate(()=>document.documentElement.scrollWidth)
console.log(JSON.stringify(R,null,1))
await b.close()
