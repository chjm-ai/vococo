const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
(async()=>{
  const [origin,token,screenshots]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  const page=await browser.newPage({viewport:{width:1280,height:1000}}), errors=[], remote=[];
  page.on('pageerror',e=>errors.push(e.message));
  // CSP 拒绝也会产生 request 事件；只记录实际到达网络层的请求。
  await page.route('https://html-preview-blocked.invalid/**',route=>{
    remote.push(route.request().url());return route.fulfill({body:'blocked test endpoint'});
  });
  try{
    await page.goto(origin);await page.fill('#pw',token);await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(async()=>{await openConv('html-e2e');closeSidebar();});
    const send=async text=>page.evaluate(async text=>{
      const response=await fetch('/send',{method:'POST',headers:{'Content-Type':'application/json','X-Auth-Token':S.token},
        body:JSON.stringify({conv:'html-e2e',text})});
      if(!response.ok) throw Error(await response.text());
    },text);
    await send('preview');
    const card=page.locator('[data-html-preview]');
    await card.waitFor();await card.locator('iframe').waitFor();
    assert.equal(await card.count(),1);
    assert.ok(await page.evaluate(()=>!!S.stream),'中途附件不能结束流式回合');
    assert.equal(await card.locator('.preview-title').textContent(),'page.html');
    assert.equal(await card.locator('.preview-toolbar').count(),1);
    assert.equal(await card.locator('.artifact-card').count(),0,'不能嵌套附件框');
    assert.equal(await card.locator('.preview-title').count(),1,'文件名仅出现一次');
    assert.equal(await card.locator('iframe').getAttribute('sandbox'),'');
    await card.frameLocator('iframe').getByRole('heading',{name:'工具 HTML 快照'}).waitFor();
    const source=await card.locator('code').textContent();
    assert.ok(source.includes('```') && source.length>30000);
    assert.equal(await page.locator('[data-artifact]').count(),1,'HTML 预览和下载共用一张卡片');
    const downloadPromise=page.waitForEvent('download');
    await card.getByRole('button',{name:'下载文件',exact:true}).click();
    const download=await downloadPromise;
    assert.equal(require('node:fs').readFileSync(await download.path(),'utf8'),source,'下载保留原文件而非净化后的预览');
    await card.getByRole('button',{name:'显示源码',exact:true}).click();
    assert.equal(await card.locator('pre').isVisible(),true);
    await card.getByRole('button',{name:'显示预览',exact:true}).click();
    const resize=card.getByRole('separator');await resize.scrollIntoViewIfNeeded();
    const bounds=await resize.boundingBox(),initial=await card.locator('iframe').evaluate(n=>n.clientHeight);
    await page.mouse.move(bounds.x+bounds.width/2,bounds.y+bounds.height/2);await page.mouse.down();
    await page.mouse.move(bounds.x+bounds.width/2,bounds.y+bounds.height/2-100,{steps:10});await page.mouse.up();
    assert.equal(await card.locator('iframe').evaluate(n=>n.clientHeight),initial-100,'HTML 文件卡片支持真实鼠标拖动');
    const frame=await (await card.locator('iframe').elementHandle()).contentFrame();
    assert.equal(await frame.evaluate(()=>{scrollTo(0,document.body.scrollHeight);return scrollY>0;}),true,'缩小后的 HTML 内容仍可滚动');
    await resize.focus();await page.keyboard.press('ArrowDown');
    assert.equal(await card.locator('iframe').evaluate(n=>n.clientHeight),initial-76);
    await card.getByRole('button',{name:'显示源码',exact:true}).click();
    assert.equal(await card.locator('pre').evaluate(n=>n.clientHeight),initial-76);
    await card.getByRole('button',{name:'显示预览',exact:true}).click();
    await resize.focus();await page.keyboard.press('Home');
    assert.equal(await card.locator('iframe').evaluate(n=>n.clientHeight),initial,'HTML 恢复原自动高度');
    assert.equal(await card.locator('iframe').getAttribute('sandbox'),'');
    await page.screenshot({path:path.join(screenshots,'show-html-midturn.png')});
    for(const theme of ['dark','light']){
      await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
      for(const width of [320,390,1280]){
        await page.setViewportSize({width,height:1000});
        assert.equal(await card.evaluate(n=>n.scrollWidth<=n.clientWidth),true,'单行工具栏窄屏不溢出');
        assert.equal(await card.locator('.preview-toolbar').evaluate(n=>n.scrollWidth<=n.clientWidth),true);
        await card.screenshot({path:path.join(screenshots,'artifact-a-'+theme+'-'+width+'.png')});
      }
    }
    await page.evaluate(()=>document.documentElement.dataset.theme='dark');
    assert.equal(await card.locator('.preview-status').isVisible(),false,'成功状态不常驻显示');
    // 刷新仍在生成的页面：真实 /history + SSE 快照恢复。
    await page.reload();await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(()=>openConv('html-e2e'));
    await card.locator('iframe').waitFor();assert.equal(await card.count(),1);
    await page.evaluate(()=>openConv('html-other'));
    assert.equal(await card.count(),0,'切会话不能把 HTML 串进另一会话');
    await send('finish');
    await page.waitForTimeout(150);
    assert.equal(await card.count(),0);
    await page.evaluate(()=>openConv('html-e2e'));
    await card.locator('iframe').waitFor();
    await page.waitForFunction(()=>!S.stream);
    assert.equal(await card.count(),1,'done 全量附件不能重建重复卡片');
    await page.reload();await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(()=>openConv('html-e2e'));
    await card.locator('iframe').waitFor();assert.equal(await card.count(),1);
    assert.equal(await card.locator('code').textContent(),source,'历史必须保留原始快照，而非重读文件');
    assert.equal(await page.evaluate(()=>window.HTML_PREVIEW_ATTACK),undefined);
    assert.deepEqual(remote,[],'静态预览不能请求外链资源');
    // 已完成历史 → 迟到 HTML/图片/视频中途帧 → done，清掉整颗旧流式气泡及状态。
    await send('late-media');
    await page.waitForFunction(()=>S.stream?.bubble.querySelector('video'));
    assert.equal(await card.count(),2,'必须复现迟到中途帧创建的重复流式卡片');
    await page.evaluate(()=>{window.lateMediaStream=S.stream;});
    await send('finish-late');
    await page.waitForFunction(()=>!S.stream);
    assert.equal(await page.evaluate(()=>window.lateMediaStream.row.isConnected),false);
    assert.equal(await page.evaluate(()=>Object.values(window.lateMediaStream.segs).some(n=>dirtySegs.has(n))),false);
    assert.equal(await card.count(),1);
    assert.equal(await page.locator('#wrap .status,#wrap .imgs,#wrap .vids').count(),0);
    assert.equal(await page.evaluate(()=>!!S.live['html-e2e'] || !!S.streamSnap['html-e2e']),false);
    await page.screenshot({path:path.join(screenshots,'show-html-late-media-cleanup.png')});
    // 历史先到、完成帧晚到 / 完成帧重放，都不能再创建第二个回合。
    await page.evaluate(async()=>{
      const response=await fetch('/history?conv=html-e2e',{headers:{'X-Auth-Token':S.token}});
      const history=await response.json();
      const turn=(history.turns||history)[0];
      handleEvent({type:'done',conv:S.conv,text:turn.assistant,turn_id:turn.id,html_previews:turn.html_previews});
      handleEvent({type:'done',conv:S.conv,text:turn.assistant,turn_id:turn.id,html_previews:turn.html_previews});
    });
    assert.equal(await card.count(),1,'历史先到、done 晚到不能复制卡片');
    // 复制 URL / 打开完全复用已有分享路径；只在用户点击时创建快照。
    const shares=()=>page.evaluate(async()=> (await (await fetch('/api/preview-shares',{headers:{'X-Auth-Token':S.token}})).json()).shares);
    assert.equal((await shares()).length,0);
    await page.evaluate(()=>{navigator.clipboard.writeText=async value=>{window.copiedPreview=value;};navigator.clipboard.write=undefined;});
    await card.getByRole('button',{name:'更多操作',exact:true}).click();
    await card.getByRole('button',{name:'复制URL（7天有效，持有链接即可访问）',exact:true}).click();
    await page.waitForFunction(()=>window.copiedPreview);
    assert.ok((await page.evaluate(()=>window.copiedPreview)).startsWith('https://vococo.chjm.cc/'));
    assert.equal((await shares()).length,1);
    // 在开窗前拦截首个请求；只把主站域名回送本地隔离实例，绝不访问公网。
    await page.context().route('https://vococo.chjm.cc/**',async route=>{
      const url=new URL(route.request().url());
      const request=route.request();
      await route.fulfill({response:await page.context().request.fetch(origin+url.pathname+url.search,{
        method:request.method(),headers:{'Content-Type':'application/json'},data:request.postData()??undefined,
      })});
    });
    const popupPromise=page.waitForEvent('popup');
    await card.getByRole('button',{name:'在浏览器打开（创建7天静态快照）',exact:true}).click();
    const popup=await popupPromise;
    await popup.waitForURL('https://vococo.chjm.cc/preview-share**');
    const shared=popup.frameLocator('iframe');
    await shared.getByRole('heading',{name:'工具 HTML 快照'}).waitFor();
    const inspect=async frame=>frame.evaluate(async()=>{
      const images=[...document.querySelectorAll('img.raster')];
      await Promise.all(images.map(n=>n.decode().catch(()=>{})));
      const rect=document.querySelector('#drawing rect'),text=[...document.querySelectorAll('#drawing text')];
      const layout=document.querySelector('#layout').getBoundingClientRect(),positioned=document.querySelector('#positioned').getBoundingClientRect();
      return {images:images.map(n=>n.naturalWidth),rect:!!rect && rect.getBoundingClientRect().width>0,
        text:text.length===2 && text[1].getBoundingClientRect().y>text[0].getBoundingClientRect().y,
        svgImage:!!document.querySelector('#drawing image')?.getAttribute('href'),
        position:[positioned.x-layout.x,positioned.y-layout.y],
        unsafe:!!document.querySelector('#unsafe-data[src],#unsafe-url[src],[onerror],script'),
        cssImages:['css-block','css-inline','css-escaped'].map(id=>getComputedStyle(document.getElementById(id)).backgroundImage)};
    });
    const sharedFrame=await (await popup.locator('iframe').elementHandle()).contentFrame();
    const expected={images:[24,24,24],rect:true,text:true,svgImage:true,position:[80,30],unsafe:false,cssImages:['none','none','none']};
    const cardFrame=await (await card.locator('iframe').elementHandle()).contentFrame();
    const observed={card:await inspect(cardFrame),share:await inspect(sharedFrame)};
    console.log('实际卡片/独立预览渲染：'+JSON.stringify(observed));
    assert.deepEqual(observed,{card:expected,share:expected});
    await cardFrame.locator('#drawing').screenshot({path:path.join(screenshots,'show-html-card-svg.png')});
    await sharedFrame.locator('#drawing').screenshot({path:path.join(screenshots,'show-html-share-svg.png')});
    await popup.screenshot({path:path.join(screenshots,'show-html-independent.png')});
    assert.deepEqual(remote,[],'独立预览也不能加载外链');
    await popup.close();
    await page.screenshot({path:path.join(screenshots,'show-html-history.png')});
    // done 丢中途帧的兜底、重复帧去重，不通过 Markdown 代码围栏拼接。
    await page.evaluate(source=>{
      clearWrap(S.conv);
      const item={id:'lost-frame',html:source,title:'完成帧兜底'};
      handleEvent({type:'done',conv:S.conv,text:'已完成',turn_id:999,html_previews:[item,item]});
    },source);
    await card.locator('iframe').waitFor();assert.equal(await card.count(),1);
    assert.deepEqual(errors,[]);
    console.log('show_html 工具 → SSE中途卡片 → 切会话 → done → 历史回放/分享/安全沙箱：通过');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
