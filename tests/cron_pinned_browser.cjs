const {chromium}=require('playwright');
const assert=require('node:assert/strict');

(async()=>{
  const [origin,token,conv,device]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    const page=await browser.newPage(device==='mobile'?{viewport:{width:390,height:844},isMobile:true,hasTouch:true,userAgent:'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148'}:{});
    const errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(origin);
    await page.fill('#pw',token); await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden && S.cronJobs.length===1);
    await page.evaluate(async conv=>{ await openConv(conv); openAgentPanelTab('tasks'); },conv);
    await page.locator('#apBody .aptask .more').click();
    await page.getByRole('button',{name:'稍后处理',exact:true}).click();
    await page.waitForFunction(()=>S.cronJobs[0].pinned===true);
    await page.evaluate(()=>{ S.agentPanelOn=false; syncAgentHeader(); S.sideTab='pinned'; renderConvs(); expandSidebarResponsive(); });
    const row=page.locator('#convBody .conv[data-conv="'+conv+'"]');
    await row.waitFor();
    assert.equal(await row.count(),1);
    if(device!=='mobile'){
      await row.hover();
      await row.locator('.more').click();
      await page.getByRole('button',{name:'取消稍后处理',exact:true}).waitFor();
      await page.evaluate(()=>closeConvMenu());
    }
    await page.reload();
    await page.waitForFunction(()=>!$('#app').hidden && S.cronJobs.length===1);
    assert.equal(await page.evaluate(()=>S.cronJobs[0].pinned),true);
    await page.evaluate(conv=>openConv(conv),conv);
    await page.waitForFunction(()=>!$('#convCronMore').hidden);
    if(await page.locator('#apClose').isVisible()) await page.locator('#apClose').click();
    await page.locator('#convCronMore').click();
    await page.getByRole('button',{name:'取消稍后处理',exact:true}).click();
    await page.waitForFunction(()=>S.cronJobs[0].pinned===false);
    await page.evaluate(()=>{ S.sideTab='pinned'; renderConvs(); });
    assert.equal(await row.count(),0);
    assert.equal(await page.evaluate(()=>S.cronJobs[0].enabled),true);
    // 请求失败必须报错并回滚，不能留下假成功的稍后状态。
    await page.route('**/conv/pin',route=>route.fulfill({status:500,contentType:'application/json',body:'{"error":"测试失败"}'}));
    const dialog=page.waitForEvent('dialog');
    const failed=page.evaluate(conv=>pinConv(conv,true),conv);
    const alert=await dialog; assert.match(alert.message(),/失败/); await alert.accept(); await failed;
    assert.equal(await page.evaluate(()=>S.cronJobs[0].pinned),false);
    assert.deepEqual(errors,[]);
    console.log('定时任务稍后处理：添加、刷新、取消及失败回滚通过');
  }finally{ await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1);});
