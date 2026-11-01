const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
  const [origin,token]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    const page=await browser.newPage({serviceWorkers:'block'}), errors=[];
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
    assert.deepEqual(errors,[]);
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
    await page.evaluate(()=>handleEvent({type:'user',conv:S.conv,text:'排队一'}));
    assert.equal(await page.locator('#wrap .row.me').filter({hasText:'排队一'}).count(),1,'停止后队列发送的消息必须立即显示，无需刷新');
    releaseAbort();
    await page.waitForTimeout(300);
    assert.deepEqual(sent,['排队一'],'停止回执不得重复推进队列');
    await page.evaluate(()=>handleEvent({type:'done',conv:S.conv,text:'第一条完成'}));
    await page.waitForFunction(()=>!S.pendingSending[S.conv]);
    assert.deepEqual(sent,['排队一','排队二']);
    await page.evaluate(()=>handleEvent({type:'done',conv:S.conv,text:'第二条完成'}));
    // 点第二条的立即发送按钮，必须先停当前回复，再只发选中的一条。
    let immediateAborts=0;
    await page.unroute('**/abort');
    await page.route('**/abort',route=>{immediateAborts++; return route.fulfill({json:{ok:true,stopped:true}});});
    await page.evaluate(()=>{
      handleEvent({type:'start',conv:S.conv});
      queuePending('仍然排队'); queuePending('优先发送');
      $('#ta').value='未发送的新草稿';
    });
    assert.deepEqual(await page.locator('#pending .pnow').nth(1).locator('svg path').evaluateAll(paths=>paths.map(p=>p.getAttribute('d'))),['M5 3h14','m18 13-6-6-6 6','M12 7v14'],'立即发送使用向上箭头加顶部横线，不使用图钉');
    await page.getByRole('button',{name:'停止当前回复并立即发送'}).nth(1).click();
    await page.waitForFunction(()=>!!S.stopping[S.conv]);
    assert.deepEqual(sent,['排队一','排队二'],'取消确认之前不能发送');
    assert.equal(await page.locator('#pending .pnow:disabled').count(),2);
    const busyRow=page.locator('#pending .pi[aria-busy="true"]');
    assert.equal(await busyRow.count(),1);
    assert.equal(await busyRow.locator('.ptext').innerText(),'正在停止并发送…');
    assert.equal(await busyRow.locator('.pdel:disabled').count(),2,'等待期间不能重复发送或删除该消息');
    assert.equal(await busyRow.locator('.pnow svg').evaluate(svg=>getComputedStyle(svg).animationName),'spin');
    await page.emulateMedia({reducedMotion:'reduce'});
    assert.equal(await busyRow.locator('.pnow svg').evaluate(svg=>getComputedStyle(svg).animationName),'none');
    await page.emulateMedia({reducedMotion:'no-preference'});
    await page.waitForTimeout(250);
    assert.equal(await busyRow.count(),1,'停止回执先到，取消终态未到时仍保持 loading');
    await page.evaluate(()=>handleEvent({type:'cancelled',conv:S.conv}));
    await page.waitForFunction(()=>!S.pendingSending[S.conv]);
    assert.deepEqual(sent,['排队一','排队二','优先发送']);
    assert.equal(immediateAborts,1);
    assert.equal(await page.locator('#wrap .row.me').filter({hasText:'优先发送'}).count(),1);
    await page.evaluate(()=>handleEvent({type:'user',conv:S.conv,text:'优先发送'}));
    assert.equal(await page.locator('#wrap .row.me').filter({hasText:'优先发送'}).count(),1,'服务端回显不能重复创建气泡');
    assert.equal(await page.locator('#ta').inputValue(),'未发送的新草稿');
    assert.deepEqual(await page.evaluate(()=>S.pending[S.conv].map(x=>x.text)),['仍然排队']);
    assert.equal(await page.locator('#pending .pi[aria-busy="true"]').count(),0);
    // 各档屏宽的长文本不挤出行边界，字号和点击区域均已放大。
    await page.evaluate(()=>{S.pending[S.conv][0].text='很长的待发送消息'.repeat(30); drawPending();});
    for(const width of [320,375,390,768,1280]){
      await page.setViewportSize({width,height:812});
      const sizes=await page.locator('#pending .pnow').evaluate(button=>{
        const row=button.closest('.pi'), box=button.getBoundingClientRect(), bounds=row.getBoundingClientRect();
        return {fits:box.left>=bounds.left && box.right<=bounds.right,width:box.width,height:bounds.height,
          font:parseFloat(getComputedStyle(row).fontSize),icon:button.querySelector('svg').getBoundingClientRect().width};
      });
      assert.ok(sizes.fits); assert.equal(sizes.width,40); assert.ok(sizes.height>=56);
      assert.equal(sizes.font,15); assert.equal(sizes.icon,24);
    }
    // 切回会话但历史还在加载：S.stream 暂时为空，也必须先停止后台运行。
    await page.route('**/history?conv=stop-switch-active',route=>route.fulfill({json:{turns:[]}}));
    await page.evaluate(async()=>{
      await openConv('stop-switch-active');
      handleEvent({type:'start',conv:S.conv}); queuePending('切回后优先发送');
      await openConv('local-switch-away');
    });
    await page.unroute('**/history?conv=stop-switch-active');
    let releaseHistory;
    const historyGate=new Promise(resolve=>{releaseHistory=resolve;});
    await page.route('**/history?conv=stop-switch-active',async route=>{await historyGate; await route.fulfill({json:{turns:[]}});});
    await page.evaluate(()=>{window.switchBack=openConv('stop-switch-active');});
    await page.waitForFunction(()=>S.histLoading===S.conv && !S.stream);
    await page.getByRole('button',{name:'停止当前回复并立即发送'}).click();
    await page.waitForFunction(()=>!!S.stopping[S.conv]);
    assert.equal(immediateAborts,2,'不能因为流式 DOM 尚未恢复就绕过停止');
    assert.deepEqual(sent,['排队一','排队二','优先发送']);
    releaseHistory(); await page.evaluate(()=>window.switchBack);
    await page.evaluate(()=>handleEvent({type:'cancelled',conv:S.conv}));
    await page.waitForFunction(()=>!S.pendingSending[S.conv]);
    assert.equal(sent.at(-1),'切回后优先发送');
    // 明确发送失败，队列内容可重试，不能留下冒充成功的用户气泡。
    await page.evaluate(()=>{delete S.pending[S.conv]; drawPending(); handleEvent({type:'done',conv:S.conv,text:'完成'});});
    await page.unroute('**/send');
    await page.route('**/send',route=>route.fulfill({status:400,json:{error:'测试拒绝'}}));
    await page.evaluate(async()=>{queuePending('失败应保留'); await sendPendingNow(S.pending[S.conv][0].id);});
    assert.equal(await page.locator('#wrap .row.me').filter({hasText:'失败应保留'}).count(),0);
    assert.equal(await page.evaluate(()=>S.pending[S.conv][0].text),'失败应保留');
    assert.ok(await page.evaluate(()=>!!S.pending[S.conv][0].error));
    assert.equal(await page.locator('#pending .pnow:disabled').count(),0);
    // 停止失败：退出 loading，保留原需求，不误发。
    await page.unroute('**/abort');
    await page.route('**/abort',route=>route.fulfill({status:500,json:{error:'停止失败'}}));
    await page.evaluate(async()=>{
      await openConv('local-stop-error'); handleEvent({type:'start',conv:S.conv}); queuePending('停止失败保留');
      await sendPendingNow(S.pending[S.conv][0].id);
    });
    assert.equal(await page.locator('#pending .pi[aria-busy="true"]').count(),0);
    assert.equal(await page.locator('#pending .ptext').innerText(),'停止失败保留');
    assert.equal(await page.locator('#pending .pnow:disabled').count(),0);
    assert.equal(sent.at(-1),'切回后优先发送');
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true,aborts}));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
