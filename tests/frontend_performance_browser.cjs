const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
  const [origin,token,viewport='desktop']=process.argv.slice(2);
  const baseline=!!process.env.PERF_BASELINE;
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    const page=await browser.newPage({serviceWorkers:'block',...(viewport==='mobile'?{
      viewport:{width:390,height:844},isMobile:true,hasTouch:true,
      userAgent:'Mozilla/5.0 (iPhone; CPU iPhone OS 18_4 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148',
    }:{})}), errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    page.setDefaultTimeout(15000);
    console.log('browser-ready');
    await page.goto(origin,{waitUntil:'domcontentloaded',timeout:15000});
    console.log('page-ready');
    await page.fill('#pw',token); await page.click('#loginBtn');
    console.log('login-ready');
    await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(()=>openConv('local-performance'));
    const metrics=await page.evaluate(async()=>{
      const target=el('div','seg'); $('#wrap').append(target);
      const prefix=Array.from({length:80},(_,i)=>`第 ${i} 段：**已经完成的内容**，不应该随着后面的字重复销毁。\n\n`).join('');
      renderPreviewMarkdown(target,prefix+'尾部',true);
      const first=target.firstChild, observer=new MutationObserver(()=>{});
      observer.observe(target,{childList:true});
      const start=performance.now();
      for(let i=0;i<100;i++) renderPreviewMarkdown(target,prefix+'尾部'+'.'.repeat(i+1),true);
      const markdownMs=performance.now()-start;
      const markdownRemoved=observer.takeRecords().reduce((n,r)=>n+r.removedNodes.length,0);
      const prefixPreserved=first===target.firstChild;
      const finalText=target.textContent;
      observer.disconnect(); target.remove();
      handleEvent({type:'start',conv:S.conv});
      const status=S.stream.status, stop=status.querySelector('button');
      observer.observe(status,{childList:true});
      const now=Date.now; Date.now=()=>S.stream.t0;
      const statusStart=performance.now();
      try{for(let i=0;i<300;i++) renderStatus();}finally{Date.now=now;}
      const statusMs=performance.now()-statusStart;
      const statusWrites=observer.takeRecords().length;
      const stopPreserved=stop===status.querySelector('button');
      observer.disconnect();
      await new Promise(resolve=>requestAnimationFrame(resolve));
      const raf=window.requestAnimationFrame; let scrollFrames=0;
      window.requestAnimationFrame=callback=>{scrollFrames++;return raf(callback);};
      try{for(let i=0;i<100;i++) scrollDown(i===50);}finally{window.requestAnimationFrame=raf;}
      await new Promise(resolve=>raf(()=>raf(resolve)));
      discardStream();
      await new Promise(resolve=>setTimeout(resolve,300));
      S.voiceSidebar.tasks=[{task_id:'perf-task',conv:'task:perf',title:'状态测试',task_status:'running'}];
      let sidebarRenders=0;
      const render=renderConvs;
      renderConvs=()=>{sidebarRenders++;return render();};
      const sidebarStart=performance.now();
      try{
        for(let i=0;i<100;i++) syncSidebarTaskStatus({id:'perf-task',status:i%2?'running':'queued'});
        await new Promise(resolve=>raf(resolve));
      }finally{renderConvs=render;}
      const sidebarMs=performance.now()-sidebarStart;
      return {markdownMs,markdownRemoved,prefixPreserved,statusMs,statusWrites,stopPreserved,scrollFrames,sidebarRenders,sidebarMs,finalTextCorrect:finalText.endsWith('尾部'+'.'.repeat(100))};
    });
    // 历史网络故意延迟：验证切换先显示内存缓存，而非等待请求结束。
    await page.route('**/history?conv=perf-cached',async route=>{
      await new Promise(resolve=>setTimeout(resolve,400));
      await route.fulfill({json:{turns:[{id:801,user:'缓存问题',assistant:'缓存立即可见'}],has_more:false}});
    });
    const switching=await page.evaluate(async()=>{
      S.convs.push({conv:'perf-cached',title:'缓存切换',turns:1});
      histCacheSet('perf-cached',[{id:801,user:'缓存问题',assistant:'缓存立即可见'}]);
      const start=performance.now(), pending=openConv('perf-cached');
      const cachedMs=performance.now()-start, cachedVisible=$('#wrap').textContent.includes('缓存立即可见');
      await pending;
      return {cachedMs,historyMs:performance.now()-start,cachedVisible};
    });
    assert.equal(switching.cachedVisible,true,'缓存会话必须在网络返回前显示');
    console.log(JSON.stringify({baseline,viewport,...metrics,...switching}));
    assert.equal(metrics.finalTextCorrect,true);
    if(!baseline){
      assert.equal(metrics.prefixPreserved,true,'已完成的段落必须保留原 DOM');
      assert.equal(metrics.markdownRemoved,100,'每次只替换变化的尾段');
      assert.equal(metrics.statusWrites,0,'同一状态和秒数不能反复重建状态栏');
      assert.equal(metrics.stopPreserved,true,'停止按钮不能随每个 token 重建');
      assert.equal(metrics.scrollFrames,1,'同一帧只调度一次滚动');
      assert.equal(metrics.sidebarRenders,1,'同一帧的状态突发只重绘一次列表');
    }
    // 改写、删除、重复段落和流式结束必须与完整解析一致。
    await page.evaluate(()=>{
      const target=el('div','seg'); $('#wrap').append(target);
      for(const source of ['甲\n\n甲\n\n乙','甲\n\n改写','只有一句','','**未闭合','**闭合**']){
        renderPreviewMarkdown(target,source,true);
        if(target.innerHTML!==mdToHtml(source,true)) throw Error('增量渲染结果与全文不符');
      }
      renderPreviewMarkdown(target,'**闭合**',false);
      if(target.innerHTML!==mdToHtml('**闭合**',false)) throw Error('最终渲染不符');
      target.remove();
    });
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
