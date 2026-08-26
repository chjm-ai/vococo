"use strict";
// 2026-08-14 从 index.html 拆出(前端模块化):发送 / 待发送队列 / 草稿 / 图片·音频·文件附件 / 大图查看器 / 语音输入。
// 与内联脚本同属全局作用域(无构建步骤),加载顺序见 index.html。

// ── 会话级待发送附件 ─────────────────────────────────────────────────────
// 图片 base64 和已上传附件都可能很大，不能放 localStorage；仅在当前页面内按会话保存。
function saveComposerAttachments(conv=S.conv){
  if(!conv) return;
  if(S.images.length || S.audios.length || S.files.length){
    S.composerAttachments[conv]={images:S.images,audios:S.audios,files:S.files};
  }else delete S.composerAttachments[conv];
}
function restoreComposerAttachments(conv){
  const attachments=S.composerAttachments[conv]||{};
  S.images=attachments.images||[];
  S.audios=attachments.audios||[];
  S.files=attachments.files||[];
  renderThumbs();
}
function clearComposerAttachments(conv=S.conv){
  if(conv) delete S.composerAttachments[conv];
  if(conv===S.conv){ S.images=[]; S.audios=[]; S.files=[]; }
}

// ── 发送 ────────────────────────────────────────────────────────────────
async function send(text, display, opts){
  opts=opts||{};
  closeCmdMenu();
  if(!$("#callView").hidden && !opts.forceMain) return window.sendCallText(text);
  text=(text||"").trim(); if(!text && !S.images.length && !S.audios.length && !S.files.length) return;
  // 发送目标钉死在发起时的会话/附件快照:下面等上传的 await 期间用户可能已经切走,
  // S.conv/S.images 等全局状态会变成新会话的——不钉死就会把这条消息、气泡、回执错投过去。
  let sendConv=S.conv;
  const isCurrent=()=>S.conv===sendConv;
  const sendImages=S.images.slice(), sendAudios=S.audios.slice(), sendFiles=S.files.slice();
  // 音频和通用文件都先上传，拿到临时 id 后再发送，避免服务端静默跳过。
  const uploads=[...sendAudios,...sendFiles];
  if(uploads.some(item=>!item.id)){
    const wbtn=$("#sendBtn");
    if(wbtn){ wbtn.disabled=true; wbtn.textContent="⋯"; wbtn.classList.add("uploading"); }
    try{ await Promise.all(uploads.map(item=>item.done||Promise.resolve())); }
    finally{ if(wbtn){ wbtn.disabled=false; wbtn.classList.remove("uploading"); updateSendBtn(); } }
  }
  // 上传失败的附件还没到服务器，保留「忽略并发送」以免卡住文字消息。
  if(uploads.some(item=>item.status==="error")){
    if(!isCurrent()) return;  // 已切走:失败提示/重试按钮没地方挂,静默放弃这次发送
    const fail=uploads.find(item=>item.status==="error");
    const b=addBubble("ai","⚠️ 附件「"+(fail?.filename||"")+"」上传失败"+(fail?.error?("："+fail.error):"")+"，可移除后重试；或忽略附件直接发送文字。");
    const skip=el("button","bact skip"); skip.textContent="忽略并发送";
    skip.onclick=()=>{ S.audios=S.audios.filter(item=>item.status!=="error"); S.files=S.files.filter(item=>item.status!=="error"); b.remove(); send(text, display, opts); };
    b.append(skip);
    return;
  }
  // 上一个任务还没结束 → 不打断,排进待发送队列,任务完成后自动发(审批/语音走 forceSend 立即发)
  if(S.stream && !opts.forceSend){
    if(isCurrent() && queuePending(text, sendImages, sendAudios, sendFiles)){
      clearComposerAttachments(sendConv); renderThumbs(); $("#ta").value=""; autoGrow(); clearDraft(sendConv);
    }
    return;
  }
  const imgs=sendImages.map(x=>x.url);
  const auds=sendAudios.map(x=>({url:x.url, filename:x.filename, text:x.text}));
  const files=sendFiles.map(x=>x.filename);
  // display:点按钮时传选项文字,气泡显示友好文字而非原始命令(如 /clarify id 0)
  const shown=(display!=null)?display:text;
  // 有文字时也要列出附件；否则用户只能看到自己的文字，误以为文件没有随消息发送。
  const fileLabel=files.length ? `\n\n📎 附件：${files.join("、")}` : "";
  // opts.reuseBubble:语音已先冒了占位气泡并回填文字,这里不再重复冒泡
  // !isCurrent():上传等待期间用户已切到别的会话,#wrap 不再属于 sendConv,气泡不能往上贴
  if(!opts.reuseBubble && isCurrent() && (shown || imgs.length || auds.length || files.length)){
    const fallback=auds.length ? "(语音/音频)" : files.length ? "(文件附件)" : "(图片)";
    const b=addBubble("me", (shown||fallback)+fileLabel, imgs, auds);
    // 有音频还没转写:气泡上加转圈 loading(转写在发送后由服务端做,短音频 1~2s,
    // 会议录音几分钟),收到回复流 start 事件(开始回答)才停转
    if(auds.some(a=>!a.text)){
      S.audioLoading = el("span","aspin");
      b.append(S.audioLoading);
    }
  }
  // 标记本轮由本客户端发出,避免收到 "user" 事件时渲染重复气泡
  if(!text.startsWith("/")) S.localSent = true;
  // 非命令消息:立刻挂一个"💭 思考中…"气泡,不等服务器 start 事件,反馈更即时(像 TG 秒回 typing)
  if(!text.startsWith("/") && !S.stream && isCurrent()){
    ensureStream();
    // 有音频待转写:状态行标"音频转写中",让用户知道还在进行(短音频 1~2s,
    // 会议录音几分钟),回复流 start 事件到达后切回正常"思考中/工作中"
    if(auds.some(a=>!a.text)) S.stream.audioPending = true;
  }
  const payload={
    conv:sendConv, text,
    images:sendImages.map(x=>({data:x.data,media_type:x.media_type})),
    audios:sendAudios.map(x=>({id:x.id})),
    files:sendFiles.map(x=>({id:x.id})),
  };
  // 新会话:发出第一条后,把本地临时会话转正(用户已切走时跳过,S.conv 已经不是这条草稿了)
  const oldConv=sendConv;
  const wasLocal=String(sendConv).startsWith("local-");
  clearDraft(sendConv);   // 发送即清草稿:必须在转正前调用,否则真实 id 落地后 local- 旧 key 清不掉
  clearComposerAttachments(sendConv);
  if(wasLocal && isCurrent()){
    S.conv=S.conv.replace("local-",""); sendConv=S.conv; payload.conv=S.conv; renderProjSelChip();
    refreshGit(S.conv);
    // S.convs 里那条草稿行(conv=local-xxx)原地更新成新的真实 id,别留一条转正前的孤儿条目——
    // 否则"新对话"复用逻辑(newChatIn)会把它当成还没发消息的草稿误重新打开
    const entry=S.convs.find(x=>x.conv===oldConv);
    if(entry){
      entry.conv=S.conv; entry.turns=1;
      if(entry.wbTaskId && typeof linkWorkbenchTaskSession==="function"){ linkWorkbenchTaskSession(entry.wbTaskId, S.conv); delete entry.wbTaskId; }
    }
  }
  if(isCurrent()){ renderThumbs(); $("#ta").value=""; autoGrow(); }
  try{
    const r=await api("/send",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    if(!r.ok){
      let detail="";
      try{ const d=await r.json(); detail=d.error||""; }catch(e){}
      throw new Error(detail||("HTTP "+r.status));
    }
    if(wasLocal && isCurrent()) setTimeout(loadConvs, 400);
  }catch(err){
    if(S.audioLoading){ S.audioLoading.remove(); S.audioLoading=null; }  // 发送失败,停掉 loading
    // /send 失败时不能把附件和文字一起吞掉,否则用户只能重新选择文件,且误以为已发送。
    if(isCurrent()){
      S.images=sendImages; S.audios=sendAudios; S.files=sendFiles;
      saveComposerAttachments(); renderThumbs();
      $("#ta").value=text; autoGrow(); saveDraft();
      addBubble("ai","⚠️ 发送失败:"+err.message+"，附件已保留，可重试。" );
    }
  }
}
// 静默发命令(如 /model 切换):不渲染用户气泡,回执由服务端 message 事件带回
async function sendCmd(cmd, conv){
  conv = conv || S.conv;
  const isCurrent = conv===S.conv;
  const payload={conv:conv, text:cmd, images:[]};
  const oldConv=conv;
  const wasLocal=String(conv).startsWith("local-");
  if(wasLocal){
    conv=conv.replace("local-",""); payload.conv=conv;
    if(isCurrent){ S.conv=conv; renderProjSelChip(); }
    if(isCurrent) refreshGit(S.conv);
    const entry=S.convs.find(x=>x.conv===oldConv);
    if(entry){ entry.conv=conv; entry.turns=1; }
  }
  try{
    await api("/send",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    if(wasLocal) setTimeout(loadConvs, 400);
  }catch(err){ addBubble("ai","⚠️ 发送失败:"+err.message); }
}
// 输入框按钮恒为「发送」;停止改由"工作中"状态行右侧的按钮承担
function updateSendBtn(){
  const btn=$("#sendBtn"); if(!btn) return;
  btn.textContent="↑"; btn.classList.remove("stop-mode");
}
async function stopReply(){
  if(!S.stream) return;
  const conv=S.conv;
  // 乐观更新:任务会话先本地把任务行置「已停止」,不等后端收尾落库——
  // 收尾含最多 5 秒 CLI interrupt + 写库,SSE 终态要晚几秒才到,干等会让
  // 用户以为没停住、反复点。SSE 终态到达后差量渲染覆盖成同一结果,无跳变。
  const optimistic = markTaskStopped(conv);
  // 防重建(点两次的根因):取消指令发出前已在途的旧帧(取消瞬间 SDK 最后吐的
  // 文本/工具事件)会在 finalizeStream 之后才到达,而 applyStreamEvent 对
  // streamy 事件一律 ensureStream——不拦会把已收掉的气泡重新拉起来,让用户
  // 以为没停住、要点第二次。短窗内该会话的 streamy 事件且当前无流时一律丢弃,
  // 窗口过后自动解除;用户手动发的新消息必在窗口之后,不受影响。
  S.stopGuard = {conv, until: Date.now() + 2000};
  clearTimeout(S.stopGuardTimer);
  S.stopGuardTimer = setTimeout(()=>{ if(S.stopGuard) S.stopGuard = null; }, 2500);
  // 顺手清掉该会话的进行中缓冲:done 到达时本来也会清,提前清掉更稳——
  // 避免切走再切回时 maybeReplayStream 把已停止回合的旧帧重放成新的流式气泡
  // (侧栏「正在回复」橙点也随之熄灭,由 markLive 重绘)。
  delete S.live[conv]; delete S.streamSnap[conv]; markLive();
  finalizeStream(); // 立即结束前端流式气泡,保留已有部分文字
  try{
    const r = await api("/abort",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({conv})});
    if(r.ok){
      const j = await r.json();
      if(!j.stopped){
        if(optimistic) restoreTaskStopped(conv, optimistic);  // 停失败,恢复原状态
        // 停不住 = 流的源头(轮次/任务)其实已经结束,比如 SSE 缓冲重放的历史流。
        // 明说一句,免得用户以为按钮没反应、反复点。
        addMsg("ai", "已停止(当前没有正在运行的回复或任务)");
      }
    }
  }catch(e){ /* 网络异常忽略,前端已处理 */ }
  flushPending(conv); // 手动中止不会触发 done/message 事件,需主动补发排队中的下一条
}
// 乐观停止:任务会话(task:<id>)点停止,本地先把任务行置「已取消」给即时反馈。
// 返回原状态供停失败时恢复;非任务会话/任务已终态返回 null。
function markTaskStopped(conv){
  if(!conv.startsWith("task:")) return null;
  const t = barTasks.get(conv.slice(5));
  if(!t || (t.status !== "queued" && t.status !== "running")) return null;
  const prev = {status: t.status, progress_note: t.progress_note};
  t.status = "cancelled"; t.progress_note = "已停止";
  renderTaskBar();
  return prev;
}
function restoreTaskStopped(conv, prev){
  const t = barTasks.get(conv.slice(5));
  if(!t) return;
  t.status = prev.status; t.progress_note = prev.progress_note;
  renderTaskBar();
}
// ── 待发送队列 ────────────────────────────────────────────────────────────
function queuePending(text, images, audios, files){
  if(!text && !(images||[]).length && !(audios||[]).length && !(files||[]).length) return;
  const q = S.pending[S.conv] || (S.pending[S.conv]=[]);
  if(q.length>=3){ pendFull(); return false; }  // 满 3 条不再排队,抖一下提示
  q.push({id:++S.pendId, text, images:images||[], audios:audios||[], files:files||[]});
  drawPending();
  return true;
}
// 队列已满:让待发送区抖一下,给个"到上限了"的即时反馈
function pendFull(){
  const box=$("#pending"); if(!box) return;
  box.classList.remove("shake"); void box.offsetWidth; box.classList.add("shake");
}
function drawPending(){
  const box=$("#pending"); if(!box) return;
  box.innerHTML="";
  for(const it of (S.pending[S.conv]||[])){
    const row=el("div","pi");
    const clk=el("span","clock"); clk.textContent="⏳";
    const tx=el("span","ptext");
    tx.textContent=it.text||((it.audios||[]).length ? "(语音/音频)" : (it.files||[]).length ? "(附件)" : "(图片)");
    const del=el("button","pdel"); del.type="button"; del.title="删除"; del.textContent="×";
    del.onclick=()=>removePending(it.id);
    row.append(clk,tx,del); box.append(row);
  }
}
function removePending(id){
  const q=S.pending[S.conv]; if(!q) return;
  const i=q.findIndex(x=>x.id===id); if(i<0) return;
  q.splice(i,1); if(!q.length) delete S.pending[S.conv]; drawPending();
}
// 任务完成 → 发出该会话待发送队列的第一条(逐条发:发一条,等它 done 再发下一条)
function flushPending(conv){
  const q=S.pending[conv]; if(!q || !q.length) return;
  const item=q.shift(); if(!q.length) delete S.pending[conv];
  if(conv===S.conv){
    drawPending();
    S.images=(item.images||[]).slice(); S.audios=(item.audios||[]).slice();
    S.files=(item.files||[]).slice(); saveComposerAttachments(); renderThumbs();
    send(item.text, null, {forceSend:true});
    return;
  }
  // 后台会话:直接发,不渲染,靠 SSE 缓冲在切回时重建
  const payload={
    conv, text:item.text,
    images:(item.images||[]).map(x=>({data:x.data,media_type:x.media_type})),
    audios:(item.audios||[]).map(x=>({id:x.id})),
    files:(item.files||[]).map(x=>({id:x.id})),
  };
  api("/send",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)}).then(loadConvs).catch(()=>{});
}
$("#composer").onsubmit = e=>{ e.preventDefault(); send($("#ta").value); };
$("#ta").addEventListener("keydown", e=>{
  // 输入法组合中(中文/日文等候选未上屏)的回车只上屏,不发送
  if(e.isComposing || e.keyCode===229) return;
  if(S.cmd.open){
    if(e.key==="ArrowDown"){ e.preventDefault(); if(S.cmd.items.length){ S.cmd.active=(S.cmd.active+1)%S.cmd.items.length; renderCmdPop(); } return; }
    if(e.key==="ArrowUp"){ e.preventDefault(); if(S.cmd.items.length){ S.cmd.active=(S.cmd.active-1+S.cmd.items.length)%S.cmd.items.length; renderCmdPop(); } return; }
    if((e.key==="Enter"||e.key==="Tab") && S.cmd.items.length){ e.preventDefault(); pickCmd(S.cmd.active); return; }
    if(e.key==="Escape"){ e.preventDefault(); closeCmdMenu(); return; }
  }
  if(e.key==="Enter" && !e.shiftKey && !IS_MOBILE_DEVICE){ e.preventDefault(); send($("#ta").value); }
});
// "工作中"状态行右侧的停止按钮(状态行每秒重绘,用事件委托一次绑定)
document.addEventListener("click", e=>{ if(e.target.closest(".statusstop")){ e.preventDefault(); stopReply(); } });
document.addEventListener("click", e=>{ if(S.cmd.open && !e.target.closest("#cmdPop") && e.target!==$("#ta")) closeCmdMenu(); });
function autoGrow(){ const t=$("#ta"); t.style.height="auto"; t.style.height=Math.min(t.scrollHeight,160)+"px"; }
$("#ta").addEventListener("input", autoGrow);
$("#ta").addEventListener("input", checkCmdMenu);
// ── 会话级输入草稿(纯本地缓存,按会话 id 分 key)────────────────────────
// 每个会话的输入框内容独立存 localStorage;切会话时恢复目标会话的草稿,
// 发送/清空时删除,避免 A 会话打的字串到 B 会话。local- 前缀的未转正新会话
// 同样按自己 id 存,互不干扰;发送转正时旧 key 一并清掉。
function draftKey(conv){ return "vococo_draft:"+conv; }
function saveDraft(){
  try{
    if(!S.conv) return;
    const v=$("#ta").value;
    if(v) localStorage.setItem(draftKey(S.conv), v);
    else localStorage.removeItem(draftKey(S.conv));  // 清空了就删 key,不留空草稿
  }catch(e){}
}
function restoreDraft(conv){
  let v=""; try{ v=localStorage.getItem(draftKey(conv))||""; }catch(e){}
  $("#ta").value=v; autoGrow();
}
function clearDraft(conv){
  try{ localStorage.removeItem(draftKey(conv||S.conv)); }catch(e){}
}
$("#ta").addEventListener("input", saveDraft);
// 欢迎屏快捷入口:点击直接发送
document.querySelectorAll("#empty .sug[data-s]").forEach(b=>{ b.onclick=()=>send(b.dataset.s); });

// ── 图片 ────────────────────────────────────────────────────────────────
// 读一个图片文件 → 必要时前端先压缩 → base64 并入待发送队列(选择/粘贴复用)。
// 压缩策略:长边超 1568px(Claude 视觉输入的有效上限)或体积超 300KB 时,
// 缩到 1568px 内并重编码 JPEG(q=0.85);GIF 保动画不压;压完更大则退回原图。
const IMG_MAX_EDGE=1568, IMG_KEEP_BYTES=300*1024;
function addImageFile(f){
  if(!f || !(f.type||"").startsWith("image/")) return;
  compressImage(f).then(im=>{ S.images.push(im); renderThumbs(); });
}
async function compressImage(f){
  const raw=await new Promise((res,rej)=>{ const rd=new FileReader(); rd.onload=()=>res(rd.result); rd.onerror=rej; rd.readAsDataURL(f); });
  const orig={data:raw.split(",")[1], media_type:f.type||"image/png", url:raw};
  if(f.type==="image/gif") return orig;
  if(f.size<=IMG_KEEP_BYTES){
    // 小文件只在尺寸也不超时才免压(有些截图体积小但像素巨大)
    try{ const im=await loadImgEl(raw); if(Math.max(im.naturalWidth,im.naturalHeight)<=IMG_MAX_EDGE) return orig; }catch(e){ return orig; }
  }
  try{
    const im=await loadImgEl(raw);
    const long=Math.max(im.naturalWidth,im.naturalHeight)||1;
    const scale=Math.min(1, IMG_MAX_EDGE/long);
    const w=Math.max(1,Math.round(im.naturalWidth*scale)), h=Math.max(1,Math.round(im.naturalHeight*scale));
    const cv=document.createElement("canvas"); cv.width=w; cv.height=h;
    const ctx=cv.getContext("2d");
    ctx.fillStyle="#fff"; ctx.fillRect(0,0,w,h);   // JPEG 无透明通道,垫白底
    ctx.drawImage(im,0,0,w,h);
    const out=cv.toDataURL("image/jpeg",0.85);
    if(out.length>=raw.length) return orig;
    return {data:out.split(",")[1], media_type:"image/jpeg", url:out};
  }catch(e){ return orig; }   // 解码失败(如 heic 某些浏览器不认)兜底发原图
}
function loadImgEl(src){ return new Promise((res,rej)=>{ const im=new Image(); im.onload=()=>res(im); im.onerror=rej; im.src=src; }); }
$("#imgBtn").onclick = ()=>$("#file").click();
// 通用附件只放行模型链路已覆盖的常见文本/文档格式;不支持的格式在上传前明确提示。
const SUPPORTED_FILE_EXTENSIONS = new Set([
  "md","markdown","txt","log","csv","tsv","json","yaml","yml","xml",
  "html","htm","css","js","jsx","ts","tsx","py","sh","bash","zsh","sql","ini","toml",
  "pdf","doc","docx","odt","rtf","xls","xlsx","numbers","ppt","pptx","key","pages",
]);
const SUPPORTED_FILE_TYPES = new Set([
  "application/pdf","application/json","application/xml","text/csv","text/markdown",
]);
function fileExtension(name){
  const value=String(name||"").toLowerCase();
  const dot=value.lastIndexOf(".");
  return dot>0 && dot<value.length-1 ? value.slice(dot+1) : "";
}
function isSupportedFile(f){
  const type=String(f?.type||"").split(";",1)[0].toLowerCase();
  return type.startsWith("text/") || SUPPORTED_FILE_TYPES.has(type) || SUPPORTED_FILE_EXTENSIONS.has(fileExtension(f?.name));
}
function rejectUnsupportedFile(f){
  const name=f?.name||"未命名文件";
  const ext=fileExtension(name);
  addBubble("ai", `⚠️ 暂不支持文件「${name}」${ext ? `（.${ext}）` : ""}，请上传图片、音频或常见文本/文档格式。`);
}
function handleAttachmentFiles(files){
  for(const f of Array.from(files||[])){
    const type=String(f.type||"").toLowerCase();
    if(type.startsWith("image/")) addImageFile(f);
    else if(type.startsWith("audio/")) addAudioFile(f);
    else if(isSupportedFile(f)) addFile(f);
    else rejectUnsupportedFile(f);
  }
}
$("#file").onchange = e=>{
  handleAttachmentFiles(e.target.files);
  e.target.value="";
};
// 拖到输入框时复用文件选择流程,阻止浏览器把文件直接打开/导航离开当前页面。
const dropTarget=document.querySelector(".inrow");
if(dropTarget){
  ["dragenter","dragover"].forEach(type=>dropTarget.addEventListener(type,e=>{
    e.preventDefault(); e.stopPropagation(); dropTarget.classList.add("dragover");
  }));
  dropTarget.addEventListener("dragleave",e=>{
    if(!e.relatedTarget || !dropTarget.contains(e.relatedTarget)) dropTarget.classList.remove("dragover");
  });
  dropTarget.addEventListener("drop",e=>{
    e.preventDefault(); e.stopPropagation(); dropTarget.classList.remove("dragover");
    handleAttachmentFiles(e.dataTransfer?.files);
  });
}
// 粘贴:聊天框内 Ctrl/⌘+V 粘贴剪贴板里的图片(可多张),并入队列;纯文本粘贴不受影响
$("#ta").addEventListener("paste", e=>{
  const items = e.clipboardData && e.clipboardData.items; if(!items) return;
  let got=false;
  for(const it of items){
    if(it.kind==="file" && (it.type||"").startsWith("image/")){ const f=it.getAsFile(); if(f){ addImageFile(f); got=true; } }
  }
  if(got) e.preventDefault();   // 有图才拦截,让纯文本粘贴走默认行为
});
function renderThumbs(){
  const box=$("#thumbs"); box.innerHTML="";
  S.images.forEach((im,idx)=>{ const w=el("div","t"); const g=el("img"); g.src=im.url; const x=el("button","x"); x.textContent="×"; x.onclick=()=>{S.images.splice(idx,1);renderThumbs();}; w.append(g,x); box.append(w); });
  S.audios.forEach((au,idx)=>{
    const w=el("div","t audiochip "+(au.status||"done"));
    const label=el("span","alabel");
    label.textContent = au.status==="uploading" ? `🎵 ${au.filename} · 上传中…`
      : au.status==="error" ? `🎵 ${au.filename} · 上传失败`
      : au.text ? `🎵 ${au.filename}` : `🎵 ${au.filename} · 已上传`;
    if(au.status==="error" && au.error) label.title=au.error;
    const x=el("button","x"); x.textContent="×";
    x.onclick=()=>{ S.audios.splice(idx,1); renderThumbs(); };
    w.append(label,x); box.append(w);
  });
  S.files.forEach((file,idx)=>{
    const w=el("div","t filechip "+(file.status||"done"));
    const label=el("span","flabel");
    label.textContent = file.status==="uploading" ? `📎 ${file.filename} · 上传中…`
      : file.status==="error" ? `📎 ${file.filename} · 上传失败`
      : `📎 ${file.filename}`;
    if(file.status==="error" && file.error) label.title=file.error;
    const x=el("button","x"); x.textContent="×";
    x.onclick=()=>{ S.files.splice(idx,1); renderThumbs(); };
    w.append(label,x); box.append(w);
  });
}

// ── 音频 ────────────────────────────────────────────────────────────────
// 选完音频立刻上传(只存文件秒回,不等转写):AI"解读"音频靠的是转写文字,不是
// 原生多模态——协议层没有 audio 这种 content block(见后端 core/agent.py 的
// 说明),转写在发送后由服务端做(send() 里会冒"转写中"占位气泡),失败文本
// 由服务端拼进消息,模型回复里自然说明,不会卡住发送。
const AUDIO_MAX_BYTES = 100*1024*1024;
function addAudioFile(f){
  if(!f || !(f.type||"").startsWith("audio/")) return;
  if(f.size > AUDIO_MAX_BYTES){ addBubble("ai", `⚠️ 音频"${f.name}"超过 100MB 上限,没有上传。`); return; }
  const item = {id:null, filename:f.name||"audio", text:"", mediaType:f.type||"audio/mpeg", url:URL.createObjectURL(f), status:"uploading"};
  S.audios.push(item); renderThumbs();
  uploadAudio(f, item);
}
function uploadAudio(f, item){
  // 暴露 item.done:send() 发送前会 await 它,保证"文件已到服务器"才发出消息,
  // 不会出现音频还在上传(id 为 null)就被发送、后端静默跳过导致音频丢失的情况
  item.done = (async()=>{
    try{
      const form = new FormData(); form.append("audio", f, item.filename);
      const ac = new AbortController();
      const timer = setTimeout(()=>ac.abort(), 90000);  // 上传卡住 90s 报超时,别无限挂
      const r = await api("/upload_audio", {method:"POST", body:form, signal:ac.signal});
      clearTimeout(timer);
      const d = await r.json();
      if(!r.ok || d.error) throw new Error(d.error || ("HTTP "+r.status));
      item.id=d.id; item.text=d.text; item.status="done";
    }catch(e){
      item.status="error"; item.error = e.name==="AbortError" ? "上传超时,请重试" : e.message;
    }
    renderThumbs();
  })();
}

// ── 通用文件 ──────────────────────────────────────────────────────────────
const FILE_MAX_BYTES = 32*1024*1024;
function addFile(f){
  if(!f) return;
  if(f.size > FILE_MAX_BYTES){ addBubble("ai", `⚠️ 文件"${f.name}"超过 32MB 上限，没有上传。`); return; }
  const item={id:null,filename:f.name||"附件",mediaType:f.type||"application/octet-stream",status:"uploading"};
  S.files.push(item); renderThumbs();
  uploadFile(f,item);
}
function uploadFile(f,item){
  item.done=(async()=>{
    try{
      const form=new FormData(); form.append("file",f,item.filename);
      const ac=new AbortController();
      const timer=setTimeout(()=>ac.abort(),90000);
      const r=await api("/upload_file",{method:"POST",body:form,signal:ac.signal});
      clearTimeout(timer);
      const d=await r.json();
      if(!r.ok || d.error) throw new Error(d.error || ("HTTP "+r.status));
      item.id=d.id; item.status="done";
    }catch(e){
      item.status="error"; item.error=e.name==="AbortError" ? "上传超时，请重试" : e.message;
    }
    renderThumbs();
  })();
}

// ── 图片大图查看器:点聊天里的图放大,←/→ 或按钮切换,Esc/✕/点背景关闭 ─────
const IV={list:[], idx:0};
// 历史图的 blob: URL 在 loadAuthedImg 显示后已被 revoke,不能直接复制 src;
// 从仍持有解码数据的 <img> 经 canvas 重新导出,并缓存在元素上避免重复导出。
function ivSrc(im){
  if(!im.src.startsWith("blob:")) return im.src;
  if(im._ivurl) return im._ivurl;
  try{
    const cv=document.createElement("canvas"); cv.width=im.naturalWidth; cv.height=im.naturalHeight;
    cv.getContext("2d").drawImage(im,0,0);
    im._ivurl=cv.toDataURL("image/png"); return im._ivurl;
  }catch(e){ return im.src; }
}
function openImgViewer(img, list){
  // 聊天气泡里的图默认是缩略图(懒加载,可能还没触发),按 dataset.full 认«有效图片»,
  // 不能只按 naturalWidth 判断,否则还没滚到可视区的图会被漏掉,←/→ 切换时跳过。
  // list 可选:传了就用这份(比如工作台备注区的图片),不传按聊天气泡的默认取法。
  IV.list=(list || Array.from(document.querySelectorAll("#wrap .imgs img"))).filter(g=>g.src||g.dataset.full);
  if(!IV.list.length) return;
  IV.idx=Math.max(0, IV.list.indexOf(img));
  renderImgViewer();
  $("#imgViewer").hidden=false;
}
function renderImgViewer(){
  const n=IV.list.length;
  IV.idx=(IV.idx+n)%n;
  const im=IV.list[IV.idx];
  // 先用当前已有的(缩略图或空)占位秒开,原图在 loadFullImg 里异步拉回来再换上;
  // 原图若已经缓存过(fetchCachedBlobUrl 命中),这里能同步拿到,直接显示不用等
  const ent = im.dataset.full && _imgBlobCache.get(im.dataset.full);
  const cached = ent && ent.o;   // 缓存项是 {o,size},取 blob URL(见 stream.js 的字节闸)
  $("#ivImg").src = cached || (im.naturalWidth ? ivSrc(im) : "");
  $("#ivCount").textContent=(IV.idx+1)+" / "+n;
  $("#ivPrev").style.display=$("#ivNext").style.display=$("#ivCount").style.display = n>1?"":"none";
  if(!cached) loadFullImg(im);
}
// 查看器里点开的是聊天气泡缩略图,这里单独拉一次不带 thumb 的原图换上去;换的是
// #ivImg,不动聊天气泡本身的 <img src>(那份省流量的缩略图不受影响)。走
// fetchCachedBlobUrl(stream.js)的全局缓存,而不是缓存在 img 元素上——切会话会
// 把 DOM 全部重建,元素级缓存跟着失效,同一张图切回来还得重拉一次原图。
async function loadFullImg(im){
  if(!im.dataset.full) return;  // 实时发送的 blob:/data: 图本来就是原图,ivSrc 已经够用
  const o = await fetchCachedBlobUrl(im.dataset.full);
  if(o && IV.list[IV.idx]===im) $("#ivImg").src=o;  // 拉的过程中用户可能已经切走了,别覆盖
}
function closeImgViewer(){ $("#imgViewer").hidden=true; $("#ivImg").removeAttribute("src"); IV.list=[]; }
$("#ivClose").onclick=closeImgViewer;
$("#ivPrev").onclick=e=>{ e.stopPropagation(); IV.idx--; renderImgViewer(); };
$("#ivNext").onclick=e=>{ e.stopPropagation(); IV.idx++; renderImgViewer(); };
$("#imgViewer").onclick=e=>{ if(e.target.id==="imgViewer") closeImgViewer(); };  // 点图片本身不关
document.addEventListener("keydown", e=>{
  if($("#imgViewer").hidden) return;
  if(e.key==="Escape") closeImgViewer();
  else if(e.key==="ArrowLeft"){ IV.idx--; renderImgViewer(); }
  else if(e.key==="ArrowRight"){ IV.idx++; renderImgViewer(); }
});

// ── 语音输入(录音 → 后端 SenseVoice 转写 → 填进输入框)────────────────────
// Claude 不吃音频,故先在后端转成文字。点一下开录(实时声波动效)、再点结束,转好的文字并入输入框可编辑再发。
const REC = { state:"idle", mr:null, chunks:[], stream:null, mime:"", t0:0, timer:null, cancel:false,
  actx:null, analyser:null, dataArr:null, raf:null, levels:[], waveColor:"#ef4444", _lastPush:0 };
function setRecUI(){
  // 只在"录音中"显示录音条;点发送后立即收起录音条、恢复输入框,转写状态改由聊天区占位气泡承载
  const rec=REC.state==="recording";
  const row=$("#recRow"), inrow=document.querySelector(".inrow");
  row.classList.toggle("show", rec);
  if(inrow) inrow.style.display = rec ? "none" : "";
}
function recTick(){ const s=Math.max(0,Math.floor((Date.now()-REC.t0)/1000)); $("#recTime").textContent=(s/60|0)+":"+String(s%60).padStart(2,"0"); }
// 滚动声波:采样(取音量)节流到约每 75ms 一次,但渲染每帧都画——用采样间隔内的时间差
// 算出平滑偏移量,让柱子连续向左滑而不是每 75ms 才跳一格(避免低帧率卡顿感)
function drawWave(){
  if(REC.state!=="recording" || !REC.analyser){ return; }
  REC.raf=requestAnimationFrame(drawWave);
  const now=Date.now();
  const PUSH_MS=75;
  if(now - REC._lastPush >= PUSH_MS){   // 节流:只控制"多久取一次样",不影响绘制帧率
    REC._lastPush=now;
    REC.analyser.getByteTimeDomainData(REC.dataArr);
    let sum=0; for(let i=0;i<REC.dataArr.length;i++){ const x=(REC.dataArr[i]-128)/128; sum+=x*x; }
    REC.levels.push(Math.min(1, Math.sqrt(sum/REC.dataArr.length)*3.4));
  }
  const cv=$("#recWave"); if(!cv) return;
  const dpr=window.devicePixelRatio||1, W=cv.clientWidth||1, H=cv.clientHeight||1;
  if(cv.width!==Math.round(W*dpr)||cv.height!==Math.round(H*dpr)){ cv.width=Math.round(W*dpr); cv.height=Math.round(H*dpr); }
  const ctx=cv.getContext("2d"); ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,W,H);
  const barW=3, step=5, maxBars=Math.floor(W/step)+2;
  if(REC.levels.length>maxBars) REC.levels.splice(0, REC.levels.length-maxBars);
  const offset=step*Math.min(1, (now-REC._lastPush)/PUSH_MS);   // 采样间隔内的连续滚动偏移
  ctx.fillStyle=REC.waveColor;
  const n=REC.levels.length;
  for(let i=0;i<n;i++){
    const h=Math.max(3, REC.levels[i]*(H-4)), x=W-(n-i)*step-offset, y=(H-h)/2;
    if(x+barW<0) continue;
    if(ctx.roundRect){ ctx.beginPath(); ctx.roundRect(x,y,barW,h,barW/2); ctx.fill(); }
    else ctx.fillRect(x,y,barW,h);
  }
}
function cleanupAudio(){
  if(REC.raf){ cancelAnimationFrame(REC.raf); REC.raf=null; }
  try{ if(REC.actx && REC.actx.state!=="closed") REC.actx.close(); }catch(e){}
  REC.actx=null; REC.analyser=null; REC.levels=[];
  const cv=$("#recWave"); if(cv){ const c=cv.getContext("2d"); if(c) c.clearRect(0,0,cv.width,cv.height); }
}
// 每次录音结束即释放麦克风流(见 onRecStop),这里负责按需重新获取;权限按站点记住,不会反复弹框
async function getMic(){
  if(REC.stream && REC.stream.getAudioTracks().some(t=>t.readyState==="live")) return REC.stream;
  REC.stream = await navigator.mediaDevices.getUserMedia({audio:true});
  return REC.stream;
}
function releaseMic(){ if(REC.stream){ REC.stream.getTracks().forEach(t=>t.stop()); REC.stream=null; } }
async function startRec(){
  if(REC.state!=="idle") return;
  if(!navigator.mediaDevices || !window.MediaRecorder){ alert("此浏览器不支持录音"); return; }
  try{ await getMic(); }
  catch(e){ alert("麦克风权限被拒绝或不可用"); return; }
  // 限码率:识别不需要高音质,压低码率能大幅缩短手机上传耗时(实测上传经常比转写本身还慢)
  REC.chunks=[]; REC.cancel=false; REC._lastPush=0; REC.mr=new MediaRecorder(REC.stream,{audioBitsPerSecond:24000});
  REC.mime = REC.mr.mimeType || "audio/webm";
  REC.mr.ondataavailable=e=>{ if(e.data && e.data.size) REC.chunks.push(e.data); };
  REC.mr.onstop=onRecStop;
  try{   // 实时声波(失败不影响录音本身)
    const AC=window.AudioContext||window.webkitAudioContext;
    REC.actx=new AC(); if(REC.actx.state==="suspended") await REC.actx.resume();
    REC.analyser=REC.actx.createAnalyser(); REC.analyser.fftSize=512;
    REC.actx.createMediaStreamSource(REC.stream).connect(REC.analyser);
    REC.dataArr=new Uint8Array(REC.analyser.fftSize); REC.levels=[];
    REC.waveColor=(getComputedStyle(document.documentElement).getPropertyValue("--err").trim())||"#ef4444";
  }catch(e){ REC.analyser=null; }
  REC.mr.start(); REC.state="recording"; REC.t0=Date.now(); recTick();
  REC.timer=setInterval(recTick,500); setRecUI();
  if(REC.analyser) drawWave();
}
function stopRec(cancel){
  if(REC.state!=="recording") return;
  REC.cancel=!!cancel; clearInterval(REC.timer); REC.timer=null;
  try{ REC.mr.stop(); }catch(e){}
}
// 转写中占位气泡:按"录音发起时所属会话"存进 S.voiceRec,切走再切回能补出来(否则像是丢了)
function renderVoicePending(conv){
  const st=S.voiceRec[conv]; if(!st) return;
  const b=addBubble("me","");
  b.classList.add("voicebub");
  b.innerHTML='<span class="vwait"><span class="sp"></span>转写中…</span>';
  st.bubble=b;
}
async function onRecStop(){
  cleanupAudio();   // 停声波分析
  releaseMic();     // 立刻停掉麦克风流,灭掉 iOS 状态栏录音指示灯(权限已按站点记住,下次不再弹框)
  const blob=new Blob(REC.chunks,{type:REC.mime}); const mime=REC.mime;
  REC.mr=null;
  if(REC.cancel || blob.size<1200){ REC.state="idle"; setRecUI(); return; }
  // 记住录音发起时所在的会话:转写这段时间用户可能已切到别的会话,完成后要发到这个会话而非当前会话
  const conv=S.conv;
  const ext = mime.includes("mp4")||mime.includes("mpeg")?"mp4":mime.includes("ogg")?"ogg":mime.includes("wav")?"wav":"webm";
  // 点发送:立即收起录音条,在聊天区冒一个"我"的占位气泡(转写中…),不等结果
  REC.state="transcribing"; setRecUI();
  S.voiceRec[conv]={};
  renderVoicePending(conv);
  await sendVoice(blob, ext, conv);
  REC.state="idle"; setRecUI();
}
// 把一份录音 blob 发去转写;单独抽出来是因为失败后"重试"要能重发同一份 blob,
// 不用再走一遍 startRec() 让用户重新开口(blob 本身没问题时,失败大多是网络/服务抖动)
async function sendVoice(blob, ext, conv){
  try{
    const fd=new FormData(); fd.append("audio", blob, "voice."+ext);
    const r=await api("/transcribe",{method:"POST",body:fd});
    const d=await r.json().catch(()=>({}));
    const st=S.voiceRec[conv]; delete S.voiceRec[conv];
    const here=S.conv===conv;   // 转写完成时是否还停留在发起录音的那个会话
    if(r.ok && d.text){
      if(here && st && st.bubble){
        // 还在原会话:文字回填占位气泡,再复用它自动发给 Claude(不重复冒泡)
        st.bubble.classList.remove("voicebub"); st.bubble.textContent=d.text;
        await send(d.text, null, {reuseBubble:true, forceSend:true});
      }else{
        // 已切到别的会话:占位气泡早被清空,直接发到后台会话,靠历史/SSE 缓冲在切回时补出来;
        // 若该会话还是未转正的本地新会话(local-xxx),后端不认识这个前缀,得先去掉
        const realConv = String(conv).replace(/^local-/,"");
        const payload={conv:realConv, text:d.text, images:[]};
        await api("/send",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
        if(realConv!==conv){ const c=S.convs.find(x=>x.conv===conv); if(c) c.conv=realConv; }
        loadConvs();
      }
    } else if(here && st && st.bubble){ voiceFail(st.bubble, d.error, blob, ext, conv); }
  }catch(e){
    const st=S.voiceRec[conv]; delete S.voiceRec[conv];
    if(S.conv===conv && st && st.bubble) voiceFail(st.bubble, "转写失败:"+e.message, blob, ext, conv);
  }
}
// 转写没识别到内容/失败:占位气泡改成提示+按钮,绝不把空内容误发给 Claude。
// 保留这次录到的 blob——"重试"直接重发同一份音频(网络/服务抖动场景不用再开口);
// "重录"才是真正重新说一遍(比如确实啥也没录到)
function voiceFail(b, msg, blob, ext, conv){
  b.classList.remove("voicebub"); b.innerHTML="";
  const t=el("span","vfailtxt"); t.textContent="🎤 "+(msg||"没识别到内容,再说一次");
  const retryBtn=el("button"); retryBtn.type="button"; retryBtn.className="vretry"; retryBtn.textContent="重试";
  retryBtn.onclick=()=>{
    b.classList.add("voicebub"); b.innerHTML='<span class="vwait"><span class="sp"></span>转写中…</span>';
    S.voiceRec[conv]={bubble:b};
    sendVoice(blob, ext, conv);
  };
  const rb=el("button"); rb.type="button"; rb.className="vretry"; rb.textContent="重录";
  rb.onclick=()=>{ const row=b.closest(".row"); if(row) row.remove(); startRec(); };
  b.append(t, retryBtn, rb);
}
$("#micBtn").onclick = ()=>{ if(REC.state==="idle") startRec(); };          // 麦克风:开录
$("#recSend").onclick = e=>{ e.stopPropagation(); if(REC.state==="recording") stopRec(false); }; // 右侧 ↑:完成并发送
$("#recCancel").onclick = e=>{ e.stopPropagation(); stopRec(true); };        // 左侧 ✕:取消
// 兜底:关页/切后台也释放一次,防止极端情况下(录音中直接切走)残留占用
window.addEventListener("pagehide", releaseMic);
