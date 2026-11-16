const {chromium}=require('playwright');
const assert=require('node:assert/strict');
async function checkWorkbenchSafeArea(page,output){
  const cdp=await page.context().newCDPSession(page);
  for(const [width,top] of [[390,59],[375,47],[320,20],[390,0]]){
    await page.setViewportSize({width,height:640});
    await cdp.send('Emulation.setSafeAreaInsetsOverride',{insets:{top,bottom:0,left:0,right:0}});
    await page.evaluate(async()=>{
      closeSidebar();
      await Promise.all(document.getAnimations().filter(a=>a.effect?.target?.closest('#sidebar')).map(a=>a.finished.catch(()=>{})));
    });
    for(const scroll of [0,200]){
      await page.evaluate(scroll=>{document.querySelector('#workbenchView').scrollTop=scroll;},scroll);
      await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(resolve)));
      const layout=await page.evaluate(()=>{
        const bar=document.querySelector('.wb-toolbar'),content=document.querySelector('#wbContent');
        const buttons=[...bar.querySelectorAll('.wb-title-row button')].filter(b=>b.checkVisibility());
        return {padding:parseFloat(getComputedStyle(bar).paddingTop),contentPadding:parseFloat(getComputedStyle(content).paddingTop),
          buttons:buttons.map(b=>{const r=b.getBoundingClientRect();return {top:r.top,hit:b.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))};})};
      });
      assert.equal(layout.padding,top+12,'标题栏必须保留系统安全区 '+width+' '+top);
      assert.equal(layout.contentPadding,0,'安全区只由标题栏补齐，不叠加内容留白');
      assert.ok(layout.buttons.every(b=>b.top>=top && b.hit),'首屏及滚动吸顶后按钮都在安全区下且可点击 '+JSON.stringify(layout));
    }
    if(top===59) await page.screenshot({path:output+'/workbench-safe-area.png',fullPage:true});
    await page.locator('.wb-hamb').click();
    assert.ok(await page.locator('#sidebar').evaluate(n=>n.classList.contains('open')),'移动端顶部展开按钮可用');
    await page.evaluate(()=>closeSidebar());
  }
  await cdp.send('Emulation.setSafeAreaInsetsOverride',{insets:{}});
  await cdp.detach();
}
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
      const text=await page.locator('#awbTip').innerText();
      assert.ok(text.includes(day.date));
      assert.ok(text.includes(day.runs?`${day.runs} 次运行`:'无运行记录'));
      assert.equal(await tick.evaluate(n=>n.closest('[title]')),null,'格子及祖先不再触发原生延迟提示');
    }
    const immediate=await recent.evaluate(n=>{
      const cells=[...n.querySelectorAll('.awb-tick')],tip=document.querySelector('#awbTip');
      const start=performance.now();
      const instant=cells.every(cell=>{
        cell.dispatchEvent(new PointerEvent('pointerover',{bubbles:true,pointerType:'mouse'}));
        return !tip.hidden && tip.textContent===cell.dataset.awbTip;
      });
      n.querySelector('.awb-spark').dispatchEvent(new PointerEvent('pointerover',{bubbles:true,pointerType:'mouse'}));
      return {instant,gapVisible:!tip.hidden,elapsed:performance.now()-start};
    });
    assert.ok(immediate.instant,'30格连续移入在事件当轮更新，不等待定时器');
    assert.ok(immediate.gapVisible,'经过格子间隙不闪烁');
    await page.keyboard.press('Escape');
    assert.ok(await page.locator('#awbTip').evaluate(n=>n.hidden),'Escape 关闭提示');
    await recent.locator('.awb-tick').first().dispatchEvent('pointerover',{pointerType:'touch'});
    assert.ok(await page.locator('#awbTip').evaluate(n=>n.hidden),'触屏不弹悬浮提示');
    await recent.locator('.awb-tick').first().hover();
    await page.evaluate(()=>document.querySelector('#workbenchView').dispatchEvent(new Event('scroll')));
    assert.ok(await page.locator('#awbTip').evaluate(n=>n.hidden),'滚动关闭提示');
    await recent.focus();
    assert.equal(await recent.getAttribute('aria-describedby'),'awbTip','键盘聚焦可查看最新日期');
    await page.locator('#awbSearch').focus();
    assert.ok(await page.locator('#awbTip').evaluate(n=>n.hidden),'焦点移出关闭提示');
    await page.evaluate(()=>{
      const probe=document.createElement('div'); probe.id='awbScrollProbe'; probe.style.paddingTop='200vh';
      probe.innerHTML='<button class="awb-job-recent"><span class="awb-spark"><span class="awb-tick" data-awb-tip="屏外聚焦测试"></span></span></button>';
      document.querySelector('#wbContent').append(probe); probe.querySelector('button').focus();
    });
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    assert.equal(await page.locator('#awbTip').innerText(),'屏外聚焦测试','键盘聚焦引起的滚动后提示仍可见');
    await page.evaluate(()=>{document.querySelector('#awbScrollProbe').remove(); awbHideTip();});
    assert.ok((await recent.innerText()).includes('35 运行'));
    assert.equal(await page.locator(`[data-owner="${emptyAgent}"] .awb-tick.unknown`).count(),30);
    for(const stat of await page.locator('.awb-stats .apstat').all()){
      assert.equal(await stat.evaluate(n=>getComputedStyle(n).backgroundColor),'rgba(0, 0, 0, 0)','指标内部不再有第二层背景框');
    }
    assert.equal(await page.locator('.awb-stats .apstat').count(),6);
    assert.ok((await page.locator('.awb-stats').innerText()).includes('近 7 天中位耗时'));
    assert.ok((await page.locator('.awb-stats').innerText()).includes('近 7 天调度成功率'));
    assert.equal(await page.evaluate(()=>awbTokens(12304000)),'1,230.4 万','万位数字同样使用千位分隔');
    const ownerToggle=page.locator(`[data-awb="owner"][data-id="${agent}"]`);
    assert.equal(await ownerToggle.locator(':scope>.awb-chevron:first-child').count(),1,'Agent 箭头统一放在最左侧');
    await ownerToggle.focus();
    assert.ok(await ownerToggle.evaluate(n=>getComputedStyle(n).outlineStyle!=='none'),'键盘焦点可见');
    await page.keyboard.press('Enter');
    assert.equal(await ownerToggle.getAttribute('aria-expanded'),'true');
    assert.equal(await page.locator('[data-awb="tab"]').count(),0,'展开 Agent 直接展示定时任务，不再有 Tab');
    assert.equal(await page.locator('.awb-history').count(),0,'历史只在展开具体任务后读取');
    const jobRecent=page.locator(`.awb-job-recent[data-id="${job}"]`);
    assert.equal(await jobRecent.locator('.awb-tick').count(),30);
    assert.deepEqual(await jobRecent.locator('.awb-tick').evaluateAll(nodes=>nodes.map(n=>n.className)),expectedDays.map(d=>'awb-tick '+d.status));
    await jobRecent.locator('.awb-tick').last().hover();
    assert.ok((await page.locator('#awbTip').innerText()).includes('失败'));
    assert.ok((await page.locator('#awbTip').innerText()).includes('120 Token'));
    await recent.locator('.awb-spark').dispatchEvent('pointerover',{pointerType:'mouse'});
    assert.ok(await page.locator('#awbTip').evaluate(n=>n.hidden),'进入另一行格间空隙不残留上一行提示');
    await jobRecent.locator('.awb-tick').last().hover();
    await page.mouse.move(0,0);
    assert.ok(await page.locator('#awbTip').evaluate(n=>n.hidden),'移出图表立即关闭提示');
    assert.equal(await page.locator('.awb-job .aptsub').count(),0,'任务行不再显示第二行元信息');
    assert.ok((await page.locator(`.aptmain[data-id="${job}"]`).getAttribute('title')).includes('每天 06:17'));
    const jobToggle=page.locator(`.aptmain[data-id="${job}"]`);
    assert.equal(await jobToggle.locator(':scope>.awb-chevron:first-child').count(),1);
    await jobToggle.focus();await page.keyboard.press('Enter');
    assert.equal(await jobToggle.getAttribute('aria-expanded'),'true');
    const history=page.locator(`.awb-task[data-job="${job}"] .awb-job-history`);
    await page.waitForFunction(()=>document.querySelectorAll('.awb-history .aprun').length===5);
    assert.ok(await history.locator('[data-awb="job-prev"]').isDisabled());
    assert.equal(await history.locator('[data-awb="source-run"]:disabled').count(),5,'无可靠来源的旧记录明确不可跳转');
    assert.ok((await history.innerText()).includes('未关联会话'));
    const firstIds=await history.locator('[data-awb="source-run"]').evaluateAll(nodes=>nodes.map(n=>n.dataset.id));
    // 翻页失败可在原页重试，不能跳过游标或累积旧行。
    await page.route('**/workbench/automation/runs?**',route=>route.fulfill({status:500,contentType:'application/json',body:JSON.stringify({error:'历史读取失败'})}));
    await history.locator('[data-awb="job-next"]').click();
    await history.locator('.awb-error').waitFor();
    assert.ok(await history.locator('[data-awb="job-next"]').isDisabled());
    await page.unroute('**/workbench/automation/runs?**');
    await history.locator('[data-awb="job-retry"]').click();
    await page.waitForFunction(()=>document.querySelector('.awb-pager')?.textContent.includes('第 2 页') && document.querySelectorAll('.awb-history .aprun').length===5);
    await history.locator('[data-awb="job-prev"]').click();
    await page.waitForFunction(()=>document.querySelector('.awb-pager')?.textContent.includes('第 1 页') && document.querySelectorAll('.awb-history .aprun').length===5);
    assert.deepEqual(await history.locator('[data-awb="source-run"]').evaluateAll(nodes=>nodes.map(n=>n.dataset.id)),firstIds);
    const allIds=[...firstIds];
    for(let n=2;n<=7;n++){
      await history.locator('[data-awb="job-next"]').click();
      await page.waitForFunction(n=>document.querySelector('.awb-pager')?.textContent.includes('第 '+n+' 页') && document.querySelectorAll('.awb-history .aprun').length===5,n);
      allIds.push(...await history.locator('[data-awb="source-run"]').evaluateAll(nodes=>nodes.map(n=>n.dataset.id)));
    }
    assert.equal(new Set(allIds).size,35,'每页5条，7页无重复');
    assert.ok(await history.locator('[data-awb="job-next"]').isDisabled(),'末页禁止继续翻页');
    const target=await page.evaluate(({agent,job})=>AWB.histories.get(awbHistoryKey(agent,job)).items.at(-1).target,{agent,job});
    let releaseDetail,detailStarted;
    const detailGate=new Promise(resolve=>{releaseDetail=resolve;}),detailSeen=new Promise(resolve=>{detailStarted=resolve;});
    await page.route('**/workbench/automation/run?*',async route=>{detailStarted();await detailGate;await route.continue();});
    const sourceButton=await history.locator('[data-awb="source-run"]:enabled').elementHandle();
    await sourceButton.click();
    await detailSeen;
    await recent.click();
    assert.equal(await sourceButton.evaluate(n=>n.isConnected),false,'复现其他操作造成来源按钮被重绘');
    releaseDetail();
    await page.waitForFunction(target=>S.surface==='chat' && S.conv===target.conv && document.querySelector('#wrap [data-tid="'+target.turn_id+'"]'),target);
    await page.unroute('**/workbench/automation/run?*');
    assert.ok((await page.locator('#wrap').innerText()).includes('完整结果正文'),'真实打开来源会话并定位轮次');
    const returnedOverview=page.waitForResponse(r=>new URL(r.url()).pathname==='/workbench/automation');
    await page.locator('#convTopRows .conv').filter({hasText:'工作台'}).click();
    await (await returnedOverview).finished();
    await page.waitForFunction(()=>!AWB.loading && document.querySelector('.awb-pager')?.textContent.includes('第 7 页') && document.querySelectorAll('.awb-history .aprun').length===5);
    let releaseStale,staleStarted;
    const staleGate=new Promise(resolve=>{releaseStale=resolve;}),staleSeen=new Promise(resolve=>{staleStarted=resolve;});
    await page.route('**/workbench/automation/run?*',async route=>{staleStarted();await staleGate;await route.continue();});
    const staleButton=await history.locator('[data-awb="source-run"]:enabled').elementHandle();
    await staleButton.click();await staleSeen;
    await page.evaluate(()=>openConv('main'));
    releaseStale();
    await page.waitForFunction(button=>!button.disabled,staleButton);
    assert.equal(await page.evaluate(()=>S.conv),'main','切换会话后旧来源请求不得抢回导航');
    await page.unroute('**/workbench/automation/run?*');
    await page.locator('#convTopRows .conv').filter({hasText:'工作台'}).click();
    await page.waitForFunction(()=>document.querySelectorAll('.awb-history .aprun').length===5);
    await page.locator(`[data-awb="products"][data-id="${agent}"]`).click();
    await page.waitForSelector('.awb-artifact');
    assert.equal(await page.locator('.awb-artifact strong').innerText(),'测试报告');
    const [popup]=await Promise.all([page.waitForEvent('popup'),page.locator('[data-awb="preview"]').click()]);
    await popup.waitForLoadState();
    assert.ok(popup.url().includes('conv=session%3A'));
    assert.ok((await popup.content()).includes('docPreview') || (await popup.content()).includes('dpBody'));
    await popup.close();
    await page.locator(`[data-awb="products"][data-id="${agent}"]`).click();
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
      window.awbLayoutHistories=AWB.histories; AWB.histories=structuredClone(AWB.histories);
      const history=AWB.histories.get(awbHistoryKey(agent,AWB.data.jobs.find(j=>j.agent_id===agent).id));
      history.items[0]={...history.items[0],status:'unknown',tokens:12304000,duration:321};
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
          const columns=['.awb-job-recent','.awb-job-count','.more'].map(s=>rows.map(r=>r.querySelector(s).getBoundingClientRect().x));
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
        const hierarchy=await page.evaluate(agent=>{
          const owner=document.querySelector(`[data-owner="${agent}"]`),spark=owner.querySelector('.awb-recent .awb-spark').getBoundingClientRect();
          return [...owner.querySelectorAll('.awb-job')].map(row=>{
            const bar=row.querySelector('.awb-spark').getBoundingClientRect(),name=row.querySelector('.aptname').getBoundingClientRect();
            const status=row.querySelector('.awb-job-status'),state=status.getBoundingClientRect();
            return {aligned:Math.abs(bar.x-spark.x)<=1 && Math.abs(bar.width-spark.width)<=1,
              nearby:!status.textContent || (state.x>=name.right && state.x-name.right<=8 && Math.abs(state.y+state.height/2-name.y-name.height/2)<=1)};
          });
        },agent);
        if(width>760) assert.ok(hierarchy.every(r=>r.aligned),'Agent 与任务的30天色条同列对齐 '+width);
        assert.ok(hierarchy.every(r=>r.nearby),'状态紧邻名称，不再漂在最右侧 '+width);
        const runLayout=await history.evaluate(root=>{
          const head=root.querySelector('.awb-history-head'),rows=[...root.querySelectorAll('.awb-run-link')];
          const cells=rows.map(r=>[...r.children].map(n=>n.getBoundingClientRect()));
          const centers=cells.map(bs=>bs.map(b=>b.y+b.height/2));
          const headers=[...head.children].map(n=>n.getBoundingClientRect());
          return {heads:root.querySelectorAll('.awb-history-head').length,headVisible:head.checkVisibility(),
            height:root.getBoundingClientRect().height,maxRow:Math.max(...rows.map(r=>r.getBoundingClientRect().height)),
            geometry:rows.map(r=>({width:r.clientWidth,scroll:r.scrollWidth,columns:getComputedStyle(r).gridTemplateColumns,
              cells:[...r.children].map(n=>({text:n.textContent,width:n.clientWidth,scroll:n.scrollWidth}))})),
            singleLine:centers.every(cs=>Math.max(...cs)-Math.min(...cs)<=1),
            twoLines:centers.every(cs=>Math.abs(cs[0]-cs[1])<=1 && cs[2]>cs[0] && Math.abs(cs[2]-cs[3])<=1 && Math.abs(cs[3]-cs[4])<=1),
            aligned:cells.every(bs=>bs.every((b,i)=>Math.abs(i===4?b.right-headers[i].right:b.x-headers[i].x)<=1)),
            contained:rows.every(r=>r.scrollWidth<=r.clientWidth+1 && r.querySelector('.awb-run-tokens').clientWidth>=36 && [...r.children].every(n=>{
              const b=n.getBoundingClientRect(),p=r.getBoundingClientRect();return b.x>=p.x && b.right<=p.right+1;
            }))};
        });
        assert.equal(runLayout.heads,1,'每页只出现一个表头');
        assert.ok(runLayout.contained,'记录内容不挤出行边界 '+width+' '+JSON.stringify(runLayout));
        if(width>760){
          assert.ok(runLayout.headVisible && runLayout.singleLine && runLayout.aligned,'桌面明细单行且表头列对齐 '+width+' '+JSON.stringify(runLayout));
          assert.ok(runLayout.maxRow<=40 && runLayout.height<=270,'五条记录和分页压紧，不再占据大半屏 '+width+' '+JSON.stringify(runLayout));
        }else{
          assert.ok(!runLayout.headVisible && runLayout.twoLines && runLayout.maxRow<=70,'手机每条使用两行，不硬塞桌面表格 '+width+' '+JSON.stringify(runLayout));
        }
        const layoutTick=page.locator('.awb-job-recent[data-id="layout-state-0"] .awb-tick').last();
        await layoutTick.scrollIntoViewIfNeeded();
        await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
        await layoutTick.hover();
        const tipBox=await page.locator('#awbTip').boundingBox();
        assert.ok(tipBox && tipBox.x>=0 && tipBox.x+tipBox.width<=width && tipBox.y>=0 && tipBox.y+tipBox.height<=1000,'提示完整位于屏内 '+width);
        assert.ok((await page.locator('#awbTip').innerText()).includes('无运行记录'));
        if([1440,390].includes(width)){
          await page.mouse.move(0,0);
          await page.evaluate(()=>{document.activeElement?.blur();$('#workbenchView').scrollTop=0;});
          await page.screenshot({path:output+'/workbench-'+theme+'-'+width+'.png',fullPage:true,animations:'disabled'});
        }
      }
    }
    await checkWorkbenchSafeArea(page,output);
    await page.evaluate(()=>{
      AWB.data=window.awbLayoutOriginal;AWB.histories=window.awbLayoutHistories;
      delete window.awbLayoutOriginal;delete window.awbLayoutHistories;renderAutomationWorkbench();
    });
    await page.locator('[data-awb="todos"]').click();await page.waitForSelector('[data-open-automation]');
    await page.locator('[data-open-automation]').click();await page.waitForSelector('#awbSearch');
    await page.fill('#awbSearch','不存在的关键词');
    assert.equal(await page.locator('#awbOwners>.awb-owner').count(),0);
    await page.fill('#awbSearch','');
    assert.equal(await page.locator('#awbOwners>.awb-owner').count(),1);
    await page.locator('[data-awb-fold="orphan"]>summary').click();
    await page.locator('.aptmain[data-awb="job-runs"][data-id="deadbeef"]').click();
    await page.waitForFunction(()=>document.querySelectorAll('[data-awb-fold="orphan"] .aprun').length===1);
    assert.ok(await page.locator('[data-awb-fold="orphan"]').evaluate(n=>n.open),'未归属历史切换后保持展开');
    assert.equal(await history.locator('.aprun').count(),5,'另一个任务展开不影响原任务的页码和记录');
    assert.ok((await history.locator('.awb-pager').innerText()).includes('第 7 页'));
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
    await page.waitForFunction(job=>document.querySelectorAll(`.awb-task[data-job="${job}"] .aprun`).length===5,job);
    const movedHistory=page.locator(`.awb-task[data-job="${job}"] .awb-job-history`);
    const movedIds=await movedHistory.locator('[data-awb="source-run"]').evaluateAll(nodes=>nodes.map(n=>n.dataset.id));
    for(let n=2;n<=7;n++){
      await movedHistory.locator('[data-awb="job-next"]').click();
      await page.waitForFunction(({job,n})=>document.querySelector(`.awb-task[data-job="${job}"] .awb-pager`)?.textContent.includes('第 '+n+' 页') && document.querySelectorAll(`.awb-task[data-job="${job}"] .aprun`).length===5,{job,n});
      movedIds.push(...await movedHistory.locator('[data-awb="source-run"]').evaluateAll(nodes=>nodes.map(n=>n.dataset.id)));
    }
    assert.deepEqual(movedIds,allIds,'任务移动后仍可逐页查看全部35条原历史');
    const copiedJob=await page.evaluate(({job,emptyAgent})=>AWB.data.jobs.find(j=>j.agent_id===emptyAgent && j.id!==job).id,{job,emptyAgent});
    await page.locator(`.aptmain[data-id="${copiedJob}"]`).click();
    await page.waitForFunction(job=>document.querySelector(`.awb-task[data-job="${job}"] .awb-job-history`)?.textContent.includes('暂无运行记录'),copiedJob);
    assert.ok(await page.locator(`.awb-task[data-job="${copiedJob}"] [data-awb="job-next"]`).isDisabled());
    // 新工作台的作品库入口也必须等待按需模块，不能提前引用 LIB。
    await page.setViewportSize({width:1440,height:1000});
    assert.equal(await page.evaluate(()=>typeof LIB),'undefined');
    await page.locator(`[data-awb="products"][data-id="${emptyAgent}"]`).click();
    await page.locator(`[data-awb="library"][data-id="${emptyAgent}"]`).click();
    await page.waitForFunction(()=>$('#libraryView') && !$('#libraryView').hidden && !LIB.loading);
    assert.equal(await page.evaluate(()=>LIB.filters.agent_id),emptyAgent);
    await page.goto(origin+'/workbench/window');
    await page.waitForSelector('.awb-owner');
    await checkWorkbenchSafeArea(page,output);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true,widths:8,themes:2,history:35,nodes:3,management:true,aligned:true,lazyLibrary:true}));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
