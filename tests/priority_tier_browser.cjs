// 正式整页与真实 /models、/model、/service-tier；只拦截明确模拟的保存失败。
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const GPT='gpt-6.1-sol';

async function ready(page){
  await page.waitForFunction(()=>!document.querySelector('#app').hidden &&
    S.models?.service_tiers?.['gpt-6.1-sol'] && !document.querySelector('#effortBtn').disabled);
}

async function openEffort(page){
  if(!await page.locator('#effortPop').isVisible()) await page.click('#effortBtn');
  await page.locator('#effortPop').waitFor({state:'visible'});
}

async function switchState(page,checked){
  await page.waitForFunction(expected=>{
    const input=document.querySelector('#prioritySwitch');
    return input && !input.disabled && input.checked===expected;
  },checked);
  assert.equal(await page.locator('#prioritySwitch').isChecked(),checked);
  const name=await page.locator('#effortName').innerText();
  assert.equal(name.endsWith('⚡2x'),checked);
  assert.equal(name.replace(/⚡2x$/, ''),await page.evaluate(()=>{
    const state=effortState();
    return effortShort(state.value,state.levels);
  }));
}

async function savedTier(page,token,tier){
  const conv=await page.evaluate(()=>permConvId(S.conv));
  const response=await page.request.get('/models?conv='+encodeURIComponent(conv),{headers:{'X-Auth-Token':token}});
  assert.equal(response.status(),200);
  assert.deepEqual((await response.json()).service_tiers[GPT],{supported:true,value:tier});
}

async function fits(page){
  const problems=await page.evaluate(()=>{
    const failures=[];
    const viewport=document.documentElement.clientWidth;
    if(document.documentElement.scrollWidth>viewport+1) failures.push('页面横向溢出');
    for(const selector of ['#effortPop','.priority-tier','.priority-label','.priority-tier .sw']){
      const el=document.querySelector(selector), r=el.getBoundingClientRect();
      if(r.width<=0 || r.height<=0 || r.left<0 || r.right>viewport+1 ||
        r.top<0 || r.bottom>innerHeight+1) failures.push(selector+' 超出视口');
      if(el.scrollWidth>el.clientWidth+1) failures.push(selector+' 内部横向溢出');
      const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
      if(!el.contains(hit)) failures.push(selector+' 被遮挡');
    }
    return failures;
  });
  assert.deepEqual(problems,[]);
}

async function toggle(page,checked,fail=false){
  await switchState(page,!checked);
  const responsePromise=page.waitForResponse(r=>new URL(r.url()).pathname==='/service-tier' &&
    r.request().method()==='POST');
  await page.locator('.priority-label').click();
  const response=await responsePromise;
  const conv=await page.evaluate(()=>permConvId(S.conv));
  assert.deepEqual(response.request().postDataJSON(),{conv,model:GPT,tier:checked?'priority':''});
  assert.equal(response.status(),fail?503:200);
  await switchState(page,fail?!checked:checked);
  if(fail){
    const error=page.locator('#effortPop [role="alert"]');
    await error.waitFor({state:'visible'});
    assert.match(await error.innerText(),/保存失败/);
  }else{
    assert.deepEqual(await response.json(),{ok:true,model:GPT,tier:checked?'priority':''});
    assert.equal(await page.locator('#effortPop [role="alert"]').isVisible(),false);
  }
}

async function pickModel(page,model){
  await page.click('#modelBtn');
  const posted=page.waitForResponse(r=>new URL(r.url()).pathname==='/model' &&
    r.request().method()==='POST');
  await page.locator(`#modelPop .mi[data-m="${model}"]`).click();
  assert.equal((await posted).status(),200);
  await page.waitForFunction(expected=>curModel()===expected,model);
  await openEffort(page);
}

async function failureRollback(page,token,wasChecked){
  const reject=route=>route.fulfill({status:503,contentType:'application/json',
    body:JSON.stringify({error:'测试模拟保存失败'})});
  await page.route('**/service-tier',reject);
  try{
    await toggle(page,!wasChecked,true);
    await savedTier(page,token,wasChecked?'priority':'');
    await fits(page);
  }finally{await page.unroute('**/service-tier',reject);}
}

async function exercise(page,origin,token){
  await page.goto(origin);
  await page.fill('#pw',token);
  await page.click('#loginBtn');
  await ready(page);
  await openEffort(page);
  assert.equal(await page.locator('.priority-label').innerText(),'⚡2x');
  await switchState(page,false);
  await savedTier(page,token,'');
  await fits(page);
  await failureRollback(page,token,false);
  await toggle(page,true);
  await savedTier(page,token,'priority');
  await page.reload();
  await ready(page);
  await openEffort(page);
  await switchState(page,true);
  await savedTier(page,token,'priority');
  await fits(page);
  for(const model of ['claude-sonnet-5-5','deepseek-flash']){
    await pickModel(page,model);
    assert.equal(await page.locator('#prioritySwitch').count(),0);
    assert.equal(await page.locator('.priority-label').count(),0);
    assert.doesNotMatch(await page.locator('#effortName').innerText(),/⚡2x/);
    await savedTier(page,token,'priority');
  }
  await pickModel(page,GPT);
  await switchState(page,true);
  await failureRollback(page,token,true);
  await toggle(page,false);
  await savedTier(page,token,'');
  await page.reload();
  await ready(page);
  await openEffort(page);
  await switchState(page,false);
  await savedTier(page,token,'');
  await fits(page);
}

async function openSession(page,conv){
  await page.evaluate(conv=>openConv(conv),conv);
  await page.waitForFunction(conv=>S.models._conv===conv &&
    S.models.efforts?.['gpt-6.1-sol'],conv);
}

async function chooseEffort(page,value){
  await openEffort(page);
  const posted=page.waitForResponse(r=>new URL(r.url()).pathname==='/effort');
  await page.locator(`#effortPop .mi[data-e="${value}"]`).click();
  assert.equal((await posted).status(),200);
}

async function sessionIsolation(page){
  await openSession(page,'session:browser-a');
  await chooseEffort(page,'high');
  await openEffort(page);
  await toggle(page,true);
  await openSession(page,'session:browser-b');
  await openEffort(page);
  await switchState(page,false);
  await chooseEffort(page,'low');
  await pickModel(page,'gpt-6.1-terra');
  await openSession(page,'session:browser-a');
  assert.equal(await page.evaluate(()=>curModel()),GPT);
  assert.equal(await page.evaluate(()=>effortState().value),'high');
  await openEffort(page);
  await switchState(page,true);
  await page.evaluate(()=>openCallView());
  await page.waitForFunction(()=>S.models._conv==='voice-chat:main' && S.models.efforts?.['gpt-6.1-sol']);
  assert.equal(await page.evaluate(()=>curModel()),GPT);
  await openEffort(page);
  await switchState(page,false);
  await openSession(page,'session:browser-b');
  assert.equal(await page.evaluate(()=>curModel()),'gpt-6.1-terra');
  await pickModel(page,GPT);
  assert.equal(await page.evaluate(()=>effortState().value),'low');
  await switchState(page,false);
  // A 的旧请求即使晚于 B 返回，也不能把 B 的胶囊改回 A。
  let release;
  const hold=new Promise(resolve=>{release=resolve;});
  await page.route('**/models?conv=session%3Abrowser-a',async route=>{await hold; await route.continue();});
  await page.evaluate(()=>{openConv('session:browser-a');});
  await openSession(page,'session:browser-b');
  const late=page.waitForResponse(r=>r.url().includes('/models?conv=session%3Abrowser-a'));
  release(); await late;
  await page.waitForTimeout(100);
  assert.equal(await page.evaluate(()=>curModel()),GPT);
  assert.equal(await page.evaluate(()=>effortState().value),'low');
  await page.unroute('**/models?conv=session%3Abrowser-a');
}

async function saveDuringNavigation(page){
  await openSession(page,'session:browser-b');
  await openEffort(page);
  let release;
  const hold=new Promise(resolve=>{release=resolve;});
  let started;
  const waiting=new Promise(resolve=>{started=resolve;});
  await page.route('**/service-tier',async route=>{started(); await hold; await route.continue();});
  await page.locator('.priority-label').click();
  await waiting;
  await openSession(page,'session:browser-a');
  await openSession(page,'session:browser-b');
  await openEffort(page);
  release();
  await switchState(page,true);
  await page.unroute('**/service-tier');
  await toggle(page,false);
}

async function chooseBeforeSettingsLoad(page){
  let release, started;
  const hold=new Promise(resolve=>{release=resolve;});
  const waiting=new Promise(resolve=>{started=resolve;});
  let first=true;
  const pattern='**/models?conv=session%3Abrowser-early';
  await page.route(pattern,async route=>{
    if(first){ first=false; started(); await hold; }
    await route.continue();
  });
  await page.evaluate(()=>{openConv('session:browser-early');});
  await waiting;
  await pickModel(page,'gpt-6.1-terra');
  assert.equal(await page.evaluate(()=>effortState().levels.length>0),true);
  const late=page.waitForResponse(r=>r.url().includes('/models?conv=session%3Abrowser-early'));
  release(); await late;
  await page.unroute(pattern);
}

(async()=>{
  const [origin,token,widthText]=process.argv.slice(2), width=Number(widthText);
  const browser=await chromium.launch({headless:true,
    ...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    const context=await browser.newContext({baseURL:origin,serviceWorkers:'block',
      viewport:{width,height:900},...(width===390?{isMobile:true,hasTouch:true,
        userAgent:'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1'}:{})});
    // 阻断意外的外网请求；页面与全部正常接口仍来自真实临时服务。
    await context.route('**/*',route=>new URL(route.request().url()).origin===origin?
      route.continue():route.abort());
    const page=await context.newPage(), errors=[], posts=[];
    page.setDefaultTimeout(12000);
    page.on('pageerror',error=>errors.push(error.message));
    page.on('response',response=>{
      if(new URL(response.url()).pathname==='/service-tier') posts.push(response.status());
    });
    await exercise(page,origin,token);
    assert.deepEqual(errors,[]);
    assert.deepEqual(posts,[503,200,503,200]);
    await sessionIsolation(page);
    await saveDuringNavigation(page);
    await chooseBeforeSettingsLoad(page);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true,width,successfulPosts:2,failedPosts:2}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
