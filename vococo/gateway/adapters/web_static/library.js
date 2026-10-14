"use strict";
// 独立作品库 surface；沿用现有全局工具、导航和 composer，不建立第二套应用。
const LIB = {items:[], agents:[], filters:{kind:"", agent_id:"", state:"visible", search:""}, version:0, detailVersion:0, selected:null, returnConv:null, blobs:new Map(), pageSize:60, offset:0, hasMore:false, total:null, loading:false};
function libraryText(value){ return esc(String(value ?? "")); }
function libraryDate(value){
  if(!value) return "时间未知";
  const date=new Date(typeof value==="number" ? value*1000 : value);
  return Number.isNaN(date.getTime()) ? "时间未知" : date.toLocaleDateString("zh-CN");
}
function libraryButton(action, label, icon, extra=""){
  return `<button type="button" class="lib-button" data-lib-action="${action}" ${extra}>${icon ? ic(icon) : ""}${libraryText(label)}</button>`;
}
function initLibrary(){
  const view=el("main","library-ui"); view.id="libraryView"; view.hidden=true;
  view.innerHTML=`<div class="lib-content"><header class="lib-heading"><div class="lib-title"><button type="button" class="wb-hamb" data-lib-action="sidebar" aria-label="展开侧栏">${ic("panel")}</button><h1>作品库</h1></div></header>
    <div class="lib-filters"><select id="libraryState" aria-label="作品状态"><option value="visible">全部可见</option><option value="pinned">已置顶</option><option value="hidden">已隐藏</option></select>
    <select id="libraryAgent" aria-label="筛选 Agent"><option value="">全部 Agent</option></select>
    <select id="libraryKind" aria-label="作品类型"><option value="">全部作品</option><option value="image">图片</option><option value="website">网站</option></select></div>
    <label class="lib-search">${ic("search")}<input id="librarySearch" type="search" placeholder="搜索作品" aria-label="搜索作品"></label>
    <p id="libraryMessage" role="status"></p><div id="libraryWorks" class="lib-works"></div><div class="lib-results">${libraryButton("more","加载更多","",'id="libraryMore" hidden')}</div></div>`;
  $("#app").append(view);
  const detail=el("dialog","library-ui lib-detail"); detail.id="libraryDetail";
  detail.setAttribute("aria-labelledby","libraryDetailTitle");
  detail.innerHTML=`<header class="lib-drawer-top"><span>作品详情</span>${libraryButton("close","关闭","close")}</header><p id="libraryDetailMessage" role="status"></p><div id="libraryDetailBody"></div>`;
  document.body.append(detail);
  const viewer=el("dialog","library-viewer"); viewer.id="libraryViewer";
  viewer.innerHTML=`<button type="button" class="lib-viewer-close" data-lib-action="viewer-close" aria-label="关闭">${ic("close")}</button><img alt="作品原图">`;
  document.body.append(viewer);
  viewer.addEventListener("close",()=>{
    if(viewer.dataset.objectUrl) URL.revokeObjectURL(viewer.dataset.objectUrl);
    viewer.querySelector("img").removeAttribute("src"); delete viewer.dataset.objectUrl;
  });
  view.addEventListener("click", libraryClick); detail.addEventListener("click", libraryClick); viewer.addEventListener("click",libraryClick);
  detail.addEventListener("close",()=>{
    // close 事件异步派发；旧弹窗已重新打开时不能清掉新的详情。
    if(detail.open) return;
    LIB.selected=null; LIB.detailVersion++; libraryReleaseImages(detail);
  });
  for(const [id,key] of [["libraryState","state"],["libraryAgent","agent_id"],["libraryKind","kind"]]){
    $("#"+id).onchange=e=>{ LIB.filters[key]=e.target.value; loadLibrary(); };
  }
  let timer; $("#librarySearch").oninput=e=>{
    LIB.filters.search=e.target.value; clearTimeout(timer); resetLibraryPage();
    timer=setTimeout(loadLibrary,250);
  };
}
async function openLibrary(){
  if(S.surface!=="library"){
    saveComposerState(); LIB.returnConv=S.conv; closeCallView();
  }
  closeWorkbench(); S.surface="library";
  $("#chatMain").hidden=true; $("#libraryView").hidden=false;
  navRecord({kind:"library"}); closeSidebar(); renderConvs();
  await loadLibrary();
}
function closeLibrary(){
  $("#libraryView").hidden=true;
  if($("#libraryDetail").open) $("#libraryDetail").close();
  LIB.version++; LIB.detailVersion++; libraryReleaseImages();
}
async function libraryJson(path, options={}){
  const epoch=libraryImages.epoch, token=S.token;
  const response=await fetch(path,{...options,headers:{...options.headers,"X-Auth-Token":token},cache:"no-store"});
  if(epoch!==libraryImages.epoch || token!==S.token) throw new Error("登录状态已变化");
  if(response.status===401){ showLogin(); throw new Error("请重新登录"); }
  if(!response.ok) throw new Error(response.status===404 ? "作品不存在或接口尚未就绪" : `请求失败（${response.status}）`);
  return response.status===204 ? {} : response.json();
}
async function loadLibrary(more=false){
  if(more && (LIB.loading || !LIB.hasMore)) return;
  if(!more) resetLibraryPage();
  const version=LIB.version, offset=LIB.offset;
  LIB.loading=true; $("#libraryMore").disabled=true;
  $("#libraryMessage").textContent=more ? "正在加载更多…" : "正在读取作品…";
  try{
    const params=new URLSearchParams({...LIB.filters,limit:String(LIB.pageSize),offset:String(offset)});
    const data=await libraryJson("/library/items?"+params);
    if(version!==LIB.version || S.surface!=="library") return;
    applyLibraryPage(data,more);
    renderLibraryAgents(); renderLibrary(more);
  }catch(error){ if(version===LIB.version) $("#libraryMessage").textContent="读取失败："+error.message; }
  finally{ if(version===LIB.version){ LIB.loading=false; $("#libraryMore").disabled=false; } }
}
function resetLibraryPage(){
  LIB.version++; LIB.items=[]; LIB.offset=0; LIB.hasMore=false; LIB.total=null; LIB.loading=false;
  libraryReleaseImages($("#libraryView"));
  $("#libraryWorks").replaceChildren(); $("#libraryMore").hidden=true;
}
function applyLibraryPage(data,more){
  const rows=Array.isArray(data.items) ? data.items : [];
  const seen=new Set(LIB.items.map(item=>item.id));
  LIB.items.push(...rows.filter(item=>!seen.has(item.id)));
  LIB.offset+=rows.length;
  LIB.hasMore=rows.length>0 && (typeof data.has_more==="boolean" ? data.has_more : rows.length===LIB.pageSize);
  LIB.total=Number.isInteger(data.count) && data.count>=LIB.items.length ? data.count : null;
  if(!more || Array.isArray(data.agents)) LIB.agents=Array.isArray(data.agents) ? data.agents : [];
}
function renderLibraryAgents(){
  const select=$("#libraryAgent");
  select.replaceChildren(new Option("全部 Agent",""), ...LIB.agents.map(agent=>new Option(agent.name,agent.id)));
  if(LIB.filters.agent_id && !LIB.agents.some(agent=>agent.id===LIB.filters.agent_id)) select.add(new Option("未知 Agent",LIB.filters.agent_id));
  select.value=LIB.filters.agent_id;
}
function libraryCover(item,detail=false){
  const path=libraryImagePath(item,detail);
  if((item.kind==="image" || item.cover_url || item.thumbnail_url) && path) return `<div class="lib-cover${item.kind==="website" ? " lib-screenshot" : ""}"><span>${item.kind==="image" ? "正在读取图片…" : "正在读取封面…"}</span><img data-library-image="${libraryText(item.id)}" data-library-path="${libraryText(path)}" ${item.kind==="website" ? 'data-library-cover="true"' : ""} alt="${libraryText(item.title)}" hidden></div>`;
  return `<div class="lib-cover lib-website" aria-label="暂无封面">${ic("external")}</div>`;
}
function libraryCard(item){
  const id=`data-id="${libraryText(item.id)}"`, isWebsite=item.kind==="website";
  const source=(item.sources||[]).find(entry=>librarySourceConv(entry));
  return `<article class="lib-card"><button class="lib-card-open" type="button" data-lib-action="${isWebsite ? "website" : "preview"}" ${id}>${libraryCover(item)}<div class="lib-card-copy"><h3>${libraryText(item.title||"未命名作品")}</h3></div></button>${source ? libraryButton("source","查看会话","external",`data-conv="${libraryText(librarySourceConv(source))}" ${id} aria-label="查看来源会话"`) : ""}</article>`;
}
function renderLibrary(more=false){
  $("#libraryMore").hidden=!LIB.hasMore;
  $("#libraryMessage").textContent=LIB.items.length ? "" : "暂无匹配作品。";
  const works=$("#libraryWorks");
  const rendered=new Set([...works.querySelectorAll('.lib-card-open')].map(button=>button.dataset.id));
  const markup=LIB.items.filter(item=>!more || !rendered.has(String(item.id))).map(libraryCard).join("");
  if(more) works.insertAdjacentHTML("beforeend",markup); else works.innerHTML=markup;
  $("#libraryState").value=LIB.filters.state;
  $("#libraryKind").value=LIB.filters.kind;
  libraryLoadImages($("#libraryView"));
}
// 缓存只存 Blob；每个 DOM 自有 URL，LRU 淘汰不会撤销仍在显示的图片。
const libraryImages={cache:new Map(), bytes:0, queue:[], active:0, epoch:0, observer:null, controllers:new Map()};
function libraryReleaseImages(root=document){
  for(const [controller,job] of libraryImages.controllers){
    if(root.contains(job.image) || !job.image.isConnected) controller.abort();
  }
  for(const [image,url] of LIB.blobs){
    if(root!==document && !root.contains(image) && image.isConnected) continue;
    URL.revokeObjectURL(url); image.removeAttribute("src"); LIB.blobs.delete(image);
  }
  libraryImages.queue=libraryImages.queue.filter(job=>job.image.isConnected && !root.contains(job.image));
  root.querySelectorAll('img[data-library-image]').forEach(image=>{
    image.dataset.libraryGeneration=String(Number(image.dataset.libraryGeneration || 0)+1);
    libraryImages.observer?.unobserve(image.parentElement); delete image.dataset.loading;
  });
}
function libraryClearPrivateImages(){
  libraryImages.epoch++; LIB.version++; LIB.detailVersion++;
  libraryImages.observer?.disconnect(); libraryImages.queue=[];
  for(const controller of libraryImages.controllers.keys()) controller.abort();
  libraryImages.cache.clear(); libraryImages.bytes=0; libraryReleaseImages();
  LIB.items=[]; LIB.selected=null;
  $("#libraryWorks")?.replaceChildren(); $("#libraryDetailBody")?.replaceChildren();
  if($("#libraryDetail")?.open) $("#libraryDetail").close();
}
function libraryImagePath(item,detail=false){
  if(detail) return (item.kind==="website" ? "/library/cover?id=" : "/library/file?id=")+encodeURIComponent(item.id);
  if(typeof item.thumbnail_url==="string"){
    // 不接受任意远端地址、跨作品 ID 或带口令的缩略图 URL。
    try{
      const url=new URL(item.thumbnail_url || "/",location.origin);
      if(url.origin===location.origin && url.pathname==="/library/thumbnail" && url.searchParams.get("id")===String(item.id) && [...url.searchParams.keys()].every(key=>key==="id" || key==="v")) return url.pathname+url.search;
    }catch(error){ /* 无效 URL 不进入图片请求队列。 */ }
    return "";
  }
  // 旧后端才回退；同样经过邻近视口、并发限制与内存缓存。
  const version=item.updated_at || item.created_at || "";
  return libraryImagePath(item,true)+"&v="+encodeURIComponent(version);
}
function libraryRememberBlob(path,blob){
  const cache=libraryImages.cache;
  if(cache.has(path)){ libraryImages.bytes-=cache.get(path).size; cache.delete(path); }
  cache.set(path,blob); libraryImages.bytes+=blob.size;
  while(cache.size>96 || libraryImages.bytes>24*1024*1024){
    const key=cache.keys().next().value;
    libraryImages.bytes-=cache.get(key).size; cache.delete(key);
  }
}
function libraryImageCurrent(job){
  const isDetail=job.root.id==="libraryDetail";
  return job.epoch===libraryImages.epoch && job.token===S.token && job.image.isConnected && job.image.dataset.libraryPath===job.path && Number(job.image.dataset.libraryGeneration || 0)===job.generation && !job.root.hidden && (!isDetail || (job.root.open && job.detailVersion===LIB.detailVersion));
}
function libraryDisplayBlob(job,blob){
  if(!libraryImageCurrent(job)) return;
  const url=URL.createObjectURL(blob); LIB.blobs.set(job.image,url);
  job.image.src=url; job.image.hidden=false; job.image.previousElementSibling.hidden=true;
}
async function libraryRunImage(job){
  if(!libraryImageCurrent(job)) return;
  const cached=libraryImages.cache.get(job.path);
  if(cached){ libraryRememberBlob(job.path,cached); libraryDisplayBlob(job,cached); return; }
  const controller=new AbortController(); libraryImages.controllers.set(controller,job);
  try{
    const response=await libraryReadImage(job,controller);
    if(!libraryImageCurrent(job)) return;
    if(response.status===401){ showLogin(); return; }
    if(!response.ok) throw new Error(`图片读取失败（${response.status}）`);
    const blob=await response.blob();
    if(!blob.type.startsWith("image/")) throw new Error("图片格式不可预览");
    if(!libraryImageCurrent(job)) return;
    if(job.root.id==="libraryView") libraryRememberBlob(job.path,blob);
    libraryDisplayBlob(job,blob);
  }catch(error){
    if(libraryImageCurrent(job) && error.name!=="AbortError") job.image.previousElementSibling.textContent=job.image.dataset.libraryCover ? "网站封面暂不可用" : error.message;
  }finally{
    libraryImages.controllers.delete(controller);
    if(libraryImageCurrent(job)) delete job.image.dataset.loading;
  }
}
async function libraryReadImage(job,controller,retried=false){
  const response=await fetch(job.path,{headers:{"X-Auth-Token":job.token},signal:controller.signal,redirect:"error",cache:"no-store"});
  if(!response.ok) await response.arrayBuffer();
  if(response.status!==409 || retried || job.root.id!=="libraryView" || !libraryImageCurrent(job)) return response;
  // 只刷新冲突的单件作品，且最多重试一次；不触发全列表刷新或 IO 重试环。
  const item=await libraryJson("/library/item?id="+encodeURIComponent(job.image.dataset.libraryImage),{signal:controller.signal});
  if(!libraryImageCurrent(job)) return response;
  const path=libraryImagePath(item);
  if(item.id!==job.image.dataset.libraryImage || !path || path===job.path) return response;
  const index=LIB.items.findIndex(entry=>String(entry.id)===String(item.id));
  if(index>=0) LIB.items[index]=item;
  job.path=path; job.image.dataset.libraryPath=path;
  return libraryReadImage(job,controller,true);
}
function libraryImageNearby(job){
  if(job.root.id!=="libraryView") return true;
  const box=job.image.parentElement.getBoundingClientRect(), viewport=job.root.getBoundingClientRect();
  return box.bottom>=viewport.top-160 && box.top<=viewport.bottom+160;
}
function libraryPumpImages(){
  while(libraryImages.active<4 && libraryImages.queue.length){
    const job=libraryImages.queue.shift();
    if(!libraryImageCurrent(job)) continue;
    // 快速滚动时，排队期间已经离开邻近视口的图不能继续抢带宽。
    if(!libraryImageNearby(job)){
      delete job.image.dataset.loading; libraryImages.observer?.observe(job.image.parentElement); continue;
    }
    libraryImages.active++;
    libraryRunImage(job).finally(()=>{ libraryImages.active--; libraryPumpImages(); });
  }
}
function libraryQueueImage(image,root){
  if(image.dataset.loading || image.getAttribute("src")) return;
  image.dataset.loading="1";
  libraryImages.queue.push({image,root,path:image.dataset.libraryPath,epoch:libraryImages.epoch,token:S.token,generation:Number(image.dataset.libraryGeneration || 0),detailVersion:LIB.detailVersion});
  libraryPumpImages();
}
async function libraryFile(id, cover=false){
  // 仅从作品 ID 构造同源接口；封面不能变成原图下载，也不信服务端任意 URL。
  const epoch=libraryImages.epoch, token=S.token;
  const response=await fetch((cover ? "/library/cover?id=" : "/library/file?id=")+encodeURIComponent(id),{headers:{"X-Auth-Token":token},redirect:"error",cache:"no-store"});
  if(epoch!==libraryImages.epoch || token!==S.token) throw new Error("登录状态已变化");
  if(response.status===401){ showLogin(); throw new Error("请重新登录"); }
  if(!response.ok) throw new Error(`原件读取失败（${response.status}）`);
  const blob=await response.blob();
  if(epoch!==libraryImages.epoch || token!==S.token) throw new Error("登录状态已变化");
  if(!blob.type.startsWith("image/")) throw new Error("原件不是可预览的图片");
  return blob;
}
function libraryLoadImages(root){
  if(!libraryImages.observer && typeof IntersectionObserver!=="undefined"){
    libraryImages.observer=new IntersectionObserver(entries=>{
      for(const entry of entries) if(entry.isIntersecting){
        libraryImages.observer.unobserve(entry.target);
        const image=entry.target.querySelector("img[data-library-image]");
        if(image) libraryQueueImage(image,$("#libraryView"));
      }
    },{root:$("#libraryView"),rootMargin:"160px"});
  }
  for(const image of root.querySelectorAll("img[data-library-image]")){
    if(image.getAttribute("src") || image.dataset.loading) continue;
    if(root.id==="libraryView" && libraryImages.observer) libraryImages.observer.observe(image.parentElement);
    else libraryQueueImage(image,root);
  }
}
function libraryWebsiteUrl(value){
  if(typeof value!=="string" || !/^(https?:\/\/|\/(?!\/))/i.test(value.trim())) return "";
  try{
    const url=new URL(value,location.origin);
    if(!["https:","http:"].includes(url.protocol) || url.username || url.password) return "";
    return url.href;
  }catch(error){ return ""; }
}
async function openLibraryDetail(id){
  const version=++LIB.detailVersion; LIB.selected=null;
  libraryReleaseImages($("#libraryDetail"));
  $("#libraryDetailBody").innerHTML='<h2 id="libraryDetailTitle">作品详情</h2>';
  $("#libraryDetailMessage").textContent="正在读取详情…";
  if(!$("#libraryDetail").open) $("#libraryDetail").showModal();
  try{
    const item=await libraryJson("/library/item?id="+encodeURIComponent(id));
    if(version!==LIB.detailVersion || !$("#libraryDetail").open) return;
    LIB.selected=item; renderLibraryDetail();
  }catch(error){ if(version===LIB.detailVersion) $("#libraryDetailMessage").textContent="读取失败："+error.message; }
}
function librarySourceConv(source){
  // 主会话配置只在服务端；仅使用其反验后的路由结果，不从 session_key 自猜。
  if(source.can_open===false) return "";
  return typeof source.conv==="string" ? source.conv : "";
}
function librarySourceMarkup(source){
  const deleted=source.deleted || source.metadata?.deleted;
  const conv=librarySourceConv(source);
  const label=deleted ? "来源已删除" : conv || (source.session_key ? `${source.session_key} · 不可直接打开` : "来源未知");
  return `<div class="lib-source">${!deleted && conv ? libraryButton("source",label,"external",`data-conv="${libraryText(conv)}"`) : libraryText(label)}<p>${libraryText(source.action||"来源动作未知")} · 轮次 ${libraryText(source.turn_id ?? "未知")}</p>${source.metadata ? `<details><summary>来源记录</summary><pre>${libraryText(JSON.stringify(source.metadata,null,2))}</pre></details>` : ""}</div>`;
}
function libraryReferenceOptions(){
  const conv=LIB.returnConv || S.conv;
  const conversations=new Map([[conv,"当前会话"]]);
  for(const item of S.convs) if(!conversations.has(item.conv)) conversations.set(item.conv,item.title||item.conv);
  return [...conversations].map(([id,title])=>`<option value="${libraryText(id)}">${libraryText(title)}</option>`).join("");
}
function renderLibraryDetail(){
  const item=LIB.selected; if(!item) return;
  libraryReleaseImages($("#libraryDetail"));
  $("#libraryDetailMessage").textContent="";
  $("#libraryDetailBody").innerHTML=`<div class="lib-detail-cover">${libraryCover(item,true)}</div><div class="lib-detail-body"><h2 id="libraryDetailTitle">${libraryText(item.title||"未命名作品")}</h2><form id="libraryEdit"><label>标题<input id="libraryTitle" required value="${libraryText(item.title||"")}"></label><label>归属 Agent<select id="libraryDetailAgent"><option value="">待归属</option>${LIB.agents.map(agent=>`<option value="${libraryText(agent.id)}">${libraryText(agent.name)}</option>`).join("")}</select></label>${libraryButton("save","保存", "save")}</form>
    <p class="lib-subtle">创建：${libraryDate(item.created_at)} · 更新：${libraryDate(item.updated_at)}</p><div class="lib-actions">${libraryButton("pin",item.pinned ? "取消置顶" : "置顶","pin")}${libraryButton("hide",item.hidden ? "恢复作品" : "隐藏作品","eye")}${libraryButton(item.kind==="image" ? "download" : "website",item.kind==="image" ? "下载原图" : "打开网站",item.kind==="image" ? "download" : "external")}</div>
    <section class="lib-section"><h3>引用到真实对话</h3><label>目标会话<select id="libraryTarget">${libraryReferenceOptions()}</select></label>${libraryButton("reference","加入待发送附件","clip")}<p class="lib-subtle">只加入附件，不会自动发送。图片由服务端解析为真实附件，网站引用携带作品 ID。</p></section>
    <section class="lib-section"><h3>来源会话</h3>${(item.sources||[]).map(librarySourceMarkup).join("")||'<p class="lib-subtle">暂无可追溯来源</p>'}</section></div>`;
  const select=$("#libraryDetailAgent");
  if(item.agent_id && !LIB.agents.some(agent=>agent.id===item.agent_id)) select.add(new Option("未知 Agent",item.agent_id));
  select.value=item.agent_id||"";
  $("#libraryEdit").onsubmit=async e=>{
    e.preventDefault(); const button=e.target.querySelector("button"); button.disabled=true;
    try{ await libraryPatch(item,{title:$("#libraryTitle").value.trim(),agent_id:select.value||""}); }
    catch(error){ $("#libraryDetailMessage").textContent=error.message; }
    finally{ button.disabled=false; }
  };
  libraryLoadImages($("#libraryDetail"));
}
async function libraryPatch(item, changes){
  if("title" in changes && !changes.title) throw new Error("标题不能为空");
  const id=item.id, version=LIB.detailVersion;
  await libraryJson("/library/item?id="+encodeURIComponent(id),{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify(changes)});
  if(LIB.selected?.id===id && version===LIB.detailVersion){
    const updated=await libraryJson("/library/item?id="+encodeURIComponent(id));
    if(version===LIB.detailVersion && LIB.selected?.id===id){ LIB.selected=updated; renderLibraryDetail(); }
  }
  await loadLibrary();
  if(LIB.selected) libraryLoadImages($("#libraryDetail"));
}
async function libraryPreview(item){
  const blob=await libraryFile(item.id), url=URL.createObjectURL(blob), viewer=$("#libraryViewer"), image=viewer.querySelector("img");
  image.src=url; viewer.dataset.objectUrl=url; viewer.showModal();
}
function closeLibraryViewer(){
  const viewer=$("#libraryViewer"); if(viewer.open) viewer.close();
}
async function libraryDownload(item){
  const blob=await libraryFile(item.id), url=URL.createObjectURL(blob);
  const link=el("a"); link.href=url;
  const extension={"image/png":"png","image/jpeg":"jpg","image/webp":"webp","image/gif":"gif","image/svg+xml":"svg"}[blob.type]||"img";
  link.download=(item.title||"作品").replace(/[\\/:*?"<>|]/g,"_")+"."+extension;
  document.body.append(link); link.click(); link.remove();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}
async function referenceLibraryItem(item, conv){
  if(!conv) throw new Error("请先选择目标会话");
  if(S.sending[conv]) throw new Error("目标会话正在发送，请稍后再引用");
  $("#libraryDetail").close();
  await openConv(conv);
  if(S.conv!==conv || S.surface!=="chat") throw new Error("目标会话已切换，请重新选择");
  if(!S.libraryRefs.some(reference=>reference.id===item.id)) S.libraryRefs.push({id:item.id,title:item.title,kind:item.kind});
  saveComposerAttachments(); renderThumbs(); $("#ta").focus();
}
async function libraryClick(event){
  const button=event.target.closest("[data-lib-action]"); if(!button || button.disabled) return;
  const action=button.dataset.libAction, item=button.dataset.id ? LIB.items.find(entry=>String(entry.id)===button.dataset.id) : LIB.selected;
  button.disabled=true;
  try{ await libraryAction(action,button,item); }
  catch(error){
    const message=$("#libraryDetail").open ? $("#libraryDetailMessage") : $("#libraryMessage");
    message.textContent=error.message;
  }finally{ button.disabled=false; }
}
async function libraryAction(action,button,item){
  if(action==="sidebar") return expandSidebarResponsive();
  if(action==="close") return $("#libraryDetail").close();
  if(action==="viewer-close") return closeLibraryViewer();
  if(action==="more") return loadLibrary(true);
  if(action==="source"){ $("#libraryDetail").close(); return openConv(button.dataset.conv); }
  if(action==="detail") return openLibraryDetail(button.dataset.id);
  if(action==="preview") return libraryPreview(item);
  if(!item) return;
  if(action==="pin") return libraryPatch(item,{pinned:!item.pinned});
  if(action==="hide") return libraryPatch(item,{hidden:!item.hidden});
  if(action==="save"){
    if(!$("#libraryEdit").reportValidity()) return;
    return libraryPatch(item,{title:$("#libraryTitle").value.trim(),agent_id:$("#libraryDetailAgent").value||""});
  }
  if(action==="download") return libraryDownload(item);
  if(action==="reference") return referenceLibraryItem(item,$("#libraryTarget").value);
  if(action==="website"){
    const url=libraryWebsiteUrl(item.open_url || item.url); if(!url) throw new Error("网站链接无效");
    window.open(url,"_blank","noopener,noreferrer");
  }
}
window.openLibrary=openLibrary; window.closeLibrary=closeLibrary;
window.referenceLibraryItem=referenceLibraryItem;
initLibrary();
// token 赋值与退回登录页都同步失效，不等待下一次列表请求。
let libraryToken=S.token;
Object.defineProperty(S,"token",{configurable:true,enumerable:true,
  get:()=>libraryToken,
  set:value=>{ if(value!==libraryToken){ libraryToken=value; libraryClearPrivateImages(); } }
});
const libraryOriginalShowLogin=showLogin;
showLogin=function(){ libraryClearPrivateImages(); return libraryOriginalShowLogin(); };
