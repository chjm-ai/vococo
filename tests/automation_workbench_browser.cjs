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
    await page.locator('#convTopRows .conv').filter({hasText:'工作台'}).click();
    await page.waitForSelector('.awb-owner');
    assert.equal(await page.locator('.awb-owner-toggle').count(),3);
    assert.ok(await page.locator(`[data-owner="${emptyAgent}"]`).evaluate(n=>!n.checkVisibility()),'无任务 Agent 默认折叠');
    const recent=page.locator(`[data-owner="${agent}"] .awb-recent`);
    const expectedDays=await page.evaluate(agent=>AWB.data.agents.find(a=>a.id===agent).run_days,agent);
    assert.equal(await recent.locator('.awb-tick').count(),30);
    assert.deepEqual(await recent.locator('.awb-tick').evaluateAll(nodes=>nodes.map(n=>n.dataset.date)),expectedDays.map(d=>d.date));
    assert.deepEqual(await recent.locator('.awb-tick').evaluateAll(nodes=>nodes.map(n=>n.className)),expectedDays.map(d=>'awb-tick '+d.status));
    for(const [i,day] of expectedDays.entries()){
      const tick=recent.locator('.awb-tick').nth(i);
      await tick.hover();
      const title=await tick.getAttribute('title');
      assert.ok(title.includes(day.date));
      assert.ok(title.includes(day.runs?`${day.runs} 次运行`:'无运行记录'));
    }
    assert.ok((await recent.innerText()).includes('35 运行'));
    assert.equal(await page.locator(`[data-owner="${emptyAgent}"] .awb-tick.unknown`).count(),30);
    for(const stat of await page.locator('.awb-stats .apstat').all()){
      assert.equal(await stat.evaluate(n=>getComputedStyle(n).backgroundColor),'rgba(0, 0, 0, 0)','指标内部不再有第二层背景框');
    }
    await recent.click();
    await page.waitForFunction(()=>document.querySelectorAll('.awb-history .aprun').length===30);
    await page.locator(`[data-awb="tab"][data-id="${agent}"][data-tab="tasks"]`).click();
    const jobRecent=page.locator(`.awb-job-recent[data-id="${job}"]`);
    assert.equal(await jobRecent.locator('.awb-tick').count(),30);
    assert.deepEqual(await jobRecent.locator('.awb-tick').evaluateAll(nodes=>nodes.map(n=>n.className)),expectedDays.map(d=>'awb-tick '+d.status));
    await jobRecent.locator('.awb-tick').last().hover();
    assert.ok((await jobRecent.locator('.awb-tick').last().getAttribute('title')).includes('失败'));
    assert.ok((await jobRecent.locator('.awb-tick').last().getAttribute('title')).includes('120 Token'));
    assert.equal(await page.locator('.awb-job .aptsub').count(),0,'任务行不再显示第二行元信息');
    assert.ok((await page.locator(`.aptmain[data-id="${job}"]`).getAttribute('title')).includes('每天 06:17'));
    await jobRecent.click();
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
    assert.equal(await page.locator(`[data-owner="${agent}"] .awb-job-status`).innerText(),'已停用');
    const countBefore=await page.evaluate(()=>WB_DATA.tasks.length);
    await page.keyboard.press('Space');await page.keyboard.press('Delete');
    assert.equal(await page.evaluate(()=>WB_DATA.tasks.length),countBefore,'旧待办快捷键不生效');
    // 仅在浏览器内增加长短内容样本，验证跨行列对齐，不写入任何业务数据。
    await page.evaluate(agent=>{
      window.awbLayoutOriginal=AWB.data; AWB.data=structuredClone(AWB.data);
      const base=AWB.data.agents.find(a=>a.id===agent),job=AWB.data.jobs.find(j=>j.agent_id===agent);
      for(const [i,name] of ['短名称','用于验证长名称和长指标的 Agent'].entries()){
        const id='layout-'+i;
        AWB.data.agents.push({...structuredClone(base),id,name,artifacts:i?12345:0,works:i?987:0,
          stats:{...base.stats,tokens:i?123456789:12,median_duration:i?678.9:1}});
        AWB.data.jobs.push({...job,id:'layout-job-'+i,agent_id:id});
      }
      for(const [i,state] of ['idle','running','queued','error'].entries()){
        AWB.data.jobs.push({...structuredClone(job),id:'layout-state-'+i,enabled:true,
          name:i?'用于验证任务长名称省略及单行布局的定时任务':'未运行',
          last_status:state==='error'?'error':'success',activity:state,runs:i?1234567:0,
          recent_runs:i?job.recent_runs:[],run_days:i?job.run_days:job.run_days.map(d=>({...d,runs:0,status:'unknown'}))});
      }
      renderAutomationWorkbench();
    },agent);
    assert.equal(await page.locator('.awb-job-recent[data-id="layout-state-0"] .awb-tick.unknown').count(),30);
    assert.equal(await page.locator('.awb-job-status.idle').innerText(),'');
    for(const label of ['运行中','排队中','上次异常']) assert.ok(await page.getByText(label,{exact:true}).count()>0);
    for(const theme of ['dark','light']){
      await page.emulateMedia({colorScheme:theme});
      for(const width of [1440,1280,1041,1040,768,390,375,320]){
        await page.setViewportSize({width,height:1000});
        await page.evaluate(width=>{setSidebarCollapsed(width<=760);closeSidebar();},width);
        assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'页面不横向溢出 '+width);
        assert.ok(await page.locator('#wbContent').evaluate(n=>n.scrollWidth<=n.clientWidth+1),'工作台不横向溢出 '+width);
        await page.locator(`[data-awb="menu"][data-id="${job}"]`).click();
        await page.waitForSelector('.convmenu');
        const box=await page.locator('.convmenu').boundingBox();
        assert.ok(box.x>=0 && box.x+box.width<=width+1,'菜单位于可视区 '+width);
        await page.keyboard.press('Escape');
        const columns=await page.evaluate(()=>{
          const rows=[...document.querySelectorAll('#awbOwners>.awb-owner .awb-owner-row')];
          return ['.awb-recent','.awb-owner-metrics','.awb-link'].map(selector=>rows.map(r=>r.querySelector(selector)).filter(n=>n.checkVisibility()).map(n=>n.getBoundingClientRect().x));
        });
        for(const xs of columns) if(xs.length) assert.ok(Math.max(...xs)-Math.min(...xs)<=1,'跨行列起点对齐 '+width+' '+JSON.stringify(xs));
        const jobsLayout=await page.evaluate(agent=>{
          const rows=[...document.querySelectorAll(`[data-owner="${agent}"] .awb-job`)];
          const columns=['.awb-job-recent','.awb-job-count','.awb-job-status','.more'].map(s=>rows.map(r=>r.querySelector(s).getBoundingClientRect().x));
          const singleLine=rows.every(r=>{
            const items=[...r.children].map(n=>n.getBoundingClientRect());
            const centers=items.map(b=>b.y+b.height/2);
            const name=r.querySelector('.aptname'),style=getComputedStyle(name);
            const spark=r.querySelector('.awb-spark');
            return Math.max(...centers)-Math.min(...centers)<=1 && name.clientWidth>=40 &&
              style.whiteSpace==='nowrap' && style.textOverflow==='ellipsis' &&
              (!spark || spark.scrollWidth<=spark.clientWidth+1);
          });
          return {columns,singleLine,names:rows.map(r=>r.querySelector('.aptname').clientWidth)};
        },agent);
        for(const xs of jobsLayout.columns) assert.ok(Math.max(...xs)-Math.min(...xs)<=1,'任务跨行列对齐 '+width);
        assert.ok(jobsLayout.singleLine,'任务保持单行、名称可读且色条不溢出 '+width+' '+JSON.stringify(jobsLayout));
        if([1440,390].includes(width)) await page.screenshot({path:output+'/workbench-'+theme+'-'+width+'.png',fullPage:true,animations:'disabled'});
      }
    }
    await page.evaluate(()=>{AWB.data=window.awbLayoutOriginal;delete window.awbLayoutOriginal;renderAutomationWorkbench();});
    await page.locator('[data-awb="todos"]').click();await page.waitForSelector('[data-open-automation]');
    await page.locator('[data-open-automation]').click();await page.waitForSelector('#awbSearch');
    await page.fill('#awbSearch','不存在的关键词');
    assert.equal(await page.locator('#awbOwners>.awb-owner').count(),0);
    await page.fill('#awbSearch','');
    assert.equal(await page.locator('#awbOwners>.awb-owner').count(),1);
    await page.locator('[data-awb-fold="orphan"]>summary').click();
    await page.locator('.aptmain[data-awb="job-runs"][data-id="deadbeef"]').click();
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
    const movedRecent=page.locator(`.awb-job-recent[data-id="${job}"]`);
    assert.equal(await movedRecent.locator('.awb-tick').count(),30,'移动后仍显示原任务的30天历史格');
    assert.deepEqual(await movedRecent.locator('.awb-tick').evaluateAll(nodes=>nodes.map(n=>n.className)),expectedDays.map(d=>'awb-tick '+d.status));
    await movedRecent.click();
    await page.waitForFunction(()=>document.querySelectorAll('.awb-history .aprun').length===30);
    await page.locator(`[data-awb="more"][data-id="${emptyAgent}"]`).click();
    await page.waitForFunction(()=>document.querySelectorAll('.awb-history .aprun').length===35);
    // 新工作台的作品库入口也必须等待按需模块，不能提前引用 LIB。
    await page.setViewportSize({width:1440,height:1000});
    assert.equal(await page.evaluate(()=>typeof LIB),'undefined');
    await page.locator(`[data-awb="products"][data-id="${emptyAgent}"]`).click();
    await page.locator(`[data-awb="library"][data-id="${emptyAgent}"]`).click();
    await page.waitForFunction(()=>$('#libraryView') && !$('#libraryView').hidden && !LIB.loading);
    assert.equal(await page.evaluate(()=>LIB.filters.agent_id),emptyAgent);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true,widths:8,themes:2,history:35,nodes:3,management:true,aligned:true,lazyLibrary:true}));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
