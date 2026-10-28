// 真实设置页脚本 + aiohttp 接口联调；临时配置不碰生产数据。
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {spawn}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');
const staticDir=path.join(root,'vococo/gateway/adapters/web_static');
const serverCode=`
import tempfile
from pathlib import Path
from aiohttp import web
from vococo import config
from vococo.gateway import settings_store
from vococo.gateway.adapters.web import WebAdapter
with tempfile.TemporaryDirectory() as directory:
    config.WEB_AUTH_TOKEN = ''
    config.AI_BRAIN_DIR = Path(directory)
    settings_store._PATH = Path(directory) / 'settings.json'
    settings_store.list_skills = lambda: []
    settings_store.upsert_web_provider('codex-gpt', {'base_url':'http://127.0.0.1:8317','api_key':'test','model':'gpt-6.1-sol'})
    settings_store.set_web_default_model('gpt-6.1-sol')
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([web.get('/settings',adapter._handle_settings),web.get('/models',adapter._handle_models),web.post('/settings/model',adapter._handle_settings_model)])
    async def fixture(request): return web.Response(text='<html><body></body></html>',content_type='text/html')
    app.router.add_get('/',fixture)
    web.run_app(app,host='127.0.0.1',port=0,print=lambda text: print(text,flush=True))
`;
(async()=>{
  const server=spawn('uv',['run','python','-u','-c',serverCode],{cwd:root,env:{...process.env,CLAUDE_CODE_OAUTH_TOKEN:'test-token'}});
  let browser, stderr='';
  server.stderr.on('data',data=>{stderr+=data;});
  try{
    const origin=await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(new Error('服务启动超时 '+stderr)),20000);
      server.once('exit',code=>{clearTimeout(timer);reject(new Error('服务退出 '+code+' '+stderr));});
      server.stdout.on('data',data=>{const found=String(data).match(/http:\/\/127\.0\.0\.1:\d+/);if(found){clearTimeout(timer);resolve(found[0]);}});
    });
    browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_EXECUTABLE});
    const page=await browser.newPage();
    await page.goto(origin);
    await page.setContent('<button id="settingsBtn"></button><button id="setClose"></button><div id="setTabs"></div><div id="setModal"><div class="setbox"><div class="setbody"><div class="setpane" id="setPane"></div></div></div></div><div id="convBody"></div><div id="messages"></div>');
    await page.addStyleTag({path:path.join(staticDir,'styles.css')});
    await page.addScriptTag({content:`
      const $=selector=>document.querySelector(selector);
      const S={model:'gpt-6.1-sol',models:{},conv:''};
      const ic=()=>'';
      const esc=value=>String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      const api=(url,options)=>fetch(url,options);
      function closeSidebar(){}
      function renderModelChip(){}
      function renderEffortChip(){}
    `});
    const index=fs.readFileSync(path.join(staticDir,'index.html'),'utf8');
    const loadModels=index.match(/async function loadModels\(\)\{[\s\S]*?\n\}/)[0];
    await page.addScriptTag({content:loadModels});
    const core=fs.readFileSync(path.join(staticDir,'app-core.js'),'utf8');
    await page.addScriptTag({content:core.slice(core.indexOf('function html(strings'),core.indexOf('// 标题栏弹层互斥'))});
    await page.addScriptTag({path:path.join(staticDir,'settings.js')});
    await page.evaluate(async()=>{await loadModels();SET.tab='models';await openSettings();});
    await page.locator('#mdAddBtn').click();
    await page.locator('#mdId').fill('gpt-new-browser');
    await page.locator('#mdLabel').fill('测试模型 <安全>');
    await page.locator('#mdProvider').selectOption('codex-gpt');
    await page.locator('#mdOrder').fill('-10');
    await page.locator('#mdSave').click();
    await page.waitForFunction(()=>S.models.choices.some(m=>m[0]==='gpt-new-browser'));
    assert.deepEqual(await page.evaluate(()=>S.models.choices.find(m=>m[0]==='gpt-new-browser')),['gpt-new-browser','测试模型 <安全>','codex']);
    assert.equal(await page.evaluate(()=>S.models.choices.filter(m=>m[2]==='codex')[0][0]),'gpt-new-browser');
    await page.locator('[data-modelrow="gpt-new-browser"] [data-mdedit]').click();
    await page.locator('#mdLabel').fill('已改名');
    await page.locator('#mdSave').click();
    await page.waitForFunction(()=>S.models.choices.some(m=>m[1]==='已改名'));
    await page.locator('[data-modelrow="gpt-new-browser"] .track').click();
    await page.waitForFunction(()=>!S.models.choices.some(m=>m[0]==='gpt-new-browser'));
    await page.locator('[data-modelrow="gpt-new-browser"] .track').click();
    await page.waitForFunction(()=>S.models.choices.some(m=>m[0]==='gpt-new-browser'));
    await page.locator('[data-modelrow="gpt-6.1-terra"] [data-mdedit]').click();
    assert.equal(await page.locator('#mdProvider').isDisabled(),true);
    await page.locator('#mdLabel').fill('Terra 热更新');
    await page.locator('#mdOrder').fill('-20');
    await page.locator('#mdSave').click();
    await page.waitForFunction(()=>S.models.choices.some(m=>m[1]==='Terra 热更新'));
    await page.locator('#mdAddBtn').click();
    await page.locator('#mdId').fill('gpt-wrong-route');
    await page.locator('#mdSave').click();
    await page.waitForFunction(()=>document.querySelector('#mdErr').textContent.includes('供应商'));
    assert.equal(await page.evaluate(()=>S.models.choices.some(m=>m[0]==='gpt-wrong-route')),false);
    await page.locator('#mdCancel').click();
    page.on('dialog',dialog=>dialog.accept());
    await page.locator('[data-mddel="gpt-new-browser"]').click();
    await page.waitForFunction(()=>!S.models.choices.some(m=>m[0]==='gpt-new-browser'));
    assert.equal(await page.evaluate(()=>S.model),'gpt-6.1-sol');
    assert.equal(await page.evaluate(()=>S.models.default),'gpt-6.1-sol');
    // Claude 展示别名编辑后仍只有一条原始配置，删除时不会留一个新副本。
    await page.locator('#mdAddBtn').click();
    await page.locator('#mdId').fill('claude-new-browser');
    await page.locator('#mdProvider').selectOption('codex-gpt');
    await page.locator('#mdSave').click();
    await page.waitForFunction(()=>S.models.choices.some(m=>m[0]==='codex-gpt/claude-new-browser'));
    await page.locator('[data-modelrow="codex-gpt/claude-new-browser"] [data-mdedit]').click();
    assert.equal(await page.locator('#mdId').inputValue(),'claude-new-browser');
    await page.locator('#mdLabel').fill('Claude 改名');
    await page.locator('#mdSave').click();
    await page.waitForFunction(()=>SET.data.models.custom.some(m=>m.label==='Claude 改名'));
    assert.equal(await page.evaluate(()=>SET.data.models.custom.filter(m=>m.id.endsWith('claude-new-browser')).length),1);
    await page.locator('[data-mddel="claude-new-browser"]').click();
    await page.waitForFunction(()=>!S.models.choices.some(m=>m[0]==='codex-gpt/claude-new-browser'));
    for(const width of [375,1280]){
      await page.setViewportSize({width,height:900});
      await page.locator('[data-modelrow="gpt-6.1-terra"] [data-mdedit]').click();
      assert.equal(await page.locator('#mdProvider').isVisible(),true);
      await page.locator('#mdCancel').click();
    }
    console.log('PASS: 新增、供应商、排序、改名、隐藏恢复、错误路由、删除均热生效；当前模型未切换');
  }finally{if(browser) await browser.close();server.kill();}
})().catch(error=>{console.error(error);process.exitCode=1;});
