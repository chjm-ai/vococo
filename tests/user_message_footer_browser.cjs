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
      await page.setViewportSize({width,height:900});
      await page.evaluate(()=>{
        $('#wrap').replaceChildren(buildTurnBlock({id:123,user:'已发送内容\n第二行',assistant:'回复',ts:1700000000},'main',true));
      });
      await page.mouse.move(0,0);
      const row=page.locator('#wrap .row.me'), foot=row.locator('.userfoot');
      await page.waitForTimeout(200);
      assert.equal(await foot.evaluate(n=>getComputedStyle(n).opacity),'0');
      assert.equal(await foot.locator('.tftime').innerText(),await page.evaluate(()=>fmtTime(1700000000)));
      assert.equal(await foot.locator('button').count(),1,'用户消息不应出现重新生成按钮');
      const bubble=row.locator('.bubble');
      await bubble.hover(); await page.waitForTimeout(200);
      assert.equal(await foot.evaluate(n=>getComputedStyle(n).opacity),'1');
      const before=await row.boundingBox();
      await foot.locator('button').click();
      assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),'已发送内容\n第二行');
      await page.locator('#msg').focus().catch(()=>{});
      await page.mouse.move(0,0); await page.waitForTimeout(200);
      assert.deepEqual(await row.boundingBox(),before,'底栏显隐不应推动消息布局');
      assert.ok(await bubble.evaluate(n=>n.getBoundingClientRect().right<=innerWidth),'窄屏气泡不能越界');
      await foot.locator('button').focus(); await page.waitForTimeout(200);
      assert.equal(await foot.evaluate(n=>getComputedStyle(n).opacity),'1','键盘聚焦时可见');
    }
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
      b.textContent='语音转写内容';
      appendUserFoot(b.closest('.row'),b.textContent);
    });
    assert.equal(await page.locator('.userfoot').count(),2,'转写成功后补充底栏');
    console.log(JSON.stringify({ok:true,widths:[320,375,390,768,1280]}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
