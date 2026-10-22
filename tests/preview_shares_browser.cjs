const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
(async()=>{
  const [origin,token,screenshots]=process.argv.slice(2),browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  const context=await browser.newContext({permissions:['clipboard-read','clipboard-write']});
  const page=await context.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  try{
    await page.goto(origin);await page.fill('#pw',token);await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(async()=>{await openConv('share-browser-test');$('#wrap').replaceChildren();closeSidebar();setSidebarCollapsed(true);});
    const source='<style>body{background:#f4f0e8;color:#29251f;padding:24px;font-family:sans-serif}h1{font-size:28px}.tile{padding:16px;background:white;border-radius:12px}</style><h1>跨设备静态快照</h1><div class="tile">只有持有链接的人可访问 · 7天有效</div>';
    await page.evaluate(source=>addBubble('ai','```html\n'+source+'\n```'),source);
    const card=page.locator('.chat-preview');
    await card.locator('iframe').waitFor();
    const before=await context.request.get(origin+'/api/preview-shares',{headers:{'X-Auth-Token':token}});
    assert.equal((await before.json()).shares.length,0,'渲染不应自动创建分享');
    // 首次失败必须关闭空白窗口并允许重试。
    await page.route('**/api/preview-shares',route=>route.request().method()==='POST'?route.fulfill({status:503,contentType:'application/json',body:'{"error":"测试失败"}'}):route.continue());
    await card.getByRole('button',{name:'在浏览器打开',exact:false}).click();
    await card.locator('.preview-share-note').filter({hasText:'打开失败'}).waitFor();
    await page.unroute('**/api/preview-shares');
    const popupPromise=page.waitForEvent('popup');
    await card.getByRole('button',{name:'在浏览器打开',exact:false}).click();
    const popup=await popupPromise;
    await popup.waitForURL('**/preview-share#*');
    await popup.frameLocator('iframe').getByRole('heading',{name:'跨设备静态快照'}).waitFor();
    assert.equal(await popup.evaluate(()=>opener),null);
    const shareURL=popup.url();
    await card.getByRole('button',{name:'复制分享链接',exact:false}).click();
    assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),shareURL);
    const stranger=await browser.newContext(),other=await stranger.newPage(),publicRequests=[];
    other.on('request',request=>publicRequests.push({url:request.url(),headers:request.headers()}));
    await other.goto(shareURL);
    await other.frameLocator('iframe').getByRole('heading',{name:'跨设备静态快照'}).waitFor();
    assert.equal(await other.evaluate(()=>localStorage.getItem('token')),null);
    assert(publicRequests.every(request=>!request.url.includes(shareURL.split('#')[1])&&!request.headers['x-auth-token']),'公开访问不能泄露能力密钥到请求URL或携带主站口令');
    await popup.screenshot({path:path.join(screenshots,'preview-share-standalone.png')});
    // 修改源文不改变既有快照。
    await page.evaluate(()=>{const card=document.querySelector('.chat-preview');card._preview.source='<h1>编辑后的内容</h1>';scheduleChatPreview(card);});
    await other.reload();await other.frameLocator('iframe').getByRole('heading',{name:'跨设备静态快照'}).waitFor();
    for(const width of [320,375,390,768,1280]){
      await page.setViewportSize({width,height:844});
      const rects=await card.evaluate(n=>{const bar=n.querySelector('.preview-toolbar'),r=bar.getBoundingClientRect();return {right:r.right,width:r.width,buttons:[...bar.querySelectorAll('button')].filter(b=>!b.hidden).map(b=>b.getBoundingClientRect().right)};});
      assert(rects.buttons.every(right=>right<=rects.right+1),'标题栏图标不应越界 '+JSON.stringify(rects));
      if(width===390)await card.screenshot({path:path.join(screenshots,'preview-share-mobile.png')});
    }
    await card.getByRole('button',{name:'管理与撤销限时分享'}).click();
    await card.getByRole('button',{name:'撤销',exact:true}).click();
    await other.reload();await other.getByText('分享不存在、已撤销或已过期').waitFor();
    await popup.getByText('分享不存在、已撤销或已过期').waitFor({timeout:15000});
    assert.equal(await popup.locator('iframe').count(),0,'已打开设备应在下次校验时移除内容');
    // 从另一卡片撤销必须同步清除原卡片缓存，并可再次创建。
    const original=page.locator('.chat-preview').first();
    let reopened=page.waitForEvent('popup');
    await original.getByRole('button',{name:'在浏览器打开',exact:false}).click();
    let reopenedPage=await reopened;await reopenedPage.waitForURL('**/preview-share#*');
    await page.evaluate(()=>addBubble('ai','```html\n<h1>另一张卡片</h1>\n```'));
    const manager=page.locator('.chat-preview').last();
    await manager.getByRole('button',{name:'管理与撤销限时分享'}).click();
    await manager.getByRole('button',{name:'撤销',exact:true}).click();
    await original.getByRole('button',{name:'在浏览器打开',exact:false}).waitFor();
    reopened=page.waitForEvent('popup');
    await original.getByRole('button',{name:'在浏览器打开',exact:false}).click();
    reopenedPage=await reopened;await reopenedPage.waitForURL('**/preview-share#*');
    await reopenedPage.frameLocator('iframe').getByRole('heading',{name:'编辑后的内容'}).waitFor();
    await original.getByRole('button',{name:'管理与撤销限时分享'}).click();
    await original.getByRole('button',{name:'撤销',exact:true}).click();
    await page.evaluate(()=>document.querySelectorAll('.chat-preview')[1].remove());
    // 重新加载主站后仍有管理入口，可撤销丢失了原始密钥的分享。
    const hostile=`<meta http-equiv="refresh" content="0;url=https://evil.example/refresh"><base href="https://evil.example"><script>fetch('https://evil.example/script');localStorage.setItem('stolen','yes')</script><style>@import 'https://evil.example/import';body{background-image:url(https://evil.example/css)}</style><img src="https://evil.example/img" onerror="alert(1)"><form action="/send"><button>发送</button></form><a href="https://evil.example/click" ping="https://evil.example/ping">导航链接</a><svg><a href="https://evil.example/svg"><text>SVG</text></a></svg><iframe src="/">bad</iframe><h1>安全隔离</h1>`;
    const created=await context.request.post(origin+'/api/preview-shares',{headers:{'X-Auth-Token':token},data:{html:hostile}});
    const hostileShare=await created.json(),blocked=[],dialogs=[];
    other.on('request',r=>{if(r.url().startsWith('https://evil.example'))blocked.push(r.url());});
    other.on('dialog',d=>{dialogs.push(d.message());d.dismiss();});
    await other.goto(origin+hostileShare.path);
    const safe=other.frameLocator('iframe');await safe.getByRole('heading',{name:'安全隔离'}).waitFor();
    await safe.getByText('导航链接').click();await other.waitForTimeout(500);
    assert.deepEqual(blocked,[]);assert.deepEqual(dialogs,[]);
    assert(other.url().includes('/preview-share#'));
    const frame=other.frames()[1];
    assert.equal(await frame.evaluate(()=>{try{return localStorage.length}catch(e){return e.name}}),'SecurityError');
    assert.equal(await frame.locator('script,meta[http-equiv="refresh"],form,[href],[src],iframe').count(),0);
    await other.screenshot({path:path.join(screenshots,'preview-share-security.png')});
    // 会话切换期间完成的请求不能打开旧内容或污染新会话。
    await page.evaluate(()=>{window._oldFetch=fetch;window.fetch=async(...args)=>{if(args[0]==='/api/preview-shares'&&args[1]?.method==='POST')await new Promise(r=>setTimeout(r,400));return window._oldFetch(...args)};});
    await card.getByRole('button',{name:'在浏览器打开',exact:false}).click();
    await page.evaluate(()=>{S.conv='another-session';$('#wrap').replaceChildren();addBubble('ai','新会话');});
    await page.waitForTimeout(700);
    assert.equal(await page.locator('.preview-share-note').count(),0);
    await page.evaluate(()=>{fetch=window._oldFetch;addBubble('ai','```html\n<h1>管理</h1>\n```');});
    await page.getByRole('button',{name:'管理与撤销限时分享'}).click();
    await page.waitForFunction(()=>document.querySelectorAll('.preview-share-note button').length===2);
    assert.equal(await page.getByRole('button',{name:'撤销',exact:true}).count(),2);
    const mobileContext=await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true,
      userAgent:'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1'});
    const mobile=await mobileContext.newPage();await mobile.goto(origin);await mobile.fill('#pw',token);await mobile.click('#loginBtn');
    await mobile.waitForFunction(()=>!$('#app').hidden);
    await mobile.evaluate(async source=>{await openConv('share-mobile');$('#wrap').replaceChildren();closeSidebar();setSidebarCollapsed(true);addBubble('ai','```html\n'+source+'\n```');},source);
    await mobile.locator('.chat-preview iframe').waitFor();
    for(const width of [320,375,390]){
      await mobile.setViewportSize({width,height:844});
      assert(await mobile.locator('.preview-toolbar').evaluate(n=>{const r=n.getBoundingClientRect();return [...n.children].every(c=>c.getBoundingClientRect().right<=r.right+1)}));
      await mobile.locator('.chat-preview').screenshot({path:path.join(screenshots,'preview-share-iphone-'+width+'.png')});
    }
    await mobileContext.close();
    assert.deepEqual(errors,[]);
    console.log('PASS: 真实页面打开/复制/撤销、独立无登录context、快照、手机宽度、恶意HTML、失败恢复与切会话隔离');
    await stranger.close();
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
