const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
(async()=>{
  const [origin,token,screenshots]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  const page=await browser.newPage({viewport:{width:1280,height:1000}}), errors=[], remote=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('request',r=>{if(r.url().includes('html-preview-blocked.invalid'))remote.push(r.url());});
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
    assert.equal(await card.locator('.preview-title').textContent(),'工具快照');
    assert.equal(await card.locator('iframe').getAttribute('sandbox'),'');
    await card.frameLocator('iframe').getByRole('heading',{name:'工具 HTML 快照'}).waitFor();
    const source=await card.locator('code').textContent();
    assert.ok(source.includes('```') && source.length>30000);
    await card.getByRole('button',{name:'显示源码',exact:true}).click();
    assert.equal(await card.locator('pre').isVisible(),true);
    await card.getByRole('button',{name:'显示预览',exact:true}).click();
    await page.screenshot({path:path.join(screenshots,'show-html-midturn.png')});
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
    await card.getByRole('button',{name:'复制URL（7天有效，持有链接即可访问）',exact:true}).click();
    await page.waitForFunction(()=>window.copiedPreview);
    assert.ok((await page.evaluate(()=>window.copiedPreview)).startsWith('https://vococo.chjm.cc/'));
    assert.equal((await shares()).length,1);
    const popupPromise=page.waitForEvent('popup');
    await card.getByRole('button',{name:'在浏览器打开（创建7天静态快照）',exact:true}).click();
    const popup=await popupPromise;await popup.close();
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
