// 用真实页面和点击事件验证；接口夹具避免操作生产会话或调用模型。
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../vococo/gateway/adapters/web_static');
(async()=>{
  const browser = await chromium.launch({headless:true});
  try{
    const page = await browser.newPage();
    const errors=[];
    let archived=true, deleted=false;
    page.on('pageerror', e=>errors.push(e.message));
    await page.route('http://localhost:8401/**', async route=>{
      const url=new URL(route.request().url());
      const file=path.join(root, url.pathname==='/' ? 'index.html' : url.pathname);
      if(fs.existsSync(file) && fs.statSync(file).isFile()){
        const ext=path.extname(file);
        return route.fulfill({body:fs.readFileSync(file), contentType:ext==='.js'?'application/javascript':ext==='.css'?'text/css':'text/html'});
      }
      let data={};
      if(url.pathname==='/conversations') data={main:{last_ts:0},conversations:[
        ...(deleted?[]:[{conv:'active',title:'未归档会话',last_ts:20}]),
        ...(url.searchParams.get('filter')==='active' && archived?[]:[{conv:'archived',title:'归档会话',archived,last_ts:30}])
      ]};
      if(url.pathname==='/history') data={turns:[]};
      if(url.pathname==='/conv/perm') data={mode:'standard'};
      if(url.pathname==='/conv/git') data={};
      if(url.pathname==='/conv/archive') archived=route.request().postDataJSON().archived;
      if(url.pathname==='/conv/delete'){ deleted=true; data={ok:true}; }
      return route.fulfill({json:data});
    });
    await page.goto('http://localhost:8401/');
    await page.evaluate(()=>{
      $('#login').style.display='none'; $('#app').hidden=false; $('#chatMain').hidden=false;
      S.conv='local-draft'; S.project=null; S.convFilter='active';
      S.models={default:'test',choices:[['test','测试模型','api']],efforts:{test:{levels:[['high','高']],value:'high'}}};
      S.commands=[{name:'help',desc:'帮助'}];
      renderProjSelChip(); renderModelChip(); renderEffortChip(); renderPermChip();
      $('#callView').hidden=true;
    });
    const buttons=['projSelBtn','permBtn','effortBtn','modelBtn'];
    const pops=['projSelPop','permPop','effortPop','modelPop','cmdPop'];
    for(const first of buttons){
      for(const second of buttons){
        await page.evaluate(()=>{ for(const id of ['projSelPop','permPop','effortPop','modelPop']) $('#'+id).hidden=true; });
        await page.click('#'+first);
        await page.click('#'+second);
        const visible=await page.evaluate(ids=>ids.filter(id=>!$('#'+id).hidden),pops);
        assert.equal(visible.length,first===second?0:1, first+' → '+second);
      }
    }
    await page.click('#modelBtn');
    await page.fill('#ta','/');
    assert.deepEqual(await page.evaluate(ids=>ids.filter(id=>!$('#'+id).hidden),pops),['cmdPop']);
    await page.click('#permBtn');
    assert.equal(await page.evaluate(()=>S.cmd.open),false);
    await page.keyboard.press('Escape');
    assert.deepEqual(await page.evaluate(ids=>ids.filter(id=>!$('#'+id).hidden),pops),[]);
    await page.evaluate(async()=>{ await loadConvs(); updateEmpty(); });
    await page.waitForFunction(()=>$('#homeRecent').textContent.includes('已归档'));
    assert.match(await page.locator('#homeRecent').innerText(),/归档会话.*已归档/s);
    assert.equal(await page.evaluate(()=>S.convs.some(c=>c.conv==='archived')),false,'侧栏仍保持未归档筛选');
    await page.locator('.home-item').filter({has:page.getByText('归档会话',{exact:true})}).click();
    await page.waitForFunction(()=>S.conv==='archived');
    assert.equal(await page.locator('#convTitle').innerText(),'归档会话');
    assert.equal(await page.evaluate(()=>findConv('archived').archived),true);
    await page.evaluate(async()=>{ await loadConvs(); });
    assert.equal(await page.evaluate(()=>findConv('archived').archived),true,'刷新侧栏仍保留当前归档会话的操作对象');
    await page.evaluate(async()=>{ await toggleArchive('archived'); });
    assert.equal(await page.evaluate(()=>findConv('archived').archived),false);
    await page.evaluate(async()=>{
      S.conv='local-draft';
      S.homeConvs=[{conv:'active',title:'未归档会话',last_ts:20}];
      await delConv('active');
    });
    assert.equal(await page.evaluate(()=>findConv('active')),undefined,'删除清理首页和侧栏副本');
    await page.waitForFunction(()=>homeConvsRequest===null);
    deleted=false;
    await page.evaluate(async()=>{
      $('#empty').style.display='none';
      await loadConvs();
      $('#empty').style.display='flex';
      S.homeConvs=null;
      const realApi=api;
      let release;
      api=(url,options)=>{
        if(url==='/conversations'){
          api=realApi;
          return new Promise(resolve=>{ release=()=>resolve({ok:true,json:async()=>({conversations:[{conv:'active',title:'旧响应会话'}]})}); });
        }
        return realApi(url,options);
      };
      loadHomeConvs();
      await delConv('active');
      release();
    });
    await page.waitForFunction(()=>homeConvsRequest===null && S.homeConvs!==null);
    assert.equal(await page.evaluate(()=>S.homeConvs.some(c=>c.conv==='active')),false,'删除后旧响应不能复活会话');
    for(const width of [320,375,390,768,1280]){
      await page.setViewportSize({width,height:900});
      await page.evaluate(()=>{
        S.conv='local-draft'; renderProjSelChip(); updateEmpty();
        closeComposerPopovers();
      });
      await page.click('#modelBtn');
      await page.evaluate(()=>{
        usageTipEl=document.createElement('div'); usageTipEl.style.display='block';
      });
      await page.click('#effortBtn');
      assert.equal(await page.evaluate(()=>usageTipEl.style.display),'none');
      assert.deepEqual(await page.evaluate(ids=>ids.filter(id=>!$('#'+id).hidden),pops),['effortPop']);
      await page.locator('#convTitle').click();
      assert.deepEqual(await page.evaluate(ids=>ids.filter(id=>!$('#'+id).hidden),pops),[]);
    }
    deleted=false;
    await page.evaluate(()=>{
      S.convFilter='archived'; S.homeConvs=null;
      S.convs=[{conv:'archived',title:'归档会话',archived:true,last_ts:30}];
      updateEmpty();
    });
    await page.waitForFunction(()=>$('#homeRecent').textContent.includes('未归档会话'));
    await page.evaluate(()=>{
      S.agents=[{id:'project',project_hash:'abc',main_conv:'pabc:main'}];
      S.conv='local-pabc:draft';
      S.homeConvs=[{conv:'pabc:old',title:'本项目归档',archived:true,last_ts:40},
        {conv:'pdef:old',title:'别的项目归档',archived:true,last_ts:50}];
      renderEmptyRecent();
    });
    assert.match(await page.locator('#homeRecent').innerText(),/本项目归档/);
    assert.doesNotMatch(await page.locator('#homeRecent').innerText(),/别的项目归档/);
    assert.deepEqual(errors,[]);
    console.log('PASS: 浮层逐对互斥、重复点击、命令菜单、Escape、外部点击、额度提示清理；五档屏宽；归档列表、点击标题、双向筛选与 Agent 范围');
  }finally{ await browser.close(); }
})().catch(e=>{ console.error(e); process.exitCode=1; });
