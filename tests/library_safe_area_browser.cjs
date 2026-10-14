// 加载正式 APP；仅替换 CSS env 值，模拟 Chromium 无法提供的 iOS PWA 顶部安全区。
const {chromium}=require('playwright');
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../vococo/gateway/adapters/web_static');
const origin='http://localhost:8401';
const image=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS9sAAAAASUVORK5CYII=','base64');
const item={id:'img-1',kind:'image',title:'安全区测试图片',sources:[]};
async function verifySafeArea(page,width,inset){
  await page.setViewportSize({width,height:844});
  await page.evaluate(inset=>{
    closeSidebar(); document.documentElement.style.setProperty('--test-safe-area-top',inset+'px');
    $('#libraryView').scrollTop=0;
  },inset);
  const label=`${width}/${inset}`;
  const top=await page.locator('.lib-content').evaluate(node=>parseFloat(getComputedStyle(node).paddingTop));
  assert.equal(top,16+(width<=760?inset:0),label+' 内容只加一层安全区');
  const title=await page.locator('#libraryView h1').boundingBox();
  assert.ok(title.y>=(width<=760?inset:0),label+' 标题避开状态栏');
  await page.evaluate(()=>openLibraryDetail('img-1'));
  const drawerTop=await page.locator('.lib-drawer-top').evaluate(node=>parseFloat(getComputedStyle(node).paddingTop));
  assert.equal(drawerTop,width<=760?16+inset:12,label+' 详情顶栏安全区');
  const close=await page.locator('#libraryDetail [data-lib-action="close"]').boundingBox();
  assert.ok(close.y>=(width<=760?inset:0),label+' 详情关闭按钮可操作');
  await page.locator('#libraryDetail [data-lib-action="close"]').click();
  assert.ok(await page.locator('#libraryView h1').isVisible(),label+' 关闭详情后回到作品库');
}
(async()=>{
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{channel:'chrome'})});
  try{
    const page=await browser.newPage(), errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.route(origin+'/**',async route=>{
      const url=new URL(route.request().url());
      const file=path.join(root,url.pathname==='/'?'index.html':url.pathname);
      if(fs.existsSync(file)&&fs.statSync(file).isFile()){
        const body=file.endsWith('.css')?fs.readFileSync(file,'utf8').replace(/env\(safe-area-inset-top(?:,\s*0px)?\)/g,'var(--test-safe-area-top,0px)'):fs.readFileSync(file);
        return route.fulfill({body,contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html'});
      }
      if(url.pathname==='/library/items') return route.fulfill({json:{items:[item],agents:[]}});
      if(url.pathname==='/library/item') return route.fulfill({json:item});
      if(url.pathname==='/library/file') return route.fulfill({body:image,contentType:'image/png'});
      return route.fulfill({json:{turns:[],conversations:[]}});
    });
    await page.goto(origin);
    await page.evaluate(()=>{
      S.token='test-secret'; $('#login').style.display='none'; $('#app').hidden=false;
      $('#chatMain').hidden=false; $('#callView').hidden=true;
      return openLibrary();
    });
    await page.waitForSelector('[data-lib-action="preview"][data-id="img-1"]');
    for(const theme of ['light','dark']){
      await page.evaluate(theme=>{document.documentElement.dataset.theme=theme;},theme);
      for(const width of [320,375,390,760,768,1280]) for(const inset of [0,59]) await verifySafeArea(page,width,inset);
    }
    assert.deepEqual(errors,[]);
    console.log('作品库安全区通过：24组主题/屏宽/安全区，标题、详情顶栏、关闭详情返回。');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
