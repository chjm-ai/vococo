const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
  const [origin,token]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    const page=await browser.newPage(), errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(origin); await page.fill('#pw',token); await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden);
    // 只替换历史数据；会话切换、事件缓冲、快照及重绘均走正式页面代码。
    let draft='开头。', pending=true;
    await page.route('**/history?conv=switch-a',route=>route.fulfill({json:{turns:[{
      id:501,user:'测试切换',pending,draft:pending?draft:'',assistant:pending?'':draft,
    }],has_more:false}}));
    await page.evaluate(async()=>{
      await openConv('switch-a');
      handleEvent({type:'start',conv:'switch-a',id:101});
      handleEvent({type:'text',conv:'switch-a',id:102,seg:0,text:'开头。'});
      await openConv('local-switch-b');
      handleEvent({type:'text',conv:'switch-a',id:103,seg:0,text:'开头。中间。'});
      await openConv('switch-a');
    });
    assert.equal(await page.evaluate(()=>streamText(S.stream)),'开头。中间。','快照恢复必须补上后台对同段全文的更新');
    draft='开头。中间。追平。';
    await page.evaluate(()=>reloadHistory(true));
    assert.equal(await page.evaluate(()=>[...S.stream.flow.querySelectorAll('.seg')].map(n=>n.dataset.raw).join('')),draft,'断流时历史草稿仍能追平显示');
    draft='开头。中间。追平。结尾。';
    await page.evaluate(text=>{
      handleEvent({type:'text',conv:'switch-a',id:104,seg:0,text});
      handleEvent({type:'done',conv:'switch-a',id:105,turn_id:501,text});
    },draft);
    const actual=await page.evaluate(()=>[...$('#wrap').querySelectorAll('.row.ai .seg')].map(n=>n.dataset.raw||n.textContent).join(''));
    assert.equal(actual,draft,'切回 A 后历史追平，再继续同段全文吐字，最终正文不能重复');
    assert.equal(await page.locator('#wrap .row.ai').count(),1);
    assert.equal(await page.locator('#wrap .status').count(),0);
    pending=false;
    await page.evaluate(()=>reloadHistory(true));
    assert.equal(await page.locator('#wrap .row.ai').count(),1,'完成后重载历史仍只有一条回复');
    // 草稿不带段号：覆盖同段、多段、丢失末帧和上游改写，不能拿它伪造真实段号。
    for(const scenario of [
      {name:'同段分次追上',initial:['甲'],draft:'甲乙丙',updates:[[0,'甲乙'],[0,'甲乙丙丁']],final:'甲乙丙丁'},
      {name:'工具前后多个段',initial:['甲','乙'],draft:'甲乙丙',updates:[[2,'丙丁']],final:'甲乙丙丁'},
      {name:'工具之后收尾',initial:['甲'],draft:'甲乙',updates:[],final:'甲乙',tool:true},
      {name:'末帧丢失',initial:['甲'],draft:'甲乙',updates:[],final:'甲乙丙'},
      {name:'完成事件无全文',initial:['甲'],draft:'甲乙',updates:[],final:undefined,expected:'甲乙'},
      {name:'正文改写',initial:['甲'],draft:'甲乙',updates:[[0,'重新回答']],final:'重新回答'},
    ]){
      pending=true; draft=scenario.initial.join('');
      await page.evaluate(async initial=>{
        await openConv('local-switch-b'); await openConv('switch-a');
        handleEvent({type:'start',conv:S.conv});
        initial.forEach((text,seg)=>handleEvent({type:'text',conv:S.conv,seg,text}));
      },scenario.initial);
      draft=scenario.draft;
      await page.evaluate(()=>reloadHistory(true));
      await page.evaluate(async scenario=>{
        for(const [seg,text] of scenario.updates) handleEvent({type:'text',conv:S.conv,seg,text});
        if(scenario.tool){
          handleEvent({type:'tool_start',conv:S.conv,name:'Bash',tool_id:'order-tool'});
          handleEvent({type:'tool_input',conv:S.conv,name:'Bash',tool_id:'order-tool',input:{command:'pwd'}});
          handleEvent({type:'tool_end',conv:S.conv,name:'Bash',tool_id:'order-tool',ok:true});
          await openConv('local-switch-b'); await openConv('switch-a');
        }
        handleEvent({type:'done',conv:S.conv,turn_id:501,text:scenario.final});
      },scenario);
      const text=await page.evaluate(()=>[...$('#wrap').querySelectorAll('.row.ai .seg')].map(n=>n.dataset.raw||n.textContent).join(''));
      assert.equal(text,scenario.expected||scenario.final,scenario.name);
      assert.equal(await page.locator('#wrap .row.ai').count(),1,scenario.name);
      if(scenario.tool) assert.deepEqual(await page.evaluate(()=>[...$('#wrap .flow').children].map(n=>n.classList.contains('seg')?n.dataset.raw:'工具')),['甲','乙','工具'],'快照恢复及收尾保持正文与工具顺序');
    }
    // 主会话正在输出时收到定时结果：不能打断输出，也不能永久漏刷。
    for(const terminal of ['done','cancelled']){
      let finished=false;
      await page.route('**/history?conv=agent-feed',route=>route.fulfill({json:{turns:[
        {id:601,user:'当前问题',pending:!finished,assistant:finished?'当前回答':'',draft:finished?'':'当前'},
        {id:602,user:'',pending:false,assistant:'定时结果已到达'},
      ],has_more:false}}));
      await page.evaluate(async()=>{
        await openConv('local-switch-b');
        S.conv='agent-feed'; S.stream=null; $('#wrap').replaceChildren();
        handleEvent({type:'start',conv:S.conv});
        handleEvent({type:'text',conv:S.conv,seg:0,text:'当前'});
        handleEvent({type:'agent_run',conv:S.conv});
      });
      assert.equal(await page.evaluate(()=>streamText(S.stream)),'当前','外部结果不能打断当前输出');
      assert.equal(await page.evaluate(()=>S.historyRefreshPending?.['agent-feed']),true);
      // 强制核对仍在运行的历史，也不能丢掉待补刷标记。
      await page.evaluate(()=>reloadHistory(true));
      assert.equal(await page.evaluate(()=>S.historyRefreshPending?.['agent-feed']),true);
      finished=true;
      await page.evaluate(type=>handleEvent({type,conv:S.conv,turn_id:601,text:'当前回答'}),terminal);
      await page.waitForFunction(()=>$('#wrap').textContent.includes('定时结果已到达'));
      assert.equal(await page.evaluate(()=>S.historyRefreshPending?.['agent-feed']),undefined);
      await page.unroute('**/history?conv=agent-feed');
    }
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true,scenarios:9}));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
