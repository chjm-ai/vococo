const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');

// 用户报告的示例②，保留 loop 和最后一条消息，不用更短流程图替代。
const sequenceSource=`sequenceDiagram
    participant U as 用户
    participant V as Vococo
    participant M as 模型
    U->>V: 发送消息
    V->>M: 提交请求
    loop 逐步生成
        M-->>V: 返回新内容
        V-->>U: 更新图表预览
    end
    V-->>U: 完成定稿`;

async function testSequenceHeight(browser,origin,token,screenshots,theme){
  const measurements=[],errors=[];
  for(const device of [
    {name:'desktop',width:1280,height:1000},
    {name:'mobile',width:390,height:844,mobile:true},
    {name:'mobile-small',width:320,height:740,mobile:true},
  ]){
    const page=await browser.newPage({viewport:{width:device.width,height:device.height},
      ...(device.mobile?{isMobile:true,hasTouch:true,userAgent:'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1'}:{})});
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(origin);await page.fill('#pw',token);await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(async theme=>{document.documentElement.dataset.theme=theme;await openConv('sequence-height-test');$('#wrap').replaceChildren();closeSidebar();setSidebarCollapsed(true);},theme);
    const measure=async (name,expectedSource)=>{
      const card=page.locator('.chat-preview').last(),iframe=card.locator('iframe');
      const frame=await (await iframe.elementHandle()).contentFrame();
      await frame.locator('svg').waitFor({state:'visible'});
      await frame.evaluate(()=>document.fonts.ready);
      const sample={device:device.name,theme,phase:name,viewport:page.viewportSize(),
        outer:await iframe.evaluate(n=>({clientHeight:n.clientHeight,scrollHeight:n.scrollHeight,
          width:n.getBoundingClientRect().width,height:n.getBoundingClientRect().height,
          cssHeight:getComputedStyle(n).height,sandbox:n.getAttribute('sandbox')})),
        inner:await frame.evaluate(()=>{
          const svg=document.querySelector('svg'),vb=svg.viewBox.baseVal;
          const rect=n=>{const r=n.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom};};
          const last=[...svg.querySelectorAll('text')].find(n=>n.textContent.includes('完成定稿'));
          return {clientHeight:document.documentElement.clientHeight,scrollHeight:document.documentElement.scrollHeight,
            bodyClientHeight:document.body.clientHeight,bodyScrollHeight:document.body.scrollHeight,
            scrollY,svg:rect(svg),svgAttributes:{width:svg.getAttribute('width'),height:svg.getAttribute('height'),
              viewBox:svg.getAttribute('viewBox'),style:svg.getAttribute('style'),preserveAspectRatio:svg.getAttribute('preserveAspectRatio')},
            viewBox:{x:vb.x,y:vb.y,width:vb.width,height:vb.height},bbox:(()=>{const b=svg.getBBox();return {x:b.x,y:b.y,width:b.width,height:b.height};})(),
            svgCss:{height:getComputedStyle(svg).height,width:getComputedStyle(svg).width,maxWidth:getComputedStyle(svg).maxWidth},
            bodyMargin:getComputedStyle(document.body).margin,lastMessage:last?rect(last):null,
            background:getComputedStyle(document.body).backgroundColor,
            minTextSize:Math.min(...[...svg.querySelectorAll('text')].map(n=>parseFloat(getComputedStyle(n).fontSize)*svg.getBoundingClientRect().width/vb.width)),
            overflow:document.documentElement.scrollHeight-document.documentElement.clientHeight};
        })};
      assert.equal((await card.locator('.preview-source code').textContent()).trimEnd(),expectedSource,'必须渲染完整原始示例');
      measurements.push(sample);
      await card.screenshot({path:path.join(screenshots,`sequence-height-${theme}-${device.name}-${name}.png`)});
      return sample;
    };
    // 正常对照：已有短流程图应完整落在同一个 iframe 容器内。
    await page.evaluate(()=>addBubble('ai','```mermaid\nflowchart LR\n A[开始] --> B[完成]\n```'));
    await page.waitForFunction(()=>document.querySelector('.preview-status')?.dataset.state==='done');
    await measure('flowchart-control','flowchart LR\n A[开始] --> B[完成]');
    await page.evaluate(()=>$('#wrap').replaceChildren());
    // 真正走 applyStreamEvent → finalizeStream；先保留未结束 loop 的成功快照。
    const partial=sequenceSource.slice(0,sequenceSource.indexOf('    V-->>U: 完成定稿'));
    await page.evaluate(text=>applyStreamEvent({type:'text',conv:S.conv,text,seg:0}),'```mermaid\n'+partial);
    await page.waitForSelector('.chat-preview iframe');
    await page.evaluate(text=>applyStreamEvent({type:'text',conv:S.conv,text,seg:0}),'```mermaid\n'+sequenceSource+'\n```');
    await page.evaluate(()=>finalizeStream(undefined,undefined,200));
    await page.waitForFunction(()=>document.querySelector('.preview-status')?.dataset.state==='done');
    await measure('stream-final',sequenceSource);
    await page.evaluate(source=>{
      $('#wrap').replaceChildren();
      $('#wrap').append(buildTurnBlock({id:201,user:'示例②时序图',assistant:'```mermaid\n'+source+'\n```'},S.conv,false));
    },sequenceSource);
    await page.waitForFunction(()=>document.querySelector('.preview-status')?.dataset.state==='done');
    await measure('history',sequenceSource);
    // 不重新渲染源码，单靠 ResizeObserver 应处理视口、分屏和源码切换后的尺寸。
    for(const width of [320,768,1280]){
      await page.setViewportSize({width,height:1000});await page.waitForTimeout(100);
      await measure('resize-'+width,sequenceSource);
    }
    const card=page.locator('.chat-preview'),toggle=card.getByRole('button',{name:'显示源码',exact:true});
    await toggle.click();await page.setViewportSize({width:390,height:844});
    await card.getByRole('button',{name:'显示预览',exact:true}).click();await page.waitForTimeout(100);
    await measure('source-restored',sequenceSource);
    await card.locator('.preview-view').evaluate(n=>n.style.width='75%');await page.waitForTimeout(100);
    await measure('split-width',sequenceSource);
    await page.close();
  }
  // 全部尺寸采集完才断言，失败也留下完整的手机/桌面证据。
  fs.writeFileSync(path.join(screenshots,`sequence-height-${theme}-metrics.json`),JSON.stringify({measurements,errors},null,2));
  console.log(JSON.stringify({metrics:path.join(screenshots,`sequence-height-${theme}-metrics.json`),
    samples:measurements.map(m=>({device:m.device,phase:m.phase,clientHeight:m.inner.clientHeight,
      scrollHeight:m.inner.scrollHeight,svgHeight:m.inner.svg.height,overflow:m.inner.overflow})),errors}));
  assert.deepEqual(errors,[]);
  for(const sample of measurements){
    assert.equal(sample.outer.sandbox,'','不能放松沙箱');
    assert.ok(sample.inner.minTextSize>=11.9,'图表文字实际显示尺寸不能低于 12px');
  }
  const clipped=measurements.filter(m=>m.inner.svg.bottom>m.inner.clientHeight+1 || m.inner.overflow>1);
  assert.deepEqual(clipped.map(m=>({device:m.device,phase:m.phase,clientHeight:m.inner.clientHeight,
    scrollHeight:m.inner.scrollHeight,svgBottom:m.inner.svg.bottom,lastMessage:m.inner.lastMessage})),[],
    '图表初始视图应完整显示，不应要求用户在沙箱 iframe 内另行纵向滚动');
}

async function testStreamTheme(browser,origin,token,screenshots){
  const page=await browser.newPage({viewport:{width:390,height:844},isMobile:true,hasTouch:true}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(origin);await page.fill('#pw',token);await page.click('#loginBtn');
  await page.waitForFunction(()=>!$('#app').hidden);
  await page.evaluate(async()=>{await openConv('sequence-stream-theme');$('#wrap').replaceChildren();closeSidebar();setSidebarCollapsed(true);setTheme('light');});
  const stream=async text=>page.evaluate(text=>applyStreamEvent({type:'text',conv:S.conv,text,seg:0}),text);
  await stream('```mermaid\n'+sequenceSource.slice(0,sequenceSource.indexOf('    V-->>U: 完成定稿')));
  await page.waitForSelector('.chat-preview iframe');
  const card=page.locator('.chat-preview');
  await page.evaluate(()=>setTheme('dark'));
  await stream('```mermaid\n'+sequenceSource+'\n');
  const settled=()=>page.waitForFunction(()=>{
    const state=document.querySelector('.chat-preview')._preview;
    return state.renderTheme===previewDiagramTheme().key && state.rendered?.includes('完成定稿');
  });
  await settled();assert.equal(await card.locator('.preview-status').getAttribute('data-state'),'busy');
  await card.getByRole('button',{name:'显示源码',exact:true}).click();
  await page.evaluate(()=>setTheme('light'));await page.setViewportSize({width:768,height:1000});
  await settled();await card.getByRole('button',{name:'显示预览',exact:true}).click();
  await stream('```mermaid\n'+sequenceSource+'\n```');
  await page.evaluate(()=>finalizeStream(undefined,undefined,300));
  await page.waitForFunction(()=>document.querySelector('.preview-status').dataset.state==='done');
  await settled();await page.waitForTimeout(100);
  assert.equal(await card.locator('iframe').count(),1);
  const frame=await (await card.locator('iframe').elementHandle()).contentFrame();
  assert.equal(await frame.evaluate(()=>document.documentElement.scrollHeight<=document.documentElement.clientHeight+1),true);
  assert.equal(await card.locator('iframe').getAttribute('sandbox'),'');
  assert.match(await card.locator('.preview-source').textContent(),/完成定稿/);
  await card.screenshot({path:path.join(screenshots,'preview-refined-stream-theme-final.png')});
  assert.deepEqual(errors,[]);await page.close();
}

async function testPreviewThemeLayout(browser,origin,token,screenshots){
  const page=await browser.newPage({viewport:{width:1280,height:1300}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(origin);await page.fill('#pw',token);await page.click('#loginBtn');
  await page.waitForFunction(()=>!$('#app').hidden);
  await page.evaluate(async source=>{
    await openConv('sequence-theme-layout');$('#wrap').replaceChildren();closeSidebar();setSidebarCollapsed(true);
    addBubble('ai','```mermaid\n'+source+'\n```');
    addBubble('ai','```mermaid\nflowchart LR\n A[发送消息] --> B[生成预览] --> C[完成定稿]\n```');
    addBubble('ai','```html\n<h2>HTML 独立布局</h2><p>不套图表比例</p>\n```');
  },sequenceSource);
  const diagrams=page.locator('.chat-preview[data-lang="mermaid"]'),html=page.locator('.chat-preview[data-lang="html"] iframe');
  await page.waitForFunction(()=>document.querySelectorAll('.preview-status[data-state="done"]').length===3);
  const htmlBefore=await html.getAttribute('srcdoc');
  await html.evaluate(n=>window.htmlTestFrame=n);
  const verify=async (theme,label)=>{
    await page.waitForFunction(()=>[...document.querySelectorAll('.chat-preview[data-lang="mermaid"]')].every(n=>
      n._preview.renderTheme===previewDiagramTheme().key && n.querySelector('.preview-status').dataset.state==='done'));
    const expected=await page.evaluate(()=>{
      const css=getComputedStyle(document.documentElement),canvas=document.createElement('canvas').getContext('2d');
      const color=name=>{canvas.fillStyle=css.getPropertyValue(name).trim();return canvas.fillStyle;};
      return {background:color('--bg2'),text:color('--text'),actor:color('--accent'),surface:color('--panel')};
    });
    for(let i=0;i<2;i++){
      const card=diagrams.nth(i),frame=await (await card.locator('iframe').elementHandle()).contentFrame();
      await frame.locator('svg').waitFor({state:'visible'});
      const colors=await frame.evaluate(()=>{
        const normalize=value=>{const ctx=document.createElement('canvas').getContext('2d');ctx.fillStyle=value;return ctx.fillStyle;};
        const svg=document.querySelector('svg'),node=svg.querySelector('rect.actor,.node rect');
        return {background:normalize(getComputedStyle(document.body).backgroundColor),
          text:normalize(getComputedStyle(svg.querySelector('text tspan')||svg.querySelector('text')).fill),
          fill:normalize(getComputedStyle(node).fill),border:normalize(getComputedStyle(node).stroke),
          radius:node.getAttribute('rx'),loop:svg.querySelectorAll('.preview-loop-background').length,
          overflow:document.documentElement.scrollHeight-document.documentElement.clientHeight};
      });
      assert.equal(colors.background,expected.background);assert.equal(colors.text,expected.text);
      assert.equal(colors.fill,expected.surface);assert.ok(Number(colors.radius)>0);
      if(i===0){assert.equal(colors.border,expected.actor);assert.equal(colors.loop,1);}
      assert.ok(colors.overflow<=1,'主题切换后也不应纵向溢出');
      const bar=card.locator('.preview-toolbar');
      assert.equal(await bar.locator('.preview-title svg').count(),0);
      assert.equal(await bar.locator('.preview-title').innerText(),'MERMAID');
      assert.equal(await bar.evaluate(n=>{
        const r=n.getBoundingClientRect(),[title,status,source,copy]=[...n.children].map(n=>n.getBoundingClientRect());
        return status.left-title.right<=16 && r.right-copy.right<=9 && source.left>=status.right &&
          [title,status,source,copy].every(p=>p.top>=r.top && p.bottom<=r.bottom);
      }),true,'左右分组与单行布局');
      await card.screenshot({path:path.join(screenshots,`preview-refined-${label}-${theme}-${i===0?'sequence':'flow'}.png`)});
      if(label==='mobile' && i===0){
        const view=card.locator('.preview-view');await view.evaluate(n=>n.scrollLeft=n.scrollWidth);
        await card.screenshot({path:path.join(screenshots,`preview-refined-mobile-${theme}-sequence-right.png`)});
        await view.evaluate(n=>n.scrollLeft=0);
      }
    }
    assert.equal(await html.getAttribute('srcdoc'),htmlBefore,'HTML 不受 Mermaid 主题重渲染影响');
    assert.equal(await html.evaluate(n=>n===window.htmlTestFrame && !n.style.height && !n.style.width),true,'HTML 保留独立尺寸规则');
  };
  for(const device of [{name:'desktop',width:1280,height:1300},{name:'mobile',width:390,height:844}]){
    await page.setViewportSize({width:device.width,height:device.height});
    for(const theme of ['dark','light']){
      await page.evaluate(theme=>setTheme(theme),theme);await verify(theme,device.name);
    }
  }
  // 自动跟随系统也更新；手动固定主题时不跟系统变色。
  await page.evaluate(()=>setTheme('auto'));
  await page.emulateMedia({colorScheme:'dark'});await verify('dark','auto');
  await page.emulateMedia({colorScheme:'light'});await verify('light','auto');
  await page.evaluate(()=>setTheme('dark'));await verify('dark','fixed');
  const before=await diagrams.first().locator('iframe').getAttribute('srcdoc');
  await page.emulateMedia({colorScheme:'dark'});await page.waitForTimeout(100);
  assert.equal(await diagrams.first().locator('iframe').getAttribute('srcdoc'),before);
  // 快速切换在串行队列里最终收敛到最新主题，不能混入旧全局配置。
  await page.evaluate(()=>{setTheme('light');setTheme('dark');setTheme('light');});await verify('light','rapid');
  const view=diagrams.first().locator('.preview-view');
  assert.equal(await view.evaluate(n=>n.scrollWidth>n.clientWidth),true,'手机宽图通过外层横向滚动保留字号');
  await view.evaluate(n=>n.scrollLeft=n.scrollWidth);
  assert.ok(await view.evaluate(n=>n.scrollLeft)>0,'横向滚动可达右侧参与者');
  // 缓存 DOM 离开页面期间没有收到主题通知，重新挂回时仍须刷新到当前主题。
  await page.evaluate(()=>{
    window.parkedPreview=document.querySelector('.chat-preview');window.parkedParent=window.parkedPreview.parentNode;
    window.parkedPreview.remove();setTheme('dark');
  });
  await page.waitForTimeout(50);
  await page.evaluate(()=>window.parkedParent.append(window.parkedPreview));await verify('dark','resumed');
  // 独立图种可能自带 10/11px 标签，也必须按实际最小字号决定缩放下限。
  await page.evaluate(()=>addBubble('ai','```mermaid\ngantt\n title 发布计划\n dateFormat YYYY-MM-DD\n section 开发\n 实现预览 :a, 2026-10-01, 3d\n 回归验证 :after a, 2d\n```'));
  const gantt=page.locator('.chat-preview').last();
  await page.waitForFunction(()=>[...document.querySelectorAll('.preview-status')].at(-1).dataset.state==='done');
  const ganttFrame=await (await gantt.locator('iframe').elementHandle()).contentFrame();
  const minGanttText=await ganttFrame.evaluate(()=>{
    const svg=document.querySelector('svg'),scale=svg.getBoundingClientRect().width/svg.viewBox.baseVal.width;
    return Math.min(...[...svg.querySelectorAll('text')].map(n=>parseFloat(getComputedStyle(n).fontSize)*scale));
  });
  assert.ok(minGanttText>=11.9,'甘特图也保持最小 12px 标签');
  // 真实观察器加一层计数包装，检查同步添加/移除不会重新订阅已离页节点。
  await page.evaluate(()=>{
    window.observedPreviewViews=new Set();
    window.realPreviewObserve=previewResizeObserver.observe.bind(previewResizeObserver);
    window.realPreviewUnobserve=previewResizeObserver.unobserve.bind(previewResizeObserver);
    previewResizeObserver.observe=n=>{observedPreviewViews.add(n);realPreviewObserve(n);};
    previewResizeObserver.unobserve=n=>{observedPreviewViews.delete(n);realPreviewUnobserve(n);};
    const n=document.createElement('div');$('#wrap').append(n);window.transientPreview=n;
    renderPreviewMarkdown(n,'```mermaid\nflowchart LR\n A --> B\n```');n.remove();
  });
  await page.waitForTimeout(100);
  assert.equal(await page.evaluate(()=>observedPreviewViews.size),0);
  await page.evaluate(()=>$('#wrap').append(transientPreview));
  await page.waitForTimeout(100);
  assert.equal(await page.evaluate(()=>observedPreviewViews.size),1);
  await page.evaluate(()=>transientPreview.remove());await page.waitForTimeout(100);
  assert.equal(await page.evaluate(()=>observedPreviewViews.size),0);
  await page.evaluate(()=>{previewResizeObserver.observe=realPreviewObserve;previewResizeObserver.unobserve=realPreviewUnobserve;});
  assert.deepEqual(errors,[]);await page.close();
}

(async()=>{
  const [origin,token,screenshots,scenario]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    if(scenario?.startsWith('sequence-height-')) return await testSequenceHeight(browser,origin,token,screenshots,scenario.split('-').at(-1));
    if(scenario==='sequence-theme-layout') return await testPreviewThemeLayout(browser,origin,token,screenshots);
    if(scenario==='sequence-stream-theme') return await testStreamTheme(browser,origin,token,screenshots);
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
        assert.equal(await bar.locator('.preview-title > svg').count(),0);
        assert.equal(await bar.locator('button').count(),await card.getAttribute('data-lang')==='html'?4:2);
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
            && rect.height<=48 && parts[1].left-parts[0].right<=16
            && rect.right-parts.at(-1).right<=9 && parts[2].left>=parts[1].right;
        }),true,'文字和状态在左、操作在右且保持单行');
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
