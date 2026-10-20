// Playwright 加载真实正式页面；仅 HTTP 数据用内存夹具，不接触服务、数据库或模型。
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(__dirname,'../vococo/gateway/adapters/web_static');
const output=process.env.RESEARCH_BROWSER_ARTIFACTS || '/tmp/vococo-research-session-browser';
const origin='http://research-fixture.local';
(async()=>{
  fs.mkdirSync(output,{recursive:true});
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  const context=await browser.newContext({viewport:{width:1280,height:900}});
  await context.tracing.start({screenshots:true,snapshots:true,sources:true});
  const page=await context.newPage(), errors=[], requests=[];
  const histories={source:[],other:[{id:301,ts:1,user:'另一个会话',assistant:'另一条普通回复',pending:false,events:[]}]};
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/*',async route=>{
    const url=new URL(route.request().url());
    requests.push(url.href);
    if(url.origin!==origin) return route.abort();
    const file=path.join(root,url.pathname==='/'?'index.html':url.pathname);
    if(file.startsWith(root+path.sep) && fs.existsSync(file) && fs.statSync(file).isFile()){
      return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html'});
    }
    let data={};
    if(url.pathname==='/history') data={turns:histories[url.searchParams.get('conv')]||[],has_more:false};
    if(url.pathname==='/conversations') data={main:{},conversations:[{conv:'source',title:'研究来源会话'},{conv:'other',title:'另一个会话'}]};
    if(url.pathname==='/conv/perm') data={mode:'standard'};
    return route.fulfill({json:data});
  });
  try{
    await page.goto(origin);
    await page.evaluate(async()=>{
      S.token='isolated-fixture'; $('#login').style.display='none'; $('#app').hidden=false;
      $('#chatMain').hidden=false; $('#callView').hidden=true;
      S.convs=[{conv:'source',title:'研究来源会话'},{conv:'other',title:'另一个会话'}];
      renderConvs(); await openConv('source');
      handleEvent({type:'start',conv:'source',id:1});
      handleEvent({type:'text',conv:'source',seg:0,text:'主回复仍在生成中',id:2});
      window.researchFixtureStream=S.stream;
    });
    histories.source=[{id:101,ts:1,user:'请继续回答当前问题',assistant:'',pending:true,draft:'主回复仍在生成中',events:[]}];
    await page.evaluate(()=>{
      handleEvent({type:'research_update',conv:'source',task_id:'fixture-research',title:'隔离研究',status:'running',progress_note:'搜集资料 · 2个来源',id:3,
        progress:{id:'fixture-research',title:'隔离研究',stage:'collect',status:'running',task_status:'running',usage:{searches:3,fetches:2},budget:{searches:12,fetches:8},checkpoints:{},sources:[],activity:[]}});
    });
    await page.locator('.research-card').waitFor();
    assert.match(await page.locator('.research-card').innerText(),/抓取 2\/8/);
    const report={id:202,ts:2,user:'',assistant:'隔离研究最终报告：证据核验完成。',pending:false,events:[{type:'external_result',key:'research:fixture-research:2'}]};
    histories.source.push(report);
    await page.evaluate(report=>{
      handleEvent({type:'research_result',conv:'source',task_id:'fixture-research',title:'隔离研究',status:'done',text:report.assistant,turn_id:report.id,id:4});
    },report);
    assert.equal(await page.evaluate(()=>S.stream===window.researchFixtureStream),true,'报告到达不能替换主回复对象');
    assert.equal(await page.evaluate(()=>!!S.live.source),true,'报告到达不能结束主回复状态');
    assert.equal(await page.locator('#wrap .row.ai').filter({hasText:report.assistant}).count(),1,'报告独立气泡显示一次');
    await page.getByText('主回复仍在生成中',{exact:true}).waitFor();
    await page.evaluate(async()=>{
      await reloadHistory(true);
      if(S.stream!==window.researchFixtureStream) throw new Error('历史核对误结束主回复');
      handleEvent({type:'text',conv:'source',seg:0,text:'主回复仍在生成中，报告到达后继续输出。',id:5});
    });
    await page.getByText('主回复仍在生成中，报告到达后继续输出。',{exact:true}).waitFor();
    await page.screenshot({path:path.join(output,'01-report-during-pending.png'),fullPage:true});
    await page.evaluate(()=>openConv('other'));
    await page.getByText('另一条普通回复',{exact:true}).waitFor();
    await page.evaluate(()=>openConv('source'));
    await page.locator('#wrap .turn-block[data-tid="202"]').waitFor();
    assert.match(await page.locator('#wrap .turn-block[data-tid="202"]').innerText(),/隔离研究最终报告/,'切换回来从历史重新显示报告');
    assert.equal(await page.locator('#wrap .turn-block[data-tid="202"] .row.me').count(),0,'后台结果没有空白用户气泡');
    assert.equal(await page.locator('#wrap .turn-block[data-tid="202"] .tfregen').count(),0,'后台结果不提供错误的重新生成入口');
    await page.screenshot({path:path.join(output,'02-return-to-source.png'),fullPage:true});
    histories.source[0]={...histories.source[0],assistant:'主回复独立正常完成。',pending:false,draft:''};
    await page.evaluate(()=>{
      handleEvent({type:'text',conv:'source',seg:0,text:'主回复独立正常完成。',id:6});
      handleEvent({type:'done',conv:'source',text:'主回复独立正常完成。',turn_id:101,id:7});
    });
    await page.getByText('主回复独立正常完成。',{exact:true}).waitFor();
    assert.equal(await page.locator('#wrap .turn-block[data-tid="202"]').count(),1,'主回复完成后研究报告仍在');
    await page.screenshot({path:path.join(output,'03-main-reply-completed.png'),fullPage:true});
    assert.deepEqual(errors,[],'浏览器无 JS 异常');
    assert.ok(requests.some(url=>url.endsWith('/stream.js')),'加载真实 stream.js');
    assert.ok(requests.every(url=>url.startsWith(origin+'/')),'没有访问外部或生产服务');
    const result={ok:true,browser:await browser.version(),assertions:['真实 index.html/stream.js','pending期间研究进度','报告独立显示','主回复对象与状态不变','报告到达后继续输出','历史核对不结束主回复','切会话再返回报告保留','主回复独立完成','无空白用户气泡/错误重新生成按钮','无浏览器JS异常','无生产/外部请求'],errors,requests:requests.length,output};
    fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));
  }catch(error){
    await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{});
    console.error('浏览器异常',errors); throw error;
  }finally{
    await context.tracing.stop({path:path.join(output,'trace.zip')});
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
