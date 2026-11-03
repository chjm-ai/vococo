// Chromium 跑真实页面；接口夹具隔离生产，可回放真机冒烟产出的 WebSink 事件。
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'../vococo/gateway/adapters/web_static');
const origin='http://checklist-fixture.local',output='/tmp/vococo-checklist-browser';
const items=[{id:'1',subject:'分析访谈纪要，提炼核心洞察（测试数据）',status:'in_progress'},
  {id:'2',subject:'创建飞书文档骨架',status:'pending'},
  {id:'3',subject:'补充关键业务启发内容',status:'pending'},
  {id:'4',subject:'生成产品建议画板',status:'pending'}];
(async()=>{
  fs.mkdirSync(output,{recursive:true});
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  let histories={source:[],other:[]};
  await page.addInitScript(()=>{window.EventSource=class extends EventTarget{close(){}};});
  await page.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(url.origin!==origin)return route.abort();
    const file=path.join(root,url.pathname==='/'?'index.html':url.pathname);
    if(file.startsWith(root+path.sep)&&fs.existsSync(file)&&fs.statSync(file).isFile())
      return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html'});
    let data={};
    if(url.pathname==='/history')data={turns:histories[url.searchParams.get('conv')]||[],has_more:false};
    if(url.pathname==='/tasks')data=[];
    if(url.pathname==='/conversations')data={main:{},conversations:[{conv:'source',title:'执行清单测试'},{conv:'other',title:'其他聊天'}]};
    if(url.pathname==='/conv/perm')data={mode:'standard'};
    return route.fulfill({json:data});
  });
  const boot=()=>page.evaluate(async()=>{
    S.token='fixture';$('#login').style.display='none';$('#app').hidden=false;$('#chatMain').hidden=false;$('#callView').hidden=true;
    S.convs=[{conv:'source',title:'执行清单测试'},{conv:'other',title:'其他聊天'}];renderConvs();await openConv('source');
  });
  const emit=data=>page.evaluate(e=>handleEvent({...e,conv:'source'}),data);
  try{
    await page.goto(origin);await boot();
    await emit({type:'start',id:1});await emit({type:'checklist',items,id:2});
    assert.equal(await page.locator('.execution-checklist').count(),1);
    assert.match(await page.locator('.execution-checklist summary').innerText(),/0\/4/);
    assert.equal(await page.locator('#chatTaskBar').isHidden(),true,'不进入后台任务条');
    assert.equal(await page.locator('.execution-checklist').getAttribute('open'),null,'默认单行收起');
    assert.equal(await page.locator('#composer #checklistDock .execution-checklist').count(),1,'位于输入框上方');
    assert.equal(await page.locator('#wrap .execution-checklist').count(),0,'聊天气泡不再重复展示');
    await emit({type:'checklist',items:[{...items[0],status:'completed'},...items.slice(1)],id:3});
    assert.equal(await page.locator('.execution-checklist').count(),1,'更新复用一张卡');
    assert.equal(await page.locator('.execution-checklist').getAttribute('open'),null,'更新不强制展开');
    await page.locator('.execution-checklist summary').focus();await page.keyboard.press('Enter');
    assert.equal(await page.locator('.execution-checklist').getAttribute('open'),'','原生键盘展开');
    await emit({type:'checklist',items,id:4});
    assert.equal(await page.locator('.execution-checklist').getAttribute('open'),'','更新保留展开');
    assert.equal(await page.locator('.execution-checklist summary').evaluate(el=>el===document.activeElement),true,'更新保留键盘焦点');
    await page.locator('.execution-checklist summary').click();
    await page.evaluate(async()=>{await openConv('other');});
    assert.equal(await page.locator('.execution-checklist').count(),0,'不串到其他聊天');
    await emit({type:'checklist',items:[{...items[0],status:'completed'},...items.slice(1)],id:5});
    await page.evaluate(async()=>{await openConv('source');});
    assert.equal(await page.locator('.execution-checklist').count(),1,'切回重建不重复');
    assert.match(await page.locator('.execution-checklist summary').innerText(),/1\/4/);
    await emit({type:'checklist',items,id:6});
    await page.evaluate(()=>updateEmpty());
    for(const width of [320,375,390,768,1280]){
      await page.setViewportSize({width,height:900});
      await page.evaluate(()=>{closeSidebar();setSidebarCollapsed(true);});
      await page.waitForTimeout(500);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`${width} 页面无横向溢出`);
      assert.equal(await page.locator('.execution-checklist').evaluate(el=>el.scrollWidth<=el.clientWidth),true,`${width} 清单无横向溢出`);
      const box=await page.locator('.execution-checklist').boundingBox(),dock=await page.locator('#checklistDock').boundingBox();
      assert.ok(Math.abs(box.x+box.width/2-dock.x-dock.width/2)<2,JSON.stringify({width,box,dock}));
      if(width===1280)assert.ok(box.width<dock.width,'不撑满输入区');
      if(width===390||width===1280)await page.screenshot({path:path.join(output,`checklist-${width}.png`),fullPage:true});
      await page.locator('.execution-checklist summary').click();
      assert.equal(await page.locator('.execution-checklist .todo').isVisible(),true,'点击展开列表');
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`${width} 展开无溢出`);
      if(width===390)await page.screenshot({path:path.join(output,'checklist-expanded-390.png'),fullPage:true});
      await page.locator('.execution-checklist summary').click();
    }
    const hostile=[{id:'1',subject:'<img src=x onerror=alert(1)>'+('长标题'.repeat(60)),status:'in_progress'}];
    await emit({type:'checklist',items:hostile,id:7});
    assert.equal(await page.locator('.execution-checklist img').count(),0,'标题只当文本');
    await page.setViewportSize({width:320,height:900});
    assert.equal(await page.locator('.execution-checklist').evaluate(el=>el.scrollWidth<=el.clientWidth),true,'长文本不溢出');
    await emit({type:'checklist',items:[],id:8});
    assert.equal(await page.locator('.execution-checklist').count(),0,'删除最后一项移除卡片');
    await emit({type:'checklist',items,id:9});
    await emit({type:'cancelled',id:10});
    assert.match(await page.locator('.execution-checklist summary').innerText(),/0\/4/,'中断不再显示进行中');
    assert.equal(await page.locator('.execution-checklist li.done').count(),0,'中断不伪造完成');
    histories.source=[{id:101,ts:Date.now()/1000,user:'测试',assistant:'已中断',pending:false,events:[{type:'checklist',items,session_id:'sdk'}]}];
    await page.reload();await boot();
    assert.equal(await page.locator('.execution-checklist').count(),1,'刷新从历史快照恢复');
    assert.match(await page.locator('.execution-checklist summary').innerText(),/0\/4/);
    await page.evaluate(()=>{
      const newer={id:102,events:[{type:'checklist',items:[]}]};
      restoreHistoryChecklist([...S.histCache.source,newer]);
    });
    assert.equal(await page.locator('.execution-checklist').count(),0,'最新空快照不复活旧清单');
    await page.evaluate(()=>restoreHistoryChecklist(S.histCache.source));
    await page.evaluate(()=>document.documentElement.dataset.theme='dark');
    await page.screenshot({path:path.join(output,'checklist-dark.png'),fullPage:true});
    const smokePath=process.env.CHECKLIST_SMOKE_ARTIFACT;
    if(smokePath){
      const smoke=JSON.parse(fs.readFileSync(smokePath,'utf8'));
      histories.source=smoke.turns;
      await page.reload();await boot();
      assert.equal(await page.locator('.execution-checklist').count(),1,'只显示最新历史清单');
      assert.match(await page.locator('.execution-checklist summary').last().innerText(),/已完成\s+1\/1/);
      await page.evaluate(()=>{S.stream=null;$('#wrap').replaceChildren();});
      for(const event of smoke.events)await emit(event);
      assert.equal(await page.locator('.execution-checklist li.done').last().count(),1,'真实 SSE 回执回放到完成');
    }
    assert.deepEqual(errors,[],'无浏览器脚本异常');
    console.log(JSON.stringify({ok:true,widths:[320,375,390,768,1280],realSmokeReplay:!!smokePath,screenshots:output}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
