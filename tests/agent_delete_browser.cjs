// 正式页面点击删除按钮,真实接口验证归档和停用,不替换返回数据。
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
  const [origin,token,agentId]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,
    ...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[],dialogs=[];
    page.on('pageerror',error=>errors.push(error.message));
    page.on('dialog',async dialog=>{dialogs.push(dialog.message()); await dialog.accept();});
    await page.goto(origin);
    await page.fill('#pw',token);
    await page.click('#loginBtn');
    await page.waitForFunction(id=>S.agents?.some(a=>a.id===id),agentId);
    await page.evaluate(async id=>{
      const agent=agentById(id);
      await openConv(agent.main_conv);
      openAgentPanelTab('setup');
    },agentId);
    const response=page.waitForResponse(r=>r.url().endsWith('/agents/delete'));
    await page.getByRole('button',{name:'移除 Agent',exact:true}).click();
    const deleted=await response;
    assert.equal(deleted.status(),200);
    assert.deepEqual((await deleted.json()).pending_tasks,[]);
    await page.waitForFunction(id=>!S.agents.some(a=>a.id===id),agentId);
    assert.ok(dialogs.some(text=>text.includes('排队及运行中的后台任务会取消')));
    assert.deepEqual(errors,[]);
    console.log('PASS: 点击删除 → 取消关联任务 → Agent 从列表消失');
  }finally{ await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1);});
