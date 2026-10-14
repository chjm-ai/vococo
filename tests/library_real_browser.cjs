// 不拦截请求：通过真实本地 WebAdapter 访问正式页面、接口并发送作品引用。
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
  const [origin,token,imageId,siteId,screenshot]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,
    ...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    const page=await browser.newPage(),errors=[],requests=[];
    page.on('pageerror',error=>errors.push(error.message));
    page.on('request',request=>requests.push(request.url()));
    await page.goto(origin);
    await page.fill('#pw',token);
    await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(()=>openLibrary());
    await page.waitForFunction(()=>!LIB.loading && LIB.items.length===2);
    assert.equal(await page.locator('.lib-card').count(),2);
    assert.equal(await page.locator('#libraryCount,#libraryHistory,#libraryView [data-lib-action="reset"],#libraryView [data-lib-action="refresh"],#libraryView [data-lib-action="history"]').count(),0);
    assert.equal(await page.locator('#libraryView h1').innerText(),'作品库');
    await page.waitForFunction(()=>$('#libraryWorks img')?.src.startsWith('blob:'));
    await page.selectOption('#libraryState','pinned');
    await page.waitForFunction(()=>!LIB.loading && LIB.filters.state==='pinned' && LIB.items.length===1);
    assert.equal(await page.locator('.lib-card').count(),1);
    await page.selectOption('#libraryState','visible');
    await page.waitForFunction(()=>!LIB.loading && LIB.items.length===2);
    await page.selectOption('#libraryKind','image');
    await page.waitForFunction(()=>!LIB.loading && LIB.items.length===1);
    assert.equal(await page.locator('.lib-card-open').getAttribute('data-id'),imageId);
    await page.selectOption('#libraryKind','website');
    await page.waitForFunction(()=>!LIB.loading && LIB.items.length===1);
    assert.equal(await page.locator('.lib-card-open').getAttribute('data-id'),siteId);
    await page.selectOption('#libraryKind','');
    await page.waitForFunction(()=>!LIB.loading && LIB.items.length===2);
    await page.locator(`[data-lib-action="detail"][data-id="${imageId}"]`).click();
    await page.waitForSelector('#libraryTitle');
    await page.fill('#libraryTitle','真实本地接口改名');
    await page.locator('[data-lib-action="save"]').click();
    await page.waitForFunction(()=>$('#libraryDetailTitle').textContent==='真实本地接口改名',null,{timeout:10000})
      .catch(async error=>{console.error(await page.locator('#libraryDetail').innerText());throw error;});
    await page.screenshot({path:screenshot,fullPage:true});
    await page.selectOption('#libraryTarget','main');
    await page.locator('[data-lib-action="reference"]').click();
    await page.waitForSelector('.library-ref-chip');
    await page.evaluate(()=>openLibrary());
    await page.evaluate(siteId=>openLibraryDetail(siteId),siteId);
    assert.equal(await page.evaluate(()=>libraryWebsiteUrl(LIB.selected.open_url || LIB.selected.url)),origin+'/pub/library-smoke/');
    const popupPromise=page.waitForEvent('popup');
    await page.locator('[data-lib-action="website"]').click();
    const popup=await popupPromise;
    await popup.waitForLoadState();
    assert.equal(popup.url(),origin+'/pub/library-smoke/');
    assert.equal(await popup.locator('h1').innerText(),'隔离测试网站');
    await popup.close();
    await page.selectOption('#libraryTarget','main');
    await page.locator('[data-lib-action="reference"]').click();
    await page.waitForFunction(()=>S.libraryRefs.length===2);
    const sendResponse=page.waitForResponse(response=>response.url().endsWith('/send'));
    await page.fill('#ta','真实接口作品引用冒烟');
    await page.evaluate(()=>send($('#ta').value));
    assert.equal((await sendResponse).status(),200);
    assert.ok(requests.some(url=>url.includes('/library.js?v=')));
    assert.ok(requests.some(url=>url.includes('/library.css?v=')));
    assert.ok(requests.some(url=>url.includes('/library/file?id=')));
    assert.ok(requests.filter(url=>url.includes('/library')).every(url=>!url.includes('token=')));
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true,screenshot,requests:requests.filter(url=>url.includes('/library'))}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
