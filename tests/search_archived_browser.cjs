// 默认列表、关键词和清空走真实接口；延迟/失败请求仅用于验证边界状态。
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
  const [origin,token]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,
    ...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    const page=await browser.newPage(), errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(origin);
    await page.fill('#pw',token);
    await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden);
    await page.click('#searchBtn');
    await page.waitForFunction(()=>SM.results.length===2);
    assert.ok(await page.evaluate(()=>SM.results.every(r=>r.archived)));
    assert.match(await page.locator('#smList').innerText(),/最近归档测试/);
    assert.match(await page.locator('#smList').innerText(),/归档任务测试/);
    await page.fill('#smInput','关键词');
    await page.waitForFunction(()=>SM.results.length===1 && SM.results[0].conv==='active');
    await page.fill('#smInput','');
    await page.waitForFunction(()=>SM.results.length===2);
    await page.locator('#smList .smrow').filter({hasText:'最近归档测试'}).click();
    await page.waitForFunction(()=>S.conv==='archived');
    assert.equal(await page.locator('#convTitle').innerText(),'最近归档测试');
    await page.click('#searchBtn');
    await page.waitForFunction(()=>SM.results.length===2);
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('#searchModal').isVisible(),false);
    // 关闭重开时旧响应不能覆盖；输入防抖期间旧响应也不能覆盖。
    await page.evaluate(()=>{
      window.realSearchApi=api;
      window.pendingSearch=[];
      api=(url,opts)=>url.startsWith('/conv/search')
        ? new Promise(resolve=>pendingSearch.push(resolve)) : realSearchApi(url,opts);
      openSearchModal(); closeSearchModal(); openSearchModal();
      pendingSearch[1]({ok:true,json:async()=>({results:[]})});
    });
    await page.waitForFunction(()=>$('#smList').textContent.includes('暂无已归档'));
    await page.evaluate(()=>pendingSearch[0]({ok:true,json:async()=>({results:[{conv:'stale',title:'旧响应'}]})}));
    assert.equal(await page.locator('#smList .smrow').count(),0);
    await page.evaluate(()=>openSearchModal());
    await page.fill('#smInput','新词');
    await page.evaluate(()=>pendingSearch[2]({ok:true,json:async()=>({results:[{conv:'stale'}]})}));
    assert.equal(await page.evaluate(()=>SM.results.length),0);
    await page.waitForFunction(()=>pendingSearch.length===4);
    await page.evaluate(()=>pendingSearch[3]({ok:false}));
    await page.waitForFunction(()=>$('#smList').textContent.includes('加载失败'));
    assert.deepEqual(errors,[]);
    console.log('搜索默认归档：真实接口、清空、点击、键盘、空态、失败和旧响应隔离均通过');
  }finally{ await browser.close(); }
})().catch(e=>{console.error(e); process.exit(1);});
