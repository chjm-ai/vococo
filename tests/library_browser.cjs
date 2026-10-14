// 用真实 APP 验证作品库；隔离接口夹具，不访问生产数据或调用模型。
const {chromium}=require('playwright');
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../vococo/gateway/adapters/web_static');
const origin='http://localhost:8401';
const image=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS9sAAAAASUVORK5CYII=','base64');
async function libraryBounds(page){
  return page.evaluate(()=>{
    const selectors=['#libraryView','.lib-content','.lib-heading','.lib-status','.lib-type','.lib-search','#libraryWorks'];
    const bounds=selectors.map(selector=>{
      const node=document.querySelector(selector), box=node.getBoundingClientRect();
      return {selector,x:box.x,width:box.width,clientWidth:node.clientWidth,overflow:node.scrollWidth>node.clientWidth+1};
    });
    const cards=[...document.querySelectorAll('.lib-card')].map(node=>{
      const box=node.getBoundingClientRect(),cover=node.querySelector('.lib-cover').getBoundingClientRect();
      return {width:box.width,height:box.height,coverWidth:cover.width,coverHeight:cover.height};
    });
    return {bounds,cards,columns:getComputedStyle(document.querySelector('#libraryWorks')).gridTemplateColumns.split(' ').length};
  });
}
function assertStableLayout(actual,baseline,label){
  for(const [index,box] of actual.bounds.entries()){
    assert.ok(!box.overflow,label+' '+box.selector+' 无横向溢出');
    assert.ok(Math.abs(box.x-baseline.bounds[index].x)<1,label+' '+box.selector+' 左边界稳定');
    assert.ok(Math.abs(box.width-baseline.bounds[index].width)<1,label+' '+box.selector+' 宽度稳定');
  }
}
async function clickLibraryFilter(page,selector,count){
  // 窄屏侧栏是遮罩浮层；触发原按钮事件，不改 CSS 或内容布局。
  await page.locator(selector).evaluate(button=>button.click());
  await page.waitForFunction(count=>!LIB.loading && LIB.items.length===count,count);
}
function assertCompactCards(layout,label){
  assert.ok(layout.bounds[1].width<=1200,label+' 内容最大宽度 1200');
  assert.ok(Math.abs(layout.bounds[1].width-Math.min(1200,layout.bounds[0].clientWidth))<1,label+' 内容显式宽度不随作品数收缩');
  const gridWidth=layout.bounds.at(-1).width;
  assert.equal(layout.columns,Math.max(1,Math.floor((gridWidth+12)/(160+12))),label+' 按实际空间填充更多小卡列');
  assert.ok(layout.cards[0].height<360,label+' 卡片收紧');
  assert.ok(layout.cards[0].coverHeight/layout.cards[0].coverWidth<.6,label+' 封面收紧');
}
async function verifyLayoutStates(page,label,holdList,setFailure){
  await clickLibraryFilter(page,'[data-lib-action="reset"]',43);
  await page.waitForFunction(()=>$('#libraryWorks img')?.src.startsWith('blob:'));
  const baseline=await libraryBounds(page);
  assertStableLayout(baseline,baseline,label+' 多件');
  assertCompactCards(baseline,label);
  await clickLibraryFilter(page,'[data-state="pinned"]',1);
  const single=await libraryBounds(page);
  assertStableLayout(single,baseline,label+' 单件');
  assert.ok(Math.abs(single.cards[0].width-baseline.cards[0].width)<1,label+' 单件不放大');
  await clickLibraryFilter(page,'[data-state="hidden"]',0);
  assertStableLayout(await libraryBounds(page),baseline,label+' 空态');
  const held=holdList();
  await page.locator('[data-lib-action="reset"]').evaluate(button=>button.click());
  await held.entered;
  try{ assertStableLayout(await libraryBounds(page),baseline,label+' 加载中'); }
  finally{ held.release(); }
  await page.waitForFunction(()=>!LIB.loading && LIB.items.length===43);
  setFailure(true);
  await page.locator('[data-lib-action="refresh"]').evaluate(button=>button.click());
  await page.waitForFunction(()=>!LIB.loading && $('#libraryMessage').textContent.includes('读取失败'));
  assertStableLayout(await libraryBounds(page),baseline,label+' 请求失败');
  setFailure(false);
  return baseline;
}
async function verifyLibraryLayout(page,holdList,setFailure){
  const results=[];
  for(const theme of ['light','dark']) for(const width of [320,375,390,768,1280,1600]) for(const collapsed of [false,true]){
    await page.setViewportSize({width,height:900});
    await page.evaluate(({theme,collapsed})=>{
      document.documentElement.dataset.theme=theme;
      closeSidebar(); setSidebarCollapsed(collapsed);
      if(!collapsed) expandSidebarResponsive();
    },{theme,collapsed});
    await page.waitForTimeout(300); // 等待侧栏原有过渡结束再量边界。
    const label=`${theme} ${width} 侧栏${collapsed?'收起':'展开'}`;
    const layout=await verifyLayoutStates(page,label,holdList,setFailure);
    assert.ok(layout.cards[0].width<=320,label+' 小卡宽度上限');
    if(width===1600) assert.equal(layout.bounds[1].width,1200,label+' 宽屏达到最大内容宽度');
    results.push({theme,width,collapsed,contentWidth:layout.bounds[1].width,columns:layout.columns,cardWidth:layout.cards[0].width});
    if(process.env.LIBRARY_SCREENSHOT_DIR){
      await clickLibraryFilter(page,'[data-lib-action="reset"]',43);
      await page.screenshot({path:path.join(process.env.LIBRARY_SCREENSHOT_DIR,`${theme}-${width}-${collapsed?'closed':'open'}.png`)});
    }
  }
  console.log('作品库布局矩阵：'+JSON.stringify(results));
  await page.evaluate(()=>{ closeSidebar(); setSidebarCollapsed(false); });
}
async function verifyCardActions(page){
  const card=page.locator('.lib-card').first(), actions=card.locator('.lib-card-actions');
  await page.mouse.move(0,0);
  await page.evaluate(()=>document.activeElement.blur());
  await page.waitForTimeout(200);
  assert.equal(await actions.evaluate(node=>getComputedStyle(node).opacity),'0','桌面非悬停隐藏操作');
  const height=await card.evaluate(node=>node.getBoundingClientRect().height);
  await card.hover();
  await page.waitForTimeout(200);
  assert.equal(await actions.evaluate(node=>getComputedStyle(node).opacity),'1','悬停显示操作');
  await page.mouse.move(0,0);
  await card.locator('.lib-card-open').focus();
  await page.keyboard.press('Tab');
  assert.equal(await actions.locator('button').first().evaluate(node=>node===document.activeElement),true,'键盘可进入操作');
  await page.waitForTimeout(200);
  assert.equal(await actions.evaluate(node=>getComputedStyle(node).opacity),'1','focus-within 显示操作');
  assert.equal(await card.evaluate(node=>node.getBoundingClientRect().height),height,'操作显隐不改变高度');
}
async function verifyTouchActions(browser){
  const context=await browser.newContext({hasTouch:true,isMobile:true,viewport:{width:390,height:844}});
  const page=await context.newPage();
  try{
    await page.setContent('<style>'+fs.readFileSync(path.join(root,'styles.css'),'utf8')+fs.readFileSync(path.join(root,'library.css'),'utf8')+'</style><main class="library-ui"><article class="lib-card"><div class="lib-card-actions"><button class="lib-button">置顶</button></div></article></main>');
    assert.equal(await page.evaluate(()=>matchMedia('(hover:none)').matches),true,'真实触屏媒体条件');
    const button=page.locator('button');
    assert.equal(await button.evaluate(node=>getComputedStyle(node.parentElement).opacity),'1','触屏操作常显');
    await button.evaluate(node=>node.onclick=()=>node.dataset.tapped='true');
    await button.tap();
    assert.equal(await button.getAttribute('data-tapped'),'true','触屏可直接点操作');
  }finally{ await context.close(); }
}
(async()=>{
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {channel:'chrome'})});
  try{
    const page=await browser.newPage(), errors=[], sends=[], queries=[], downloads=[];
    let failList=false, failPatch=false, failSend=false, failCover=false, previewCalls=0, listGate;
    const coverRequests=[];
    function holdList(){
      let markEntered,release;
      const entered=new Promise(resolve=>{ markEntered=resolve; });
      const response=new Promise(resolve=>{ release=resolve; });
      listGate={markEntered,response};
      return {entered,release};
    }
    let delayImageDetail=false, releaseDetail, markDetailEntered;
    const detailEntered=new Promise(resolve=>{ markDetailEntered=resolve; });
    let delaySendResponse=false, releaseSend, markSendEntered;
    const sendEntered=new Promise(resolve=>{ markSendEntered=resolve; });
    let extraItems=[], paginationMeta=false, delayMore=false, releaseMore, markMoreEntered;
    const moreEntered=new Promise(resolve=>{ markMoreEntered=resolve; });
    const agents=[{id:'design',name:'AI 设计'},{id:'dev',name:'开发'}];
    const items=[
      {id:'img-1',kind:'image',title:'真实图片',agent_id:'design',pinned:false,hidden:false,file_url:'https://untrusted.invalid/image?token=bad',created_at:'2026-10-12',sources:[{session_key:'web:one',conv:'one',can_open:true,turn_id:12,action:'generate_image',metadata:{prompt:'<script>不执行</script>'}}]},
      {id:'site-1',kind:'website',title:'品牌网站',url:'https://example.com/site/',cover_url:'/library/cover?id=site-1',agent_id:'dev',pinned:true,hidden:false,sources:[]},
      {id:'hidden-1',kind:'image',title:'隐藏图片',agent_id:null,pinned:false,hidden:true,sources:[{session_key:'web:deleted',metadata:{deleted:true}}]},
      {id:'bad-site',kind:'website',title:'无效链接',url:'javascript:alert(1)',agent_id:null,pinned:false,hidden:false,sources:[]}
    ];
    items[0].sources.push({session_key:'personal',conv:'main',can_open:true,action:'send_image'},{session_key:'telegram:private',conv:'',can_open:false,action:'send_image'});
    page.on('pageerror',error=>errors.push(error.message));
    await page.route(origin+'/**',async route=>{
      const request=route.request(), url=new URL(request.url());
      const file=path.join(root,url.pathname==='/'?'index.html':url.pathname);
      if(fs.existsSync(file)&&fs.statSync(file).isFile()) return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html'});
      let data={};
      if(url.pathname==='/library/items'){
        queries.push(Object.fromEntries(url.searchParams));
        if(listGate){
          const gate=listGate; listGate=null; gate.markEntered(); await gate.response;
        }
        if(failList) return route.fulfill({status:500,json:{error:'失败'}});
        const state=url.searchParams.get('state'),kind=url.searchParams.get('kind'),agent=url.searchParams.get('agent_id'),search=url.searchParams.get('search');
        const matches=[...items,...extraItems].filter(item=>(state==='hidden'?item.hidden:state==='pinned'?item.pinned&&!item.hidden:state==='all'||!item.hidden)&&(!kind||item.kind===kind)&&(!agent||item.agent_id===agent)&&(!search||item.title.includes(search)));
        const offset=Number(url.searchParams.get('offset')||0), limit=Number(url.searchParams.get('limit')||60);
        data={agents,items:matches.slice(offset,offset+limit),...(paginationMeta ? {count:matches.length,has_more:offset+limit<matches.length} : {})};
        if(delayMore && offset>0){
          delayMore=false; const captured=JSON.parse(JSON.stringify(data));
          await new Promise(resolve=>{ releaseMore=resolve; markMoreEntered(); });
          return route.fulfill({json:captured});
        }
      }
      if(url.pathname==='/library/item'){
        const item=items.find(item=>item.id===url.searchParams.get('id'));
        if(!item) return route.fulfill({status:404,json:{}});
        if(request.method()==='PATCH'){
          if(failPatch) return route.fulfill({status:500,json:{}});
          Object.assign(item,request.postDataJSON());
        }
        if(delayImageDetail && item.id==='img-1' && request.method()==='GET'){
          delayImageDetail=false;
          const captured=JSON.parse(JSON.stringify(item));
          await new Promise(resolve=>{ releaseDetail=resolve; markDetailEntered(); });
          return route.fulfill({json:captured});
        }
        data=item;
      }
      if(url.pathname==='/library/cover'){
        assert.equal(request.headers()['x-auth-token'],'test-secret','网站封面需要鉴权');
        assert.equal(url.searchParams.has('token'),false,'封面 URL 不携带口令');
        assert.equal(url.searchParams.get('id'),'site-1','封面用作品 ID 构造受控路径');
        coverRequests.push(request.url());
        if(failCover) return route.fulfill({status:404,json:{error:'无封面'}});
        return route.fulfill({body:image,contentType:'image/png'});
      }
      if(url.pathname==='/library/file'){
        assert.notEqual(url.searchParams.get('id'),'site-1','网站截图不能走原图接口');
        assert.equal(request.headers()['x-auth-token'],'test-secret');
        assert.equal(url.searchParams.has('token'),false);
        return route.fulfill({body:image,contentType:'image/png'});
      }
      if(url.pathname==='/library/history/preview'){ assert.equal(request.method(),'POST'); previewCalls++; data={scope:'支持范围',candidates:3,duplicates:1}; }
      if(url.pathname==='/send'){
        if(failSend) return route.fulfill({status:400,json:{error:'测试拒绝'}});
        sends.push(request.postDataJSON());
        if(delaySendResponse){
          delaySendResponse=false;
          await new Promise(resolve=>{ releaseSend=resolve; markSendEntered(); });
        }
      }
      if(url.pathname==='/history') data={turns:[]};
      if(url.pathname==='/conversations') data={main:{},conversations:[{conv:'one',title:'会话一'},{conv:'two',title:'会话二'}]};
      if(url.pathname==='/conv/perm') data={mode:'standard'};
      return route.fulfill({json:data});
    });
    await page.goto(origin);
    await page.evaluate(()=>{
      S.token='test-secret'; $('#login').style.display='none'; $('#app').hidden=false;
      $('#chatMain').hidden=false; $('#callView').hidden=true; S.conv='one';
      S.convs=[{conv:'one',title:'会话一'},{conv:'two',title:'会话二'}]; renderConvs();
    });
    await page.evaluate(()=>openLibrary());
    assert.equal(await page.locator('#convTopRows .ct').filter({hasText:'作品库'}).count(),1);
    assert.equal(await page.locator('.lib-card').count(),3);
    await page.waitForFunction(()=>$('#libraryWorks img')?.src.startsWith('blob:'));
    assert.equal(await page.locator('[src*="token="]').count(),0);
    await page.waitForFunction(()=>$('[data-library-cover]')?.src.startsWith('blob:'));
    assert.ok(coverRequests.length>0,'列表加载网站封面');
    assert.equal(await page.locator('[data-id="bad-site"] .lib-website').count(),1,'无封面显示简洁网站占位');
    assert.equal(await page.locator('[data-id="bad-site"] img').count(),0,'无封面不误加载原图');
    await verifyCardActions(page);
    await verifyTouchActions(browser);
    await page.locator('[data-state="pinned"]').click();
    await page.waitForFunction(()=>$('#libraryCount').textContent==='已加载 1 件作品');
    await page.locator('[data-lib-action="reset"]').click();
    await page.selectOption('#libraryAgent','design');
    await page.waitForFunction(()=>$('#libraryCount').textContent==='已加载 1 件作品');
    assert.equal(queries.at(-1).agent_id,'design');
    await page.fill('#librarySearch','没有这个标题');
    await page.waitForFunction(()=>$('#libraryCount').textContent==='已加载 0 件作品');
    await page.locator('[data-lib-action="reset"]').click();
    await page.locator('[data-lib-action="detail"][data-id="img-1"]').click();
    await page.waitForSelector('#libraryTitle');
    assert.match(await page.locator('.lib-source pre').textContent(),/<script>不执行<\/script>/);
    assert.equal(await page.locator('[data-lib-action="source"][data-conv="main"]').count(),1,'personal主会话使用服务端main映射');
    assert.equal(await page.locator('[data-conv="personal"]').count(),0);
    assert.match(await page.locator('.lib-source').filter({hasText:'telegram:private'}).innerText(),/不可直接打开/);
    assert.equal(await page.evaluate(()=>librarySourceConv({session_key:'unknown'})),'','未知来源不猜网页会话');
    assert.equal(await page.evaluate(()=>librarySourceConv({session_key:'web:one'})),'','缺少服务端映射也不自猜');
    assert.equal(await page.evaluate(()=>librarySourceConv({conv:'one',can_open:false})),'','明确不可打开时不建跳转');
    failPatch=true;
    await page.locator('#libraryDetail [data-lib-action="pin"]').click();
    await page.waitForFunction(()=>$('#libraryDetailMessage').textContent.includes('失败'));
    assert.equal(items[0].pinned,false);
    failPatch=false;
    await page.fill('#libraryTitle','修改后的图片');
    await page.locator('[data-lib-action="save"]').click();
    await page.waitForFunction(()=>$('#libraryDetailTitle').textContent==='修改后的图片');
    assert.equal(items[0].title,'修改后的图片');
    await page.locator('#libraryDetail [data-lib-action="pin"]').click();
    await page.waitForFunction(()=>$('#libraryDetail [data-lib-action="pin"]').textContent.includes('取消'));
    assert.equal(items[0].pinned,true);
    const downloadPromise=page.waitForEvent('download');
    await page.locator('[data-lib-action="download"]').click();
    const download=await downloadPromise; downloads.push(download.suggestedFilename());
    assert.equal(downloads[0],'修改后的图片.png');
    await page.selectOption('#libraryTarget','two');
    await page.locator('[data-lib-action="reference"]').click();
    await page.waitForFunction(()=>S.surface==='chat'&&S.conv==='two'&&S.libraryRefs.length===1);
    assert.equal(sends.length,0,'引用不能自动发送');
    await page.evaluate(()=>openConv('one'));
    assert.equal(await page.locator('.library-ref-chip').count(),0,'引用按会话隔离');
    await page.evaluate(()=>openConv('two'));
    assert.equal(await page.locator('.library-ref-chip').count(),1);
    failSend=true;
    await page.evaluate(()=>send('请参考图片'));
    assert.equal(await page.evaluate(()=>S.libraryRefs[0].id),'img-1','发送失败保留引用');
    failSend=false;
    await page.evaluate(()=>{ S.stream=null; return send('请参考图片'); });
    assert.deepEqual(sends.at(-1).library_refs,['img-1']);
    assert.equal(await page.locator('.library-ref-chip').count(),0);
    await page.evaluate(()=>{ S.stream=null; return openLibrary(); });
    await page.locator('[data-kind="website"]').click();
    await page.waitForFunction(()=>$('#libraryCount').textContent==='已加载 2 件作品');
    await page.locator('[data-lib-action="detail"][data-id="site-1"]').click();
    await page.waitForSelector('#libraryTarget');
    await page.waitForFunction(()=>$('#libraryDetail [data-library-cover]')?.src.startsWith('blob:'));
    assert.equal(await page.locator('#libraryDetail [data-lib-action="download"]').count(),0,'网站详情不提供原图下载');
    failCover=true;
    await page.evaluate(()=>{ libraryReleaseImages(); renderLibraryDetail(); });
    await page.waitForFunction(()=>$('#libraryDetail .lib-cover span').textContent==='网站封面暂不可用');
    assert.equal(await page.locator('#libraryDetail img').isVisible(),false,'封面失败不会显示破图');
    failCover=false;
    await page.evaluate(()=>{ libraryReleaseImages(); renderLibraryDetail(); });
    await page.waitForFunction(()=>$('#libraryDetail [data-library-cover]')?.src.startsWith('blob:'));
    await page.evaluate(()=>{ window.__opened=[]; window.open=(...args)=>window.__opened.push(args); });
    await page.locator('[data-lib-action="website"]').click();
    assert.deepEqual(await page.evaluate(()=>window.__opened[0]),['https://example.com/site/','_blank','noopener,noreferrer']);
    await page.evaluate(()=>{
      LIB.selected={...LIB.selected,url:'http://127.0.0.1:8401/pub/demo/',open_url:'/pub/demo/'};
      renderLibraryDetail();
    });
    await page.locator('[data-lib-action="website"]').click();
    assert.deepEqual(await page.evaluate(()=>window.__opened[1]),[origin+'/pub/demo/','_blank','noopener,noreferrer']);
    assert.equal(await page.evaluate(()=>libraryWebsiteUrl('https://demo.kimmy.site/')),'https://demo.kimmy.site/');
    assert.equal(await page.evaluate(()=>libraryWebsiteUrl('//evil.invalid/pub/demo/')),'');
    assert.equal(await page.evaluate(()=>libraryWebsiteUrl('https://user:password@evil.invalid/')),'');
    await page.locator('[data-lib-action="reference"]').click();
    await page.waitForSelector('.library-ref-chip');
    await page.locator('.library-ref-chip button').click();
    assert.equal(await page.evaluate(()=>S.libraryRefs.length),0);
    await page.evaluate(()=>openLibrary());
    await page.locator('[data-lib-action="reset"]').click();
    await page.locator('[data-state="hidden"]').click();
    await page.waitForSelector('[data-lib-action="detail"][data-id="hidden-1"]');
    await page.locator('[data-lib-action="detail"][data-id="hidden-1"]').click();
    await page.waitForSelector('#libraryTarget');
    assert.match(await page.locator('.lib-source').innerText(),/来源已删除/);
    await page.locator('#libraryDetail [data-lib-action="hide"]').click();
    await page.waitForFunction(()=>$('#libraryCount').textContent==='已加载 0 件作品');
    assert.equal(items[2].hidden,false);
    await page.locator('[data-lib-action="close"]').click();
    await page.locator('[data-lib-action="history"]').click();
    await page.waitForFunction(()=>$('#libraryHistory').textContent.includes('candidates'));
    assert.equal(previewCalls,1);
    assert.match(await page.locator('#libraryHistory').innerText(),/没有导入.*不代表历史全量/);
    failList=true;
    await page.locator('[data-lib-action="refresh"]').click();
    await page.waitForFunction(()=>$('#libraryMessage').textContent.includes('读取失败'));
    failList=false;
    await page.locator('[data-lib-action="reset"]').click();
    extraItems=Array.from({length:119},(_,index)=>({id:'extra-'+index,kind:'website',title:'分页网站 '+index,url:'https://example.com/'+index,agent_id:'dev',pinned:false,hidden:false,sources:[]}));
    await page.locator('[data-lib-action="reset"]').click();
    await page.waitForFunction(()=>LIB.items.length===60);
    assert.equal(await page.locator('#libraryCount').innerText(),'已加载 60 件作品','旧API无总数字段时不冒称总数');
    assert.equal(queries.at(-1).limit,'60'); assert.equal(queries.at(-1).offset,'0');
    await page.locator('#libraryMore').click();
    await page.waitForFunction(()=>LIB.items.length===120);
    assert.equal(queries.at(-1).offset,'60');
    await page.locator('#libraryMore').click();
    await page.waitForFunction(()=>LIB.items.length===123);
    assert.equal(queries.at(-1).offset,'120');
    assert.equal(await page.locator('#libraryMore').isVisible(),false);
    assert.equal(await page.locator('.lib-card').count(),123,'第61件及后续页面可浏览');
    paginationMeta=true;
    await page.locator('[data-lib-action="reset"]').click();
    await page.waitForFunction(()=>$('#libraryCount').textContent==='已加载 60 件作品 · 筛选总数 123 件');
    delayMore=true;
    await page.evaluate(()=>{ window.__moreRequest=loadLibrary(true); });
    await moreEntered;
    await page.selectOption('#libraryAgent','design');
    await page.waitForFunction(()=>LIB.items.length===1&&LIB.filters.agent_id==='design');
    assert.equal(queries.at(-1).offset,'0','筛选重置分页');
    releaseMore(); await page.evaluate(()=>window.__moreRequest);
    assert.equal(await page.locator('.lib-card').count(),1,'旧翻页响应不混入新筛选');
    assert.equal(await page.evaluate(()=>LIB.offset),1);
    extraItems=[]; paginationMeta=false;
    await page.locator('[data-lib-action="reset"]').click();
    await page.waitForFunction(()=>LIB.items.length===4);
    extraItems=Array.from({length:39},(_,index)=>({id:'layout-'+index,kind:'website',title:'布局作品 '+index,url:'https://example.com/'+index,agent_id:'dev',pinned:false,hidden:false,sources:[]}));
    items[0].pinned=false;
    await verifyLibraryLayout(page,holdList,value=>{ failList=value; });
    items[0].pinned=true; extraItems=[];
    await page.locator('[data-lib-action="reset"]').click();
    await page.waitForFunction(()=>!LIB.loading && LIB.items.length===4);
    await page.evaluate(()=>openLibraryDetail('img-1'));
    delayImageDetail=true;
    await page.locator('#libraryDetail [data-lib-action="pin"]').click();
    await detailEntered;
    await page.evaluate(()=>{ $('#libraryDetail').close(); return openLibraryDetail('site-1'); });
    const raceReload=page.waitForResponse(response=>response.url().includes('/library/items?'));
    releaseDetail(); await raceReload;
    await page.waitForFunction(()=>$('#libraryMessage').textContent==='');
    assert.equal(await page.evaluate(()=>LIB.selected.id),'site-1','迟到详情响应不能覆盖新作品');
    await page.locator('[data-lib-action="close"]').click();
    await page.evaluate(()=>openConv('one'));
    await page.evaluate(()=>{
      S.stream=null; S.libraryRefs=[{id:'site-1',title:'新的未发送网站',kind:'website'}];
      $('#ta').value='新的草稿'; saveComposerState();
      queuePending('排队的图片',[],[],[],[{id:'img-1',title:'图片',kind:'image'}]);
      return flushPending('one');
    });
    assert.deepEqual(sends.at(-1).library_refs,['img-1']);
    assert.equal(await page.evaluate(()=>S.libraryRefs[0].id),'site-1','自动发送不能覆盖新引用');
    assert.equal(await page.locator('#ta').inputValue(),'新的草稿');
    await page.evaluate(()=>{ queuePending('失败队列',[],[],[],[{id:'img-1'}]); return openConv('two'); });
    failSend=true;
    await page.evaluate(()=>flushPending('one'));
    assert.equal(await page.evaluate(()=>S.pending.one[0].libraryRefs[0].id),'img-1','后台发送失败保留队列');
    const sentBeforeRetry=sends.length;
    failSend=false;
    await page.evaluate(()=>flushPending('one'));
    assert.equal(sends.length,sentBeforeRetry,'失败消息不自动反复重发');
    await page.evaluate(()=>openConv('one'));
    await page.getByRole('button',{name:'重试',exact:true}).click();
    await page.waitForFunction(()=>!S.pending.one);
    assert.deepEqual(sends.at(-1).library_refs,['img-1']);
    assert.equal(await page.evaluate(()=>S.libraryRefs[0].id),'site-1');
    delaySendResponse=true;
    await page.evaluate(()=>{
      queuePending('队列第一条',[],[],[],[{id:'img-1'}]);
      queuePending('队列第二条',[],[],[],[{id:'site-1'}]);
      window.__pendingFlush=flushPending('one');
    });
    await sendEntered;
    await page.evaluate(()=>flushPending('one'));
    releaseSend();
    await page.evaluate(()=>window.__pendingFlush);
    assert.equal(await page.evaluate(()=>!!S.pending.one),false,'done先于HTTP回执时仍继续队列');
    assert.deepEqual(sends.at(-1).library_refs,['site-1']);
    await page.evaluate(()=>openLibrary());
    await page.locator('[data-lib-action="detail"][data-id="img-1"]').click();
    await page.waitForSelector('[data-lib-action="source"]');
    await page.locator('[data-lib-action="source"][data-conv="one"]').click();
    await page.waitForFunction(()=>S.surface==='chat'&&S.conv==='one');
    assert.equal(await page.locator('#libraryView').isVisible(),false);
    await page.evaluate(()=>navGo(-1));
    await page.waitForFunction(()=>S.surface==='library');
    await page.evaluate(()=>openWorkbench());
    assert.equal(await page.locator('#libraryView').isVisible(),false);
    await page.evaluate(()=>openCallView());
    assert.equal(await page.locator('#libraryView').isVisible(),false);
    assert.equal(await page.evaluate(()=>S.surface),'call');
    assert.deepEqual(errors,[]);
    console.log('Library 前端测试通过：筛选、详情、鉴权图片、修改失败、恢复、引用隔离/发送失败/移除、下载、预览、主题/屏宽与导航。');
  }finally{ await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
