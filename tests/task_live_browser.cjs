const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
  const [origin,token,conv]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    const page=await browser.newPage(), errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(origin);
    await page.fill('#pw',token); await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(conv=>openConv(conv),conv);
    await page.waitForFunction(()=>S.stream && streamText(S.stream).includes('执行中'));
    assert.equal(await page.evaluate(()=>S.stream.tools.length),1,'完成前必须显示工具调用');
    assert.equal(await page.locator('#wrap').textContent().then(t=>t.includes('先检查代码')),true,'工具前不能缺尾字');
    await page.reload();
    await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(conv=>openConv(conv),conv);
    await page.waitForFunction(()=>S.stream && streamText(S.stream).includes('执行中'));
    assert.equal(await page.evaluate(()=>S.stream.tools.length),1,'刷新后必须恢复工具过程');
    console.log('READY');
    await page.waitForFunction(()=>!S.stream && $('#wrap').textContent.includes('全部完成'));
    await page.waitForFunction(()=>$('#wrap .row.ai[data-tid], #wrap .turn-block[data-tid]'));
    assert.equal(await page.locator('#wrap .row.ai').count(),1,'终态核对不能重复回复');
    assert.equal(await page.locator('#wrap .status').count(),0);
    assert.deepEqual(errors,[]);
    console.log('后台任务实时展示与刷新恢复通过');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
