// 可选的本机原文件验收；所有外网请求都拦截，不上传文档或截图。
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
(async()=>{
  const [origin,token,output]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  await context.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());
    if(url.origin===origin) return route.continue();
    if(url.origin!=='https://vococo.chjm.cc') return route.abort();
    assert(['/preview-share','/preview-share/content'].includes(url.pathname));
    await route.fulfill({response:await context.request.fetch(origin+url.pathname,{
      method:request.method(),headers:{'Content-Type':'application/json'},data:request.postData()??undefined,
    })});
  });
  try{
    const page=await context.newPage();
    await page.goto(origin);await page.fill('#pw',token);await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(async()=>{await openConv('file-validation');closeSidebar();});
    const cards=page.locator('[data-html-preview]');
    const count=await cards.count();assert(count>0);
    for(let i=0;i<count;i++){
      const card=cards.nth(i);await card.locator('iframe').waitFor();
      const popupPromise=page.waitForEvent('popup');
      await card.getByRole('button',{name:'在浏览器打开',exact:false}).click();
      const popup=await popupPromise;await popup.waitForURL('**/preview-share#*');
      await popup.waitForFunction(()=>!!document.querySelector('iframe')?.srcdoc);
      for(const [kind,iframe] of [['card',card.locator('iframe')],['share',popup.locator('iframe')]]){
        const frame=await (await iframe.elementHandle()).contentFrame();
        await frame.waitForSelector('img,svg image');
        const metrics=await frame.evaluate(async()=>{
          const imgs=[...document.querySelectorAll('img')];
          await Promise.all(imgs.map(n=>n.decode()));
          const svg=[...document.querySelectorAll('svg image')];
          return {images:imgs.length,loaded:imgs.filter(n=>n.naturalWidth>0).length,
            svgImages:svg.length,shapes:document.querySelectorAll('svg rect,svg line,svg circle,svg polygon').length,
            text:[...document.querySelectorAll('svg text')].filter(n=>n.getBoundingClientRect().width>0).length};
        });
        assert(metrics.images+metrics.svgImages>0);
        assert.equal(metrics.images,metrics.loaded);
        const images=frame.locator('img,svg image');
        for(let j=0;j<await images.count();j++)
          await images.nth(j).screenshot({path:path.join(output,`file-${i}-${kind}-image-${j}.png`)});
        if(metrics.svgImages){assert(metrics.shapes>0);assert(metrics.text>0);}
        console.log(`本机文件 ${i+1} ${kind}: ${JSON.stringify(metrics)}`);
      }
      await popup.close();
    }
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
