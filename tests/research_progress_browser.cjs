// 真实页面与 Chromium；接口夹具隔离生产、数据库及付费研究。
const fs=require('node:fs'), path=require('node:path'), assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(__dirname,'../vococo/gateway/adapters/web_static');
const origin='http://research-fixture.local', output=process.env.RESEARCH_PROGRESS_ARTIFACTS || '/tmp/vococo-research-progress-browser';
const now=Date.now()/1000;
const progress={id:'research-one',stage:'collect',status:'running',task_status:'running',title:'电池回收行业公开资料研究',created_at:now-80,updated_at:now,deadline:now+220,
  usage:{searches:3,fetches:2,tokens:4200},budget:{seconds:300,searches:12,fetches:8,tokens:20000},limited_stages:[],checkpoints:{plan:'研究范围：核对公开政策与行业报告。'},
  sources:[{id:'s1',title:'已取得的行业报告',url:'https://example.com/report',fetched_at:now,claims_count:2},{id:'s2',title:'不安全来源',url:'javascript:alert(1)',claims_count:0}],
  activity:[{id:'a1',stage:'collect',tool:'web_search',query:'电池回收 2026 政策',status:'done',started_at:now-30,finished_at:now-20,summary:'找到 5 条公开网页'},
    {id:'a2',stage:'collect',tool:'web_fetch',url:'https://example.com/report',status:'running',started_at:now-10}]};
const task={id:progress.id,title:progress.title,kind:'research',origin:'chat',source_session_key:'web:source',conversation_id:'source',dispatch_chat_id:'source',status:'running',created_at:now-80,updated_at:now,progress_note:'搜集资料 · 2个来源'};
(async()=>{
  fs.mkdirSync(output,{recursive:true});
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  const page=await browser.newPage({viewport:{width:1280,height:900}}), errors=[], requests=[],stops=[];
  const histories={source:[],other:[]};
  let tasks=[task], currentProgress=progress, progressFails=true;
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{
    window.fixtureEventSources=[];
    window.EventSource=class extends EventTarget{
      constructor(url){super();this.url=url;window.fixtureEventSources.push(this);}
      close(){}
    };
  });
  await page.route('**/*',async route=>{
    const url=new URL(route.request().url()); requests.push(url.href);
    if(url.origin!==origin)return route.abort();
    const file=path.join(root,url.pathname==='/'?'index.html':url.pathname);
    if(file.startsWith(root+path.sep)&&fs.existsSync(file)&&fs.statSync(file).isFile()) return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html'});
    let data={};
    if(url.pathname==='/history')data={turns:histories[url.searchParams.get('conv')]||[],has_more:false};
    if(url.pathname==='/tasks')data=tasks;
    if(url.pathname.endsWith('/research/progress')){
      if(progressFails)return route.fulfill({status:503,json:{error:'隔离故障夹具'}});
      data=currentProgress;
    }
    if(url.pathname.endsWith('/stop'))stops.push({url:url.pathname,method:route.request().method(),token:route.request().headers()['x-auth-token']});
    if(url.pathname==='/conversations')data={main:{},conversations:[{conv:'source',title:'研究来源会话'},{conv:'other',title:'其他会话'}]};
    if(url.pathname==='/conv/perm')data={mode:'standard'};
    return route.fulfill({json:data});
  });
  const boot=()=>page.evaluate(async()=>{
    S.token='fixture'; window.connectTaskStreamOnce(); $('#login').style.display='none'; $('#app').hidden=false; $('#chatMain').hidden=false; $('#callView').hidden=true;
    S.convs=[{conv:'source',title:'研究来源会话'},{conv:'other',title:'其他会话'}];renderConvs();await openConv('source');await window.refreshTaskStatus();
  });
  try{
    await page.goto(origin);await boot();
    await page.locator('#chatTaskBar').getByText(progress.title,{exact:true}).waitFor();
    assert.equal(await page.locator('.research-card').count(),0,'进度接口失败时仍保留任务栏入口');
    progressFails=false;await page.waitForTimeout(50);await page.evaluate(()=>window.refreshTaskStatus());
    await page.locator('.research-card').waitFor({timeout:2500});
    assert.equal(await page.locator('.research-card').count(),1,'无首次 SSE 也能恢复单张卡');
    assert.equal(await page.locator('#chatTaskBar').isHidden(),true,'当前会话研究不重复进入底部状态条');
    await page.evaluate(t=>{
      const es=window.fixtureEventSources.find(es=>es.url.startsWith('/tasks/stream'));
      if(!es)throw Error('任务事件流未连接');
      // 兼容缺少来源字段的摘要，必须合并旧行而不是覆盖。
      const {source_session_key,conversation_id,...summary}=t;
      window.fixtureTaskUpdate=summary=>es.dispatchEvent(new MessageEvent('task_update',{data:JSON.stringify(summary)}));
      window.fixtureTaskUpdate({...summary,progress_note:'摘要进度更新'});
    },task);
    assert.equal(await page.locator('#chatTaskBar').isHidden(),true,'task_update 摘要不得让研究重复进入底栏');
    await page.locator('.research-card summary').click();
    await page.getByText('电池回收 2026 政策',{exact:false}).waitFor();
    assert.equal(await page.locator('.research-card a[href^="javascript:"]').count(),0,'来源链接只允许 HTTP(S)');
    assert.equal(await page.locator('.research-card a[target="_blank"]').first().getAttribute('rel'),'noopener noreferrer');
    assert.match(await page.locator('.research-card').innerText(),/搜索 3\/12/);
    assert.match(await page.locator('.research-card').innerText(),/抓取 2\/8/);
    await page.evaluate(p=>{
      handleEvent({type:'start',conv:'source',id:1});handleEvent({type:'text',conv:'source',seg:0,text:'普通回复继续生成',id:2});window.fixtureStream=S.stream;
      for(let i=0;i<3;i++)handleEvent({type:'research_update',conv:'source',task_id:p.id,title:p.title,status:'running',progress:p,progress_note:'搜集资料 · 2个来源',id:3+i});
    },progress);
    assert.equal(await page.locator('.research-card').count(),1);
    assert.equal(await page.locator('.research-card details').getAttribute('open'),'','进度更新保留展开状态');
    assert.equal(await page.locator('#wrap .row.ai').filter({hasText:'搜集资料 · 2个来源'}).count(),0,'进度不作为普通消息重复出现');
    await page.locator('.research-cancel').click();
    await page.waitForTimeout(50);
    assert.deepEqual(stops,[{url:'/tasks/research-one/stop',method:'POST',token:'fixture'}]);
    for(const width of [320,375,390,768,1280]){
      await page.setViewportSize({width,height:900});
      await page.evaluate(()=>{closeSidebar();setSidebarCollapsed(true);});
      await page.waitForTimeout(250);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`${width} 页面无横向溢出`);
      assert.equal(await page.locator('.research-card').evaluate(el=>el.scrollWidth<=el.clientWidth),true,`${width} 卡片无横向溢出`);
      if(width===390||width===1280)await page.screenshot({path:path.join(output,`research-${width}.png`),fullPage:true});
    }
    currentProgress={...progress,stage:'report',status:'done',task_status:'done',limited_stages:['review']};tasks=[{...task,status:'done'}];
    const report={id:202,ts:now,user:'',assistant:'有限研究报告：核验预算不足。',pending:false,events:[{type:'external_result',key:'research:research-one:2'}]};histories.source=[report];
    await page.evaluate(({p,report})=>{
      handleEvent({type:'research_update',conv:'source',task_id:p.id,progress:p,status:'done'});
      for(let i=0;i<2;i++)handleEvent({type:'research_result',conv:'source',task_id:p.id,text:report.assistant,turn_id:report.id});
      if(S.stream!==window.fixtureStream||!S.live.source)throw Error('研究中断了普通回复');
      handleEvent({type:'text',conv:'source',seg:0,text:'普通回复继续生成，研究已独立交付',id:8});
    },{p:currentProgress,report});
    assert.equal(await page.locator('.research-card').count(),1,'终态保留研究过程');
    assert.equal(await page.locator('#wrap .row.ai').filter({hasText:report.assistant}).count(),1,'报告不重复');
    assert.match(await page.locator('.research-card').innerText(),/有限报告/);
    assert.doesNotMatch(await page.locator('.research-card summary').innerText(),/全部完成/);
    await page.evaluate(async()=>{await openConv('other');await openConv('source');await window.refreshTaskStatus();});
    await page.locator('.research-card').waitFor();
    assert.equal(await page.locator('#wrap .row.ai').filter({hasText:report.assistant}).count(),1,'历史报告与实时报告去重');
    assert.equal(await page.locator('.research-card').count(),1);
    await page.reload();await boot();await page.locator('.research-card').waitFor();
    assert.match(await page.locator('.research-card').innerText(),/有限报告/,'刷新保留过程与受限状态');
    tasks=[task,{id:'ordinary',title:'普通后台任务',origin:'chat',dispatch_chat_id:'source',status:'running',progress_note:'处理中'},{...task,id:'elsewhere',title:'其他会话研究',source_session_key:'web:other',conversation_id:'other'}];
    await page.evaluate(()=>window.refreshTaskStatus());
    await page.locator('#chatTaskBar').getByText('普通后台任务',{exact:true}).waitFor();
    await page.locator('#chatTaskBar').getByText('其他会话研究',{exact:true}).waitFor();
    assert.equal(await page.locator('#chatTaskBar').getByText(progress.title,{exact:true}).count(),0);
    currentProgress={...progress,status:'failed',task_status:'failed'};
    await page.evaluate(p=>handleEvent({type:'research_update',conv:'source',task_id:p.id,progress:p,status:'failed'}),currentProgress);
    assert.match(await page.locator('.research-card summary').innerText(),/失败/);
    assert.equal(await page.locator('.research-cancel').count(),0);
    const resumed={id:203,assistant:'恢复后的新报告'};
    await page.evaluate(report=>{
      for(let i=0;i<2;i++)handleEvent({type:'research_result',conv:'source',task_id:'research-one',turn_id:report.id,text:report.assistant});
    },resumed);
    assert.equal(await page.locator('#wrap .row.ai').filter({hasText:resumed.assistant}).count(),1,'同任务新 turn 报告展示一次');
    assert.equal(await page.locator('#wrap .row.ai').filter({hasText:report.assistant}).count(),1,'原报告仍保留');
    tasks=[{...task,source_session_key:'custom-shared-session',conversation_id:'main',dispatch_chat_id:'main'}];
    for(const conv of ['main','voice-chat:main']){
      await page.evaluate(async conv=>{S.conv=conv;await window.refreshTaskStatus();},conv);
      await page.waitForFunction(conv=>document.querySelector('.research-card')?.dataset.conv===conv,conv);
      assert.equal(await page.locator('#chatTaskBar').isHidden(),true,`${conv} 使用规范化会话恢复研究卡，不重复底栏`);
      await page.evaluate(p=>handleEvent({type:'research_update',conv:'main',task_id:p.id,progress:{...p,updated_at:p.updated_at+10}}),progress);
      assert.equal(await page.locator('.research-card').count(),1,`${conv} 收到 main SSE 后研究入口仍可见`);
      assert.equal(await page.locator('.research-card').getAttribute('data-conv'),conv);
      assert.equal(await page.locator('#chatTaskBar').isHidden(),true,`${conv} SSE 不重新引入重复底栏`);
    }
    assert.deepEqual(errors,[]);assert.ok(requests.every(url=>url.startsWith(origin+'/')));
    console.log(JSON.stringify({ok:true,widths:[320,375,390,768,1280],errors,output,browser:await browser.version()}));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
