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
function loadPreviewMermaid(){
  if(previewMermaidPromise) return previewMermaidPromise;
  previewMermaidPromise = new Promise((resolve,reject)=>{
    const script = document.createElement("script"); script.src = "/mermaid-11.12.2.min.js";
    script.onload = ()=>{
      mermaid.initialize({startOnLoad:false,securityLevel:"strict",suppressErrorRendering:true,htmlLabels:false,
        maxTextSize:PREVIEW_LIMIT,maxEdges:300,flowchart:{htmlLabels:false},
        secure:["securityLevel","startOnLoad","maxTextSize","maxEdges","suppressErrorRendering"]});
      resolve(mermaid);
    };
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
function setupChatPreview(node){
  if(node._preview){
    if(node.isConnected && !node._preview.queued && node._preview.needsResume) scheduleChatPreview(node);
    return;
  }
  const source=node.querySelector("code").textContent;
  const state=node._preview={source,closed:node.dataset.live!=="true",fencedClosed:node.dataset.closed==="true",version:0,lastAt:0};
  const bar=document.createElement("div"); bar.className="preview-toolbar";
  const label=document.createElement("span"); label.className="preview-title";
  label.innerHTML=previewIcon(node.dataset.lang==="mermaid"?"branch":"panel");
  label.append(node.dataset.lang.toUpperCase());
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
  bar.append(label,state.status,toggle,copy);
  state.note=document.createElement("div"); state.note.className="preview-note"; state.note.hidden=true;
  state.view=document.createElement("div"); state.view.className="preview-view";
  node.prepend(bar,state.note,state.view); scheduleChatPreview(node);
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
  if(source.length>PREVIEW_LIMIT){ setPreviewStatus(state,"内容过大，已停止自动预览，请查看源码","warning"); return; }
  if(!state.closed && !state.fencedClosed) source=source.slice(0,source.lastIndexOf("\n")+1);
  if(!source.trim()){ setPreviewStatus(state,state.closed?"内容为空":"生成中，等待完整内容…",state.closed?"warning":"busy"); return; }
  if(source===state.rendered){ finishPreviewStatus(state); return; }
  const input=source;
  try{
    if(node.dataset.lang==="mermaid"){
      // 禁止模型通过初始化指令/前置配置覆盖严格模式，或加载动态图表模块。
      if(/%%\{|^\s*---|\b(?:click|href)\b|\bimg\s*:|\b(?:url|image-set)\s*\(|@(?:import|font-face)|\\|<\s*[a-z]|&(?:lt|#0*60|#x0*3c);|(?:https?:|data:|blob:|file:|\/\/)/i.test(source)) throw new Error("不支持交互或自定义配置");
      const engine=await loadPreviewMermaid();
      const result=await engine.render("chatdiagram"+(++previewSequence),source);
      source=result.svg;
    }
    // 新 token 仍在追加时也提交成功的旧快照，避免持续输出使预览一直饿死。
    if(version!==state.version && !state.source.startsWith(input)) return;
    if(!node.isConnected){state.needsResume=true;return;}
    const frame=makeSafePreviewFrame(source);
    if(node.dataset.lang==="mermaid") frame.classList.add("preview-diagram");
    state.view.replaceChildren(frame); state.rendered=input;
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
  for(const record of records) for(const node of record.addedNodes){
    if(node.nodeType!==1) continue;
    if(node.matches(".chat-preview")) setupChatPreview(node);
    node.querySelectorAll(".chat-preview").forEach(setupChatPreview);
  }
});
chatPreviewObserver.observe(document.body,{childList:true,subtree:true});
