const {chromium}=require('playwright');
const assert=require('node:assert/strict');

(async()=>{
  const [origin,token,owner,first,output,device]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    const page=await browser.newPage(device==='mobile'?{viewport:{width:390,height:844},isMobile:true,hasTouch:true,userAgent:'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148'}:{});
    const errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(origin);
    await page.fill('#pw',token); await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(async owner=>{await openCallView({agentId:owner,fresh:true});},owner);
    await page.waitForSelector('#callView:not([hidden])');
    const current=await page.evaluate(()=>S.conv);
    assert.notEqual(current,first);
    await page.evaluate(async owner=>{
      for(const filter of ['active','archived','all']){
        S.convFilter=filter; S.homeConvs=null;
        await loadConvs();
        if(convOwnership(S.conv).agent_id!==owner || !findConv(S.conv))
          throw new Error('列表刷新不得丢失当前聊天及所属 Agent：'+filter);
      }
      S.convFilter='active'; await loadConvs();
    },owner);
    const partition=await page.evaluate(async({owner,first})=>{
      const apiRows=(await (await api('/conversations')).json()).conversations;
      const current=S.convs.find(c=>c.conv===S.conv);
      const recent=el('div'); renderRecentTab(recent,false);
      const a=agentById(owner);
      // 即使旧客户端把聊天置顶，稍后列表也不能显示它。
      const wasPinned=current.pinned; current.pinned=true;
      const pinnedChat=pinnedConvs(false).some(row=>row.build()?.dataset.conv===current.conv);
      current.pinned=wasPinned;
      renderEmptyRecent();
      return {apiChats:apiRows.filter(c=>c.conversation_mode==='chat').length,
        taskPresent:apiRows.some(c=>c.title==='必须保留的任务'),
        recentChat:!!recent.querySelector('[data-conv="'+current.conv+'"]'),
        recentTask:recent.textContent.includes('必须保留的任务'),
        agentChat:agentConvs(a).some(c=>c.conversation_mode==='chat'),pinnedChat,
        homeChat:$('#homeRecent').textContent.includes('已保存的旧聊天')};
    },{owner,first});
    assert.deepEqual(partition,{apiChats:0,taskPresent:true,recentChat:false,recentTask:true,agentChat:false,pinnedChat:false,homeChat:false});
    await page.click('#chatHistoryBtn');
    await page.waitForSelector('#chatHistoryList button');
    assert.deepEqual(await page.locator('#chatHistoryList .chat-name').allTextContents(),['已保存的旧聊天']);
    await page.click('#chatHistoryList button');
    await page.waitForFunction(first=>S.conv===first && $('#transcript').textContent.includes('第一段回答'),first);
    await page.click('#clearVoiceCtxBtn');
    await page.waitForFunction(first=>S.conv!==first && S.surface==='call',first);
    const second=await page.evaluate(()=>S.conv);
    assert.equal(await page.locator('#callChatTitle').textContent(),'新聊天');
    assert.ok(!(await page.locator('#transcript').textContent()).includes('第一段回答'));
    await page.click('#chatHistoryBtn');
    await page.waitForSelector('#chatHistoryList button');
    for(const width of [320,375,390,768,1280]){
      await page.setViewportSize({width,height:844});
      const bounds=await page.locator('#chatHistoryDialog').boundingBox();
      assert.ok(bounds.x>=0 && bounds.x+bounds.width<=width+1,JSON.stringify({width,bounds}));
    }
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#chatHistoryDialog').evaluate(d=>d.open),false);
    await page.screenshot({path:output+'/chat-history.png'});
    // 回放导航必须恢复同一 Agent/聊天，不能跳回 general 或截断前进栈。
    await page.evaluate(()=>newChatIn(null,false));
    await page.waitForFunction(()=>S.surface==='chat');
    await page.evaluate(()=>navGo(-1));
    await page.waitForFunction(second=>S.surface==='call' && S.conv===second,second);
    assert.ok(await page.evaluate(()=>NAV.idx<NAV.stack.length-1));
    // 聊天入口仍可进入所属 Agent 的设置，不能丢失职责面板。
    await page.click('#chatAgentBtn');
    await page.waitForFunction(()=>S.surface==='chat');
    assert.equal(await page.evaluate(()=>currentAgent()?.id),owner);
    await page.evaluate(async({owner,second})=>{await openCallView({agentId:owner,conv:second});},{owner,second});
    // 延迟打开后立即离开，迟到的响应不能抢回页面。
    let release;
    await page.route('**/chats/open',async route=>{await new Promise(r=>release=r);await route.continue();});
    await page.evaluate(owner=>{openCallView({agentId:owner,fresh:true});},owner);
    await page.waitForTimeout(80);
    await page.evaluate(()=>newChatIn(null,false));
    const task=await page.evaluate(()=>S.conv);
    release();
    await page.waitForTimeout(250);
    assert.equal(await page.evaluate(()=>S.conv),task);
    assert.equal(await page.evaluate(()=>S.surface),'chat');
    const main=await page.evaluate(owner=>{
      const a=agentById(owner);
      S.sideTab='agents'; S.expanded.add(agentKey(a)); renderConvs();
      const row=document.querySelector('.agmain[data-conv="'+a.main_conv+'"]');
      if(!row || row.querySelector('.ct').textContent!=='主会话')
        throw new Error('Agent 固定首行必须显示主会话');
      row.click();
      return a.main_conv;
    },owner);
    await page.waitForFunction(main=>S.conv===main && S.surface==='chat' && !S.histLoading,main);
    await page.evaluate(owner=>openAgentMain(agentById(owner)),owner);
    assert.equal(await page.evaluate(()=>S.conv),main,'再次点 Agent 不得创建新聊天');
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true}));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
