const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
(async()=>{
  const [origin,token,screenshots]=process.argv.slice(2),browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  const publicOrigin='https://share.example.com',localhostOrigin=origin.replace('127.0.0.1','localhost');
  // 固定域名只代理到隔离服务，所有其他外网请求一律阻断，绝不触达生产。
  const isolateNetwork=async context=>context.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());
    if([origin,localhostOrigin].includes(url.origin))return route.continue();
    if(url.origin!==publicOrigin)return route.abort();
    assert(['/preview-share','/preview-share/content'].includes(url.pathname));
    assert(!request.headers()['x-auth-token']&&!request.headers().cookie,'公开分享不能携带主站凭据');
    const response=await context.request.fetch(origin+url.pathname+url.search,{
      method:request.method(),headers:{'Content-Type':'application/json'},
      data:request.postData()??undefined,maxRedirects:0,
    });
    await route.fulfill({response});
  });
  const context=await browser.newContext({permissions:['clipboard-read','clipboard-write']});
  await isolateNetwork(context);
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
    await card.getByRole('button',{name:'复制URL',exact:false}).click();
    await card.getByText('复制URL失败：测试失败', {exact:false}).waitFor();
    await page.evaluate(()=>{window.deniedWrite=navigator.clipboard.write;navigator.clipboard.write=()=>Promise.reject(new Error('先拒绝剪贴板'));});
    await card.getByRole('button',{name:'复制URL',exact:false}).click();
    await page.waitForFunction(()=>!document.querySelector('.chat-preview')._preview.shareBusy);
    await page.evaluate(()=>navigator.clipboard.write=window.deniedWrite);
    assert.deepEqual(errors,[],'剪贴板先拒绝、接口后失败也不能产生未捕获异常');
    await page.unroute('**/api/preview-shares');
    assert.equal(await card.locator('.preview-toolbar button').count(),3);
    assert.equal(await card.getByRole('button',{name:/管理|撤销|删除|复制源码|复制分享链接/}).count(),0);
    const copy=card.getByRole('button',{name:'复制URL',exact:false});
    const shares=async()=>(await (await context.request.get(origin+'/api/preview-shares',{headers:{'X-Auth-Token':token}})).json()).shares;
    // 不先打开页面，复制即可创建；复制动作不能调用 window.open。
    await page.evaluate(()=>{window.realOpen=window.open;window.open=()=>{throw new Error('复制不应开窗')};});
    await copy.click();await card.getByText('已复制URL；', {exact:false}).waitFor();
    const shareURL=await page.evaluate(()=>navigator.clipboard.readText());
    assert.match(shareURL,/^https:\/\/share\.example\.com\/preview-share#[\w-]{43}$/);
    assert.equal(shareURL,publicOrigin+await card.evaluate(n=>n._preview.share.path),'复制必须完整保留密钥 fragment');
    assert.equal((await shares()).length,1);
    await copy.click();await copy.waitFor({state:'visible'});
    await page.waitForFunction(()=>!document.querySelector('.chat-preview')._preview.shareBusy);
    assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),shareURL);
    assert.equal((await shares()).length,1,'重复复制应复用');
    await page.evaluate(()=>window.open=window.realOpen);
    const popupPromise=page.waitForEvent('popup');
    await card.getByRole('button',{name:'在浏览器打开',exact:false}).click();
    const popup=await popupPromise;
    await popup.waitForURL('**/preview-share#*');
    await popup.frameLocator('iframe').getByRole('heading',{name:'跨设备静态快照'}).waitFor();
    assert.equal(await popup.evaluate(()=>opener),null);
    assert.equal(popup.url(),shareURL,'打开与复制使用相同快照');
    assert.equal((await shares()).length,1);
    const stranger=await browser.newContext();
    await isolateNetwork(stranger);
    const other=await stranger.newPage(),publicRequests=[];
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
    // 内容更新自动创建新快照；同时点击复制和打开只能创建一次。
    await page.evaluate(()=>{window.clipboardCalls=0;window.originalWrite=navigator.clipboard.write.bind(navigator.clipboard);navigator.clipboard.write=(...args)=>{clipboardCalls++;return originalWrite(...args)};});
    let posts=0;
    await page.route('**/api/preview-shares',async route=>{
      if(route.request().method()==='POST'){posts++;await new Promise(r=>setTimeout(r,200));}
      await route.continue();
    });
    await page.evaluate(()=>{const buttons=document.querySelector('.chat-preview')._preview.shareButtons;buttons.copy.click();buttons.open.click();buttons.copy.click();});
    assert.equal(await page.evaluate(()=>clipboardCalls),1,'点击内立即调用剪贴板API，兼容WebKit点击授权');
    await page.waitForFunction(()=>!document.querySelector('.chat-preview')._preview.shareBusy);
    assert.equal(posts,1);assert.equal((await shares()).length,2);
    const updatedURL=await page.evaluate(()=>navigator.clipboard.readText());
    assert.notEqual(updatedURL,shareURL);
    await other.goto(updatedURL);await other.frameLocator('iframe').getByRole('heading',{name:'编辑后的内容'}).waitFor();
    await page.unroute('**/api/preview-shares');
    // 剪贴板拒绝要明确反馈，重试复用已创建快照。
    await page.evaluate(()=>{window.realWrite=navigator.clipboard.write;navigator.clipboard.write=()=>Promise.reject(new Error('剪贴板权限被拒绝'));});
    await copy.click();await card.getByText('复制URL失败：剪贴板权限被拒绝', {exact:false}).waitFor();
    await page.evaluate(()=>navigator.clipboard.write=window.realWrite);
    await copy.click();await page.waitForFunction(()=>!document.querySelector('.chat-preview')._preview.shareBusy);
    assert.equal((await shares()).length,2);
    // 前端无撤销入口，鉴权 API 撤销依旧即时生效。
    const revoke=async id=>{const response=await context.request.delete(origin+'/api/preview-shares/'+id,{headers:{'X-Auth-Token':token}});assert.equal(response.status(),200);};
    await revoke((await page.evaluate(()=>document.querySelector('.chat-preview')._preview.share)).id);
    await other.reload();await other.getByText('分享不存在、已撤销或已过期').waitFor();
    await copy.click();await page.waitForFunction(()=>!document.querySelector('.chat-preview')._preview.shareBusy);
    assert.notEqual(await page.evaluate(()=>navigator.clipboard.readText()),updatedURL,'API撤销后缓存必须失效');
    // 原始设备也必须在轮询后移除已撤销快照。
    for(const share of await shares()) await revoke(share.id);
    await popup.getByText('分享不存在、已撤销或已过期').waitFor({timeout:15000});
    assert.equal(await popup.locator('iframe').count(),0);
    // 恶意快照继续通过后端安全与撤销链路验证。
    const hostile=`<meta http-equiv="refresh" content="0;url=https://evil.example/refresh"><base href="https://evil.example"><script>fetch('https://evil.example/script');localStorage.setItem('stolen','yes')</script><style>@import 'https://evil.example/import';body{background-image:url(https://evil.example/css)}</style><img src="https://evil.example/img" onerror="alert(1)"><form action="/send"><button>发送</button></form><a href="https://evil.example/click" ping="https://evil.example/ping">导航链接</a><svg><a href="https://evil.example/svg"><text>SVG</text></a></svg><iframe src="/">bad</iframe><h1>安全隔离</h1>`;
    const created=await context.request.post(origin+'/api/preview-shares',{headers:{'X-Auth-Token':token},data:{html:hostile}});
    const hostileShare=await created.json(),attempted=[],dialogs=[],network=[],failed=[],finished=[],responses=[],csp=[];
    // request 也可能上报被 CSP 拦截的尝试；网络路由才是外连检测信号。
    await stranger.route('https://evil.example/**',route=>{network.push(route.request().url());return route.fulfill({body:'blocked test endpoint'});});
    // 用无 CSP 的页面验证路由探针有效；fulfill 保证测试本身不访问外网。
    const probe=await stranger.newPage();
    await probe.goto('https://evil.example/route-probe');
    assert.deepEqual(network,['https://evil.example/route-probe']);
    await probe.close();network.length=0;
    other.on('request',r=>{if(r.url().startsWith('https://evil.example'))attempted.push(r.url());});
    other.on('requestfailed',r=>{if(r.url().startsWith('https://evil.example'))failed.push({url:r.url(),error:r.failure()?.errorText});});
    other.on('requestfinished',r=>{if(r.url().startsWith('https://evil.example'))finished.push(r.url());});
    other.on('response',r=>{if(r.url().startsWith('https://evil.example'))responses.push(r.url());});
    other.on('console',message=>{if(/Content Security Policy/i.test(message.text()))csp.push(message.text());});
    other.on('dialog',d=>{dialogs.push(d.message());d.dismiss();});
    await other.goto(origin+hostileShare.path);
    const safe=other.frameLocator('iframe');await safe.getByRole('heading',{name:'安全隔离'}).waitFor();
    await safe.getByText('导航链接').click();await other.waitForTimeout(500);
    assert.deepEqual(network,[],'恶意资源不能到达网络路由');
    assert.deepEqual(responses,[],'恶意资源不能收到响应');
    assert.deepEqual(finished,[],'恶意资源不能成功完成请求');
    // Chromium 可能不发出 requestfailed；一旦上报，必须确认是 CSP 而非网络故障。
    assert(failed.every(request=>request.error==='csp'),'请求失败原因必须是 CSP：'+JSON.stringify(failed));
    for(const resource of ['import','css']){
      assert(csp.some(message=>message.includes('https://evil.example/'+resource)&&message.includes('has been blocked')),'缺少资源的 CSP 拦截证据：'+resource);
    }
    console.log('CSP evidence: '+JSON.stringify({attempted,network,failed,finished,responses,csp}));
    assert.deepEqual(dialogs,[]);
    assert(other.url().includes('/preview-share#'));
    const frame=other.frames()[1];
    assert.equal(await frame.evaluate(()=>{try{return localStorage.length}catch(e){return e.name}}),'SecurityError');
    assert.equal(await frame.locator('script,meta[http-equiv="refresh"],form,[href],[src],iframe').count(),0);
    await other.screenshot({path:path.join(screenshots,'preview-share-security.png')});
    // 会话切换期间完成的请求不能打开旧内容或污染新会话。
    await page.evaluate(()=>{window._oldFetch=fetch;window.fetch=async(...args)=>{if(args[0]==='/api/preview-shares'&&args[1]?.method==='POST')await new Promise(r=>setTimeout(r,400));return window._oldFetch(...args)};});
    const pendingPopup=page.waitForEvent('popup');
    await card.getByRole('button',{name:'在浏览器打开',exact:false}).click();
    const abandoned=await pendingPopup;
    await page.evaluate(()=>{S.conv='another-session';$('#wrap').replaceChildren();addBubble('ai','新会话');});
    await page.waitForTimeout(700);
    assert.equal(await page.locator('.preview-share-note').count(),0);
    assert.equal(abandoned.isClosed(),true,'切会话应关闭此次请求的空白窗口');
    assert.equal(popup.isClosed(),false,'已有独立页面不受切会话影响');
    await page.evaluate(()=>{fetch=window._oldFetch;addBubble('ai','```html\n<h1>新会话</h1>\n```');});
    assert.equal(await page.getByRole('button',{name:/管理|撤销|删除/}).count(),0);
    const mobileContext=await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true,
      permissions:['clipboard-read','clipboard-write'],
      userAgent:'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1'});
    await isolateNetwork(mobileContext);
    const mobile=await mobileContext.newPage();await mobile.goto(localhostOrigin);await mobile.fill('#pw',token);await mobile.click('#loginBtn');
    await mobile.waitForFunction(()=>!$('#app').hidden);
    await mobile.evaluate(async source=>{await openConv('share-mobile');$('#wrap').replaceChildren();closeSidebar();setSidebarCollapsed(true);addBubble('ai','```html\n'+source+'\n```');},source);
    await mobile.locator('.chat-preview iframe').waitFor();
    await mobile.getByRole('button',{name:'复制URL',exact:false}).click();
    await mobile.getByText('已复制URL；',{exact:false}).waitFor();
    const localhostURL=await mobile.evaluate(()=>navigator.clipboard.readText());
    assert.match(localhostURL,/^https:\/\/share\.example\.com\/preview-share#[\w-]{43}$/);
    assert.equal(localhostURL,publicOrigin+await mobile.locator('.chat-preview').evaluate(n=>n._preview.share.path),'localhost 复制也必须保留完整密钥并使用固定域名');
    await other.goto(localhostURL);
    await other.frameLocator('iframe').getByRole('heading',{name:'跨设备静态快照'}).waitFor();
    for(const width of [320,375,390]){
      await mobile.setViewportSize({width,height:844});
      assert(await mobile.locator('.preview-toolbar').evaluate(n=>{const r=n.getBoundingClientRect();return [...n.children].every(c=>c.getBoundingClientRect().right<=r.right+1)}));
      await mobile.locator('.chat-preview').screenshot({path:path.join(screenshots,'preview-share-iphone-'+width+'.png')});
    }
    await mobileContext.close();
    assert.deepEqual(errors,[]);
    console.log('PASS: 复制URL创建/复用/更新、并发防重、API撤销、独立无登录context、手机宽度、恶意HTML、失败恢复与切会话隔离');
    await stranger.close();
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
