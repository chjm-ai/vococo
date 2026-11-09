// 长会话切页对照测量(配合 history_slow_probe.py)。只给 /history 注入延迟,其余走真实服务。
// 输出一份 JSON 指标到 stdout。
// 慢接口由 Python 服务真实分片发送;对照只替换三个前端资源。
const {chromium}=require('playwright');
const assert=require('node:assert/strict');

const SLOW=Number(process.env.SLOW_MS)||1200;
const fs=require('node:fs');
const path=require('node:path');

// 延迟由 Python 隔离服务真实分片发送,不拦截 /history,保留浏览器取消行为。

(async()=>{
  const [origin,token,tag]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  const out={tag,slow_ms:SLOW};
  try{
    const page=await browser.newPage({serviceWorkers:'block',viewport:{width:1280,height:800}});
    const errors=[]; page.on('pageerror',e=>errors.push(e.message));
    const net={req:{},failed:{},abortLog:[]};
    page.on('request',r=>{ const u=new URL(r.url()); if(u.pathname==='/history'){ const c=u.searchParams.get('conv'); net.req[c]=(net.req[c]||0)+1; } });
    page.on('requestfailed',r=>{ const u=new URL(r.url()); if(u.pathname==='/history'){ const c=u.searchParams.get('conv'); net.failed[c]=(net.failed[c]||0)+1; } });
    // 页面内看浏览器真的调了 abort(false positive 排除:signal.aborted 但没人 abort)
    await page.addInitScript(()=>{
      const orig=window.fetch.bind(window);
      window.__aborts=[];
      window.fetch=(u,o)=>{
        if(String(u).startsWith('/history?') && o && o.signal && o.signal.aborted===false){
          o.signal.addEventListener('abort',()=>window.__aborts.push(String(u)),{once:true});
        }
        return orig(u,o);
      };
    });
    if(process.env.BASELINE_DIR){
      await page.route('**/*',async route=>{
        const pathname=new URL(route.request().url()).pathname;
        const name=pathname==='/'?'index.html':pathname.slice(1);
        if(!['index.html','app-core.js','sidebar.js'].includes(name)) return route.continue();
        await route.fulfill({body:fs.readFileSync(path.join(process.env.BASELINE_DIR,name)),contentType:name.endsWith('.js')?'application/javascript':'text/html'});
      });
    }
    let git=0,taskReqs=0;
    page.on('request',r=>{ const u=new URL(r.url()); if(u.pathname==='/conv/git') git++; if(u.pathname==='/tasks') taskReqs++; });
    await page.goto(origin); await page.fill('#pw',token); await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden);
    // 先等页面原有一次性预热结束,单独测前台打开,不把预热算成重复请求。
    await page.waitForTimeout(3000+SLOW*8);
    await page.evaluate(()=>{ idbGet=async()=>null; });
    const clearHistCache=()=>page.evaluate(()=>{ S.histCache={}; S.histPaging={}; idbGet=async()=>null; });
    const waitIdle=()=>page.waitForFunction(()=>!S.histLoading,null,{timeout:30000});
    const resetNet=()=>page.evaluate(()=>{ window.__aborts=[]; });
    const snapshotNet=()=>page.evaluate(()=>window.__aborts.slice());

    // 从 openConv 发起到指定会话内容可见
    const openAndTime=async(conv,expectText)=>page.evaluate(async({conv,expectText})=>{
      const t0=performance.now();
      const p=openConv(conv);
      const ok=await new Promise(resolve=>{
        const tick=()=>{
          const txt=$('#wrap').textContent||'';
          if(txt.includes(expectText) && !$('#convLoading').classList.contains('on')) return resolve(true);
          if(performance.now()-t0>20000) return resolve(false);
          requestAnimationFrame(tick);
        };
        tick();
      });
      const ms=performance.now()-t0;
      await p.catch(()=>{});
      return {ms:Math.round(ms),ok,loadingStill:S.histLoading};
    },{conv,expectText});

    // ① 冷打开(无缓存)
    out.cold=[];
    for(const i of [0,1]){
      await clearHistCache();
      out.cold.push({conv:`probe-${i}`,...(await openAndTime(`probe-${i}`,`回答 ${i}-59`))});
      await waitIdle();
    }

    // ② 120ms 连切 5 条 → 停在 probe-4
    await clearHistCache(); await resetNet();
    const burstGit=git, burstTasks=taskReqs;
    const failedBefore={...net.failed};
    out.burst=await page.evaluate(async()=>{
      const t0=performance.now();
      const promises=[];
      let lastEntry; const switchTimes=[];
      for(const c of ['probe-0','probe-1','probe-2','probe-3','probe-4']){
        lastEntry=performance.now()-t0; switchTimes.push(Math.round(lastEntry));
        promises.push(openConv(c));
        if(c!=='probe-4') await new Promise(r=>setTimeout(r,120));
      }
      const ok=await new Promise(resolve=>{
        const tick=()=>{
          if(($('#wrap').textContent||'').includes('回答 4-59') && !$('#convLoading').classList.contains('on')) return resolve(true);
          if(performance.now()-t0>25000) return resolve(false);
          requestAnimationFrame(tick);
        };
        tick();
      });
      const ms=performance.now()-t0;
      await Promise.all(promises.map(p=>p.catch(()=>{})));
      const txt=$('#wrap').textContent||'';
      return {lastVisibleMs:Math.round(ms),fromLastSwitchMs:Math.round(ms-lastEntry),switchTimes,ok,
        foreign:[...$('#wrap').querySelectorAll('.row')].filter(n=>/回答 [0-3]-/.test(n.textContent||'')).length,
        bubbles:$('#wrap').querySelectorAll('.row.ai .bubble').length};
    });
    out.burst.abortedByPage=await snapshotNet();
    await page.waitForTimeout(450);
    out.burst.git=git-burstGit; out.burst.tasks=taskReqs-burstTasks;
    out.burst.networkCancelled=Object.entries(net.failed).reduce((n,[c,v])=>n+v-(failedBefore[c]||0),0);

    // ③ A→B→A(带进行中回复的 A)
    await page.evaluate(()=>{ S.live['probe-live']=[{type:'start',conv:'probe-live'},{type:'text',conv:'probe-live',seg:0,text:'进行中的草稿正文'}]; });
    await clearHistCache(); await resetNet();
    out.aba=await page.evaluate(async()=>{
      const t0=performance.now();
      const gate=[];
      const p1=openConv('probe-live');
      await new Promise(r=>setTimeout(r,150));
      const p2=openConv('probe-2');
      await new Promise(r=>setTimeout(r,150));
      const p3=openConv('probe-live');
      await new Promise(r=>setTimeout(r,50));
      gate.push({histLoading:S.histLoading,stream:!!S.stream});   // 旧 A 的请求此时已被中止
      await Promise.all([p1,p2,p3].map(p=>p.catch(()=>{})));
      gate.push({histLoading:S.histLoading,stream:!!S.stream});
      await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
      const ai=[...$('#wrap').querySelectorAll('.row.ai .bubble')].map(n=>n.textContent);
      return {ms:Math.round(performance.now()-t0),gate,aiBubbles:ai.length,
        draftBubbles:ai.filter(t=>t.includes('进行中的草稿正文')).length};
    });
    out.aba.abortedByPage=await snapshotNet();
    await waitIdle();

    // ④ 慢请求期间切走:旧 /history 应被页面 abort(浏览器记 requestfailed),状态不标离线
    await clearHistCache(); await resetNet();
    out.abort=await page.evaluate(async()=>{
      openConv('probe-3');
      await new Promise(r=>setTimeout(r,200));
      await openConv('probe-1').catch(()=>{});
      return {serviceState:S.serviceState};
    });
    await page.waitForTimeout(300);
    out.abort.abortedByPage=await snapshotNet();

    // ⑤ 断网:fetch 直接失败 → 标离线、骨架屏收起
    await page.context().setOffline(true);
    out.offline=await page.evaluate(async()=>{
      const t0=performance.now();
      await openConv('probe-offline-x').catch(()=>{});
      return {serviceState:S.serviceState,skeleton:$('#convLoading').classList.contains('on'),histLoading:S.histLoading,ms:Math.round(performance.now()-t0)};
    });
    await page.context().setOffline(false);

    // ⑥ 带进行中回复的历史恢复(冷打开 probe-live)
    await clearHistCache();
    out.pending=await openAndTime('probe-live','进行中的草稿正文');
    out.pending.draftBubbles=await page.evaluate(()=>[...$('#wrap').querySelectorAll('.row.ai .bubble')].filter(n=>n.textContent.includes('进行中的草稿正文')).length);
    await waitIdle();

    // ⑦ 从最近 40 轮补到全部 60 轮,再跳到最早一轮锚点。
    out.earlier=await page.evaluate(async()=>{
      await openConv('probe-0'); await new Promise(r=>setTimeout(r,50));
      const before=(S.histCache['probe-0']||[]).length;
      const t0=performance.now();
      await loadEarlierHistory();
      return {before,after:(S.histCache['probe-0']||[]).length,ms:Math.round(performance.now()-t0),hasMore:S.histPaging['probe-0']?.hasMore};
    });

    // ⑧ 热切回已打开过的会话
    await page.evaluate(()=>openConv('probe-4'));
    await page.evaluate(()=>openConv('probe-0'));
    out.hot=await openAndTime('probe-4','回答 4-59');

    out.anchor=await page.evaluate(async()=>{
      const first=S.histCache['probe-0'][0];
      await openConvAtTurn('probe-0',first.id,first.ts);
      return {found:!!document.querySelector('.chat-turn-anchor') || ($('#wrap').textContent||'').includes('回答 0-0')};
    });
    // 控制旧请求恰好在第三次打开之后完成,明确复现旧 finally 的 A→B→A 竞态。
    out.ownership=await page.evaluate(async()=>{
      const original=api, waiting=[];
      api=(url,opts)=>url.startsWith('/history?')
        ?new Promise(resolve=>waiting.push(()=>resolve(new Response(JSON.stringify({turns:[],has_more:false})))))
        :original(url,opts);
      S.histCache={};
      try{
        const first=openConv('probe-live'); await new Promise(r=>setTimeout(r,30));
        const middle=openConv('probe-2'); await new Promise(r=>setTimeout(r,30));
        const latest=openConv('probe-live'); await new Promise(r=>setTimeout(r,30));
        waiting[0](); waiting[1](); await Promise.all([first,middle]);
        const state={histLoading:S.histLoading,stream:!!S.stream};
        waiting[2](); await latest;
        return state;
      }finally{ api=original; }
    });
    await page.route('**/conv/perm?conv=perm-*',async route=>{
      const conv=new URL(route.request().url()).searchParams.get('conv');
      await new Promise(r=>setTimeout(r,conv==='perm-a'?350:50));
      await route.fulfill({json:{mode:conv==='perm-a'?'full':'standard'}});
    });
    out.permission=await page.evaluate(async()=>{
      S.perm={_conv:S.conv,mode:'full'}; renderPermChip();
      const a=openConv('perm-a');
      const cleared=!$('#permBtn').classList.contains('full');
      await new Promise(r=>setTimeout(r,30));
      await openConv('perm-b'); await a;
      return {cleared,conv:S.perm?._conv,mode:S.perm?.mode};
    });
    out.cachedSwitch=await page.evaluate(async()=>{
      await openConv('probe-4'); delete S.histCache['probe-3'];
      const cold=openConv('probe-3'); await new Promise(r=>setTimeout(r,50));
      const wasLoading=$('#convLoading').classList.contains('on');
      S.git={is_project:true,is_repo:true,name:'旧仓库',branch:'old-branch',path:'/old-repository'};
      $('#gitPop').innerHTML='old-repository';
      const warm=openConv('probe-4');
      $('#convGit').click();
      const state={wasLoading,skeleton:$('#convLoading').classList.contains('on'),oldGit:$('#gitPop').textContent.includes('old-repository')};
      await Promise.all([cold,warm]);
      return state;
    });
    out.requests={git,tasks:taskReqs,historyStarted:net.req,historyFailed:net.failed};
    out.errors=errors;
    assert.deepEqual(errors,[],'页面不应有 JS 报错');
    process.stdout.write(JSON.stringify(out));
  }finally{ await browser.close(); }
})().catch(e=>{ console.error(e); process.exit(1); });
