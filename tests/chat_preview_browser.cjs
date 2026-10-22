const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
(async()=>{
  const [origin,token,screenshots]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    const page=await browser.newPage({viewport:{width:1280,height:1000}}),errors=[], external=[];
    page.on('pageerror',e=>errors.push(e.message));
    // CSP 拦截也会产生 request/requestfailed；只有到达网络路由才算泄漏。
    await page.route('https://preview-attacker.invalid/**',route=>{external.push(route.request().url());return route.fulfill({body:'blocked test endpoint'});});
    await page.goto(origin); await page.fill('#pw',token); await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(async()=>{await openConv('local-preview-test');$('#wrap').replaceChildren();});
    const stream=async text=>{await page.evaluate(text=>applyStreamEvent({type:'text',conv:S.conv,text,seg:0}),text);await page.waitForTimeout(70);};
    const assertCompact=async (target, expectedState)=>{
      for(const card of await target.locator('.chat-preview').all()){
        const bar=card.locator('.preview-toolbar'),status=bar.getByRole('status');
        assert.equal(await status.getAttribute('data-state'),expectedState);
        assert.ok(await status.getAttribute('aria-label'));
        assert.equal(await status.getAttribute('title'),await status.getAttribute('aria-label'));
        assert.equal(await status.innerText(),'','状态文本不占行');
        assert.equal(await card.locator('.preview-note').isVisible(),false);
        assert.equal(await bar.locator('.preview-title > svg[aria-hidden="true"]').count(),1);
        assert.equal(await bar.locator('button').count(),2);
        for(const button of await bar.getByRole('button').all()){
          assert.ok(await button.getAttribute('aria-label'));
          assert.equal(await button.getAttribute('title'),await button.getAttribute('aria-label'));
          assert.equal(await button.innerText(),'','操作只显示 SVG');
          assert.equal(await button.locator('svg[aria-hidden="true"]').count(),1);
        }
        assert.equal(await bar.getByRole('button',{name:'显示源码',exact:true}).getAttribute('aria-pressed'),'false');
        assert.equal(await bar.evaluate(n=>{
          const rect=n.getBoundingClientRect(),parts=[...n.children].map(n=>n.getBoundingClientRect());
          return parts.every(r=>r.top>=rect.top && r.bottom<=rect.bottom && r.left>=rect.left && r.right<=rect.right)
            && Math.max(...parts.map(r=>r.top))<Math.min(...parts.map(r=>r.bottom))
            && rect.height<=48 && parts.at(-1).right-parts[0].left<=260;
        }),true,'标题、状态、按钮紧邻且单行');
      }
    };
    await stream('```mermaid\n');
    assert.match(await page.locator('.preview-status').getAttribute('aria-label'),/生成中/);
    assert.equal(await page.locator('.preview-source').isVisible(),false);
    assert.equal(await page.locator('.chat-preview iframe').count(),0);
    await stream('```mermaid\nflowchart LR\n  A[开始] --> B[预览]\n');
    await page.waitForSelector('.chat-preview iframe');
    const first=await page.locator('.chat-preview iframe').elementHandle();
    await page.evaluate(()=>{window.savedFrame=document.querySelector('.chat-preview iframe');window.savedFrame.loads=0;window.savedFrame.addEventListener('load',()=>window.savedFrame.loads++);});
    const before=await first.getAttribute('srcdoc');
    await stream('```mermaid\nflowchart LR\n  A[开始] --> B[预览]\n  B --> C[');
    await page.waitForTimeout(1000);
    assert.equal(await page.evaluate(()=>document.querySelector('.chat-preview iframe')===window.savedFrame),true,'半行不销毁旧预览');
    assert.equal(await page.evaluate(()=>window.savedFrame.loads),0,'正文每帧更新不重新加载 iframe');
    assert.equal(await first.getAttribute('srcdoc'),before);
    await stream('```mermaid\nflowchart LR\n  A[开始] --> B[预览]\n  B --> C[完成]\n');
    await page.waitForFunction(()=>document.querySelector('.chat-preview iframe').srcdoc.includes('完成')).catch(async e=>{console.error(await page.evaluate(()=>({status:document.querySelector('.preview-status').getAttribute('aria-label'),source:document.querySelector('.chat-preview')._preview.source,rendered:document.querySelector('.chat-preview')._preview.rendered,svg:document.querySelector('.chat-preview iframe').srcdoc})));throw e;});
    await assertCompact(page,'busy');
    assert.notEqual(await page.locator('.preview-status svg').evaluate(n=>getComputedStyle(n).animationName),'none');
    await page.emulateMedia({reducedMotion:'reduce'});
    assert.equal(await page.locator('.preview-status svg').evaluate(n=>getComputedStyle(n).animationName),'none');
    await page.emulateMedia({reducedMotion:'no-preference'});
    await page.screenshot({path:path.join(screenshots,'chat-preview-streaming.png'),fullPage:true});
    await page.locator('.chat-preview').screenshot({path:path.join(screenshots,'chat-preview-compact-streaming.png')});
    await stream('```mermaid\nflowchart LR\n  A[开始] --> B[预览]\n  B --> C[完成]\n```');
    await page.evaluate(()=>finalizeStream(undefined,undefined,123));
    await page.waitForFunction(()=>document.querySelector('.preview-status').getAttribute('aria-label')==='已完成');
    await stream('```html\n');
    assert.match(await page.locator('.preview-status').last().getAttribute('aria-label'),/生成中/);
    await stream('```html\n<h2>HTML 渐进预览</h2>\n');
    await page.waitForFunction(()=>[...document.querySelectorAll('.chat-preview iframe')].some(n=>n.srcdoc.includes('HTML 渐进预览')));
    const htmlFrame=await page.locator('.chat-preview iframe').last().elementHandle();
    const stableHtml=await htmlFrame.getAttribute('srcdoc');
    await stream('```html\n<h2>HTML 渐进预览</h2>\n<p>尚未完整');
    await page.waitForTimeout(900);
    assert.equal(await htmlFrame.getAttribute('srcdoc'),stableHtml,'HTML 半行同样保留上版');
    await stream('```html\n<h2>HTML 渐进预览</h2>\n<p>已定稿</p>\n```');
    await page.evaluate(()=>finalizeStream(undefined,undefined,124));
    await page.waitForFunction(()=>[...document.querySelectorAll('.chat-preview iframe')].some(n=>n.srcdoc.includes('已定稿')));
    // 历史多个入口：老数据、时间线、草稿、独立消息。
    await page.evaluate(()=>{
      $('#wrap').replaceChildren();
      const mermaid='```mermaid\nflowchart LR\n A[输入] --> B[安全预览]\n B --> C[定稿]\n```';
      const html='```html\n<style>body{font-family:system-ui;padding:20px;color:#29261f;background:#fff7ed}h2{color:#bd5d3a}.card{padding:20px;border:1px solid #edc9b4;border-radius:12px}</style><div class="card"><h2>HTML 静态预览</h2><p>无脚本 · 无外部请求 · 独立沙箱</p></div>\n```';
      $('#wrap').append(buildTurnBlock({id:1,user:'流程示意',assistant:mermaid},S.conv,false));
      $('#wrap').append(buildTurnBlock({id:2,user:'静态卡片',assistant:html,events:[{type:'text',text:html}]},S.conv,true));
    });
    await page.waitForFunction(()=>document.querySelectorAll('.chat-preview iframe').length===2 && [...document.querySelectorAll('.preview-status')].every(n=>n.getAttribute('aria-label')==='已完成'));
    await page.frameLocator('.chat-preview iframe').first().locator('svg').waitFor({state:'visible'});
    await page.frameLocator('.chat-preview iframe').nth(1).getByText('HTML 静态预览').waitFor({state:'visible'});
    await page.setViewportSize({width:1280,height:1300});
    await page.evaluate(()=>document.documentElement.dataset.theme='dark');
    await assertCompact(page,'done');
    await page.screenshot({path:path.join(screenshots,'chat-preview-desktop.png'),fullPage:true});
    await page.locator('.chat-preview').first().screenshot({path:path.join(screenshots,'chat-preview-compact-desktop.png')});
    await page.evaluate(()=>document.documentElement.dataset.theme='light');
    await assertCompact(page,'done');
    await page.locator('.chat-preview').first().screenshot({path:path.join(screenshots,'chat-preview-compact-light.png')});
    await page.evaluate(()=>document.documentElement.dataset.theme='dark');
    for(const width of [320,375,390,768,1280]){
      await page.setViewportSize({width,height:1300});
      await page.evaluate(()=>{closeSidebar();setSidebarCollapsed(true);});
      await page.waitForTimeout(300);
      assert.equal(await page.evaluate(()=>[...document.querySelectorAll('.chat-preview')].every(n=>n.getBoundingClientRect().width<=innerWidth)),true,'预览适配 '+width);
      await assertCompact(page,'done');
      if(width===390) await page.screenshot({path:path.join(screenshots,'chat-preview-mobile.png'),fullPage:true});
    }
    const toolbar=page.locator('.preview-toolbar').first();
    await toolbar.getByRole('button',{name:'显示源码',exact:true}).focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('.preview-source').first().isVisible(),true);
    assert.equal(await page.locator('.preview-view').first().isVisible(),false);
    assert.equal(await toolbar.getByRole('button',{name:'显示预览',exact:true}).getAttribute('aria-pressed'),'true');
    await toolbar.getByRole('button',{name:'显示预览',exact:true}).click();
    assert.equal(await toolbar.getByRole('button',{name:'显示源码',exact:true}).getAttribute('aria-pressed'),'false');
    await page.context().grantPermissions(['clipboard-read','clipboard-write']);
    await toolbar.getByRole('button',{name:'复制源码',exact:true}).click();
    assert.match(await page.evaluate(()=>navigator.clipboard.readText()),/flowchart LR/);
    const copied=toolbar.getByRole('button',{name:'已复制',exact:true});
    await copied.waitFor();
    assert.equal(await copied.getAttribute('data-feedback'),'done');
    assert.equal(await copied.innerText(),'');
    await toolbar.getByRole('button',{name:'复制源码',exact:true}).waitFor();
    await page.evaluate(()=>{window.originalClipboard=navigator.clipboard.writeText;navigator.clipboard.writeText=()=>Promise.reject(new Error('denied'));});
    await toolbar.getByRole('button',{name:'复制源码',exact:true}).click();
    await toolbar.getByRole('button',{name:'复制失败，请重试或手动复制源码',exact:true}).waitFor();
    assert.equal(await toolbar.locator('button[data-feedback="warning"] svg').count(),1);
    await page.evaluate(()=>navigator.clipboard.writeText=window.originalClipboard);
    await toolbar.getByRole('button',{name:'复制源码',exact:true}).waitFor();
    // 错误围栏清晰降级；普通代码不受影响；围栏内的嵌套文本不误识别。
    await page.evaluate(()=>{
      const n=document.createElement('div');n.id='edgeCases';n.className='bubble';$('#wrap').append(n);
      n.innerHTML=mdToHtml('```mermaid\ninvalid diagram\n```\n\n```python\nprint("<script>alert(1)</script>")\n```');
    });
    await page.waitForFunction(()=>document.querySelector('#edgeCases .preview-status').getAttribute('aria-label').includes('无法预览'));
    assert.equal(await page.locator('#edgeCases .preview-status').getAttribute('data-state'),'warning');
    assert.match(await page.locator('#edgeCases .preview-note').innerText(),/切换源码检查语法或安全限制/);
    // 放到消息区顶部，避免短异常卡片落在固定输入框下面。
    await page.locator('#edgeCases').evaluate(n=>{n.parentNode.prepend(n);document.querySelector('#messages').scrollTop=0;});
    await page.locator('#edgeCases .chat-preview').screenshot({path:path.join(screenshots,'chat-preview-error.png')});
    await page.locator('#edgeCases').evaluate(n=>n.parentNode.append(n));
    assert.equal(await page.locator('#edgeCases .codeblock').count(),1);
    assert.match(await page.locator('#edgeCases .codeblock').innerText(),/print/);
    // 恶意 HTML：脚本、导航、刷新、表单、SVG、CSS 外连均不得到达网络。
    await page.evaluate(()=>{
      const attack='<meta http-equiv="refresh" content="0;url=https://preview-attacker.invalid/meta"><base href="https://preview-attacker.invalid/"><script>parent.pwned=true;fetch("https://preview-attacker.invalid/script")</script><style>@import url(https://preview-attacker.invalid/css);body{background:url(https://preview-attacker.invalid/bg)}</style><img src="https://preview-attacker.invalid/image" onerror="parent.pwned=true"><a href="https://preview-attacker.invalid/nav" target="_top">恶意链接</a><form action="https://preview-attacker.invalid/form"><button>提交</button></form><iframe src="https://preview-attacker.invalid/frame"></iframe><svg><a xlink:href="javascript:alert(1)">x</a></svg>';
      const n=document.createElement('div');n.id='attack';$('#wrap').append(n);n.innerHTML=mdToHtml('```html\n'+attack+'\n```');
    });
    await page.waitForSelector('#attack iframe');
    const iframe=page.locator('#attack iframe');
    assert.equal(await iframe.getAttribute('sandbox'),'');
    const srcdoc=await iframe.getAttribute('srcdoc');
    assert.ok(!/<script|<form|http-equiv="refresh"|onerror|xlink:href|target=/i.test(srcdoc));
    const frame=await (await iframe.elementHandle()).contentFrame();
    await frame.getByText('恶意链接').click();await page.waitForTimeout(300);
    assert.equal(page.url(),origin+'/');assert.equal(await page.evaluate(()=>window.pwned),undefined);assert.deepEqual(external,[]);
    assert.equal(await frame.evaluate(()=>{try{return parent.localStorage.getItem('token');}catch(e){return 'blocked';}}),'blocked');
    await page.evaluate(()=>{
      const n=document.createElement('div'); n.id='mermaidAttack'; $('#wrap').append(n);
      n.innerHTML=mdToHtml('```mermaid\nflowchart LR\n A@{img: "https://preview-attacker.invalid/mermaid"}\n```');
      const lengths=document.createElement('div');lengths.id='lengths';$('#wrap').append(lengths);
      lengths.innerHTML=mdToHtml('```html\n<b>围栏长度</b>\n````\n后续正文');
    });
    await page.waitForFunction(()=>document.querySelector('#mermaidAttack .preview-status').getAttribute('aria-label').includes('无法预览'));
    assert.match(await page.locator('#lengths > p').innerText(),/后续正文/);
    assert.deepEqual(external,[],'Mermaid 渲染阶段也不能外连');
    // 在调度之前切走，再挂回同一个 DOM，必须恢复任务。
    await page.evaluate(()=>{
      const n=document.createElement('div');n.id='resumePreview';$('#wrap').append(n);
      renderPreviewMarkdown(n,'```html\n<h2>恢复渲染</h2>\n```');window.resumeNode=n;n.remove();
    });
    await page.waitForTimeout(100);
    await page.evaluate(()=>$('#wrap').append(window.resumeNode));
    await page.waitForSelector('#resumePreview iframe',{state:'attached'});
    // 通知和 Agent 面板不在聊天容器内，也必须初始化预览。
    await page.evaluate(()=>{
      const panel=document.createElement('div');panel.id='outsideChatPreview';document.body.append(panel);
      panel.innerHTML=mdToHtml('```html\n<h2>面板预览</h2>\n```');
    });
    await page.frameLocator('#outsideChatPreview iframe').getByText('面板预览').waitFor({state:'visible'});
    await page.evaluate(()=>document.querySelector('#outsideChatPreview').remove());
    // 本地 text/html 不再被 text/* 分支截成源码，使用同一个安全处理函数。
    await page.evaluate(()=>renderDocBlob(new Blob(['<h1>本地文档</h1><script>parent.pwned=true</script>'],{type:'text/html'}),'example.html'));
    await page.waitForSelector('#dpBody iframe',{state:'attached'});
    assert.match(await page.locator('#dpBody iframe').getAttribute('srcdoc'),/本地文档/);
    assert.equal(await page.locator('#dpBody iframe').getAttribute('sandbox'),'');
    await page.evaluate(()=>{
      const n=document.createElement('div');n.id='large';$('#wrap').append(n);n.innerHTML=mdToHtml('```mermaid\n'+'x'.repeat(31000)+'\n```');
      $('#wrap').append(buildTurnBlock({id:3,user:'恢复草稿',pending:true,draft:'```html\n<h2>仍在生成</h2>\n'},S.conv,true));
    });
    await page.waitForFunction(()=>document.querySelector('#large .preview-status').getAttribute('aria-label').includes('内容过大'));
    assert.match(await page.locator('#large .preview-note').innerText(),/内容过大/);
    assert.match(await page.locator('.chat-preview').last().locator('.preview-status').getAttribute('aria-label'),/生成中/);
    assert.equal(await page.locator('.chat-preview').last().locator('.preview-status').getAttribute('data-state'),'busy');
    await page.evaluate(()=>{
      const n=document.createElement('div');n.id='previewWarnings';$('#wrap').append(n);
      n.innerHTML=mdToHtml('```html\n\n```\n\n```html\n<p>围栏未闭合</p>');
    });
    await page.waitForFunction(()=>[...document.querySelectorAll('#previewWarnings .preview-status')].every(n=>n.dataset.state==='warning'));
    assert.match(await page.locator('#previewWarnings .preview-note').first().innerText(),/内容为空/);
    assert.match(await page.locator('#previewWarnings .preview-note').last().innerText(),/围栏未闭合/);
    assert.deepEqual(errors,[]);
    const samples=await page.evaluate(()=>[...document.querySelectorAll('.chat-preview')].slice(0,2).map(n=>({lang:n.dataset.lang,source:n._preview.source})));
    const mobile=await browser.newPage({viewport:{width:390,height:1300},isMobile:true,hasTouch:true,userAgent:'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1'});
    await mobile.goto(origin);await mobile.fill('#pw',token);await mobile.click('#loginBtn');
    await mobile.waitForFunction(()=>!$('#app').hidden);
    await mobile.evaluate(async samples=>{
      await openConv('local-mobile-preview');
      for(const sample of samples) addBubble('ai','```'+sample.lang+'\n'+sample.source+'\n```');
      closeSidebar();
    },samples);
    await mobile.frameLocator('.chat-preview iframe').first().locator('svg').waitFor({state:'visible'});
    await mobile.frameLocator('.chat-preview iframe').nth(1).getByText('HTML 静态预览').waitFor({state:'visible'});
    await assertCompact(mobile,'done');
    await mobile.locator('.preview-toolbar').first().getByRole('button',{name:'显示源码',exact:true}).tap();
    assert.equal(await mobile.locator('.preview-source').first().isVisible(),true);
    await mobile.locator('.preview-toolbar').first().getByRole('button',{name:'显示预览',exact:true}).tap();
    await assertCompact(mobile,'done');
    await mobile.screenshot({path:path.join(screenshots,'chat-preview-mobile.png'),fullPage:true});
    await mobile.locator('.chat-preview').first().screenshot({path:path.join(screenshots,'chat-preview-compact-mobile.png')});
    await mobile.close();
    console.log(JSON.stringify({ok:true,cases:['未闭合占位','完整行渐进','iframe保留','完成定稿','历史多入口','源码切换','语法错误','普通代码','HTML隔离','恶意代码','本地HTML','大小限制','草稿','五档屏宽'],screenshots}));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
