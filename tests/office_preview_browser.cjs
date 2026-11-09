const {chromium,webkit}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const fs=require('node:fs');
const assert=require('node:assert/strict');
(async()=>{
  for(const engine of [process.argv[3]==='webkit' ? webkit : chromium]){
    const browser=await engine.launch({headless:true,...(engine===chromium && process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
    try{
      for(const width of [375,1280]){
        const page=await browser.newPage({viewport:{width,height:844}});
        const errors=[];page.on('pageerror',error=>errors.push(error.message));
        await page.goto(process.argv[2]);
        await page.addScriptTag({content:`
          const S={conv:'test',docPreview:{}}, DOTS='加载中';
          const $=s=>document.querySelector(s), el=(tag,cls)=>{const n=document.createElement(tag);if(cls)n.className=cls;return n;};
          function ic(){return ''} function esc(s){return String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');}
          function isLocalHtmlPath(){return false} async function api(url){return fetch(url)}
        `});
        await page.addScriptTag({content:fs.readFileSync('vococo/gateway/adapters/web_static/markdown.js','utf8')});
        await page.evaluate(()=>openDocPreview({kind:'path',target:'sales.xlsx',title:'销售.xlsx'}));
        await page.getByRole('button',{name:'库存',exact:true}).click();
        assert.equal(await page.locator('.dp-office-grid td').first().textContent(),'剩余');
        await page.getByRole('button',{name:'销售',exact:true}).click();
        assert.equal(await page.locator('.dp-office-grid td').nth(2).textContent(),'<script>alert(1)</script>');
        assert.equal(await page.locator('#dpBody script').count(),0);
        const download=page.waitForEvent('download');
        await page.locator('#dpDlBtn').click();
        assert.equal((await download).suggestedFilename(),'sales.xlsx');
        await page.evaluate(()=>openDocPreview({kind:'path',target:'report.docx',title:'报告.docx'}));
        assert.equal(await page.locator('.dp-office-grid td').first().textContent(),'项目');
        assert.equal(await page.locator('#dpBody img').count(),1);
        assert.equal(await page.locator('#dpBody img[onerror]').count(),0);
        assert(await page.locator('#dpBody img').evaluate(image=>image.complete && image.naturalWidth>0));
        const fits=await page.evaluate(()=>document.querySelector('#docPreview').getBoundingClientRect().right<=innerWidth+1);
        assert(fits,'手机和桌面面板不得横向溢出');
        await page.evaluate(()=>openDocPreview({kind:'path',target:'bad.xlsx',title:'损坏.xlsx'}));
        assert.match(await page.locator('.dp-err').textContent(),/无法预览/);
        assert(await page.locator('#dpDlBtn').isVisible(),'失败仍保留原件下载');
        assert.deepEqual(errors,[]);
        await page.close();
      }
      console.log(engine.name()+': Excel 切表、Word 正文/表格/图片、下载、损坏文件、移动端和安全转义通过');
    }finally{await browser.close();}
  }
})().catch(error=>{console.error(error);process.exit(error.message.includes("Executable doesn't exist")?77:1);});
