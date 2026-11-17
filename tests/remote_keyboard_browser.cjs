const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
  const [origin,token,screenshot]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    const page=await browser.newPage({viewport:{width:1280,height:900},serviceWorkers:'block'}), errors=[];
    page.on('pageerror',e=>errors.push(e.stack));
    await page.goto(origin); await page.fill('#pw',token); await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden);
    await page.route('**/conversations*',route=>route.fulfill({json:{main:{conv:'main',title:'总助理'},conversations:[]}}));
    await page.route('**/history?*',route=>route.fulfill({json:{turns:[]}}));
    await page.evaluate(async()=>{
      await openConv('local-remote-b');
      S.sideTab='recent'; S.recentShowMain=false;
      S.convs=[
        {conv:'local-remote-a',title:'第一个',turns:0,last_ts:3},
        {conv:'local-remote-b',title:'第二个',turns:0,last_ts:2},
        {conv:'local-remote-c',title:'第三个',turns:0,last_ts:1},
      ];
      renderConvs(); $('#ta').value='第二个会话草稿';
    });
    const shortcut=key=>page.keyboard.press('Control+Alt+'+key);
    await shortcut('ArrowUp');
    await page.waitForFunction(()=>S.conv==='local-remote-a' && document.activeElement.id==='ta');
    await shortcut('ArrowUp');
    assert.equal(await page.evaluate(()=>S.conv),'local-remote-a','到边界不循环');
    await shortcut('ArrowDown');
    await page.waitForFunction(()=>S.conv==='local-remote-b' && document.activeElement.id==='ta');
    assert.equal(await page.locator('#ta').inputValue(),'第二个会话草稿');
    // 重放 Safari/BTT 日志：组合键正确，但被标记为组字或 keyCode 229。
    for(const [code,key,next] of [['ArrowUp','ArrowUp','ArrowDown'],['KeyK','k','j']]){
      for(const flags of [{isComposing:true},{keyCode:229},{isComposing:true,keyCode:229}]){
        const up=await page.evaluate(({flags,code,key})=>{
          const e=new KeyboardEvent('keydown',{key,code,ctrlKey:true,altKey:true,bubbles:true,cancelable:true,...flags});
          $('#ta').dispatchEvent(e); return e.defaultPrevented;
        },{flags,code,key});
        assert.equal(up,true,'明确会话切换不受错误组字标记阻断');
        await page.waitForFunction(()=>S.conv==='local-remote-a' && document.activeElement.id==='ta');
        await shortcut(next);
        await page.waitForFunction(()=>S.conv==='local-remote-b' && document.activeElement.id==='ta');
        assert.equal(await page.locator('#ta').inputValue(),'第二个会话草稿');
      }
    }
    await page.keyboard.type('jk');
    assert.equal(await page.evaluate(()=>S.conv),'local-remote-b','普通字母不触发切换');
    await page.fill('#ta','第二个会话草稿');
    // 桌面手动收起侧栏后，快捷键必须真正展开，而不是聚焦在被裁切的行上。
    await page.click('#collapseBtn');
    await shortcut('l');
    assert.equal(await page.evaluate(()=>document.body.classList.contains('sb-collapsed')),false,'聚焦列表应展开桌面侧栏');
    assert.equal(await page.evaluate(()=>document.activeElement.dataset.conv),'local-remote-b');
    await page.keyboard.press('ArrowDown');
    assert.equal(await page.evaluate(()=>S.conv),'local-remote-b','上下只移动焦点');
    assert.equal(await page.evaluate(()=>document.activeElement.dataset.conv),'local-remote-c');
    await page.evaluate(()=>renderConvs());
    assert.equal(await page.evaluate(()=>document.activeElement.dataset.conv),'local-remote-c','侧栏重绘不丢高亮');
    assert.equal(await page.evaluate(()=>getComputedStyle(document.activeElement).outlineStyle),'solid');
    await page.screenshot({path:screenshot+'.focused.png'});
    // 菜单作用于高亮行，而不是仍在正文中打开的会话；上下循环，Esc 返回原行。
    await shortcut('m');
    assert.match(await page.evaluate(()=>document.activeElement.textContent),/重命名/);
    assert.equal(await page.evaluate(()=>getComputedStyle(document.activeElement).outlineStyle),'solid');
    await page.keyboard.press('ArrowUp');
    assert.match(await page.evaluate(()=>document.activeElement.textContent),/删除/);
    await page.keyboard.press('ArrowDown');
    assert.match(await page.evaluate(()=>document.activeElement.textContent),/重命名/);
    await page.screenshot({path:screenshot+'.menu.png',animations:'disabled'});
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.convmenu').count(),0);
    assert.equal(await page.evaluate(()=>document.activeElement.dataset.conv),'local-remote-c');
    // 工作台/作品库状态下，桌面侧栏菜单也要能用 Esc 退出。
    await page.evaluate(()=>{S.surface='library';});
    await shortcut('m');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.convmenu').count(),0);
    assert.equal(await page.evaluate(()=>document.activeElement.dataset.conv),'local-remote-c');
    await page.evaluate(()=>{S.surface='chat';});
    await page.keyboard.press('Shift+F10');
    await page.keyboard.press('ArrowDown');
    assert.match(await page.evaluate(()=>document.activeElement.textContent),/稍后处理/);
    await page.keyboard.press('ArrowUp');
    let renameDefault;
    page.once('dialog',async dialog=>{renameDefault=dialog.defaultValue();await dialog.accept('第三个已改名');});
    await page.keyboard.press('Enter');
    await page.waitForFunction(()=>S.convs.find(c=>c.conv==='local-remote-c')?.title==='第三个已改名');
    assert.equal(renameDefault,'第三个');
    assert.equal(await page.evaluate(()=>S.conv),'local-remote-b');
    assert.equal(await page.evaluate(()=>document.activeElement.dataset.conv),'local-remote-c');
    // 鼠标操作之后，遥控器程序式聚焦也必须可见，不能依赖 :focus-visible 的浏览器判断。
    await page.click('#ta');
    await page.evaluate(()=>{
      document.dispatchEvent(new KeyboardEvent('keydown',{key:'l',code:'KeyL',ctrlKey:true,altKey:true,bubbles:true,cancelable:true}));
    });
    assert.equal(await page.evaluate(()=>document.activeElement.dataset.conv),'local-remote-b');
    assert.equal(await page.evaluate(()=>getComputedStyle(document.activeElement).outlineStyle),'solid');
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('Enter');
    await page.waitForFunction(()=>S.conv==='local-remote-c' && document.activeElement.id==='ta');
    assert.equal(await page.evaluate(()=>$('#sidebar').classList.contains('open')),false);
    // 列表边界、修饰键和中文输入不能触发发送或导航。
    await shortcut('i');
    await page.fill('#ta','正在输入');
    await page.keyboard.press('ArrowUp');
    assert.equal(await page.evaluate(()=>document.activeElement.id),'ta');
    await shortcut('m');
    assert.equal(await page.locator('.convmenu').count(),0,'输入框中不误开会话菜单');
    assert.equal(await page.evaluate(()=>S.conv),'local-remote-c');
    let sends=0,aborts=0;
    await page.route('**/send',route=>{sends++;return route.fulfill({json:{ok:true}});});
    await page.route('**/abort',route=>{aborts++;return route.fulfill({json:{ok:true,stopped:true}});});
    for(const flags of [{isComposing:true},{keyCode:229},{isComposing:true,keyCode:229}]){
      await page.evaluate(flags=>{
        for(const key of ['Enter','ArrowUp','Escape']) $('#ta').dispatchEvent(new KeyboardEvent('keydown',{key,code:key,bubbles:true,cancelable:true,...flags}));
      },flags);
    }
    assert.equal(sends,0);
    assert.equal(aborts,0);
    assert.equal(await page.evaluate(()=>S.conv),'local-remote-c','无明确修饰键的组字操作不切会话');
    await page.keyboard.press('Shift+Enter');
    assert.ok((await page.locator('#ta').inputValue()).includes('\n'));
    await page.keyboard.press('Enter');
    await page.waitForFunction(()=>$('#ta').value==='');
    assert.equal(sends,1);
    await page.evaluate(()=>document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',repeat:true,bubbles:true,cancelable:true})));
    assert.equal(sends,1);
    // Esc 先关浮层，不穿透到停止；长按也不穿透。
    await page.evaluate(()=>{handleEvent({type:'start',conv:S.conv});$('#modelPop').hidden=false;});
    await page.keyboard.press('Escape');
    assert.equal(aborts,0);
    assert.equal(await page.locator('#modelPop').isVisible(),false);
    await page.evaluate(()=>document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',repeat:true,bubbles:true,cancelable:true})));
    assert.equal(aborts,0);
    // 文档预览和目录选择同样先关闭，不能意外停止正在生成的回复。
    await page.evaluate(()=>{$('#docPreview').hidden=false;});
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#docPreview').isVisible(),false);
    assert.equal(aborts,0);
    // 通过真实三选一弹窗入口验证自动进入、安全初始焦点及正反向 Tab 循环。
    await page.evaluate(()=>{
      window.remoteModalResult=null;
      delConfirmChoice('测试未提交改动').then(value=>{window.remoteModalResult=value;});
    });
    await page.waitForFunction(()=>document.activeElement.id==='delCancel');
    assert.equal(await page.evaluate(()=>window.remoteModalResult),null);
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(()=>document.activeElement.id),'delForce');
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.evaluate(()=>document.activeElement.id),'delCancel');
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.evaluate(()=>document.activeElement.id),'delArchive');
    assert.equal(await page.evaluate(()=>getComputedStyle(document.activeElement).outlineStyle),'solid');
    await page.screenshot({path:screenshot+'.modal.png',animations:'disabled'});
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(()=>document.activeElement.id),'delCancel');
    await page.keyboard.press('Tab');
    await page.keyboard.press('Enter');
    await page.waitForFunction(()=>window.remoteModalResult==='delete');
    assert.equal(aborts,0);
    await page.evaluate(()=>{
      $('#delForce').disabled=true;
      delConfirmChoice('再次测试').then(value=>{window.remoteModalResult=value;});
    });
    await page.waitForFunction(()=>document.activeElement.id==='delCancel');
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.evaluate(()=>document.activeElement.id),'delArchive','Tab跳过禁用按钮');
    await page.keyboard.press('Escape');
    await page.waitForFunction(()=>window.remoteModalResult==='cancel');
    await page.evaluate(()=>{$('#delForce').disabled=false;$('#ta').focus();$('#projModal').hidden=false;});
    await page.waitForFunction(()=>document.activeElement.id==='pmCancel');
    // 即使焦点被外部脚本移出，下次 Tab 也回到弹窗内。
    await page.evaluate(()=>$('#ta').focus());
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(()=>!!document.activeElement.closest('#projModal')),true);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#projModal').isVisible(),false);
    assert.equal(aborts,0);
    // 高层跨会话审批盖在编辑弹窗上时，Tab和Esc只操作最上层。
    await page.evaluate(()=>{$('#cronModal').hidden=false;});
    await page.waitForFunction(()=>document.activeElement.id==='cfCancel');
    await page.evaluate(()=>openChoiceModal('other-conv',{clarify_id:'stacked-modal',prompt:'顶层审批',options:[['/clarify stacked-modal yes','同意'],['/clarify stacked-modal no','拒绝']]}));
    await page.waitForFunction(()=>document.activeElement.id==='choiceModalClose');
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(()=>!!document.activeElement.closest('#choiceModalOpts')),true);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#choiceModal').isVisible(),false);
    assert.equal(await page.locator('#cronModal').isVisible(),true);
    await page.waitForFunction(()=>document.activeElement.id==='cfCancel');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#cronModal').isVisible(),false);
    assert.equal(aborts,0);
    assert.equal(sends,1);
    await shortcut('i');
    await page.keyboard.press('Escape');
    await page.waitForFunction(()=>!S.stream);
    assert.equal(aborts,1);
    // 没有运行时按 Esc 不发停止请求。
    await page.keyboard.press('Escape');
    assert.equal(aborts,1);
    // 语音聚焦默认关闭，选择右 Command 后不吞键，弹窗中不抢焦点。
    await page.evaluate(()=>{localStorage.removeItem('vococo_voice_focus_key');$('#messages').focus();});
    await page.keyboard.press('MetaRight');
    assert.equal(await page.evaluate(()=>document.activeElement.id),'messages');
    await page.evaluate(()=>localStorage.setItem('vococo_voice_focus_key','MetaRight'));
    await page.keyboard.press('MetaRight');
    assert.equal(await page.evaluate(()=>document.activeElement.id),'ta');
    await page.evaluate(()=>{$('#searchModal').hidden=false;$('#smInput').focus();});
    await page.keyboard.press('MetaRight');
    await shortcut('ArrowUp');
    assert.equal(await page.evaluate(()=>document.activeElement.id),'smInput');
    assert.equal(await page.evaluate(()=>S.conv),'remote-c');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#searchModal').isVisible(),false);
    // 当前审批只聚焦容器，第一次 Enter 不默认同意，再用方向键选择。
    await page.evaluate(()=>{
      $('#ta').value=''; $('#messages').focus();
      renderChoice({clarify_id:'remote-one',prompt:'确认操作',options:[['/clarify remote-one yes','同意'],['/clarify remote-one no','拒绝']]});
    });
    await page.waitForFunction(()=>document.activeElement.classList.contains('choice'));
    await page.keyboard.press('Enter');
    assert.equal(sends,1);
    await page.keyboard.press('ArrowRight');
    await page.keyboard.press('ArrowRight');
    assert.match(await page.evaluate(()=>document.activeElement.textContent),/拒绝/);
    await page.keyboard.press('Enter');
    await page.waitForTimeout(100);
    assert.equal(sends,2);
    assert.equal(await page.locator('.choice button:disabled').count(),2);
    // 正在写草稿时不抢焦点，已有弹窗 Esc 也不能取消后台运行。
    await shortcut('i'); await page.fill('#ta','未说完的草稿');
    await page.evaluate(()=>renderChoice({clarify_id:'remote-two',prompt:'第二个操作',options:[['/clarify remote-two yes','同意']]}));
    await page.waitForTimeout(50);
    assert.equal(await page.evaluate(()=>document.activeElement.id),'ta');
    await page.evaluate(()=>openChoiceModal('other-conv',{clarify_id:'remote-modal',prompt:'其他会话',options:[['/clarify remote-modal yes','同意'],['/clarify remote-modal no','拒绝']]}));
    await page.waitForFunction(()=>document.activeElement.id==='choiceModalClose');
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('ArrowDown');
    assert.match(await page.evaluate(()=>document.activeElement.textContent),/拒绝/);
    await page.keyboard.press('Escape');
    assert.equal(aborts,1);
    assert.equal(sends,2);
    // 设置入口确实能保存选择，不只在测试中手写 localStorage。
    await page.evaluate(()=>vococoLazy.run('settings',()=>{SET.tab='appearance';renderAppearancePane();$('#setModal').hidden=false;}));
    await page.waitForSelector('#voiceFocusKey');
    await page.selectOption('#voiceFocusKey','AltRight');
    assert.equal(await page.evaluate(()=>vococoKeyboard.voiceFocusKey()),'AltRight');
    await page.keyboard.press('Escape');
    // 临时诊断通过真实设置入口开启；不影响输入框快捷键，也不收集文字。
    assert.equal(JSON.parse(await page.evaluate(()=>vococoKeyboardDiagnostic.report())).events.length,0);
    await page.evaluate(()=>{renderAppearancePane();$('#setModal').hidden=false;});
    await page.click('#keyDiagStart');
    await page.waitForFunction(()=>document.activeElement.id==='ta');
    await page.keyboard.type('private-text-8492');
    await shortcut('ArrowUp');
    await page.waitForFunction(()=>S.conv==='local-remote-b' && document.activeElement.id==='ta');
    await page.evaluate(()=>{
      $('#ta').dispatchEvent(new KeyboardEvent('keydown',{code:'ArrowDown',key:'ArrowDown',ctrlKey:true,altKey:true,isComposing:true,bubbles:true,cancelable:true}));
      $('#ta').dispatchEvent(new CompositionEvent('compositionstart',{data:'秘密文本',bubbles:true}));
      $('#ta').dispatchEvent(new CompositionEvent('compositionend',{data:'秘密文本',bubbles:true}));
      $('#ta').dispatchEvent(new WheelEvent('wheel',{deltaX:20,bubbles:true}));
    });
    await page.evaluate(()=>{renderAppearancePane();$('#setModal').hidden=false;});
    await page.click('#keyDiagShow');
    const diagnosticText=await page.locator('#keyDiagReport').inputValue();
    const diagnostic=JSON.parse(diagnosticText);
    assert.equal(diagnostic.active,false);
    assert.equal(diagnostic.voiceFocusKey,'AltRight');
    assert.ok(diagnostic.events.some(e=>e.type==='keydown' && e.code==='ArrowUp' && e.shortcutMatch && e.prevented && e.focus==='输入框'));
    assert.ok(diagnostic.events.some(e=>e.code==='ArrowDown' && e.composing && e.prevented));
    assert.equal(diagnostic.version,2);
    assert.ok(diagnostic.events.filter(e=>e.type==='keydown').every(e=>e.recorderState==='idle'));
    assert.ok(!diagnostic.events.some(e=>e.type.startsWith('web-recorder')),'切换不应触发网页录音');
    assert.ok(diagnostic.events.some(e=>e.type==='keyup' && e.code==='AltLeft'));
    assert.ok(diagnostic.events.some(e=>e.type==='horizontal-wheel' && e.direction==='right'));
    assert.ok(!/private-text|秘密文本|local-remote|KeyP/.test(diagnosticText));
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(()=>vococoKeyboardDiagnostic.report()),diagnosticText,'停止后不再采集');
    await page.screenshot({path:screenshot+'.diagnostic.png',animations:'disabled'});
    // 录音请求要有独立证据；模拟权限失败，不访问真实麦克风。
    await page.evaluate(async()=>{
      vococoKeyboardDiagnostic.start();
      const originalMic=getMic, originalAlert=window.alert;
      getMic=async()=>{throw new Error('测试权限拒绝');}; window.alert=()=>{};
      try{await startRec();}finally{getMic=originalMic;window.alert=originalAlert;vococoKeyboardDiagnostic.stop();}
    });
    const recording=JSON.parse(await page.evaluate(()=>vococoKeyboardDiagnostic.report()));
    assert.ok(recording.events.some(e=>e.type==='web-recorder-request'));
    assert.ok(!recording.events.some(e=>e.type==='web-recorder-start'));
    await page.evaluate(()=>{
      vococoKeyboardDiagnostic.start();
      for(let i=0;i<810;i++) document.dispatchEvent(new KeyboardEvent('keyup',{code:'AltLeft'}));
    });
    const capped=JSON.parse(await page.evaluate(()=>vococoKeyboardDiagnostic.report()));
    assert.equal(capped.active,false);
    assert.equal(capped.events.length,801);
    assert.equal(capped.events.at(-1).reason,'达到800条上限');
    await page.keyboard.press('Escape');
    // 手机、桌面窄窗都能通过同一快捷键打开列表，焦点在屏幕内。
    for(const width of [320,390,768,1280]){
      await page.setViewportSize({width,height:900});
      await shortcut('l');
      const focus=await page.evaluate(()=>{
        const n=document.activeElement,r=n.getBoundingClientRect();
        return {left:r.left,right:r.right,width:innerWidth,outline:getComputedStyle(n).outlineStyle};
      });
      assert.ok(focus.left>=0 && focus.right<=focus.width+1,JSON.stringify(focus));
      assert.equal(focus.outline,'solid');
      await page.keyboard.press('Escape');
    }
    await shortcut('l');
    await page.screenshot({path:screenshot});
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true,sends,aborts,screenshot}));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
