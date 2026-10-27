// 真实页面验证用户消息底栏,不替换消息渲染或剪贴板实现。
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
  const [origin,token]=process.argv.slice(2);
  console.error('启动浏览器');
  const browser=await chromium.launch({headless:true,timeout:15000,
    ...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    const context=await browser.newContext({permissions:['clipboard-read','clipboard-write']});
    const page=await context.newPage();
    page.setDefaultTimeout(10000);
    console.error('打开页面');
    await page.goto(origin);
    console.error('登录');
    await page.fill('#pw',token); await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(async()=>{await openConv('main');});
    for(const width of [320,375,390,768,1280]){
      console.error('验证屏宽',width);
      await page.setViewportSize({width,height:900});
      await page.evaluate(()=>{
        $('#wrap').replaceChildren(buildTurnBlock({id:123,user:'已发送内容\n第二行',assistant:'回复',ts:1700000000},'main',true));
      });
      await page.mouse.move(0,0);
      const row=page.locator('#wrap .row.me'), foot=row.locator('.userfoot');
      await page.waitForTimeout(200);
      assert.equal(await foot.evaluate(n=>getComputedStyle(n).opacity),'0');
      assert.equal(await foot.locator('.tftime').innerText(),await page.evaluate(()=>fmtTime(1700000000)));
      assert.equal(await foot.locator('button').count(),2,'用户消息只显示复制和展开,不显示重新生成');
      const bubble=row.locator('.bubble');
      const message=row.locator('.user-message-text');
      assert.equal(await message.evaluate(n=>getComputedStyle(n).webkitLineClamp),'2','用户文字默认限制为两行');
      await bubble.hover(); await page.waitForTimeout(200);
      assert.equal(await foot.evaluate(n=>getComputedStyle(n).opacity),'1');
      const before=await row.boundingBox();
      await foot.locator('button[aria-label="复制"]').click();
      assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),'已发送内容\n第二行');
      const toggle=foot.locator('.user-message-toggle');
      await toggle.click();
      assert.equal(await message.evaluate(n=>n.classList.contains('user-message-collapsed')),false,'展开只移除文字折叠');
      assert.equal(await toggle.getAttribute('aria-expanded'),'true');
      await toggle.click();
      assert.equal(await message.evaluate(n=>n.classList.contains('user-message-collapsed')),true,'再次点击恢复两行折叠');
      await page.locator('#msg').focus().catch(()=>{});
      await page.mouse.move(0,0); await page.waitForTimeout(200);
      assert.deepEqual(await row.boundingBox(),before,'底栏显隐不应推动消息布局');
      assert.ok(await bubble.evaluate(n=>n.getBoundingClientRect().right<=innerWidth),'窄屏气泡不能越界');
      await foot.locator('.user-message-toggle').focus(); await page.waitForTimeout(200);
      assert.equal(await foot.evaluate(n=>getComputedStyle(n).opacity),'1','键盘聚焦时可见');
      const aiRow=page.locator('#wrap .row.ai'), aiFoot=aiRow.locator('.turnfoot');
      assert.equal(await aiFoot.evaluate(n=>getComputedStyle(n).opacity),'0','未悬浮的 AI 底栏隐藏');
      await aiRow.hover();
      await page.waitForFunction(n=>getComputedStyle(n).opacity==='1',await aiFoot.elementHandle());
      assert.equal(await aiFoot.evaluate(n=>getComputedStyle(n).opacity),'1','悬浮 AI 回复时底栏显示');
      assert.equal(await aiFoot.locator('button').count(),2,'保留复制和重新生成');
      await aiFoot.locator('button[aria-label="复制"]').click();
      assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),'回复');
      await bubble.click(); await page.mouse.move(0,0);
      await page.waitForFunction(n=>getComputedStyle(n).opacity==='0',await aiFoot.elementHandle());
      assert.equal(await aiFoot.evaluate(n=>getComputedStyle(n).opacity),'0','离开 AI 回复后底栏隐藏');
      await aiFoot.locator('button').first().focus();
      await page.waitForFunction(()=>getComputedStyle(document.querySelector('#wrap .row.ai .turnfoot')).opacity==='1');
      assert.equal(await aiRow.evaluate(n=>n.matches(':focus-within')),true,'AI 底栏支持键盘聚焦');
    }
    await page.setViewportSize({width:320,height:900});
    await page.evaluate(()=>{
      const {row}=buildBubble('me','很长的消息'.repeat(100),['data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs='],[{url:'data:audio/wav;base64,'}],null);
      $('#wrap').replaceChildren(row);
    });
    const mediaBubble=page.locator('#wrap .row.me .bubble');
    const message=mediaBubble.locator('.user-message-text');
    const textHeight=await message.evaluate(n=>({height:n.getBoundingClientRect().height,lineHeight:parseFloat(getComputedStyle(n).lineHeight)}));
    assert.ok(textHeight.height<=textHeight.lineHeight*2+1,'折叠文字严格不超过两行');
    assert.equal(await mediaBubble.locator('.imgs img').count(),1,'图片位于折叠文字区域之外');
    assert.equal(await mediaBubble.locator('.auds audio').count(),1,'音频位于折叠文字区域之外');
    assert.equal(await mediaBubble.locator('.user-message-text .imgs, .user-message-text .auds').count(),0);
    await page.evaluate(()=>{
      $('#wrap').replaceChildren();
      addBubble('me','刚发送');
      addBubble('me','',null,null,true);
      addBubble('ai','普通回复');
    });
    assert.equal(await page.locator('.userfoot').count(),1,'空的转写占位不显示底栏');
    assert.ok(await page.locator('.userfoot .tftime').innerText(),'新发送消息具有时间');
    await page.evaluate(()=>{
      const b=$('#wrap .row.me:nth-child(2) .bubble');
      setUserBubbleText(b,'语音转写内容');
      appendUserFoot(b.closest('.row'),b.textContent);
    });
    assert.equal(await page.locator('.userfoot').count(),2,'转写成功后补充底栏');
    const mobileContext=await browser.newContext({permissions:['clipboard-read','clipboard-write'],hasTouch:true});
    const mobile=await mobileContext.newPage();
    await mobile.goto(origin);
    await mobile.fill('#pw',token); await mobile.click('#loginBtn');
    await mobile.waitForFunction(()=>!$('#app').hidden);
    await mobile.evaluate(async()=>{
      await openConv('main');
      const {row}=buildBubble('me','移动端长消息'.repeat(100),['data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs='],[{url:'data:audio/wav;base64,'}],null);
      $('#wrap').replaceChildren(row);
    });
    const mobileFoot=mobile.locator('#wrap .userfoot');
    assert.equal(await mobileFoot.evaluate(n=>getComputedStyle(n).opacity),'1','触屏展开操作常显');
    assert.equal(await mobileFoot.locator('.tftime').evaluate(n=>getComputedStyle(n).display),'none','触屏不常显时间');
    assert.notEqual(await mobileFoot.locator('button[aria-label="复制"]').evaluate(n=>getComputedStyle(n).display),'none','触屏复制按钮可见');
    assert.notEqual(await mobileFoot.locator('.user-message-toggle').evaluate(n=>getComputedStyle(n).display),'none','触屏展开按钮可见');
    await mobileFoot.locator('.user-message-toggle').click();
    assert.equal(await mobileFoot.locator('.user-message-toggle').getAttribute('aria-expanded'),'true');
    await mobile.close(); await mobileContext.close();
    console.log(JSON.stringify({ok:true,widths:[320,375,390,768,1280],mobile:true}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
