const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
(async()=>{
  const [origin,token,output,digest,filename]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  const page=await browser.newPage({viewport:{width:1280,height:900},acceptDownloads:true}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  try{
    await page.goto(origin);await page.fill('#pw',token);await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(async()=>{await openConv('file-e2e');closeSidebar();});
    const send=text=>page.evaluate(async text=>{
      const response=await fetch('/send',{method:'POST',headers:{'Content-Type':'application/json','X-Auth-Token':S.token},body:JSON.stringify({conv:'file-e2e',text})});
      if(!response.ok) throw Error(await response.text());
    },text);
    await send('deliver');
    const card=page.locator('[data-artifact]');await card.waitFor();
    assert.equal(await card.count(),1);
    assert.equal(await card.locator('.preview-title').textContent(),filename);
    assert.equal(await card.locator('.preview-toolbar').count(),1);
    assert.equal(await card.locator('.artifact-card').count(),0);
    assert.equal(await card.getByRole('button',{name:'下载文件',exact:true}).textContent(),'');
    assert.deepEqual(await page.evaluate(()=>[artifactSize(256),artifactSize(1234),artifactSize(2516582)]),['256 B','1.2 KB','2.4 MB']);
    const id=await card.getAttribute('data-artifact');
    const download=async()=>{
      const ready=page.waitForEvent('download');await card.getByRole('button',{name:'下载文件',exact:true}).click();
      const saved=await ready;assert.equal(saved.suggestedFilename(),filename);
      await saved.saveAs(path.join(output,'download.pptx'));
      assert.equal(crypto.createHash('sha256').update(fs.readFileSync(path.join(output,'download.pptx'))).digest('hex'),digest);
    };
    const preview=async()=>{
      await card.getByRole('button',{name:'预览文件 '+filename,exact:true}).click();
      await page.locator('#docPreview').waitFor({state:'visible'});
      if(filename.endsWith('.pptx')) await page.locator('#dpBody .dp-err').filter({hasText:'暂不支持预览'}).waitFor();
      else if(filename.endsWith('.svg')) await page.waitForFunction(()=>document.querySelector('#dpBody .dp-img')?.naturalWidth===80);
      else await page.locator(filename.endsWith('.md')?'#dpBody .dp-md h1':'#dpBody .dp-text').filter({hasText:'附件快照'}).waitFor();
      assert.equal(await page.locator('#dpTitle').textContent(),filename);
      assert.equal(await page.locator('#dpCopyBtn').isVisible(),false);
    };
    await preview();
    const popupReady=page.waitForEvent('popup');await page.click('#dpWinBtn');
    const popup=await popupReady;
    await popup.locator(filename.endsWith('.pptx')?'#dpBody .dp-err':filename.endsWith('.svg')?'#dpBody .dp-img':filename.endsWith('.md')?'#dpBody .dp-md h1':'#dpBody .dp-text').waitFor();
    if(filename.endsWith('.svg')) await popup.waitForFunction(()=>document.querySelector('#dpBody .dp-img')?.naturalWidth===80);
    await popup.close();
    await download();
    assert.equal(await page.locator('#docPreview').isVisible(),false,'下载不触发预览');
    await page.waitForFunction(()=>!document.querySelector('[data-artifact] button[aria-busy]'));
    assert.equal(await card.locator('.artifact-note').isVisible(),false,'成功下载不常驻提示');
    // pending 刷新依赖真实数据库，不依赖工具事件。
    await page.reload();await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(()=>openConv('file-e2e'));await card.waitFor();assert.equal(await card.count(),1);
    await download();
    await page.evaluate(()=>openConv('file-other'));assert.equal(await card.count(),0);
    await send('finish');await page.waitForTimeout(200);assert.equal(await card.count(),0);
    await page.evaluate(()=>openConv('file-e2e'));await card.waitFor();
    await page.waitForFunction(()=>!S.stream);assert.equal(await card.count(),1);
    await page.reload();await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(()=>openConv('file-e2e'));await card.waitFor();await preview();
    await page.click('#dpClose');await download();
    await page.evaluate(async()=>{
      const response=await api('/history?conv=file-e2e');const history=await response.json();
      const turn=(history.turns||history)[0];
      for(let i=0;i<2;i++) handleEvent({type:'done',conv:S.conv,text:turn.assistant,turn_id:turn.id,artifacts:turn.artifacts});
    });
    assert.equal(await card.count(),1,'重复 done 不得复制成果卡片');
    await card.locator('.preview-title').evaluate(node=>node.textContent='特别长的项目方案文件名'.repeat(10)+'.pptx');
    for(const width of [320,375,390,768,1280]){
      await page.setViewportSize({width,height:900});
      assert.equal(await card.evaluate(node=>node.scrollWidth<=node.clientWidth),true,'长文件名卡片不能横向溢出');
      assert.equal(await card.locator('.preview-toolbar').evaluate(node=>node.scrollWidth<=node.clientWidth),true);
      assert.equal(await card.getByRole('button',{name:'下载文件',exact:true}).isVisible(),true);
    }
    await card.locator('.preview-title').evaluate((node,name)=>node.textContent=name,filename);
    await page.screenshot({path:path.join(output,'send-file-history.png')});
    for(const width of [390,1280]){
      await page.setViewportSize({width,height:900});await preview();
      assert.equal(await page.locator('#docPreview').evaluate(node=>{const r=node.getBoundingClientRect();return r.left>=0 && r.right<=innerWidth;}),true);
      await page.screenshot({path:path.join(output,'file-preview-'+width+'.png')});
      await page.keyboard.press('Escape');
    }
    await card.getByRole('button',{name:'预览文件 '+filename,exact:true}).focus();await page.keyboard.press('Enter');
    await page.locator('#dpDlBtn').waitFor({state:'visible'});await page.keyboard.press('Escape');
    // 手工粘贴另一会话的成果 ID，认证通过也不能跨会话下载。
    await page.evaluate(async id=>{
      await openConv('file-other');appendArtifacts($('#wrap'),[{id,filename:'foreign.pptx',size:1}],S.conv);
    },id);
    await card.getByRole('button',{name:'预览文件 foreign.pptx',exact:true}).click();
    await page.locator('#dpBody .dp-err').waitFor();
    assert.equal(await page.locator('#dpBody .dp-md').count(),0);
    await page.click('#dpClose');
    await card.getByRole('button',{name:'下载文件',exact:true}).click();
    await card.getByRole('status').filter({hasText:'附件不存在或不属于当前会话'}).waitFor();
    // 完成帧独立兜底，即使中途 SSE 丢帧仍能展示。
    await page.evaluate(id=>{
      clearWrap(S.conv);
      handleEvent({type:'done',conv:S.conv,text:'done',turn_id:999,artifacts:[{id,filename:'fallback.pptx',size:1}]});
    },id);
    await card.waitFor();assert.equal(await card.count(),1);
    assert.deepEqual(errors,[]);
    console.log('send_file → SSE卡片 → pending/完成历史刷新 → 真实浏览器下载字节校验 → 跨会话拒绝 → done去重/兜底：通过');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
