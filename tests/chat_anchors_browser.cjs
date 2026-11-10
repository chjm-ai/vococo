// 使用真实本地服务和正式页面;只有竞态场景延迟请求,不替换接口数据。
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
  const [origin,token,rawId,taskId,width]=process.argv.slice(2), turnId=Number(rawId);
  const browser=await chromium.launch({headless:true,
    ...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    const page=await browser.newPage({viewport:{width:Number(width),height:900}}), errors=[], cursors=[];
    page.on('pageerror',error=>errors.push(error.message));
    page.on('request',request=>{if(request.url().includes('/history?') && request.url().includes('before=')) cursors.push(request.url());});
    let releaseDiagram;
    const diagramGate=new Promise(resolve=>{releaseDiagram=resolve;});
    await page.route('**/mermaid-11.12.2.min.js',async route=>{await diagramGate;await route.continue();});
    await page.goto(origin);
    await page.fill('#pw',token);
    await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden && S.agents?.length>0);
    await page.evaluate(async()=>{
      await openConv('main');
    });
    await page.waitForTimeout(500);
    assert.equal(cursors.length,0,'打开会话滚到底部不应触发历史分页');
    const bottomGap=()=>page.evaluate(()=>{
      const m=$('#messages'); return m.scrollHeight-m.clientHeight-m.scrollTop;
    });
    assert.ok(await bottomGap()<16,'首次打开定位最新消息: '+JSON.stringify(await page.evaluate(()=>{
      const m=$('#messages');return {top:m.scrollTop,height:m.scrollHeight,client:m.clientHeight,conv:S.conv,loading:S.histLoading};
    })));
    releaseDiagram();
    await page.waitForSelector('#wrap .preview-diagram');
    await page.waitForTimeout(200);
    assert.ok(await bottomGap()<16,'图表异步展开后仍应停在最新消息，距底部: '+await bottomGap());
    // 只延迟真实图表渲染，不替换渲染结果；等待期间主动上翻必须停止跟随。
    await page.evaluate(async()=>{
      const render=mermaid.render.bind(mermaid);
      let release;
      const gate=new Promise(resolve=>{release=resolve;});
      mermaid.render=async(...args)=>{window.scrollDiagramStarted=true;await gate;return render(...args);};
      window.releaseScrollDiagram=()=>{mermaid.render=render;release();};
      await openConv('local-scroll-diagram');
      await openConv('main');
    });
    await page.waitForFunction(()=>window.scrollDiagramStarted);
    await page.locator('#messages').evaluate(m=>m.scrollTop=m.scrollHeight/2);
    await page.waitForTimeout(100);
    await page.evaluate(()=>releaseScrollDiagram());
    await page.waitForSelector('#wrap .preview-diagram');
    await page.waitForTimeout(200);
    assert.ok(await bottomGap()>100,'主动上翻后，图表展开不能拉回底部');
    assert.equal(cursors.length,0,'在对话中间阅读不应触发历史分页');
    await page.evaluate(()=>reloadHistory());
    await page.waitForTimeout(100);
    assert.ok(await bottomGap()>100,'刷新同一会话保留阅读位置');
    const earlier=page.waitForResponse(r=>r.url().includes('/history?') && r.url().includes('before='));
    await page.locator('#messages').evaluate(m=>m.scrollTop=0);
    await earlier;
    await page.waitForFunction(()=>!S.histPaging.main.loading);
    assert.equal(cursors.length,1,'滚到顶部才加载一页更早记录');
    await page.evaluate(()=>openConv('local-scroll-test'));
    await page.evaluate(()=>openConv('main'));
    await page.waitForTimeout(200);
    await page.waitForFunction(()=>{
      const m=$('#messages');return m.scrollHeight-m.clientHeight-m.scrollTop<16;
    });
    assert.ok(await bottomGap()<16,'从缓存重新打开仍定位最新消息');
    await page.locator('#messages').evaluate(m=>m.scrollTop=m.scrollHeight/2);
    await page.waitForTimeout(100);
    await page.evaluate(()=>openConv('main'));
    await page.waitForTimeout(100);
    assert.ok(await bottomGap()<16,'重复打开当前会话也回到最新消息');
    await page.evaluate(()=>{
      S.agentPanelTab='feed';
      if(!S.agentPanelOn) toggleAgentPanel();
      else renderAgentPanel();
    });
    await page.locator('#apBody .aprun').filter({hasText:'旧日报'}).click();
    await page.waitForSelector(`.chat-anchor[data-tid="${turnId}"]`,{timeout:10000}).catch(async error=>{
      console.error(await page.evaluate(()=>({conv:S.conv,anchor:_chatAnchor,seq:_convOpenSeq,paging:S.histPaging,ids:S.histCache.main?.map(t=>t.id)})),errors,cursors);
      throw error;
    });
    assert.ok(cursors.length>=2,'旧结果跨过两页仍能定位');
    assert.equal(await page.evaluate(()=>S.conv),'main');
    assert.equal(await page.locator('#agentPanel').isVisible(),false,'点击后关闭动态面板,直接显示聊天');
    const position=await page.evaluate(turnId=>{
      const m=$('#messages').getBoundingClientRect(), n=$('#wrap').querySelector(`[data-tid="${turnId}"]`).getBoundingClientRect();
      return {top:n.top-m.top,height:m.height};
    },turnId);
    assert.ok(position.top>=0 && position.top<position.height/2,JSON.stringify(position));
    await page.evaluate(async()=>{await reloadHistory(); scrollDown(true);});
    await page.waitForTimeout(100);
    assert.ok(await page.evaluate(()=>$('#messages').scrollTop<$('#messages').scrollHeight-$('#messages').clientHeight-100),'刷新不会强制回到底部');
    // 最新一轮无须分页。
    const latest=await page.evaluate(()=>S.histCache.main.at(-1));
    await page.evaluate(t=>openConvAtTurn('main',t.id,t.ts),latest);
    assert.equal(await page.locator(`.chat-anchor[data-tid="${latest.id}"]`).count(),1);
    // 动态中的独立摘要仍应定位后台任务完整回复。
    await page.evaluate(()=>toggleAgentPanel());
    await page.locator('#apBody .aprun').filter({hasText:'后台长回复'}).click();
    await page.waitForSelector(`.chat-anchor[data-tid="${taskId}"]`);
    assert.equal(await page.evaluate(()=>S.conv),'task:anchor-background');
    assert.match(await page.locator(`.chat-anchor[data-tid="${taskId}"]`).innerText(),/后台完整结果/);
    // 时间戳不一致代表 ID 已复用,不能错误定位。
    const dialog=page.waitForEvent('dialog');
    const invalid=page.evaluate(t=>openConvAtTurn('main',t.id,t.ts+1),latest);
    const warning=await dialog;
    assert.match(warning.message(),/未能定位/); await warning.dismiss(); await invalid;
    // 加载中切走:旧定位不得把用户拉回来。
    await page.route('**/history?conv=main',async route=>{await new Promise(r=>setTimeout(r,200));await route.continue();});
    await page.evaluate(async({latest,turnId})=>{
      await Promise.all([openConvAtTurn('main',latest.id,latest.ts),openConvAtTurn('main',turnId)]);
    },{latest,turnId});
    assert.equal(await page.locator('#wrap .chat-anchor').getAttribute('data-tid'),String(turnId),'连续点击同会话只定位最后一次');
    await page.evaluate(turnId=>{window.anchorWait=openConvAtTurn('main',turnId);},turnId);
    await page.evaluate(()=>openConv('local-anchor-test'));
    await page.evaluate(()=>window.anchorWait);
    assert.equal(await page.evaluate(()=>S.conv),'local-anchor-test');
    assert.equal(await page.locator('#wrap .chat-anchor').count(),0);
    // 取消定位只取消滚动,不能连正常历史加载也丢弃。
    await page.evaluate(async()=>{delete S.histCache.main; await idbDel('hist:main');});
    const historyRequest=page.waitForRequest(request=>request.url().endsWith('/history?conv=main'));
    await page.evaluate(turnId=>{window.cancelWait=openConvAtTurn('main',turnId);},turnId);
    await historyRequest;
    await page.evaluate(()=>scrollDownSmooth());
    await page.evaluate(()=>window.cancelWait);
    assert.ok(await page.locator('#wrap [data-tid]').count()>=40,'取消锚点后历史仍然显示');
    assert.equal(await page.evaluate(()=>$('#convLoading').classList.contains('on')),false);
    assert.equal(await page.locator('#wrap .chat-anchor').count(),0);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true,pages:cursors.length}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
