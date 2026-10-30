const {chromium}=require('playwright');
const assert=require('node:assert/strict');

(async()=>{
  const [origin,token,agent,project,completed,stopped,device]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    const page=await browser.newPage(device==='mobile'?{viewport:{width:390,height:844},isMobile:true,hasTouch:true,userAgent:'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148'}:{});
    const errors=[], sends=[];
    page.on('pageerror',e=>errors.push(e.message));
    page.on('request',request=>{if(new URL(request.url()).pathname==='/send') sends.push(request.postDataJSON());});
    await page.goto(origin);
    await page.fill('#pw',token); await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden && S.agents.length>1);

    // 固定同一帧内的事件顺序：不能靠机器快慢碰巧在断言前绘制。
    const boundary=await page.evaluate(()=>{
      const raf=window.requestAnimationFrame, frames=[];
      window.requestAnimationFrame=callback=>{frames.push(callback);return 1;};
      try{
        discardStream();
        applyStreamEvent({type:'text',seg:0,text:'先检查'});
        applyStreamEvent({type:'text',seg:0,text:'先检查代码'});
        applyStreamEvent({type:'tool_start',name:'Read',tool_id:'boundary'});
        applyStreamEvent({type:'tool_input',name:'Read',tool_id:'boundary',input:{file_path:'README.md'}});
        return {raw:streamText(S.stream),visible:S.stream.flow.textContent};
      }finally{
        discardStream(); window.requestAnimationFrame=raf;
        for(const callback of frames) raf(callback);
      }
    });
    assert.equal(boundary.raw,'先检查代码');
    assert.ok(boundary.visible.includes('先检查代码'),'工具出现前必须绘制完整正文，不能只有 dataset.raw');

    await page.evaluate(()=>newChatIn(null,false));
    await page.waitForFunction(()=>!S.histLoading);
    const normal=await page.evaluate(()=>S.conv);
    assert.match(normal,/^session:[a-f0-9]{32}$/);
    await page.fill('#ta','统一会话浏览器验收');
    await page.click('#projSelBtn');
    const choices=await page.locator('#projSelPop .mi').evaluateAll(rows=>rows.map(row=>row.dataset.h));
    assert.deepEqual(choices,['',project],'只能选择总助理和已创建的 Agent，不能混入任务工作目录');
    assert.equal(await page.locator('#projSelPop .mi.on').getAttribute('data-h'),'');
    await page.click('#projSelPop .mi[data-h="'+project+'"]');
    await page.click('#projSelBtn');
    assert.equal(await page.locator('#projSelPop .mi.on').getAttribute('data-h'),project);
    await page.click('#projSelBtn');
    assert.equal(await page.evaluate(()=>S.conv),normal,'切换项目不能换 ID');
    assert.equal(await page.inputValue('#ta'),'统一会话浏览器验收');
    await page.click('#sendBtn');
    await page.waitForFunction(()=>!S.stream && $('#wrap').textContent.includes('普通会话回复'));
    await page.waitForFunction(()=>!$('#barMeta').hidden && $('#ctxPct').textContent==='10%');
    assert.equal(await page.evaluate(()=>S.conv),normal,'首次发送不能换 ID');
    assert.equal(sends[0].conv,normal);
    assert.equal(sends[0].agent_id,agent); assert.equal(sends[0].project_hash,project);
    await page.reload();
    await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(conv=>openConv(conv),normal);
    await page.waitForFunction(()=>!S.histLoading && $('#wrap').textContent.includes('普通会话回复'));
    assert.deepEqual(await page.evaluate(conv=>convOwnership(conv),normal),{agent_id:agent,project_hash:project});
    await page.fill('#ta','刷新后继续'); await page.click('#sendBtn');
    await page.waitForFunction(()=>!S.stream && $('#wrap .row.ai') && document.querySelectorAll('#wrap .row.ai').length===2);
    assert.equal(sends[1].conv,normal,'刷新续聊仍使用同一 ID');
    assert.equal(sends[1].agent_id,agent); assert.equal(sends[1].project_hash,project);

    await page.evaluate(conv=>openConv(conv),completed);
    await page.waitForFunction(()=>S.stream && streamText(S.stream).includes('执行中'));
    assert.ok((await page.locator('#wrap').textContent()).includes('先检查代码'));
    assert.equal(await page.evaluate(()=>S.stream.tools.length),1);
    await page.reload(); await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(conv=>openConv(conv),completed);
    await page.waitForFunction(()=>S.stream && streamText(S.stream).includes('执行中'));
    assert.equal(await page.evaluate(()=>S.stream.tools.length),1,'刷新恢复工具过程');
    await page.waitForFunction(()=>!$('#barMeta').hidden && $('#ctxPct').textContent==='17%');
    assert.equal(await page.evaluate(()=>{
      setMeta({}); refillCurrentMeta(); return !$('#barMeta').hidden;
    }),true,'侧栏延迟到达必须补回上下文圈');
    const usageTitle=await page.evaluate(conv=>{
      const row=S.voiceSidebar.tasks.find(x=>x.conv===conv), title=row.title;
      handleEvent({type:'usage',conv,title:null,ctx_tokens:36000,ctx_window:200000,total_tokens:0});
      return {title:row.title,expected:title,pct:$('#ctxPct').textContent};
    },completed);
    assert.equal(usageTitle.title,usageTitle.expected,'用量更新不能覆盖历史任务名称');
    assert.equal(usageTitle.pct,'18%','运行中用量事件应立即更新进度圈');
    await page.evaluate(conv=>openConv(conv),stopped);
    await page.waitForFunction(()=>S.stream && streamText(S.stream).includes('执行中'));
    const abort=page.waitForResponse(response=>new URL(response.url()).pathname==='/abort');
    await page.locator('#wrap .statusstop').click();
    assert.equal((await (await abort).json()).stopped,true);
    await page.waitForFunction(()=>!S.stream);
    await page.reload(); await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(conv=>openConv(conv),stopped);
    await page.waitForFunction(()=>!S.histLoading && $('#wrap').textContent.includes('先检查代码'));
    assert.equal(await page.locator('#wrap .status').count(),0,'停止后刷新不能复活流');
    const partition=await page.evaluate(async({normal,completed,stopped})=>{
      await Promise.all([loadConvs(),loadVoiceSidebar()]);
      const rows=(await (await api('/conversations')).json()).conversations;
      return {normal:rows.filter(row=>row.conv===normal).length,
        duplicates:rows.filter(row=>[completed,stopped].includes(row.conv)).length,
        tasks:S.voiceSidebar.tasks.filter(row=>[completed,stopped].includes(row.conv)).map(row=>({conv:row.conv,status:row.task_status}))};
    },{normal,completed,stopped});
    assert.equal(partition.normal,1); assert.equal(partition.duplicates,0);
    assert.equal(partition.tasks.length,2);
    assert.equal(partition.tasks.find(row=>row.conv===stopped).status,'cancelled');
    await page.evaluate(conv=>openConv(conv),completed);
    await page.waitForFunction(()=>S.stream && streamText(S.stream).includes('执行中'));
    console.log('READY');
    await page.waitForFunction(()=>!S.stream && $('#wrap').textContent.includes('全部完成'));
    await page.waitForFunction(()=>$('#wrap .row.ai[data-tid],#wrap .turn-block[data-tid]'));
    assert.equal(await page.locator('#wrap .row.ai').count(),1,'完成后不能重复回复');
    assert.equal(await page.locator('#wrap .status').count(),0);
    await page.reload(); await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(conv=>openConv(conv),completed);
    await page.waitForFunction(()=>!S.histLoading && $('#wrap').textContent.includes('全部完成'));
    assert.equal(await page.locator('#wrap .row.ai').count(),1);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({normal,device,ok:true}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
