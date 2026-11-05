// Playwright 不是项目依赖：全局装一份，或 PLAYWRIGHT_MODULE=<…>/node_modules/playwright/index.js 指向 npx 缓存里的那份
const pkg = (await import(process.env.PLAYWRIGHT_MODULE || 'playwright')).default
const { chromium } = pkg
const B='http://127.0.0.1:8014', S='/tmp/ck10/shots', R={}, errs=[], bad=[]
const b=await chromium.launch({args:['--no-proxy-server','--autoplay-policy=no-user-gesture-required']})
const pg=await b.newPage({viewport:{width:1440,height:900}})
pg.on('console',m=>{if(m.type()==='error')errs.push(m.text())})
pg.on('response',r=>{if(r.status()>=400)bad.push(r.url().slice(0,70))})
await pg.goto(B+'/index.html',{waitUntil:'networkidle'})
// ① 无 soon/即将
const body=await pg.locator('body').innerText()
const html=await pg.content()
R['1_soon_free']=!/soon|即将|Coming soon/i.test(body)&&!/soon-tag|is-soon/.test(html)
// ② data-link href
const links=await pg.evaluate(()=>[...document.querySelectorAll('a[data-link]')].map(a=>({k:a.dataset.link,h:a.getAttribute('href')})))
R['2_links']=links.every(l=>l.h&&l.h!=='#')&&links.some(l=>l.k==='film')
// ③ 三按钮顺序 + 点宣传片跳转
const btns=await pg.evaluate(()=>[...document.querySelectorAll('.hero-cta .btn')].slice(0,3).map(a=>a.textContent.trim().slice(0,10)))
R['3_order']=btns.join('|')
const filmBtn=pg.locator('.hero-cta a[data-link="film"]').first()
await filmBtn.click();await pg.waitForLoadState('domcontentloaded')
R['3_film_nav']=pg.url().endsWith('film.html')
// ④ 视频就绪+播放
const v=pg.locator('video')
await v.waitFor({state:'visible'})
const rs=await v.evaluate(e=>new Promise(res=>{const f=()=>e.readyState>=2?res(e.readyState):(e.oncanplay=()=>res(e.readyState));setTimeout(()=>res(e.readyState),8000)}))
R['4_ready']=rs
await v.evaluate(e=>{e.muted=true;return e.play()})
await pg.waitForTimeout(1500)
R['4_playing']=(await v.evaluate(e=>e.currentTime))>0.1
// ?lang=en
await pg.goto(B+'/film.html?lang=en');await pg.waitForTimeout(1200)
const src=await pg.locator('video').getAttribute('src')
R['4_en_src']=src?.includes('film_en')||''
R['4_en_title']=(await pg.locator('h1').textContent()).slice(0,25)
// 中/英切换按钮
await pg.locator('[data-set-lang="zh"]').click()
R['4_switch_back']=(await pg.locator('video').getAttribute('src'))?.includes('film_zh')||''
// ⑤ console/network
R['5_console_err']=errs.length
R['5_bad_req']=bad.length?bad:'none'
// ⑥ 截图+溢出
await pg.setViewportSize({width:390,height:844})
await pg.goto(B+'/index.html');await pg.waitForTimeout(1200)
await pg.screenshot({path:S+'/home_390.png'})
R['6_m390']=await pg.evaluate(()=>document.documentElement.scrollWidth)
await pg.goto(B+'/film.html');await pg.waitForTimeout(1200)
await pg.screenshot({path:S+'/film_390.png'})
R['6_film390']=await pg.evaluate(()=>document.documentElement.scrollWidth)
await pg.setViewportSize({width:1440,height:900})
await pg.goto(B+'/index.html');await pg.waitForTimeout(800)
await pg.screenshot({path:S+'/home_1440.png'})
await pg.goto(B+'/film.html');await pg.waitForTimeout(1000)
await pg.screenshot({path:S+'/film_1440.png'})
// ⑦ en 按钮文案
await pg.goto(B+'/index.html?lang=en');await pg.waitForTimeout(1000)
const eb=await pg.evaluate(()=>[...document.querySelectorAll('.hero-cta .btn')].slice(0,3).map(a=>a.textContent.trim()))
R['7_en_btns']=eb.join('|')
console.log(JSON.stringify(R,null,1))
await b.close()
