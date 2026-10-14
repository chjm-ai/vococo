// 不拦截请求：通过真实本地 WebAdapter 访问正式页面、接口并发送作品引用。
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
  const [origin,token,imageId,siteId,screenshot]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,
    ...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    const page=await browser.newPage(),errors=[],requests=[],transfers=[];
    page.on('response',response=>{ if(response.url().includes('/library/thumbnail?')) transfers.push({url:response.url(),bytes:Number(response.headers()['content-length'])}); });
    page.on('pageerror',error=>errors.push(error.message));
    page.on('request',request=>requests.push(request.url()));
    await page.goto(origin);
    await page.fill('#pw',token);
    await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(()=>openLibrary());
    await page.waitForFunction(()=>!LIB.loading && LIB.items.length===2);
    assert.equal(await page.locator('.lib-card').count(),2);
    assert.equal(await page.locator('.lib-card-actions,.lib-card-meta,.lib-card-copy p').count(),0);
    assert.equal(await page.locator('#libraryCount,#libraryHistory,#libraryView [data-lib-action="reset"],#libraryView [data-lib-action="refresh"],#libraryView [data-lib-action="history"]').count(),0);
    assert.equal(await page.locator('#libraryView h1').innerText(),'作品库');
    await page.waitForFunction(()=>$('#libraryWorks img')?.src.startsWith('blob:'));
    assert.ok(requests.some(url=>url.includes('/library/thumbnail?id=')),'列表使用真实后端缩略图');
    assert.equal(requests.filter(url=>/\/library\/(file|cover)\?/.test(url)).length,0,'打开详情前不读原件');
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
    const cards=await page.locator('#libraryWorks .lib-card-open').evaluateAll(buttons=>buttons.map(button=>({id:button.dataset.id,action:button.dataset.libAction})));
    assert.deepEqual(cards.map(card=>card.id).sort(),[imageId,siteId].sort(),'筛选恢复后 DOM 与列表数据一致');
    assert.deepEqual((await page.evaluate(()=>LIB.items.map(item=>item.id))).sort(),cards.map(card=>card.id).sort());
    assert.equal(cards.find(card=>card.id===imageId).action,'preview');
    assert.equal(cards.find(card=>card.id===siteId).action,'website');
    await page.locator(`.lib-card-open[data-lib-action="preview"][data-id="${imageId}"]`).click();
    await page.waitForFunction(()=>$('#libraryViewer').open && !$('#libraryViewer').classList.contains('is-loading'));
    assert.ok(await page.locator('#libraryViewer img').getAttribute('src'));
    assert.equal(await page.evaluate(()=>$('#libraryViewer').dataset.objectUrl),await page.locator('#libraryViewer img').getAttribute('src'),'预览显示原图而非只停在缩略图');
    await page.locator('[data-lib-action="viewer-close"]').click();
    assert.equal(await page.evaluate(()=>$('#libraryViewer').open),false);
    // 整卡已改为原图预览；详情管理继续通过现有入口函数验证。
    await page.evaluate(imageId=>openLibraryDetail(imageId),imageId);
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
    const [popup]=await Promise.all([
      page.waitForEvent('popup'),
      page.locator('#libraryDetail [data-lib-action="website"]').click(),
    ]);
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
    console.log(JSON.stringify({ok:true,screenshot,thumbnailTransfers:transfers,thumbnailBytes:transfers.reduce((sum,item)=>sum+item.bytes,0),requests:requests.filter(url=>url.includes('/library'))}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
