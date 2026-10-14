// 真 Chrome + localhost HTTP 夹具，不拦截浏览器请求；测传输、滚动和认证竞态。
const {chromium}=require('playwright');
const http=require('node:http'), fs=require('node:fs'), path=require('node:path');
const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../vococo/gateway/adapters/web_static');
const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS9sAAAAASUVORK5CYII=','base64');
async function runRealLoading(){
  const [, , , origin,token,countText,screenshot]=process.argv, count=Number(countText);
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{channel:'chrome'})});
  try{
    const page=await browser.newPage({viewport:{width:1280,height:720}});
    const thumbnails=[], originals=[], pending=new Set(), transfers=[], errors=[];
    let maxConcurrent=0;
    page.on('pageerror',error=>errors.push(error.message));
    page.on('request',request=>{
      if(request.url().includes('/library/thumbnail?')){
        thumbnails.push(request.url());pending.add(request);maxConcurrent=Math.max(maxConcurrent,pending.size);
        assert.equal(request.headers()['x-auth-token'],token,'缩略图真实请求携带认证');
        assert.equal(new URL(request.url()).searchParams.has('token'),false);
      }
      if(/\/library\/(file|cover)\?/.test(request.url())) originals.push(request.url());
    });
    page.on('requestfinished',request=>pending.delete(request));
    page.on('requestfailed',request=>pending.delete(request));
    page.on('response',response=>{
      if(response.url().includes('/library/thumbnail?')) transfers.push(Promise.resolve({url:response.url(),bytes:Number(response.headers()['content-length']),status:response.status()}));
    });
    await page.goto(origin);await page.fill('#pw',token);await page.click('#loginBtn');
    await page.waitForFunction(()=>!$('#app').hidden);
    await page.evaluate(()=>openLibrary());
    await page.waitForFunction(()=>!LIB.loading&&LIB.items.length===60);
    await page.waitForFunction(()=>$('#libraryWorks img')?.src.startsWith('blob:'));
    const settle=async()=>{
      await page.waitForTimeout(150);
      await page.waitForFunction(()=>libraryImages.active===0&&libraryImages.queue.length===0);
    };
    await settle();const firstScreenRequests=thumbnails.length;
    assert.ok(firstScreenRequests>0&&firstScreenRequests<60,'首屏懒加载而非读取整页');
    assert.equal(originals.length,0,'列表未读取任何 file/cover 原件');
    assert.equal(await page.locator('.lib-card-actions,.lib-card-meta,.lib-card-copy p').count(),0);
    const beforeRefresh=thumbnails.length;
    await page.evaluate(()=>loadLibrary());await settle();
    const refreshExtraRequests=thumbnails.length-beforeRefresh;
    assert.equal(refreshExtraRequests,0,'真实服务刷新不重复读相同版本');
    const sample=thumbnails[0],sampleUrl=new URL(sample),id=sampleUrl.searchParams.get('id');
    const row=await page.evaluate(id=>LIB.items.find(item=>item.id===id),id);
    const originalUrl=origin+(row.kind==='image'?'/library/file?id=':'/library/cover?id=')+encodeURIComponent(id);
    const originalResponse=await fetch(originalUrl,{headers:{'X-Auth-Token':token}});
    assert.equal(originalResponse.status,200);
    const originalBytes=(await originalResponse.arrayBuffer()).byteLength;
    const completed=await Promise.all(transfers),sampleThumbnailBytes=completed.find(entry=>entry.url===sample).bytes;
    const sampleResponse=await fetch(sample,{headers:{'X-Auth-Token':token},cache:'no-store'});
    assert.equal(sampleResponse.status,200);
    const sampleBuffer=Buffer.from(await sampleResponse.arrayBuffer());
    assert.equal(sampleBuffer.length,sampleThumbnailBytes,'响应 Content-Length 与真实缩略图字节一致');
    assert.equal(sampleBuffer.subarray(8,12).toString(),'WEBP');
    assert.ok(sampleThumbnailBytes>0&&sampleThumbnailBytes<originalBytes,'真实缩略图小于噪声原图/封面');
    const beforeConflict=thumbnails.length;
    await page.evaluate(id=>{
      const image=document.querySelector(`[data-library-image="${id}"]`),url=new URL(image.dataset.libraryPath,location.origin);
      libraryReleaseImages(image.parentElement);url.searchParams.set('v','0'.repeat(64));
      image.dataset.libraryPath=url.pathname+url.search;libraryQueueImage(image,$('#libraryView'));
    },id);
    await settle();
    assert.equal(thumbnails.length-beforeConflict,2,'真实409只重试单件一次');
    assert.ok(await page.evaluate(id=>document.querySelector(`[data-library-image="${id}"]`).src.startsWith('blob:'),id));
    const beforeFailure=thumbnails.length;
    await page.evaluate(id=>{
      const image=document.querySelector(`[data-library-image="${id}"]`);
      libraryReleaseImages(image.parentElement);image.dataset.libraryPath='/library/thumbnail?id=missing-regression&v='+'0'.repeat(64);
      libraryQueueImage(image,$('#libraryView'));
    },id);
    await settle();await page.waitForTimeout(300);
    assert.equal(thumbnails.length-beforeFailure,1,'失败不触发IO自动重试环');
    assert.equal(await page.evaluate(id=>document.querySelector(`[data-library-image="${id}"]`).dataset.loading,id),undefined,'失败释放loading标记');
    await page.evaluate(({id,sample})=>{
      const image=document.querySelector(`[data-library-image="${id}"]`),url=new URL(sample);
      image.dataset.libraryPath=url.pathname+url.search;libraryLoadImages($('#libraryView'));
    },{id,sample});
    await settle();
    assert.ok(await page.evaluate(id=>document.querySelector(`[data-library-image="${id}"]`).src.startsWith('blob:'),id),'失败后可显式重试');
    await page.selectOption('#libraryKind','image');await settle();
    assert.ok(await page.evaluate(()=>LIB.items.every(item=>item.kind==='image')));
    await page.selectOption('#libraryKind','');await settle();
    await page.evaluate(()=>loadLibrary(true));
    await page.waitForFunction(count=>!LIB.loading&&LIB.items.length===count,count);
    const beforeScroll=thumbnails.length;
    for(let step=0;step<25;step++){
      await page.evaluate(()=>$('#libraryView').scrollTop+=350);await settle();
    }
    assert.ok(thumbnails.length>beforeScroll,'真实滚动才加载后面的作品');
    assert.equal(originals.length,0,'全列表滚动仍未读原件');
    assert.ok(await page.evaluate(()=>libraryImages.cache.size<=96&&libraryImages.bytes<=24*1024*1024));
    assert.ok(await page.evaluate(()=>[...document.querySelectorAll('#libraryWorks img')].filter(img=>{const r=img.getBoundingClientRect();return r.bottom>0&&r.top<720;}).every(img=>img.complete&&img.naturalWidth>0)),'缓存淘汰后可见图仍正常');
    await page.evaluate(()=>{document.documentElement.dataset.theme='dark';});
    await page.screenshot({path:screenshot});
    const cdp=await page.context().newCDPSession(page);
    await cdp.send('Network.enable');
    await cdp.send('Network.emulateNetworkConditions',{offline:false,latency:100,downloadThroughput:64000,uploadThroughput:-1});
    const slowOriginal=page.waitForRequest(request=>/\/library\/(file|cover)\?/.test(request.url()));
    await page.evaluate(id=>openLibraryDetail(id),id);await slowOriginal;
    await page.evaluate(()=>$('#libraryDetail').close());
    await settle();await page.waitForTimeout(300);
    assert.equal(await page.evaluate(()=>[...LIB.blobs.keys()].filter(image=>$('#libraryDetail').contains(image)).length),0,'原件加载中关闭详情不保留迟到URL/Blob');
    assert.equal(await page.evaluate(()=>$('#libraryDetail img')?.getAttribute('src')),null);
    await cdp.send('Network.emulateNetworkConditions',{offline:false,latency:0,downloadThroughput:-1,uploadThroughput:-1});
    await page.evaluate(id=>openLibraryDetail(id),id);
    await page.waitForFunction(()=>$('#libraryDetail img')?.src.startsWith('blob:'));
    assert.ok(originals.length>0,'详情真实读取原件');
    assert.equal(await page.locator('#libraryDetail [data-lib-action="pin"]').count(),1);
    await page.evaluate(id=>{$('#libraryDetail').close();return openLibraryDetail(id);},id);
    await page.waitForFunction(id=>LIB.selected?.id===id&&$('#libraryDetail img')?.src.startsWith('blob:'),id);
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(()=>$('#libraryDetail').open),true,'旧close事件不能清理快速重开的详情');
    await page.evaluate(()=>showLogin());
    assert.equal(await page.evaluate(()=>libraryImages.cache.size+LIB.blobs.size),0);
    const allTransfers=await Promise.all(transfers);
    assert.equal(allTransfers.filter(entry=>entry.status===409).length,1);
    assert.equal(allTransfers.filter(entry=>entry.status===404).length,1);
    assert.ok(allTransfers.every(entry=>[200,409,404].includes(entry.status)&&entry.bytes>0));assert.ok(maxConcurrent<=4);assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true,mode:'real-WebAdapter',items:count,firstScreenRequests,refreshExtraRequests,maxConcurrent,thumbnailRequests:thumbnails.length,thumbnailBytes:allTransfers.reduce((sum,entry)=>sum+entry.bytes,0),sampleThumbnailBytes,sampleOriginalBytes:originalBytes,originalPreviewRequests:originals.length,screenshot}));
  }finally{await browser.close();}
}
(async()=>{
  if(process.argv[2]==='--real') return runRealLoading();
  let origin, active=0, maxActive=0, delay=60, revision=1, forceConflict=false, itemRequests=0, listRequests=0;
  const requests=[], errors=[];
  const items=Array.from({length:120},(_,i)=>({id:'image-'+i,kind:i%2?'website':'image',title:'作品 '+i,cover_url:i%2?'/library/cover?id=image-'+i:undefined,url:'https://example.com/',updated_at:1}));
  const server=http.createServer(async(req,res)=>{
    const url=new URL(req.url,origin), pathname=url.pathname;
    const json=data=>{res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data));};
    if(pathname.startsWith('/library/') && req.headers['x-auth-token']!=='first' && req.headers['x-auth-token']!=='second') {res.statusCode=401;return json({});}
    if(pathname==='/library/items'){
      listRequests++;
      return json({items:items.filter(item=>!url.searchParams.get('kind')||item.kind===url.searchParams.get('kind')).map(item=>({...item,thumbnail_url:'/library/thumbnail?id='+item.id+'&v='+revision})),agents:[],has_more:false});
    }
    if(pathname==='/library/item'){
      itemRequests++;
      const item=items.find(item=>item.id===url.searchParams.get('id'));
      return json({...item,thumbnail_url:'/library/thumbnail?id='+item.id+'&v='+(revision+(forceConflict?1:0))});
    }
    if(['/library/thumbnail','/library/file','/library/cover'].includes(pathname)){
      const record={path:req.url,token:req.headers['x-auth-token'],bytes:0};requests.push(record);
      active++;maxActive=Math.max(maxActive,active);
      let finished=false;
      const finish=()=>{if(!finished){finished=true;active--;}};
      res.on('close',finish);
      await new Promise(resolve=>setTimeout(resolve,delay));
      if(res.destroyed) return;
      finish();
      if(forceConflict && pathname==='/library/thumbnail' && url.searchParams.get('id')==='image-0'){
        res.statusCode=409;return json({error:'版本在重试时再次变化'});
      }
      record.bytes=png.length;res.setHeader('Content-Type','image/png');return res.end(png);
    }
    const file=path.join(root,pathname==='/'?'index.html':pathname.slice(1));
    if(file.startsWith(root+path.sep)&&fs.existsSync(file)&&fs.statSync(file).isFile()){
      res.setHeader('Content-Type',file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html');return res.end(fs.readFileSync(file));
    }
    json(pathname==='/history'?{turns:[]}:{});
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));origin='http://127.0.0.1:'+server.address().port;
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{channel:'chrome'})});
  try{
    const page=await browser.newPage({viewport:{width:1280,height:720}});
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(origin);
    await page.evaluate(()=>{S.token='first';$('#login').style.display='none';$('#app').hidden=false;$('#callView').hidden=true;LIB.pageSize=120;return openLibrary();});
    const idle=()=>page.waitForFunction(()=>libraryImages.active===0 && libraryImages.queue.length===0);
    await page.waitForFunction(()=>$('#libraryWorks img')?.src.startsWith('blob:'));
    await idle();await page.waitForTimeout(150);await idle();
    const firstScreen=requests.length;
    assert.ok(firstScreen>0&&firstScreen<60,'首屏不读完整列表');
    assert.equal(requests.filter(r=>!r.path.startsWith('/library/thumbnail?')).length,0,'列表绝不读原件');
    assert.equal(await page.locator('.lib-card-actions,.lib-card-meta,.lib-card-copy p').count(),0);
    const beforeRefresh=requests.length;
    await page.evaluate(()=>loadLibrary());await page.waitForTimeout(200);await idle();
    assert.equal(requests.length,beforeRefresh,'刷新复用已加载版本');
    await page.selectOption('#libraryKind','image');await page.waitForTimeout(200);await idle();
    await page.selectOption('#libraryKind','');await page.waitForTimeout(200);await idle();
    revision=2;
    const beforeRevision=requests.length;
    await page.evaluate(()=>loadLibrary());await page.waitForTimeout(200);await idle();
    assert.ok(requests.length>beforeRevision,'封面版本变化重新读取');
    assert.ok(requests.slice(beforeRevision).every(r=>r.path.includes('&v=2')));
    forceConflict=true;revision=6;
    const beforePersistentConflict=requests.length, beforeItemRequests=itemRequests, beforeListRequests=listRequests;
    await page.evaluate(()=>loadLibrary());await page.waitForTimeout(200);await idle();await page.waitForTimeout(300);
    assert.equal(requests.slice(beforePersistentConflict).filter(record=>new URL(record.path,origin).searchParams.get('id')==='image-0').length,2,'连续409最多两次图片请求');
    assert.equal(itemRequests-beforeItemRequests,1,'只刷新单件版本一次');
    assert.equal(listRequests-beforeListRequests,1,'恢复409不触发全列表刷新');
    assert.equal(await page.evaluate(()=>document.querySelector('[data-library-image="image-0"]').dataset.loading),undefined);
    forceConflict=false;
    await page.evaluate(()=>libraryLoadImages($('#libraryView')));await page.waitForTimeout(200);await idle();
    assert.ok(await page.evaluate(()=>document.querySelector('[data-library-image="image-0"]').src.startsWith('blob:')),'409重试耗尽后仍可显式重试');
    const beforeScroll=requests.length;
    for(let step=0;step<20;step++){
      await page.evaluate(()=>$('#libraryView').scrollTop+=400);await page.waitForTimeout(100);await idle();
    }
    assert.ok(requests.length>beforeScroll,'滚动加载新视口');
    assert.ok(await page.evaluate(()=>libraryImages.cache.size<=96 && libraryImages.bytes<=24*1024*1024),'LRU 有界');
    assert.equal(await page.evaluate(()=>[...$('#libraryWorks').querySelectorAll('img')].filter(img=>{const r=img.getBoundingClientRect();return r.top<720&&r.bottom>0;}).every(img=>img.complete&&img.naturalWidth>0)),true,'LRU 淘汰不撤销可见 DOM URL');
    await page.evaluate(()=>openLibraryDetail('image-0'));await page.waitForFunction(()=>$('#libraryDetail img')?.src.startsWith('blob:'));
    assert.ok(requests.at(-1).path.startsWith('/library/file?'),'详情读取原件而非缩略图');
    assert.equal(await page.locator('#libraryDetail [data-lib-action="pin"]').count(),1);
    await page.locator('#libraryDetail [data-lib-action="close"]').click();
    delay=350;revision=3;
    await page.evaluate(()=>{$('#libraryView').scrollTop=0;return loadLibrary();});
    await page.waitForFunction(()=>libraryImages.active>0);
    const boundary=requests.length; revision=4;
    await page.evaluate(()=>{S.token='second';return loadLibrary();});
    assert.equal(await page.evaluate(()=>libraryImages.cache.size),0,'token 改变同步清空缓存');
    await page.waitForTimeout(1200);await idle();
    assert.ok(requests.slice(boundary).every(r=>r.token==='second'),'新会话不沿用旧 token');
    assert.ok(await page.evaluate(()=>[...libraryImages.cache.keys()].every(key=>key.includes('&v=4'))),'迟到旧请求不写新会话缓存');
    delay=350;revision=5;
    await page.evaluate(()=>loadLibrary());await page.waitForFunction(()=>libraryImages.active>0);
    await page.selectOption('#libraryKind','website');await page.waitForTimeout(1200);await idle();
    assert.ok(await page.evaluate(()=>[...document.querySelectorAll('#libraryWorks img')].every(img=>Number(img.dataset.libraryImage.split('-')[1])%2===1)),'旧请求不写新筛选 DOM');
    await page.evaluate(()=>showLogin());
    assert.equal(await page.evaluate(()=>libraryImages.cache.size+LIB.blobs.size),0,'注销清 Blob 与 DOM URL');
    const touch=await browser.newPage({hasTouch:true,isMobile:true,viewport:{width:390,height:844}});
    await touch.goto(origin);
    await touch.evaluate(()=>{S.token='second';$('#login').style.display='none';$('#app').hidden=false;$('#callView').hidden=true;return openLibrary();});
    await touch.locator('.lib-card-open').first().tap();
    await touch.waitForSelector('#libraryDetail [data-lib-action="pin"]');
    assert.equal(await touch.locator('.lib-card-actions').count(),0,'触屏整卡打开真实详情，操作留在详情');
    await touch.close();
    assert.ok(maxActive<=4,'整个生命周期并发最多四个');assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true,fixture:'真实HTTP夹具，非WebAdapter；原图/缩略图均用小PNG测调度，不代表生产压缩比',firstScreenRequests:firstScreen,refreshExtraRequests:0,totalRequests:requests.length,totalBytes:requests.reduce((sum,r)=>sum+r.bytes,0),maxConcurrent:maxActive}));
  }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
