const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
(async()=>{
  const [origin,token,root,screenshots]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[],remote=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('https://local-blocked.invalid/**',r=>{remote.push(r.request().url());return r.fulfill({body:'unexpected'});});
  try{
    await page.goto(origin);await page.fill('#pw',token);await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(async()=>{await openConv('html-link-e2e');closeSidebar();});
    await page.locator('#wrap .doclink').first().click();
    await page.locator('#wrap .chat-preview iframe').waitFor();
    await page.reload();await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(()=>openConv('html-link-e2e'));
    assert.equal(await page.locator('#wrap .chat-preview').count(),0,'刷新只恢复历史链接，不自动读取本地文件');
    await page.locator('#wrap .doclink').first().click();
    await page.locator('#wrap .chat-preview iframe').waitFor();
    await page.evaluate(()=>clearWrap(S.conv));
    const render=async target=>page.evaluate(target=>{
      let bubble=document.querySelector('#local-link-test');
      if(!bubble){bubble=document.createElement('div');bubble.className='bubble';bubble.id='local-link-test';$('#wrap').append(bubble);}
      renderPreviewMarkdown(bubble,'[打开HTML Demo]('+target+')');
    },target);
    let requests=0;page.on('request',r=>{if(r.url().includes('/doc/preview?'))requests++;});
    const shares=()=>page.evaluate(async()=> (await (await api('/api/preview-shares')).json()).shares);
    for(const target of [root+'/page.html','page.html','./page.html','file://'+root+'/page.html']){
      await render(target);await page.locator('#local-link-test .doclink').click();
      const card=page.locator('#local-link-test .chat-preview');
      await card.locator('iframe').waitFor({timeout:5000});
      assert.equal(await card.locator('iframe').getAttribute('sandbox'),'');
      await card.frameLocator('iframe').getByRole('heading',{name:'本地 HTML 卡片'}).waitFor();
      assert.equal(await page.locator('#docPreview').isVisible(),false);
      assert.equal((await shares()).length,0,'仅预览不能创建公开分享');
      const frame=await (await card.locator('iframe').elementHandle()).contentFrame();
      await frame.evaluate(()=>scrollTo(0,600));
      const scroll=await frame.evaluate(()=>scrollY);assert.ok(scroll>0);
      const before=requests;
      await page.locator('#local-link-test .doclink').click();await render(target);
      await page.evaluate(target=>renderPreviewMarkdown($('#local-link-test'),'[打开HTML Demo]('+target+') 后续流式文字'),target);
      assert.equal(await frame.evaluate(()=>scrollY),scroll,'流式重绘不能重载iframe或丢失滚动位置');
      assert.match(await page.locator('#local-link-test').textContent(),/后续流式文字/);
      await page.waitForTimeout(80);
      assert.equal(requests,before,'重复点击/重绘不得重读文件');
      assert.equal(await card.count(),1);
    }
    await page.evaluate(()=>{navigator.clipboard.writeText=async value=>{window.copiedLocal=value;};navigator.clipboard.write=undefined;});
    await page.locator('#local-link-test').getByRole('button',{name:'复制URL（7天有效，持有链接即可访问）',exact:true}).click();
    await page.waitForFunction(()=>window.copiedLocal);
    assert.ok((await page.evaluate(()=>window.copiedLocal)).startsWith('https://vococo.chjm.cc/'));
    assert.equal((await shares()).length,1);
    await page.screenshot({path:path.join(screenshots,'local-html-link-card.png')});
    await page.evaluate(()=>renderPreviewMarkdown($('#local-link-test'),'[A](page.html) [B](./page.html)'));
    await page.locator('#local-link-test .doclink').nth(0).click();
    await page.locator('#local-link-test .doclink').nth(1).click();
    await page.waitForFunction(()=>document.querySelectorAll('#local-link-test iframe').length===2);
    await page.evaluate(()=>renderPreviewMarkdown($('#local-link-test'),'[C](c.html) [B](./page.html)'));
    assert.equal(await page.locator('#local-link-test .chat-preview').count(),1,'C不能继承A卡片，仅保留B');
    await page.locator('#local-link-test .doclink').nth(0).click();
    await page.locator('#local-link-test [data-local-html="c.html"]').frameLocator('iframe').getByRole('heading',{name:'C 的新内容'}).waitFor();
    // A 的响应在链接已变成 C 后返回，也不能覆盖 C 或污染仍保留的 B。
    let releaseOld,oldArrived;
    const oldGate=new Promise(resolve=>releaseOld=resolve),oldReady=new Promise(resolve=>oldArrived=resolve);
    const delayOld=async route=>{
      if(new URL(route.request().url()).searchParams.get('path')!=='page.html') return route.continue();
      const response=await route.fetch();oldArrived();await oldGate;await route.fulfill({response});
    };
    await page.route('**/doc/preview?**',delayOld);
    await page.evaluate(()=>renderPreviewMarkdown($('#local-link-test'),'[A](page.html) [B](./page.html)'));
    await page.locator('#local-link-test .doclink').nth(0).click();await oldReady;
    await page.evaluate(()=>renderPreviewMarkdown($('#local-link-test'),'[C](c.html) [B](./page.html)'));
    assert.equal(await page.locator('#local-link-test [data-local-html="page.html"]').count(),0,'在途A节点必须移除');
    await page.locator('#local-link-test .doclink').nth(0).click();
    await page.locator('#local-link-test [data-local-html="c.html"]').frameLocator('iframe').getByRole('heading',{name:'C 的新内容'}).waitFor();
    releaseOld();await page.waitForTimeout(150);await page.unroute('**/doc/preview?**',delayOld);
    assert.equal(await page.locator('#local-link-test .chat-preview').count(),2);
    assert.equal(await page.locator('#local-link-test [data-local-html="page.html"]').count(),0);
    assert.equal(await page.locator('#local-link-test [data-local-html="c.html"] code').textContent(),'<h1>C 的新内容</h1>');
    await page.evaluate(()=>renderPreviewMarkdown($('#local-link-test'),'[只保留B](./page.html)'));
    assert.equal(await page.locator('#local-link-test .doclink').count(),1);
    assert.equal((await page.locator('#local-link-test .doclink').textContent()).trim(),'只保留B');
    assert.equal(await page.locator('#local-link-test .chat-preview').count(),1,'删除链接应同时移除对应卡片');
    await render('missing.html');await page.locator('#local-link-test .doclink').click();
    await page.locator('#local-link-test').getByRole('button',{name:'重试',exact:true}).waitFor();
    assert.match(await page.locator('#local-link-test').textContent(),/不存在/);
    const count=requests;await page.locator('#local-link-test').getByRole('button',{name:'重试',exact:true}).click();
    await page.waitForTimeout(100);assert.equal(requests,count+1);
    for(const [target,message] of [['large.html','200KB'],['bad.html','UTF-8'],[path.dirname(root)+'/outside.html','无权']]){
      await render(target);await page.locator('#local-link-test .doclink').click();
      await page.locator('#local-link-test').getByRole('button',{name:'重试',exact:true}).waitFor();
      assert.ok((await page.locator('#local-link-test').textContent()).includes(message));
    }
    await page.screenshot({path:path.join(screenshots,'local-html-permission-error.png')});
    await render('notes.md');await page.locator('#local-link-test .doclink').click();
    await page.locator('#dpBody h1').getByText('普通文档').waitFor();
    await page.evaluate(()=>closeDocPreview());
    for(const [name,selector,text] of [['会议纪要 空格.md','#dpBody h1','会议纪要正文'],['signed.txt','#dpBody pre','署名逐字稿正文']]){
      await render('file://'+root+'/'+encodeURIComponent(name));
      await page.locator('#local-link-test .doclink').click();
      await page.locator(selector).getByText(text).waitFor();
      await page.evaluate(()=>closeDocPreview());
    }
    await render('https://example.com/page.html');
    assert.equal(await page.locator('#local-link-test .doclink').getAttribute('href'),'https://example.com/page.html');
    await page.evaluate(()=>{window.renderDocUrl=target=>{window.externalDocTarget=target;};});
    await page.locator('#local-link-test .doclink').click();
    assert.equal(await page.evaluate(()=>window.externalDocTarget),'https://example.com/page.html');
    await page.evaluate(()=>closeDocPreview());
    for(const target of ['file://remote/page.html','//remote/page.html','javascript:page.html']){
      await render(target);assert.equal(await page.locator('#local-link-test .doclink').count(),0);
    }
    // 延迟真实服务端响应，切会话后不能把旧内容写进新会话。
    let release;const gate=new Promise(resolve=>release=resolve);
    await page.route('**/doc/preview?**',async route=>{const response=await route.fetch();await gate;await route.fulfill({response});});
    await render('page.html');await page.locator('#local-link-test .doclink').click();
    await page.evaluate(()=>openConv('other-local-html'));release();await page.waitForTimeout(150);
    assert.equal(await page.locator('[data-local-html]').count(),0);
    assert.equal(await page.evaluate(()=>window.LOCAL_ATTACK),undefined);assert.deepEqual(remote,[]);assert.deepEqual(errors,[]);
    console.log('本地HTML绝对/相对/file链接→安全卡片→按需公网分享、重绘去重/错误重试/会话隔离：通过');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
