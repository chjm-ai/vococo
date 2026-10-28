// 使用正式页面、真实目录接口与鼠标命中检测,避免只检查 hidden 而漏掉遮挡。
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
  const [origin,token,directory]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,
    ...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    for(const width of [1280,390]){
      const page=await browser.newPage({viewport:{width,height:900},
        ...(width===390?{isMobile:true,hasTouch:true,
          userAgent:'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1'}:{})});
      const errors=[];
      page.on('pageerror',error=>errors.push(error.message));
      await page.goto(origin);
      await page.fill('#pw',token);
      await page.click('#loginBtn');
      await page.waitForFunction(()=>!document.querySelector('#app').hidden);
      await page.waitForFunction(()=>S.agents?.length>0);
      if(width===390) await page.evaluate(()=>expandSidebarResponsive());
      const selectedDirectory=directory+'/'+width;
      await page.getByText('＋ 新建 Agent…',{exact:true}).click();
      const name='目录交互测试'+width;
      await page.fill('#agName',name);
      await page.check('#agDirPick');
      const openPicker=async()=>{
        const loaded=page.waitForResponse(r=>r.url().endsWith('/browse'));
        await page.click('#agBrowse');
        await loaded;
        await page.waitForFunction(()=>document.querySelector('#pmList').children.length>0);
        const top=await page.locator('#pmPath').evaluate(input=>{
          const r=input.getBoundingClientRect();
          return input.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));
        });
        assert.ok(top,'目录输入框被新建 Agent 弹窗遮挡');
      };
      await openPicker();
      await page.click('#pmCancel');
      assert.equal(await page.locator('#agentModal').isVisible(),true);
      assert.equal(await page.inputValue('#agName'),name);
      await openPicker();
      await page.fill('#pmPath','');
      const dialog=page.waitForEvent('dialog').then(async warning=>{
        const message=warning.message();
        await warning.accept();
        return message;
      });
      await page.click('#pmCreate');
      assert.equal(await dialog,'请先选或输入一个目录');
      assert.equal(await page.locator('#projModal').isVisible(),true);
      await page.fill('#pmPath',selectedDirectory);
      const browse=page.waitForResponse(r=>r.url().includes('/browse?'));
      await page.click('#pmGo');
      assert.equal((await browse).status(),200);
      await page.getByText('(无子文件夹)',{exact:true}).waitFor();
      await page.click('#pmCreate');
      assert.equal(await page.locator('#projModal').isVisible(),false);
      assert.equal(await page.inputValue('#agPath'),selectedDirectory);
      assert.equal(await page.inputValue('#agName'),name);
      const created=page.waitForResponse(r=>r.url().endsWith('/agents/create'));
      await page.click('#agCreate');
      const response=await created;
      assert.equal(response.status(),200);
      const data=await response.json();
      assert.equal(data.agent.workdir,selectedDirectory);
      await page.waitForFunction(()=>document.querySelector('#agentModal').hidden);
      assert.deepEqual(errors,[]);
      await page.screenshot({path:'/tmp/vococo-agent-directory-'+width+'.png'});
      await page.close();
    }
    console.log('PASS: 桌面/手机选择目录、取消保留表单、空路径提示及真实创建');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
