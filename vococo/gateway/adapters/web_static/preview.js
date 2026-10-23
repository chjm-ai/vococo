"use strict";
// 只保存节点自己的状态；离开会话后可被回收，不建立无限增长的源码/SVG 缓存。
const PREVIEW_LIMIT = 30000, PREVIEW_INTERVAL = 800;
let previewMermaidPromise, previewSerial = Promise.resolve(), previewSequence = 0;
function safePreviewHtml(source){
  if(source.length>200000) return "<p>内容过大，已停止预览，请下载或复制完整消息。</p>";
  const doc = new DOMParser().parseFromString(source, "text/html");
  doc.querySelectorAll("script,iframe,frame,frameset,object,embed,base,meta,link,form,animate,set,animateMotion,animateTransform,discard,svg foreignObject").forEach(n=>n.remove());
  for(const node of doc.querySelectorAll("*")){
    for(const attr of [...node.attributes]){
      if(/^on/i.test(attr.name) || /^(href|xlink:href|src|srcset|action|formaction|target|ping|download|autofocus|srcdoc)$/i.test(attr.name)) node.removeAttribute(attr.name);
    }
  }
  const policy = "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'none'; connect-src 'none'; media-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";
  return '<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="'+policy+'"><meta name="referrer" content="no-referrer">'+doc.head.innerHTML+'</head><body>'+doc.body.innerHTML+'</body></html>';
}
function makeSafePreviewFrame(source){
  const frame = document.createElement("iframe");
  frame.className = "preview-frame"; frame.title = "隔离的静态预览";
  frame.setAttribute("sandbox", ""); frame.referrerPolicy = "no-referrer";
  frame.srcdoc = safePreviewHtml(source);
  return frame;
}
function previewDiagramTheme(){
  const css=getComputedStyle(document.documentElement),tokens={};
  for(const name of ["bg2","panel","panel2","text","dim","line2","accent","fs-sm","fs-base","sp-3","sp-4","sp-5","r-sm"])
    tokens[name]=css.getPropertyValue("--"+name).trim();
  const value=name=>tokens[name],size=name=>parseFloat(value(name));
  const variables={background:value("bg2"),primaryColor:value("panel"),primaryTextColor:value("text"),
    primaryBorderColor:value("line2"),secondaryColor:value("panel2"),secondaryTextColor:value("text"),
    secondaryBorderColor:value("line2"),tertiaryColor:value("bg2"),tertiaryTextColor:value("text"),
    tertiaryBorderColor:value("line2"),lineColor:value("dim"),textColor:value("text"),
    mainBkg:value("panel"),nodeBorder:value("line2"),clusterBkg:value("panel2"),clusterBorder:value("line2"),
    edgeLabelBackground:value("bg2"),actorBkg:value("panel"),actorBorder:value("accent"),
    actorTextColor:value("text"),actorLineColor:value("line2"),signalColor:value("dim"),signalTextColor:value("text"),
    labelBoxBkgColor:value("panel2"),labelBoxBorderColor:value("line2"),labelTextColor:value("text"),
    loopTextColor:value("dim"),noteBkgColor:value("panel2"),noteTextColor:value("text"),noteBorderColor:value("line2"),
    activationBkgColor:value("panel2"),activationBorderColor:value("accent"),
    fontFamily:'-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif',fontSize:value("fs-base")};
  return {key:JSON.stringify(tokens),tokens,variables,padding:size("sp-5"),font:size("fs-base"),minFont:size("fs-sm"),
    gap:size("sp-4"),radius:size("r-sm")};
}
function initializePreviewMermaid(engine,theme){
  engine.initialize({startOnLoad:false,securityLevel:"strict",suppressErrorRendering:true,htmlLabels:false,
    theme:"base",themeVariables:theme.variables,maxTextSize:PREVIEW_LIMIT,maxEdges:300,
    flowchart:{htmlLabels:false,padding:theme.padding,nodeSpacing:theme.padding*2,rankSpacing:theme.padding*3},
    sequence:{useMaxWidth:true,actorMargin:theme.padding*2,boxMargin:theme.gap,noteMargin:theme.gap,
      messageMargin:theme.padding*2,actorFontSize:theme.font,noteFontSize:theme.font,messageFontSize:theme.font},
    secure:["securityLevel","startOnLoad","maxTextSize","maxEdges","suppressErrorRendering"]});
}
function addPreviewLoopBackgrounds(svg,theme){
  // 循环框本身只有边线；把底色放在 SVG 最底层，避免盖住先绘制的消息与生命线。
  const groups=new Set([...svg.querySelectorAll(".loopLine")].map(line=>line.parentNode));
  for(const group of groups){
    const lines=[...group.querySelectorAll(":scope > .loopLine")],xs=[],ys=[];
    for(const line of lines) for(const end of ["1","2"]){
      xs.push(Number(line.getAttribute("x"+end))); ys.push(Number(line.getAttribute("y"+end)));
    }
    if(!xs.length || ![...xs,...ys].every(Number.isFinite)) continue;
    const rect=svg.ownerDocument.createElementNS("http://www.w3.org/2000/svg","rect");
    for(const [name,value] of Object.entries({x:Math.min(...xs),y:Math.min(...ys),width:Math.max(...xs)-Math.min(...xs),
      height:Math.max(...ys)-Math.min(...ys),rx:theme.radius,fill:theme.tokens.panel2,"fill-opacity":0.4})) rect.setAttribute(name,value);
    rect.setAttribute("class","preview-loop-background"); svg.prepend(rect);
  }
}
function previewDiagramMinScale(svg,theme){
  // 部分图种（如甘特图）有独立的小字号，不能只按主题的正文字号估算缩放下限。
  const sizes=[theme.font];
  for(const node of svg.querySelectorAll("style,[style],[font-size]")){
    const css=node.localName==="style"?node.textContent:node.getAttribute("style")||"";
    for(const match of css.matchAll(/font-size\s*:\s*(\d+(?:\.\d+)?)px/g)) if(Number(match[1])>0) sizes.push(Number(match[1]));
    const attribute=parseFloat(node.getAttribute("font-size"));
    if(attribute>0) sizes.push(attribute);
  }
  return theme.minFont/Math.min(...sizes);
}
function preparePreviewDiagram(source,theme){
  const doc=new DOMParser().parseFromString(source,"image/svg+xml"),svg=doc.documentElement;
  const box=svg.viewBox.baseVal,width=box.width,height=box.height;
  if(!Number.isFinite(width) || !Number.isFinite(height) || width<=0 || height<=0) throw new Error("无效的图表尺寸");
  const minScale=previewDiagramMinScale(svg,theme),maxScale=Math.max(1,minScale);
  svg.setAttribute("style","display:block;width:100%;height:auto;margin:auto;max-width:"+width*maxScale+"px");
  svg.removeAttribute("height"); svg.setAttribute("width","100%");
  for(const rect of svg.querySelectorAll(".node rect,rect.actor")){
    rect.setAttribute("rx",theme.radius); rect.setAttribute("ry",theme.radius);
  }
  addPreviewLoopBackgrounds(svg,theme);
  const ink=theme.tokens.text,muted=theme.tokens.dim,line=theme.tokens.line2,surface=theme.tokens.panel2;
  const style=doc.createElementNS("http://www.w3.org/2000/svg","style");
  const scope="#"+svg.id;
  style.textContent=`${scope} text{font-family:${theme.variables.fontFamily};fill:${ink}}
    ${scope} .nodeLabel{font-weight:500} ${scope} .actor-line{stroke:${line};stroke-width:1}
    ${scope} .messageLine0,${scope} .messageLine1{stroke:${muted};stroke-width:1.5}
    ${scope} .flowchart-link{stroke:${muted};stroke-width:1.5}
    ${scope} .loopLine{stroke:${line};stroke-width:1;stroke-dasharray:${theme.gap/3} ${theme.gap/3}}
    ${scope} .labelBox{fill:${surface};stroke:${line}}`;
  for(const text of svg.querySelectorAll("text.actor")) text.style.fontWeight="500";
  svg.append(style);
  const padding=theme.padding;
  return {source:'<style>html,body{margin:0;background:'+theme.tokens.bg2+'}body{padding:'+padding+'px;box-sizing:border-box}</style>'+svg.outerHTML,
    geometry:{width,height,padding,minScale,maxScale}};
}
function sizePreviewDiagram(node){
  const state=node?._preview,geometry=state?.geometry,frame=state?.view.querySelector(".preview-diagram");
  if(!geometry || !frame || !node.isConnected) return;
  const available=state.view.clientWidth;
  if(!available) return;
  const {width,height,padding,minScale,maxScale}=geometry;
  // 宽图保留最小可读字号，滚动放在父页面；沙箱内既不测量 DOM，也不运行脚本。
  const frameWidth=Math.max(available,Math.ceil(width*minScale+padding*2));
  const scale=Math.min(maxScale,(frameWidth-padding*2)/width);
  frame.style.width=frameWidth+"px";
  frame.style.height=Math.ceil(height*scale+padding*2)+"px";
}
const previewResizeObserver=new ResizeObserver(entries=>{
  for(const entry of entries) sizePreviewDiagram(entry.target.closest(".chat-preview"));
});
function refreshPreviewThemes(){
  for(const node of document.querySelectorAll('.chat-preview[data-lang="mermaid"]')) scheduleChatPreview(node);
}
const previewThemeObserver=new MutationObserver(refreshPreviewThemes);
previewThemeObserver.observe(document.documentElement,{attributes:true,attributeFilter:["data-theme"]});
matchMedia("(prefers-color-scheme:light)").addEventListener("change",()=>{
  if(!document.documentElement.hasAttribute("data-theme")) refreshPreviewThemes();
});
function loadPreviewMermaid(){
  if(previewMermaidPromise) return previewMermaidPromise;
  previewMermaidPromise = new Promise((resolve,reject)=>{
    const script = document.createElement("script"); script.src = "/mermaid-11.12.2.min.js";
    script.onload = ()=>resolve(mermaid);
    script.onerror = ()=>{ previewMermaidPromise=null; script.remove(); reject(new Error("图表组件加载失败")); };
    document.head.append(script);
  });
  return previewMermaidPromise;
}
function previewCodeHtml(lang, source, closed, streaming){
  const limited=source.length>200000;
  if(limited) source=source.slice(0,200000)+"\n[源码过大，显示前 200000 字符；完整内容请复制整条消息]";
  return '<section class="chat-preview" data-lang="'+lang+'" data-closed="'+closed+'" data-live="'+streaming+'"><pre class="preview-source" hidden><code>'+esc(source)+'</code></pre></section>';
}
function appendHtmlPreviews(target, entries){
  for(const entry of entries||[]){
    if(typeof entry.id!=="string" || typeof entry.html!=="string" || entry.html.length>200000) continue;
    // done 带全量快照兜底；中途帧重放或历史补帧不能再建同一卡片。
    if([...target.querySelectorAll('[data-html-preview]')].some(n=>n.dataset.htmlPreview===entry.id)) continue;
    const holder=document.createElement("div");
    holder.innerHTML=previewCodeHtml("html",entry.html,true,false);
    const card=holder.firstElementChild;
    card.dataset.htmlPreview=entry.id; card.dataset.title=entry.title||"HTML";
    target.append(card); setupChatPreview(card);
  }
}
function previewIcon(name){
  return ic(name).replace('<svg ', '<svg aria-hidden="true" focusable="false" ');
}
function setPreviewButton(button, icon, label){
  button.innerHTML=previewIcon(icon); button.title=label; button.setAttribute("aria-label",label);
}
function setPreviewStatus(state, message, kind="busy"){
  state.status.dataset.state=kind;
  state.status.innerHTML=previewIcon(kind==="busy"?"refresh":kind==="done"?"save":"warn");
  state.status.title=message; state.status.setAttribute("aria-label",message);
  state.note.textContent=kind==="warning"?message:""; state.note.hidden=kind!=="warning";
}
function finishPreviewStatus(state){
  if(!state.closed) return setPreviewStatus(state,"生成中，预览逐步更新…");
  setPreviewStatus(state,state.fencedClosed?"已完成":"围栏未闭合，已按现有内容预览",state.fencedClosed?"done":"warning");
}
async function copyPreviewSource(state, button){
  clearTimeout(state.copyTimer);
  try{
    await navigator.clipboard.writeText(state.source);
    setPreviewButton(button,"save","已复制"); button.dataset.feedback="done";
  }catch(error){
    setPreviewButton(button,"warn","复制失败，请重试或手动复制源码"); button.dataset.feedback="warning";
  }
  state.copyTimer=setTimeout(()=>{setPreviewButton(button,"copy","复制源码");delete button.dataset.feedback;},2000);
}
async function previewShareRequest(path, method, token, body){
  const response=await fetch(path,{method,headers:{"X-Auth-Token":token,"Content-Type":"application/json"},
    ...(body?{body:JSON.stringify(body)}:{}),cache:"no-store"});
  const result=await response.json();
  if(!response.ok) throw new Error(result.error||"分享请求失败");
  return result;
}
function previewShareNote(node,message){
  if(!node.isConnected) return;
  node._preview.shareNote.textContent=message; node._preview.shareNote.hidden=false;
}
function addPreviewShareButtons(node,bar){
  const state=node._preview;
  state.shareNote=document.createElement("div");state.shareNote.className="preview-share-note";state.shareNote.hidden=true;
  state.shareNote.setAttribute("role","status");
  const open=document.createElement("button"),copy=document.createElement("button");
  for(const button of [copy,open]) button.type="button";
  setPreviewButton(open,"external","在浏览器打开（创建7天静态快照）");
  setPreviewButton(copy,"copy","复制URL（7天有效，持有链接即可访问）");
  open.onclick=()=>usePreviewShare(node,"open");
  copy.onclick=()=>usePreviewShare(node,"copy");
  state.shareButtons={open,copy};
  bar.append(copy,open);
}
async function ensurePreviewShare(state,source,token,isCurrent){
  if(state.share && state.shareSource===source && state.shareToken===token && state.share.expires*1000>Date.now()){
    const current=await previewShareRequest('/api/preview-shares','GET',token);
    if(current.shares.some(share=>share.id===state.share.id)) return state.share;
  }
  if(!isCurrent()) return null;
  const share=await previewShareRequest('/api/preview-shares','POST',token,{html:source});
  if(!isCurrent()) return null;
  state.share=share; state.shareSource=source; state.shareToken=token;
  return share;
}
function previewShareURL(share){
  return new URL(share.path,"https://vococo.chjm.cc").href;
}
async function copyPreviewShareURL(sharePromise,isCurrent){
  const text=sharePromise.then(share=>{
    if(!isCurrent() || !share) throw new Error("会话已切换");
    return previewShareURL(share);
  });
  // WebKit 要求点击时就调用剪贴板 API，网络结果通过延迟数据交付。
  if(navigator.clipboard.write && typeof ClipboardItem!=="undefined"){
    const data=text.then(url=>new Blob([url],{type:"text/plain"}));
    // 剪贴板可能先拒绝而不再消费数据；仍须接住后到的网络失败。
    data.catch(()=>{});
    return navigator.clipboard.write([new ClipboardItem({"text/plain":data})]);
  }
  return navigator.clipboard.writeText(await text);
}
async function usePreviewShare(node,action){
  const state=node._preview,conv=S.conv,source=state.source,token=S.token;
  if(state.shareBusy) return;
  if(location.protocol!=="https:" && !["localhost","127.0.0.1","[::1]"].includes(location.hostname)){
    previewShareNote(node,"请通过主站 HTTPS 地址打开后再创建跨设备分享。");return;
  }
  // 打开仍在点击事件内同步开窗；复制绝不创建窗口。
  const popup=action==="open"?window.open('about:blank','_blank'):null;
  if(action==="open" && !popup){previewShareNote(node,"新窗口被拦截，请允许弹窗后重试。");return;}
  if(popup) popup.opener=null;
  state.shareBusy=true;
  for(const button of Object.values(state.shareButtons)) button.disabled=true;
  const isCurrent=()=>node.isConnected && S.conv===conv && S.token===token;
  try{
    const pending=ensurePreviewShare(state,source,token,isCurrent);
    const copied=action==="copy"?copyPreviewShareURL(pending,isCurrent).then(()=>null,error=>error):null;
    const share=await pending;
    if(!isCurrent() || !share){popup?.close();return;}
    const url=previewShareURL(share);
    if(popup) popup.location.replace(url);
    else{const error=await copied;if(error) throw error;}
    if(isCurrent()) previewShareNote(node,action==="copy"?"已复制URL；持有链接即可访问，快照7天有效。":"已打开静态快照，7天有效；后续编辑不会改变此链接内容。");
  }catch(error){popup?.close();if(isCurrent()) previewShareNote(node,(action==="copy"?"复制URL失败：":"打开失败：")+error.message+"，请重试。");}
  finally{state.shareBusy=false;for(const button of Object.values(state.shareButtons)) button.disabled=false;}
}

function setupChatPreview(node){
  if(node._preview){
    const state=node._preview;
    if(node.isConnected) previewResizeObserver.observe(state.view);
    sizePreviewDiagram(node);
    const staleTheme=node.dataset.lang==="mermaid" && state.renderTheme!==previewDiagramTheme().key;
    if(node.isConnected && !state.queued && (state.needsResume || staleTheme)) scheduleChatPreview(node);
    return;
  }
  const source=node.querySelector("code").textContent;
  const state=node._preview={source,closed:node.dataset.live!=="true",fencedClosed:node.dataset.closed==="true",version:0,lastAt:0};
  const bar=document.createElement("div"); bar.className="preview-toolbar";
  const label=document.createElement("span"); label.className="preview-title";
  label.textContent=node.dataset.title || node.dataset.lang.toUpperCase();
  state.status=document.createElement("span"); state.status.className="preview-status";
  state.status.setAttribute("role","status"); state.status.setAttribute("aria-atomic","true");
  const toggle=document.createElement("button"); toggle.type="button"; toggle.setAttribute("aria-pressed","false");
  setPreviewButton(toggle,"command","显示源码");
  toggle.onclick=()=>{
    const pre=node.querySelector("pre"); pre.hidden=!pre.hidden; state.view.hidden=!pre.hidden;
    toggle.setAttribute("aria-pressed",String(!pre.hidden)); setPreviewButton(toggle,pre.hidden?"command":"eye",pre.hidden?"显示源码":"显示预览");
  };
  const copy=document.createElement("button"); copy.type="button"; setPreviewButton(copy,"copy","复制源码");
  copy.onclick=()=>copyPreviewSource(state,copy);
  bar.append(label,state.status,toggle);
  if(node.dataset.lang==="html") addPreviewShareButtons(node,bar);
  else bar.append(copy);
  state.note=document.createElement("div"); state.note.className="preview-note"; state.note.hidden=true;
  state.view=document.createElement("div"); state.view.className="preview-view";
  node.prepend(bar,state.note,state.view);
  if(state.shareNote) node.insertBefore(state.shareNote,state.view);
  if(node.isConnected) previewResizeObserver.observe(state.view);
  scheduleChatPreview(node);
}
function scheduleChatPreview(node){
  const state=node._preview; clearTimeout(state.timer); state.version++; state.needsResume=true;
  setPreviewStatus(state,state.closed?"正在定稿…":"生成中，预览逐步更新…");
  state.timer=setTimeout(()=>{
    if(!node.isConnected) return;
    state.lastAt=Date.now();
    // 串行渲染避免 Mermaid 的全局状态竞争；排队时读取最新版本，不积压旧快照。
    if(state.queued) return;
    state.queued=true;
    previewSerial=previewSerial.then(async()=>{ state.queued=false; if(node.isConnected) await updateChatPreview(node); }).catch(()=>{});
  }, state.closed?0:Math.max(0,PREVIEW_INTERVAL-(Date.now()-state.lastAt)));
}
async function updateChatPreview(node){
  const state=node._preview, version=state.version; state.needsResume=false;
  let source=state.source;
  if(source.length>(node.dataset.lang==="html"?200000:PREVIEW_LIMIT)){ setPreviewStatus(state,"内容过大，已停止自动预览，请查看源码","warning"); return; }
  if(!state.closed && !state.fencedClosed) source=source.slice(0,source.lastIndexOf("\n")+1);
  if(!source.trim()){ setPreviewStatus(state,state.closed?"内容为空":"生成中，等待完整内容…",state.closed?"warning":"busy"); return; }
  const theme=node.dataset.lang==="mermaid"?previewDiagramTheme():null;
  if(source===state.rendered && theme?.key===state.renderTheme){ finishPreviewStatus(state); return; }
  const input=source; let geometry;
  try{
    if(node.dataset.lang==="mermaid"){
      // 禁止模型通过初始化指令/前置配置覆盖严格模式，或加载动态图表模块。
      if(/%%\{|^\s*---|\b(?:click|href)\b|\bimg\s*:|\b(?:url|image-set)\s*\(|@(?:import|font-face)|\\|<\s*[a-z]|&(?:lt|#0*60|#x0*3c);|(?:https?:|data:|blob:|file:|\/\/)/i.test(source)) throw new Error("不支持交互或自定义配置");
      const engine=await loadPreviewMermaid();
      initializePreviewMermaid(engine,theme);
      const result=await engine.render("chatdiagram"+(++previewSequence),source);
      const diagram=preparePreviewDiagram(result.svg,theme);
      source=diagram.source; geometry=diagram.geometry;
      if(theme.key!==previewDiagramTheme().key) return;
    }
    // 新 token 仍在追加时也提交成功的旧快照，避免持续输出使预览一直饿死。
    if(version!==state.version && !state.source.startsWith(input)) return;
    if(!node.isConnected){state.needsResume=true;return;}
    const frame=makeSafePreviewFrame(source);
    if(node.dataset.lang==="mermaid") frame.classList.add("preview-diagram");
    state.view.replaceChildren(frame); state.rendered=input; state.renderTheme=theme?.key; state.geometry=geometry;
    sizePreviewDiagram(node);
    if(state.closed && state.source===input) finishPreviewStatus(state);
    else setPreviewStatus(state,"生成中，预览逐步更新…");
  }catch(error){
    if(version!==state.version) return;
    setPreviewStatus(state,state.closed?"无法预览，请切换源码检查语法或安全限制":"生成中，等待完整语法（保留上次预览）…",state.closed?"warning":"busy");
  }
}
// 流式正文仍沿用原 Markdown 解析，只转移同序号预览节点，避免每帧销毁 iframe。
function renderPreviewMarkdown(target, source, streaming=false){
  const holder=document.createElement("div"); holder.innerHTML=mdToHtml(source,streaming);
  const previous=[...target.querySelectorAll(".chat-preview")];
  holder.querySelectorAll(".chat-preview").forEach((next,index)=>{
    const old=previous[index];
    if(!old?._preview || old.dataset.lang!==next.dataset.lang) return;
    const state=old._preview, raw=next.querySelector("code").textContent;
    const closed=!streaming, fencedClosed=next.dataset.closed==="true";
    if(state.source!==raw || state.closed!==closed || state.fencedClosed!==fencedClosed){
      state.source=raw; state.closed=closed; state.fencedClosed=fencedClosed; old.querySelector("code").textContent=raw;
      scheduleChatPreview(old);
    }
    next._reuse=old;
  });
  const nodes=[...holder.childNodes].map(n=>n._reuse||n);
  for(const child of [...target.childNodes]) if(!nodes.includes(child)) child.remove();
  let cursor=target.firstChild;
  for(const child of nodes){
    if(child===cursor) cursor=cursor.nextSibling;
    else target.insertBefore(child,cursor);
  }
  target.querySelectorAll(".chat-preview").forEach(setupChatPreview);
}
// 历史、文档分屏、后台消息均经过 mdToHtml；只处理新增的预览节点，不重扫整页。
const chatPreviewObserver=new MutationObserver(records=>{
  for(const record of records) for(const removed of record.removedNodes){
    if(removed.nodeType!==1 || removed.isConnected) continue;
    const cards=removed.matches(".chat-preview")?[removed]:removed.querySelectorAll(".chat-preview");
    for(const card of cards) if(card._preview) previewResizeObserver.unobserve(card._preview.view);
  }
  for(const record of records) for(const node of record.addedNodes){
    if(node.nodeType!==1 || !node.isConnected) continue;
    if(node.matches(".chat-preview")) setupChatPreview(node);
    node.querySelectorAll(".chat-preview").forEach(setupChatPreview);
  }
});
chatPreviewObserver.observe(document.body,{childList:true,subtree:true});
