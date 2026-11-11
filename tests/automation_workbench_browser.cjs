const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
  const [origin,token,agent,emptyAgent,job,output]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  const errors=[];
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(origin);await page.fill('#pw',token);await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(()=>openWorkbench());
    await page.waitForSelector('.awb-owner');
    assert.equal(await page.locator('.awb-owner-toggle').count(),3);
    assert.ok(await page.locator(`[data-owner="${emptyAgent}"]`).evaluate(n=>!n.checkVisibility()),'无任务 Agent 默认折叠');
    const recent=page.locator(`[data-owner="${agent}"] .awb-recent`);
    assert.equal(await recent.locator('.awb-tick').count(),24);
    assert.equal(await recent.locator('.awb-tick.ok').count(),1);
    assert.equal(await recent.locator('.awb-tick.error').count(),23);
    assert.ok((await recent.innerText()).includes('35 运行'));
    assert.equal(await page.locator(`[data-owner="${emptyAgent}"] .awb-tick`).count(),0);
    for(const stat of await page.locator('.awb-stats .apstat').all()){
      assert.equal(await stat.evaluate(n=>getComputedStyle(n).backgroundColor),'rgba(0, 0, 0, 0)','指标内部不再有第二层背景框');
    }
    await recent.click();
    await page.waitForFunction(()=>document.querySelectorAll('.awb-history .aprun').length===30);
    await page.locator(`[data-awb="tab"][data-id="${agent}"][data-tab="tasks"]`).click();
    await page.locator(`[data-awb="job-runs"][data-id="${job}"]`).click();
    await page.waitForFunction(()=>document.querySelectorAll('.awb-history .aprun').length===30);
    await page.locator(`[data-awb="more"][data-id="${agent}"]`).click();
    await page.waitForFunction(()=>document.querySelectorAll('.awb-history .aprun').length===35);
    await page.locator('[data-awb="run"]').last().click();
    await page.waitForSelector('.awb-trace li');
    assert.equal(await page.locator('.awb-trace li').count(),3);
    assert.equal(await page.locator('.awb-trace li.unknown').count(),1);
    assert.equal(await page.locator('.awb-result').innerText(),'完整结果正文');
    const [popup]=await Promise.all([page.waitForEvent('popup'),page.locator('[data-awb="preview"]').click()]);
    await popup.waitForLoadState();
    assert.ok(popup.url().includes('conv=session%3A'));
    assert.ok((await popup.content()).includes('docPreview') || (await popup.content()).includes('dpBody'));
    await popup.close();
    await page.locator(`[data-awb="products"][data-id="${agent}"]`).click();
    await page.waitForSelector('.awb-artifact');
    assert.equal(await page.locator('.awb-artifact strong').innerText(),'测试报告');
    await page.locator(`[data-awb="tab"][data-id="${agent}"][data-tab="tasks"]`).click();
    await page.locator(`[data-awb="menu"][data-id="${job}"]`).click();
    await page.waitForSelector('.convmenu');
    assert.equal(await page.locator('.convmenu button').count(),6);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.convmenu').count(),0);
    await page.locator(`[data-awb="menu"][data-id="${job}"]`).click();
    await page.locator('.convmenu button').first().click();
    await page.waitForFunction(()=>!$('#cronModal').hidden);
    await page.fill('#cfName','从工作台重命名');await page.click('#cfSave');
    await page.waitForFunction(()=>$('#cronModal').hidden && AWB.data.jobs[0].name==='从工作台重命名');
    // 仅操作隔离测试任务，测试不触发真实执行。
    await page.locator(`[data-awb="menu"][data-id="${job}"]`).click();
    await page.getByRole('menuitem',{name:'停用',exact:true}).click();
    await page.waitForFunction(()=>AWB.data.jobs[0].enabled===false && !AWB.loading);
    const countBefore=await page.evaluate(()=>WB_DATA.tasks.length);
    await page.keyboard.press('Space');await page.keyboard.press('Delete');
    assert.equal(await page.evaluate(()=>WB_DATA.tasks.length),countBefore,'旧待办快捷键不生效');
    for(const theme of ['dark','light']){
      await page.emulateMedia({colorScheme:theme});
      for(const width of [1440,1280,768,390,375,320]){
        await page.setViewportSize({width,height:1000});
        await page.evaluate(width=>{setSidebarCollapsed(width<=760);closeSidebar();},width);
        assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'页面不横向溢出 '+width);
        assert.ok(await page.locator('#wbContent').evaluate(n=>n.scrollWidth<=n.clientWidth+1),'工作台不横向溢出 '+width);
        await page.locator(`[data-awb="menu"][data-id="${job}"]`).click();
        await page.waitForSelector('.convmenu');
        const box=await page.locator('.convmenu').boundingBox();
        assert.ok(box.x>=0 && box.x+box.width<=width+1,'菜单位于可视区 '+width);
        await page.keyboard.press('Escape');
        if([1440,390].includes(width)) await page.screenshot({path:output+'/workbench-'+theme+'-'+width+'.png',fullPage:true,animations:'disabled'});
      }
    }
    await page.locator('[data-awb="todos"]').click();await page.waitForSelector('[data-open-automation]');
    await page.locator('[data-open-automation]').click();await page.waitForSelector('#awbSearch');
    await page.fill('#awbSearch','不存在的关键词');
    assert.equal(await page.locator('#awbOwners>.awb-owner').count(),0);
    await page.fill('#awbSearch','');
    assert.equal(await page.locator('#awbOwners>.awb-owner').count(),1);
    await page.locator('[data-awb-fold="orphan"]>summary').click();
    await page.locator('[data-awb="job-runs"][data-id="deadbeef"]').click();
    await page.waitForFunction(()=>document.querySelector('[data-awb-fold="orphan"] .aprun')?.textContent.includes('历史未归属任务'));
    assert.ok(await page.locator('[data-awb-fold="orphan"]').evaluate(n=>n.open),'未归属历史切换后保持展开');
    await page.locator('[data-awb-fold="orphan"] [data-tab="tasks"]').click();
    await page.locator('[data-awb="menu"][data-id="deadbeef"]').click();
    await page.locator('.convmenu button').first().click();
    await page.waitForFunction(()=>!$('#cronModal').hidden);
    assert.equal(await page.locator('#cfTrigger').inputValue(),'preserve');
    await page.fill('#cfName','一次性任务仅改名');await page.click('#cfSave');
    await page.waitForFunction(()=>$('#cronModal').hidden && !AWB.loading);
    assert.equal(await page.evaluate(()=>AWB.data.jobs.find(j=>j.id==='deadbeef').schedule.kind),'once');
    // 加载失败必须可见，可重试；保留上一次成功数据。
    await page.route('**/workbench/automation',route=>route.fulfill({status:500,contentType:'application/json',body:JSON.stringify({error:'测试读取失败'})}));
    await page.locator('[data-awb="refresh"]').click();
    await page.waitForSelector('.awb-error');
    assert.ok((await page.locator('.awb-error').first().innerText()).includes('测试读取失败'));
    await page.unroute('**/workbench/automation');
    await page.locator('[data-awb="refresh"]').click();
    await page.waitForFunction(()=>!AWB.loading && !AWB.error);
    // 复制、移动后的任务历史继续可读；全部操作都只命中隔离数据。
    await page.locator(`[data-awb="tab"][data-id="${agent}"][data-tab="tasks"]`).click();
    await page.locator(`[data-awb="menu"][data-id="${job}"]`).click();
    await page.getByRole('menuitem',{name:'复制到…',exact:true}).click();
    const copiedAgents=page.waitForResponse(r=>new URL(r.url()).pathname==='/agents');
    await page.locator('.convmenu button').filter({hasText:'乙 Agent'}).click();
    await (await copiedAgents).finished();
    await page.waitForFunction(()=>AWB.data.jobs.length===3 && !AWB.loading);
    await page.locator(`[data-awb="menu"][data-id="${job}"]`).click();
    await page.getByRole('menuitem',{name:'移动到…',exact:true}).click();
    await page.locator('.convmenu button').filter({hasText:'乙 Agent'}).click();
    await page.waitForFunction(({job,emptyAgent})=>AWB.data.jobs.find(j=>j.id===job).agent_id===emptyAgent && !AWB.loading,{job,emptyAgent});
    await page.locator(`[data-awb="owner"][data-id="${emptyAgent}"]`).click();
    await page.locator(`[data-awb="job-runs"][data-id="${job}"]`).click();
    await page.waitForFunction(()=>document.querySelectorAll('.awb-history .aprun').length===30);
    await page.locator(`[data-awb="more"][data-id="${emptyAgent}"]`).click();
    await page.waitForFunction(()=>document.querySelectorAll('.awb-history .aprun').length===35);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true,widths:6,themes:2,history:35,nodes:3,management:true}));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
