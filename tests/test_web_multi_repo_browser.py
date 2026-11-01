"""真实浏览器复用现有标题栏、CSS 和 Git 渲染代码，验证点击与窄屏。"""
import json
from pathlib import Path
import subprocess

import pytest

from vococo.tools.website_preview import _runtime


STATIC = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static"


def test_multi_repo_popover_in_browser():
    try:
        node, environment = _runtime()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    script = r'''
const {chromium}=require('playwright');
const fs=require('fs'), http=require('http'), assert=require('assert');
const header=fs.readFileSync(STATIC+'/index.html','utf8').match(/<header class="bar">[\s\S]*?<\/header>/)[0];
const css=fs.readFileSync(STATIC+'/styles.css','utf8');
const source=fs.readFileSync(STATIC+'/sidebar.js','utf8').split('// ── 会话列表')[0];
const repo=(name,dirty,extra={})=>({name,project_path:'/repos/'+name,path:'/copies/'+name,
  branch:'vococo/session-long-branch',state:'ready',dirty,unmerged:0,ahead:0,behind:0,
  added:dirty?6:0,removed:dirty?2:0,files:dirty?[{x:' M',path:'src/<unsafe>.py'}]:[],...extra});
const dirty=repo('first',1,{unmerged:2});
let data={is_project:true,name:'开发',repo_count:4,changed_repos:1,dirty:1,added:6,removed:2,
  unmerged:2,unavailable:2,repositories:[repo('clean',0),dirty,
    repo('未初始化',0,{state:'uninitialized'}),repo('出错',0,{state:'error'})]};
const html='<!doctype html><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">'+
  '<style>'+css+'</style><main id="chatMain">'+header+'<div id="outside">会话内容</div></main>';
const server=http.createServer((req,res)=>{
  if(req.url.startsWith('/conv/git')){res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data));}
  else{res.setHeader('Content-Type','text/html');res.end(html);}
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({headless:true});
  try{
    const page=await browser.newPage();
    const errors=[];page.on('pageerror',e=>errors.push(String(e)));
    await page.goto('http://127.0.0.1:'+server.address().port);
    await page.addScriptTag({content:`
      var S={conv:'test'};
      var $=s=>document.querySelector(s);
      var esc=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
      var api=url=>fetch(url);
      function closeHeaderPopovers(except){for(const id of ['projPop','gitPop']){const p=$('#'+id);if(p!==except)p.hidden=true;}}
    `+source});
    await page.evaluate(()=>refreshGit('test'));
    assert.match(await page.locator('#convGit').innerText(),/4 仓库 · 1 文件/);
    assert.match(await page.locator('#convGit').innerText(),/2 待查/);
    assert.equal(await page.locator('#convProjName').isVisible(),false);
    await page.locator('#convGit').click();
    await page.waitForFunction(()=>document.querySelector('.grepos'));
    assert.match(await page.locator('.grepo').first().innerText(),/first/);
    assert.match(await page.locator('#gitPop').innerText(),/src\/<unsafe>.py/);
    assert.equal(await page.locator('#gitPop unsafe').count(),0);
    await page.locator('.grepo').nth(1).locator('summary').click();
    await page.evaluate(()=>refreshGitQuiet('test'));
    assert.equal(await page.locator('.grepo').nth(1).getAttribute('open'),'');
    for(const index of [2,3]) await page.locator('.grepo').nth(index).locator('summary').click();
    assert.match(await page.locator('#gitPop').innerText(),/尚未初始化，未计入汇总/);
    assert.match(await page.locator('#gitPop').innerText(),/无法读取当前会话副本，未计入汇总/);
    await page.locator('#convGit').click();
    assert.equal(await page.locator('#gitPop').isVisible(),false);
    await page.locator('#convGit').click();
    for(const theme of ['dark','light']){
      await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
      for(const width of [320,375,390,768,1280]){
        await page.setViewportSize({width,height:850});
        const box=await page.locator('#gitPop').boundingBox();
        assert.ok(box.x>=0&&box.x+box.width<=width,`弹层溢出 ${theme} ${width}: ${JSON.stringify(box)}`);
        const button=await page.locator('#convGit').boundingBox();
        assert.ok(button.x>=0&&button.x+button.width<=width,`标题栏溢出 ${width}`);
      }
    }
    data={...data,repo_count:1,changed_repos:0,dirty:0,added:0,removed:0,unmerged:0,
      unavailable:0,repositories:[repo('很长的仓库名'.repeat(15),0)]};
    await page.evaluate(()=>refreshGitQuiet('test'));
    assert.match(await page.locator('#gitPop').innerText(),/无变化/);
    await page.locator('summary').click();
    assert.match(await page.locator('#gitPop').innerText(),/工作区干净/);
    await page.setViewportSize({width:320,height:850});
    assert.ok(await page.locator('#gitPop').evaluate(el=>el.scrollWidth<=el.clientWidth));
    data={...data,repo_count:2,dirty:1,changed_repos:1,unavailable:1,
      repositories:[repo('shared',1,{scope:'shared',path:'/repos/shared'}),
        repo('failed-shared',0,{scope:'shared',state:'error'})]};
    await page.evaluate(()=>refreshGitQuiet('test'));
    assert.match(await page.locator('.grepo').first().innerText(),/共享目录/);
    assert.match(await page.locator('#gitPop').innerText(),/共享目录可能包含其他会话的改动/);
    assert.match(await page.locator('#convGit').getAttribute('title'),/含共享目录/);
    await page.locator('.grepo').first().locator('summary').click();
    assert.match(await page.locator('.grepo').first().innerText(),/src\/<unsafe>.py/);
    assert.ok(!(await page.locator('.grepo').first().innerText()).includes('会话副本：'));
    await page.locator('.grepo').nth(1).locator('summary').click();
    assert.match(await page.locator('.grepo').nth(1).innerText(),/无法读取共享目录，未计入汇总/);
    await page.mouse.click(10,800);
    assert.equal(await page.locator('#gitPop').isVisible(),false);
    assert.deepEqual(errors,[]);
    console.log('通过：真实点击、刷新保留展开、错误状态、长文本、5 档宽度、深浅主题');
  }finally{await browser.close();server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
'''
    result = subprocess.run(
        [node, "-e", "const STATIC=" + json.dumps(str(STATIC)) + ";\n" + script],
        env=environment, capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
