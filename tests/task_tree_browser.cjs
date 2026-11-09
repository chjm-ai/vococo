// 真实页面与 Chromium，所有请求由本地夹具拦截，不启动服务、不接触生产。
const fs=require('node:fs'), path=require('node:path'), assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'../vococo/gateway/adapters/web_static');
const origin='http://task-tree-fixture.local';
const child={id:'one',title:'子任务 <img onerror=alert(1)>',status:'waiting_children',progress_note:'等待核验',session_key:'task:one',agent_id:'general',model:'fixture-model',receipt_state:'pending',children:[
  {id:'two',title:'嵌套核验',status:'running',progress_note:'核对中',session_key:'task:two',receipt_state:'delivered',children:[]}
]};
const convs=[{conv:'source',title:'父任务'},{conv:'independent',title:'独立主题',relation:'independent'},
  {conv:'task:one',title:'侧栏不应出现的子任务',relation:'child',parent_session_key:'source',pinned:true,task_status:'running',pending_review:true}];
const voiceTasks=[{conv:'task:voice',task_id:'voice',title:'侧栏不应出现的语音子任务',relation:'child',pinned:true,parent_session_key:'source'},
  {conv:'task:legacy',task_id:'legacy',title:'保留旧独立任务',relation:'independent'}];
let current=structuredClone(child), fails=false, slow=false, requests=0, release=null, treeState='waiting', stopped=null;
(async()=>{
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  const page=await browser.newPage({viewport:{width:1280,height:900}}), errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.addInitScript(()=>{
    window.fixtureStreams=[];
    window.EventSource=class extends EventTarget{constructor(url){super();this.url=url;fixtureStreams.push(this);}close(){}};
  });
  await page.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(url.origin!==origin) return route.abort();
    const file=path.join(root,url.pathname==='/'?'index.html':url.pathname);
    if(file.startsWith(root+path.sep)&&fs.existsSync(file)&&fs.statSync(file).isFile())
      return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html'});
    let data={};
    if(url.pathname==='/tasks/tree'){
      requests++; const conv=url.searchParams.get('conv');
      data={session_key:conv.startsWith('task:')?conv:'web:'+conv,parent_session_key:conv==='task:one'?'web:source':conv==='task:two'?'task:one':null,
        state:conv==='source'?treeState:null,
        children:conv==='source'?[structuredClone(current)]:conv==='task:one'?structuredClone(current.children):[]};
      if(slow&&conv==='source'){slow=false;await new Promise(resolve=>{release=resolve;});}
      if(fails) return route.fulfill({status:503,json:{error:'离线夹具'}});
    }
    if(url.pathname==='/abort'){stopped=route.request().postDataJSON().conv;treeState='cancelled';data={ok:true,stopped:true};}
    if(url.pathname==='/conversations') data={main:{},conversations:convs};
    if(url.pathname==='/voice/sidebar') data={tasks:voiceTasks};
    if(url.pathname==='/history') data={turns:[],has_more:false};
    if(url.pathname==='/tasks') data=[];
    if(url.pathname==='/conv/search') data={results:[{conv:'task:search',title:'搜索子任务',relation:'child',parent_session_key:'source'}]};
    if(url.pathname==='/conv/perm') data={mode:'standard'};
    return route.fulfill({json:data});
  });
  const boot=()=>page.evaluate(async({convs,voiceTasks})=>{
    S.token='fixture';$('#login').style.display='none';$('#app').hidden=false;$('#chatMain').hidden=false;$('#callView').hidden=true;
    S.convs=convs;S.voiceSidebar={tasks:voiceTasks};S.agents=[{id:'general',name:'总助理',main_conv:'main'}];
    S.convFilter='all';S.sideTab='recent';S.expanded.add('agent:general');
    window.connectTaskStreamOnce();await openConv('source');
  },{convs,voiceTasks});
  const refresh=()=>page.evaluate(()=>taskTree.schedule());
  try{
    await page.goto(origin);await boot();
    await page.locator('[data-task-id="one"]').waitFor();
    assert.match(await page.locator('#taskTree').innerText(),/等待子任务/);
    assert.match(await page.locator('#taskTree').innerText(),/总助理 · fixture-model · 待回传/);
    assert.equal(await page.locator('#taskTree img').count(),0,'标题不作为 HTML 执行');
    assert.equal(await page.locator('#convBody').getByText('侧栏不应出现的子任务',{exact:true}).count(),0);
    assert.equal(await page.locator('#convBody').getByText('侧栏不应出现的语音子任务',{exact:true}).count(),0);
    assert.equal(await page.locator('#convBody').getByText('独立主题',{exact:true}).count(),1);
    for(const tab of ['agents','pinned','recent']){
      await page.evaluate(tab=>{S.sideTab=tab;renderConvs();},tab);
      assert.doesNotMatch(await page.locator('#convBody').innerText(),/侧栏不应出现/);
    }
    assert.equal(await page.evaluate(()=>agentUnread(S.agents[0])),0,'Agent 未读聚合不含 child');
    await page.evaluate(()=>{S.conv='independent';renderEmptyRecent();S.conv='source';});
    assert.doesNotMatch(await page.locator('#homeRecent').innerText(),/侧栏不应出现/);
    const summary=page.locator('#taskTree > details > summary');
    await summary.focus();await page.keyboard.press('Enter');
    assert.equal(await summary.evaluate(node=>node.parentElement.open),false,'键盘折叠');
    current.progress_note='新的进度';current.receipt_state='delivered';
    const before=requests;
    await page.evaluate(()=>{for(let i=0;i<30;i++)handleEvent({type:'agent_run',agent_id:'missing'});});
    await page.waitForTimeout(650);
    assert.equal(requests-before,1,'连续事件合并一个快照请求');
    assert.equal(await summary.evaluate(node=>node.parentElement.open),false,'更新保留折叠状态');
    await summary.focus();await page.keyboard.press('Enter');
    await page.locator('[data-task-id="one"] > .task-tree-note').getByText('新的进度',{exact:true}).waitFor();
    await page.locator('[data-task-id="one"] > button').click();
    await page.waitForFunction(()=>S.conv==='task:one');
    await page.locator('#taskTree > button').getByText('返回父任务').waitFor();
    await page.locator('[data-task-id="two"] > button').click();
    await page.waitForFunction(()=>S.conv==='task:two');
    await page.locator('#taskTree > button').click();
    await page.waitForFunction(()=>S.conv==='task:one');
    await page.locator('#taskTree > button').click();
    await page.waitForFunction(()=>S.conv==='source');
    await page.locator('[data-task-id="one"]').waitFor();
    await page.evaluate(()=>{navGo(-1);});
    await page.waitForFunction(()=>S.conv==='task:one');
    await page.evaluate(()=>{navGo(1);});
    await page.waitForFunction(()=>S.conv==='source');
    await page.locator('[data-task-id="one"]').waitFor();
    for(const status of ['queued','running','waiting_children','done','failed','cancelled']){
      current.status=status;current.receipt_state='processed';await refresh();
      await page.waitForFunction(status=>document.querySelector('[data-task-id="one"] > .task-tree-row > .task-tree-status')?.dataset.status===status,status);
    }
    assert.match(await page.locator('[data-task-id="one"] > .task-tree-meta').innerText(),/已处理/);
    fails=true;await refresh();await page.locator('.task-tree-error').waitFor();
    assert.match(await page.locator('#taskTree').innerText(),/上次快照/);
    fails=false;await page.getByRole('button',{name:'重试',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('.task-tree-error'));
    await page.evaluate(async()=>{await openConv('independent');});
    await page.waitForFunction(()=>document.querySelector('#taskTree').hidden);
    slow=true;await page.evaluate(()=>{openConv('source');});
    await page.waitForTimeout(100);assert.ok(release);
    await page.evaluate(()=>openConv('independent'));release();release=null;
    await page.waitForTimeout(150);
    assert.equal(await page.locator('#taskTree').isHidden(),true,'丢弃旧会话迟到快照');
    await page.evaluate(()=>{openSearchModal();SM.results=[{conv:'task:search',title:'搜索子任务',relation:'child',parent_session_key:'source'}];SM.sel=0;renderSearchResults('');});
    assert.match(await page.locator('#smList').innerText(),/子任务.*父任务/s);
    await page.locator('#smList .smrow').click();
    await page.waitForFunction(()=>S.conv==='task:search');
    assert.equal(await page.locator('#convTitle').innerText(),'搜索子任务');
    await page.locator('#taskTree > button').click();
    await page.waitForFunction(()=>S.conv==='source');
    await page.locator('[data-task-id="one"]').waitFor();
    current.progress_note='断线后快照恢复';
    await page.evaluate(()=>{fixtureStreams.find(es=>es.url.startsWith('/tasks/stream')).onopen();});
    await page.locator('[data-task-id="one"] > .task-tree-note').getByText('断线后快照恢复').waitFor();
    current.progress_note='任务事件更新';
    await page.evaluate(()=>{fixtureStreams.find(es=>es.url.startsWith('/tasks/stream')).dispatchEvent(new MessageEvent('task_update',{data:JSON.stringify({id:'one',relation:'child',status:'running'})}));});
    await page.locator('[data-task-id="one"] > .task-tree-note').getByText('任务事件更新').waitFor();
    await page.waitForTimeout(700);const idle=requests;await page.waitForTimeout(1100);assert.equal(requests,idle,'闲置不轮询');
    // 通话视图暂时改写 S.conv，旧请求丢弃后，返回仍应主动补快照。
    slow=true;await page.evaluate(()=>{openConv('source');});
    await page.waitForTimeout(100);assert.ok(release);
    await page.evaluate(()=>openCallView());release();release=null;
    await page.waitForTimeout(100);current.progress_note='通话返回恢复';
    await page.evaluate(()=>closeCallView());
    await page.locator('[data-task-id="one"] > .task-tree-note').getByText('通话返回恢复').waitFor();
    await page.evaluate(()=>{$('#findBar').hidden=false;_updateChromePad();});
    await page.waitForTimeout(300);
    assert.equal(await page.locator('#taskTree').evaluate(node=>node.getBoundingClientRect().top>=document.querySelector('header.bar').getBoundingClientRect().bottom),true,'展开查找条后任务树不被顶栏挡住');
    await page.evaluate(()=>{$('#findBar').hidden=true;_updateChromePad();});
    const beforeDraft=requests;
    await page.evaluate(async()=>{S.convs.push({conv:'local-fixture',draft:true,turns:0});await openConv('local-fixture');await sendCmd('/fixture');});
    await page.waitForTimeout(600);
    assert.ok(requests>beforeDraft,'草稿转正后首次拉取树快照');
    current.progress_note='转正后事件仍能刷新';await page.evaluate(()=>handleEvent({type:'agent_run',agent_id:'missing'}));
    await page.waitForTimeout(600);assert.ok(requests>beforeDraft+1,'转正后继续响应事件');
    await page.evaluate(()=>openConv('source'));await page.locator('[data-task-id="one"]').waitFor();
    await page.screenshot({path:'/tmp/task-tree-normal-desktop.png',fullPage:true});
    current.title='特别长的任务标题'.repeat(35);current.progress_note='没有断词的进度'.repeat(35);await refresh();
    await page.waitForFunction(()=>document.querySelector('.task-tree-title')?.textContent.length>200);
    for(const theme of ['dark','light']) for(const width of [320,375,390,768,1280]){
      await page.setViewportSize({width,height:900});
      await page.evaluate(theme=>{document.documentElement.dataset.theme=theme;closeSidebar();setSidebarCollapsed(true);},theme);
      await page.waitForTimeout(250);
      assert.equal(await page.locator('#taskTree').evaluate(node=>node.scrollWidth<=node.clientWidth),true,`${theme} ${width} 树无溢出`);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`${theme} ${width} 页面无溢出`);
      if(width===390||width===1280)await page.screenshot({path:`/tmp/task-tree-${theme}-${width}.png`,fullPage:true});
    }
    await page.reload();await boot();await page.locator('[data-task-id="one"]').waitFor();
    assert.match(await page.locator('#taskTree').innerText(),/已处理/,'重载重新拉取持久化快照');
    await page.getByRole('button',{name:'停止整个任务树',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('[data-tree-focus="stop"]'));
    assert.equal(stopped,'source','停止请求使用前端会话标识');
    assert.deepEqual(errors,[],'页面无 JavaScript 异常');
    console.log('PASS task tree: navigation, nested return, search, sidebar aggregation, statuses, receipts, keyboard, throttle, stale response, reconnect, reload, no polling, 10 viewport/theme layouts');
  }catch(error){console.error(error);throw error;}
  finally{if(release)release();await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
