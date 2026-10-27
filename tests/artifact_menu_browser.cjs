const {webkit}=require(process.env.WEBKIT_PLAYWRIGHT_MODULE||'playwright');
const fs=require('node:fs');
const assert=require('node:assert/strict');
(async()=>{
  const browser=await webkit.launch({headless:true});
  try{
    const page=await browser.newPage({viewport:{width:390,height:844},hasTouch:true,isMobile:true});
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.setContent('<meta name="viewport" content="width=device-width"><style>'+fs.readFileSync('vococo/gateway/adapters/web_static/styles.css','utf8')+'</style><div id="target"></div><p id="outside">外部正文</p>');
    await page.addScriptTag({content:`const S={conv:"test"};function ic(){return '<svg class="ic" viewBox="0 0 24 24"></svg>'}function esc(s){return s.replaceAll('&','&amp;').replaceAll('<','&lt;')}`});
    await page.addScriptTag({content:fs.readFileSync('vococo/gateway/adapters/web_static/preview.js','utf8')});
    await page.evaluate(()=>appendHtmlPreviews(document.querySelector('#target'),[{id:'touch',html:'<p>Hello</p>',artifact:{id:'x'.repeat(43),filename:'x.html',size:12}}]));
    const toggle=page.getByRole('button',{name:'更多操作',exact:true}),panel=page.locator('.artifact-menu-panel');
    await toggle.tap();assert.equal(await panel.isVisible(),true);
    await page.locator('#outside').tap();
    assert.equal(await panel.isVisible(),false,'WebKit 触屏无焦点时，点外部也应关闭');
    assert.equal(await toggle.getAttribute('aria-expanded'),'false');
    await toggle.tap();await page.keyboard.press('Escape');
    assert.equal(await panel.isVisible(),false,'Escape 不依赖按钮先获得焦点');
    assert.equal(await toggle.getAttribute('aria-expanded'),'false');
    await toggle.tap();
    await page.evaluate(()=>{document.querySelector('.artifact-menu-panel button').onclick=()=>{window.menuAction=true;};});
    await panel.locator('button').first().tap();
    assert.equal(await page.evaluate(()=>window.menuAction),true,'菜单内部操作不得被外点处理抢先取消');
    await toggle.tap();assert.equal(await panel.isVisible(),false);
    assert.deepEqual(errors,[]);
    console.log('WebKit 触屏菜单：外点关闭、Escape、重复开关、内部操作及 aria 状态通过');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(error.message.includes("Executable doesn't exist")?77:1);});
