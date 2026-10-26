const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
  const [origin,token]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    const page=await browser.newPage(), errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(origin); await page.fill('#pw',token); await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden);
    let aborts=0;
    await page.route('**/abort',route=>{aborts++; return route.fulfill({json:{ok:true,stopped:true}});});
    await page.evaluate(async()=>{
      await openConv('local-stop-a');
      S.convs.push({conv:S.conv,title:'停止测试',turns:1});
      handleEvent({type:'start',conv:S.conv});
      handleEvent({type:'text',conv:S.conv,seg:0,text:'已有部分回复'});
    });
    await page.locator('#wrap .statusstop').click();
    await page.waitForFunction(()=>!S.stream);
    // 取消期间已经在途的帧，先到缓冲，再到 DOM。
    await page.evaluate(()=>handleEvent({type:'thinking',conv:S.conv,text:'迟到的思考'}));
    assert.equal(await page.evaluate(()=>!!S.live[S.conv]),false,'点一次停止后，迟到帧不能重新点亮侧栏黄点');
    await page.waitForTimeout(2700);
    await page.evaluate(()=>handleEvent({type:'text',conv:S.conv,seg:0,text:'收尾超过两秒的旧回复'}));
    assert.equal(await page.locator('#wrap .status').count(),0,'收尾超过两秒也不能恢复思考中');
    assert.equal(await page.evaluate(()=>!!S.live[S.conv]),false);
    assert.ok((await page.locator('#wrap').innerText()).includes('已有部分回复'));
    await page.evaluate(async()=>{
      await openConv('local-stop-b');
      handleEvent({type:'start',conv:S.conv});
      handleEvent({type:'thinking',conv:'local-stop-a',text:'后台迟到帧'});
    });
    assert.equal(await page.evaluate(()=>!!S.live['local-stop-a']),false,'切到其他会话也不能恢复已停止会话');
    assert.equal(await page.locator('#wrap .status').count(),1,'停止 A 不能干扰 B');
    await page.evaluate(async()=>{
      handleEvent({type:'cancelled',conv:'local-stop-a'});
      handleEvent({type:'done',conv:S.conv,text:'B 完成'});
      await openConv('local-stop-a');
    });
    assert.equal(aborts,1);
    // 另一客户端停止、当前页面未按按钮，也必须消费取消终态。
    await page.evaluate(()=>{
      handleEvent({type:'user',conv:S.conv,text:'新问题'});
      handleEvent({type:'start',conv:S.conv});
      handleEvent({type:'thinking',conv:S.conv,text:'新轮思考'});
      handleEvent({type:'cancelled',conv:S.conv});
    });
    assert.equal(await page.evaluate(()=>!!S.live[S.conv]),false,'远端取消同样清空进行中缓冲');
    assert.equal(await page.locator('#wrap .status').count(),0);
    // 取消确认后，新的回合仍可正常显示并完成。
    await page.evaluate(()=>{
      handleEvent({type:'start',conv:S.conv});
      handleEvent({type:'text',conv:S.conv,seg:0,text:'新回复'});
    });
    assert.equal(await page.locator('#wrap .status').count(),1);
    await page.evaluate(()=>handleEvent({type:'done',conv:S.conv,text:'新回复'}));
    assert.equal(await page.locator('#wrap .status').count(),0);
    // 取消 SSE 先于 HTTP 回执时，不能把两条排队消息一起发出去。
    await page.unroute('**/abort');
    let releaseAbort;
    const abortGate=new Promise(resolve=>{releaseAbort=resolve;});
    await page.route('**/abort',async route=>{await abortGate; await route.fulfill({json:{ok:true,stopped:true}});});
    const sent=[];
    await page.route('**/send',route=>{sent.push(route.request().postDataJSON().text); return route.fulfill({json:{ok:true}});});
    await page.evaluate(()=>{
      handleEvent({type:'start',conv:S.conv});
      queuePending('排队一'); queuePending('排队二');
    });
    await page.locator('#wrap .statusstop').click();
    await page.evaluate(()=>handleEvent({type:'cancelled',conv:S.conv}));
    await page.waitForFunction(()=>!S.pendingSending[S.conv]);
    assert.deepEqual(sent,['排队一']);
    releaseAbort();
    await page.waitForTimeout(300);
    assert.deepEqual(sent,['排队一'],'停止回执不得重复推进队列');
    await page.evaluate(()=>handleEvent({type:'done',conv:S.conv,text:'第一条完成'}));
    await page.waitForFunction(()=>!S.pendingSending[S.conv]);
    assert.deepEqual(sent,['排队一','排队二']);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true,aborts}));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
