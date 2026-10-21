// 执行设置页真实函数，覆盖保存、失败回滚和重复操作保护。
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('vococo/gateway/adapters/web_static/settings.js', 'utf8');
const start = source.indexOf('function verbositySettingsHtml(');
assert.ok(start >= 0, '设置页缺少输出详细程度控件');
const select = {dataset:{verbosity:'gpt-6.1-sol'}, value:'low', disabled:false};
const error = {textContent:''};
const model = {value:'low',levels:[['','模型默认'],['low','低'],['medium','中'],['high','高']]};
const SET = {data:{models:{verbosities:{'gpt-6.1-sol':model}, choices:[{id:'gpt-6.1-sol',label:'GPT <Sol>'}]}}};
let requests=[], fail=false, release;
const context = vm.createContext({SET,console,
  esc:x=>String(x).replaceAll('<','&lt;').replaceAll('"','&quot;'),
  $:()=>({querySelectorAll:()=>[select],querySelector:()=>error}),
  api:async(url,options)=>{requests.push([url,JSON.parse(options.body)]);
    if(fail) return {ok:false,json:async()=>({error:'保存失败'})};
    await new Promise(resolve=>release=resolve);
    return {ok:true,json:async()=>({verbosity:requests.at(-1)[1].verbosity})};}
});
vm.runInContext(source.slice(start,source.indexOf('function renderModelsPane()',start)),context);
(async()=>{
  const html=context.verbositySettingsHtml(SET.data.models);
  assert.ok(html.includes('输出详细程度'));
  assert.ok(html.includes('GPT &lt;Sol>'));
  assert.equal((html.match(/<option /g)||[]).length,4);
  context.bindVerbositySettings();
  select.value='high'; const pending=select.onchange();
  assert.equal(select.disabled,true);
  release(); await pending;
  assert.equal(model.value,'high'); assert.equal(select.disabled,false);
  fail=true; select.value='medium'; await select.onchange();
  assert.equal(model.value,'high'); assert.equal(select.value,'high');
  assert.equal(error.textContent,'保存失败'); assert.equal(select.disabled,false);
  fail=false; select.value=''; const reset=select.onchange(); release(); await reset;
  assert.equal(model.value,'');
  assert.equal(requests.at(-1)[1].verbosity,'');
  console.log('通过：输出详细程度渲染、保存、失败回滚和恢复默认');
})().catch(error=>{console.error(error);process.exitCode=1;});
