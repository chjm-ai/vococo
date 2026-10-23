const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const http=require('node:http');
const staticDir=path.resolve(__dirname,'../vococo/gateway/adapters/web_static');
const renderer=fs.readFileSync(path.join(staticDir,'markdown.js'),'utf8').split('// ── 文档预览分屏')[0];
const sample='3. 父项\n   - 子项\n     1. 孙项\n     2. 孙项二\n   - 子项二\n4. 父项二\n\n结束';
(async()=>{
  const server=http.createServer((req,res)=>{
    res.setHeader('Content-Type',req.url==='/styles.css'?'text/css':'text/html');
    res.end(req.url==='/styles.css'?fs.readFileSync(path.join(staticDir,'styles.css')):
      '<!doctype html><link rel="stylesheet" href="/styles.css"><div class="bubble" id="result"></div>');
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    const page=await browser.newPage();
    const origin=process.argv[2];
    await page.goto(origin||'http://127.0.0.1:'+server.address().port);
    if(origin){
      await page.fill('#pw',process.argv[3]); await page.click('#loginBtn');
      await page.waitForFunction(()=>!$('#app').hidden);
      await page.evaluate(async()=>{await openConv('nested-list-test');closeSidebar();setSidebarCollapsed(true);});
    }else{
      await page.addScriptTag({content:'function esc(s){return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");} function ic(){return "";}'+renderer});
    }
    const render=src=>page.evaluate(({src,real})=>{
      if(real){$('#wrap').replaceChildren();addBubble('ai',src);document.querySelector('#wrap .bubble').id='result';}
      else document.querySelector('#result').innerHTML=mdToHtml(src);
    },{src,real:!!origin});
    await render(sample);
    assert.equal(await page.locator('#result > ol > li > ul > li > ol > li').count(),2,'混合列表保留三层结构');
    assert.equal(await page.locator('#result > ol').getAttribute('start'),'3','保留起始编号');
    assert.equal(await page.locator('#result > ol > li').count(),2);
    for(const width of [320,375,390,768,1280]){
      await page.setViewportSize({width,height:900});
      const positions=await page.locator('#result').evaluate(n=>{
        const lists=[n.querySelector('ol'),n.querySelector('ol ul'),n.querySelector('ol ul ol')];
        return {lefts:lists.map(x=>x.getBoundingClientRect().left),overflow:n.scrollWidth>n.clientWidth};
      });
      assert.ok(positions.lefts[0]<positions.lefts[1]&&positions.lefts[1]<positions.lefts[2],'每层都有可见缩进');
      assert.equal(positions.overflow,false,'窄屏不溢出');
    }
    for(const marker of ['-','*','+','1.','1)']){
      await render(`${marker} 一级\n    ${marker} 二级\n        ${marker} 三级\n${marker} 同级`);
      assert.equal(await page.locator('#result li li li').count(),1,'同类列表支持三层');
      assert.equal(await page.locator('#result > :first-child > li').count(),2);
    }
    await render('1. 标题\n   续行\n\n2. 第二项\n\n正文');
    assert.equal(await page.locator('#result > ol').count(),1,'空行不拆散编号');
    assert.match(await page.locator('#result > ol > li').first().textContent(),/标题.*续行/);
    assert.equal(await page.locator('#result > p').textContent(),'正文');
    await render('- 父项\n  - <script>alert(1)</script>\n\n# 标题');
    assert.equal(await page.locator('#result script').count(),0);
    assert.equal(await page.locator('#result > h1').textContent(),'标题');
    await render('- 父项\n  ```js\n  const a = 1;\n  ```\n- 第二项');
    assert.equal(await page.locator('#result > ul > li .codeblock').count(),1,'代码块仍归属列表项');
    await render('  ```js\nconst a = 1;\n```\n\n正文');
    assert.equal(await page.locator('#result > p').textContent(),'正文','结束围栏允许比起始围栏少缩进');
    await render('- 父项\n  1. 子项\n     ```js\n     const nested = 1;\n     ```\n- 同级');
    assert.equal(await page.locator('#result > ul > li > ol > li .codeblock').count(),1,'深层代码块保留归属');
    assert.equal(await page.locator('#result code').textContent(),'const nested = 1;');
    for(let i=1;i<=sample.length;i++){
      await page.evaluate(src=>{document.querySelector('#result').innerHTML=mdToHtml(src,true);},sample.slice(0,i));
    }
    assert.equal(await page.locator('#result > ol > li > ul > li > ol > li').count(),2,'流式输出结束保持层级');
    console.log('PASS: 多层有序/无序/混合列表、起始编号、续行、空行、代码块、转义、流式输出、五档屏宽');
  }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(e=>{console.error(e);process.exitCode=1;});
