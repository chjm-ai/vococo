const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
(async()=>{
  const [origin,token,output,digest]=process.argv.slice(2);
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
    assert.equal(await card.locator('strong').textContent(),'可编辑成果');
    const id=await card.getAttribute('data-artifact');
    const download=async()=>{
      const ready=page.waitForEvent('download');await card.getByRole('button',{name:'下载文件',exact:true}).click();
      const saved=await ready;assert.equal(saved.suggestedFilename(),'可编辑演示.pptx');
      await saved.saveAs(path.join(output,'download.pptx'));
      assert.equal(crypto.createHash('sha256').update(fs.readFileSync(path.join(output,'download.pptx'))).digest('hex'),digest);
    };
    await download();
    // pending 刷新依赖真实数据库，不依赖工具事件。
    await page.reload();await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(()=>openConv('file-e2e'));await card.waitFor();assert.equal(await card.count(),1);
    await download();
    await page.evaluate(()=>openConv('file-other'));assert.equal(await card.count(),0);
    await send('finish');await page.waitForTimeout(200);assert.equal(await card.count(),0);
    await page.evaluate(()=>openConv('file-e2e'));await card.waitFor();
    await page.waitForFunction(()=>!S.stream);assert.equal(await card.count(),1);
    await page.reload();await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(()=>openConv('file-e2e'));await card.waitFor();await download();
    await page.evaluate(async()=>{
      const response=await api('/history?conv=file-e2e');const history=await response.json();
      const turn=(history.turns||history)[0];
      for(let i=0;i<2;i++) handleEvent({type:'done',conv:S.conv,text:turn.assistant,turn_id:turn.id,artifacts:turn.artifacts});
    });
    assert.equal(await card.count(),1,'重复 done 不得复制成果卡片');
    for(const width of [320,375,390,768,1280]){
      await page.setViewportSize({width,height:900});
      assert.equal(await card.evaluate(node=>node.scrollWidth<=node.clientWidth),true,'卡片不能横向溢出');
    }
    await page.screenshot({path:path.join(output,'send-file-history.png')});
    // 手工粘贴另一会话的成果 ID，认证通过也不能跨会话下载。
    await page.evaluate(async id=>{
      await openConv('file-other');appendArtifacts($('#wrap'),[{id,filename:'foreign.pptx',size:1}],S.conv);
    },id);
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
