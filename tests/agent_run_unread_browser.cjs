// 正式页面与真实历史、已读接口；控制端仅模拟定时任务完成。
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
  const [origin,token,recordUrl,conv]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,
    ...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    const page=await browser.newPage(), errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.addInitScript(conv=>{window.testConv=conv;},conv);
    await page.goto(origin);
    await page.fill('#pw',token);
    await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden && S.agents?.length>0 && S.bootId);
    await page.evaluate(()=>{
      window.agentRunCompletions=[];
      const original=onAgentRun;
      onAgentRun=event=>{const completion=original(event); agentRunCompletions.push(completion); return completion;};
      return openConv(testConv);
    });
    const unread=()=>page.evaluate(async()=>{
      const response=await api('/conversations');
      const data=await response.json();
      return data.conversations.find(c=>c.conv===testConv)?.pending_review;
    });
    const record=async text=>{
      const response=await page.request.post(recordUrl,{data:{text}});
      assert.equal(response.ok(),true);
    };
    // 正在查看：结果先显示，服务端与侧栏灰点都应清除。
    await record('正在看的日报结果');
    await page.waitForFunction(()=>$('#wrap').textContent.includes('正在看的日报结果'));
    await page.waitForFunction(async()=>{
      const r=await api('/conversations'), d=await r.json();
      return d.conversations.find(c=>c.conv===testConv)?.pending_review===false;
    },null,{timeout:5000});
    await page.evaluate(()=>loadConvs());
    assert.equal(await unread(),false);
    assert.equal(await page.evaluate(()=>!!S.pendingReview[testConv]),false);
    // 切到别处：保留未读，打开后能看到历史且灰点消失。
    await page.evaluate(()=>openConv('local-unread-test'));
    await record('离开时的日报结果');
    await page.waitForFunction(()=>S.pendingReview[testConv]===true);
    assert.equal(await unread(),true);
    await page.evaluate(()=>openConv(testConv));
    await page.waitForFunction(()=>$('#wrap').textContent.includes('离开时的日报结果'));
    assert.equal(await unread(),false);
    // 正在流式回复时只登记补拉，未展示的定时结果不能被提前标为已读。
    await page.evaluate(()=>handleEvent({conv:testConv,type:'start'}));
    await page.waitForFunction(()=>!!S.stream);
    await record('回复期间到达的结果');
    await page.waitForFunction(()=>S.historyRefreshPending?.[testConv]===true);
    assert.equal(await page.locator('#wrap').innerText().then(text=>text.includes('回复期间到达的结果')),false);
    assert.equal(await unread(),true,'结果尚未展示时必须保留未读');
    await page.evaluate(()=>openConv('local-unread-test'));
    await page.evaluate(()=>handleEvent({conv:testConv,type:'cancelled'}));
    assert.equal(await unread(),true,'中途切走不能清除未读');
    await page.evaluate(()=>openConv(testConv));
    await page.waitForFunction(()=>$('#wrap').textContent.includes('回复期间到达的结果'));
    // 历史请求失败同样不能把未显示的结果标为已读。
    const historyPattern='**/history?conv='+encodeURIComponent(conv);
    await page.route(historyPattern,route=>route.abort());
    await record('历史加载失败时的结果');
    await page.waitForFunction(()=>S.pendingReview[testConv]===true);
    assert.equal(await unread(),true);
    assert.equal(await page.locator('#wrap').innerText().then(text=>text.includes('历史加载失败时的结果')),false);
    await page.unroute(historyPattern);
    await page.evaluate(()=>openConv(testConv));
    await page.waitForFunction(()=>$('#wrap').textContent.includes('历史加载失败时的结果'));
    // 历史还在加载时切走，迟到的刷新也不能清掉未看过的结果。
    let releaseHistory;
    const historyGate=new Promise(resolve=>{releaseHistory=resolve;});
    await page.route(historyPattern,async route=>{await historyGate; await route.continue();});
    const loadingHistory=page.waitForRequest(request=>request.url().endsWith('/history?conv='+encodeURIComponent(conv)));
    await record('加载途中切走的结果');
    await loadingHistory;
    await page.evaluate(()=>openConv('local-unread-test'));
    const loadedHistory=page.waitForResponse(response=>response.url().endsWith('/history?conv='+encodeURIComponent(conv)));
    releaseHistory();
    await loadedHistory;
    await page.unroute(historyPattern);
    assert.equal(await unread(),true);
    await page.evaluate(()=>openConv(testConv));
    await page.waitForFunction(()=>$('#wrap').textContent.includes('加载途中切走的结果'));
    // 分别延迟已读请求和已读响应：旧确认都不能吞掉后来未展示的结果。
    for(const delayResponse of [false,true]){
      let releaseRead, readStarted;
      const readGate=new Promise(resolve=>{releaseRead=resolve;});
      const readReady=new Promise(resolve=>{readStarted=resolve;});
      await page.route('**/conv/read',async route=>{
        const response=delayResponse?await route.fetch():null;
        readStarted();
        await readGate;
        if(response) await route.fulfill({response}); else await route.continue();
      });
      await record('先展示的结果 '+delayResponse);
      await readReady;
      await page.route(historyPattern,route=>route.abort());
      const failedHistory=page.waitForEvent('requestfailed',request=>request.url().endsWith('/history?conv='+encodeURIComponent(conv)));
      await record('旧确认之后未展示的结果 '+delayResponse);
      await failedHistory;
      await page.waitForFunction(()=>S.pendingReview[testConv]===true);
      releaseRead();
      await page.evaluate(()=>Promise.all(agentRunCompletions));
      assert.equal(await unread(),true,'旧已读确认不能清除后来结果的服务端未读');
      assert.equal(await page.evaluate(()=>S.pendingReview[testConv]),true,'旧响应不能覆盖新事件的本地未读');
      await page.unroute('**/conv/read');
      await page.unroute(historyPattern);
      await page.evaluate(()=>openConv(testConv));
      await page.waitForFunction(text=>$('#wrap').textContent.includes(text),'旧确认之后未展示的结果 '+delayResponse);
    }
    // 已读请求失败不能让消息丢失；重新打开应能补清未读。
    await page.route('**/conv/read',route=>route.abort());
    await record('已读失败仍保留的结果');
    await page.waitForFunction(()=>$('#wrap').textContent.includes('已读失败仍保留的结果'));
    assert.equal(await unread(),true);
    await page.unroute('**/conv/read');
    await page.evaluate(()=>openConv(testConv));
    await page.waitForFunction(async()=>{
      const r=await api('/conversations'), d=await r.json();
      return d.conversations.find(c=>c.conv===testConv)?.pending_review===false;
    });
    await page.reload();
    await page.waitForFunction(()=>!$('#app').hidden && S.agents?.length>0);
    await page.evaluate(()=>openConv(testConv));
    assert.match(await page.locator('#wrap').innerText(),/已读失败仍保留的结果/);
    assert.equal(await unread(),false);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
