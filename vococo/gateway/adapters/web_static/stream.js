"use strict";
// 2026-08-14 从 index.html 拆出(前端模块化):消息气泡渲染 + SSE 流式接收(回合事件→气泡/工具卡片)。
// 与内联脚本同属全局作用域(无构建步骤),加载顺序见 index.html。

// ── 消息气泡 ─────────────────────────────────────────────────────────────
// 图片组渲染,气泡(buildBubble/finalizeStream/mid_turn 消息/历史 AI 发图)统一走这个,
// 别各处各写一份 —— 已经因为漏了一处(buildTurnBlock 没传 imgs)导致 AI 发的图刷新后
// 消失过一次。
// 历史图默认只拉缩略图(?thumb=1,见 web.py _handle_image),且要等图片真正进入
// 可视区域才发请求——一个会话历史一次拉 40 轮,图多的话不懒加载会把所有图片的
// 鉴权请求一次性全砸出去,详情页卡半天。点大图查看原图走 openImgViewer/loadFullImg
// (composer.js),不在这里处理。
//
// 切会话时 #wrap 整个重建,同一张图的 <img> 元素会被扔掉重新创建——单靠浏览器
// HTTP 缓存只能省下"重新下载字节"这一步,fetch()+blob()+createObjectURL 这套异步
// 链路每次切回来都要重走一遍,肉眼仍是"又转一下才出图",跟没缓存一样。这里按完整
// 请求 URL 缓存 blob URL,同一张图本次标签页只走一次这套流程,之后同步复用。
// 有上限防止长会话/多会话累积内存无限涨,超了按最旧淘汰并 revoke。两道闸一起看:
// 条数闸挡"很多小图",字节闸挡"少数几个大文件"——语音消息也走这里(loadAuthedAudio),
// 单条音频动辄几 MB,只数条数的话 200 条能顶到几百 MB。
const _imgBlobCache = new Map();   // url → {o:blobUrl, size:字节}
const _IMG_BLOB_CACHE_MAX = 200;
const _IMG_BLOB_BYTES_MAX = 64*1024*1024;
let _imgBlobBytes = 0;
function _blobCacheEvict(){
  // size>1:永远保留最后插入的那条,否则单个超大文件会把自己也淘汰掉,调用方拿到已 revoke 的 URL
  while(_imgBlobCache.size>1 && (_imgBlobCache.size>_IMG_BLOB_CACHE_MAX || _imgBlobBytes>_IMG_BLOB_BYTES_MAX)){
    const oldest=_imgBlobCache.keys().next().value;
    const ent=_imgBlobCache.get(oldest);
    URL.revokeObjectURL(ent.o); _imgBlobBytes-=ent.size;
    _imgBlobCache.delete(oldest);
  }
}
// 重连/切前后台时机常伴随网络本就不稳,鉴权图片请求偶发失败以前直接放弃且不重试,
// 图就永久空白只能靠手动刷新页面才能恢复——这里加 3 次退避重试兜住那次抖动。
async function fetchCachedBlobUrl(url){
  if(_imgBlobCache.has(url)) return _imgBlobCache.get(url).o;
  const MAX_RETRY=3;
  for(let i=0;i<MAX_RETRY;i++){
    try{
      const r=await api(url);
      if(!r.ok) throw new Error("http "+r.status);
      const b=await r.blob(); const o=URL.createObjectURL(b);
      _imgBlobCache.set(url, {o, size:b.size||0});
      _imgBlobBytes += b.size||0;
      _blobCacheEvict();
      return o;
    }catch(e){
      if(i===MAX_RETRY-1) return null;
      await new Promise(res=>setTimeout(res, 500*(i+1)));
    }
  }
  return null;
}
const _imgLazyObserver = new IntersectionObserver((entries)=>{
  for(const e of entries){
    if(!e.isIntersecting) continue;
    const im=e.target; _imgLazyObserver.unobserve(im);
    loadAuthedImg(im, im.dataset.full, true);
  }
}, {rootMargin:"300px"});
function appendImgs(container, imgs){
  if(!imgs || !imgs.length) return;
  const g=el("div","imgs");
  for(const u of imgs){ const im=el("img");
    // 历史图走 /image?name=(需鉴权头,浏览器 <img> 不带) → 懒加载缩略图;
    // 实时发的是 data:/blob: URL,已是最终分辨率,直接塞 src。
    if(typeof u==="string" && u.startsWith("/")){
      im.dataset.full=u;
      _imgLazyObserver.observe(im);
    } else {
      im.src=u;
    }
    im.onclick=()=>openImgViewer(im);
    g.append(im);
  }
  container.append(g);
}
// 音频组渲染:每条一个 <audio> 播放器 + 可展开的转写文字("AI 解读"靠的就是这段
// 文字,展开出来是让用户能核对转写准不准,不是可选的装饰)。历史音频走 /audio?name=
// (需鉴权头,跟历史图片同理);实时发的是 blob: URL,直接塞 src。
function appendAuds(container, auds){
  if(!auds || !auds.length) return;
  const g=el("div","auds");
  for(const a of auds){
    const row=el("div","audiorow");
    const player=el("audio"); player.controls=true; player.preload="none";
    if(typeof a.url==="string" && a.url.startsWith("/")) loadAuthedAudio(player,a.url); else player.src=a.url;
    row.append(player);
    if(a.text){
      const det=el("details","atranscript");
      const sum=el("summary"); sum.textContent="转写文字"; det.append(sum);
      const p=el("div"); p.textContent=a.text; det.append(p);
      row.append(det);
    }
    g.append(row);
  }
  container.append(g);
}
// 走 fetchCachedBlobUrl 而不是自己 createObjectURL:后者建出来的 blob URL 从来没人
// revoke,而切会话时 #wrap 整个重建、同一条语音每切回来一次就再泄一份(音频动辄几 MB),
// 是长时间不刷新后内存暴涨的一个源头。复用图片那套缓存顺带拿到 LRU 上限 + revoke。
async function loadAuthedAudio(player, url){
  const o=await fetchCachedBlobUrl(url);
  if(o) player.src=o;
}
function buildBubble(who, text, imgs, auds){
  const row=el("div","row "+(who==="me"?"me":"ai"));
  const b=el("div","bubble");
  if(who==="me"){ b.textContent=text; } else { b.innerHTML = text?mdToHtml(text):""; }
  row.append(b);
  appendImgs(b, imgs);
  appendAuds(b, auds);
  return {row, b};
}
function addBubble(who, text, imgs, auds){
  const {row,b}=buildBubble(who, text, imgs, auds);
  $("#wrap").append(row); updateEmpty(); scrollDown(true);
  return b;
}
// AI 消息底部一行:回复时间(灰色小字)+ 复制 + 重新生成。turnId 为空(如命令回复/
// cron 推送这类不走 Sink.done 的消息)时不挂重新生成按钮——没有 turn id 无法安全定位
// 该删哪一行。isLast 由调用方按"是否本会话最新一轮"传入,只有它为真时才挂该按钮。
// interrupted=true:这条是被服务重启打断的回复,按钮换成醒目的「继续生成」(同一套
// /turn/regenerate 重发链路,视觉上给用户一个明确的恢复入口,而不是让他重打一遍)。
function buildTurnFoot(conv, turnId, text, ts, isLast, interrupted){
  const foot=el("div","turnfoot");
  const tm=el("span","tftime"); tm.textContent=ts?fmtTime(ts):"";
  foot.append(tm);
  const cp=el("button","tfbtn"); cp.type="button"; cp.title="复制"; cp.setAttribute("aria-label","复制");
  cp.innerHTML=ic("copy");
  cp.onclick=()=>copyMsgText(cp, text||"");
  foot.append(cp);
  if(isLast && turnId!=null){
    const rg=el("button","tfbtn tfregen"+(interrupted?" tfrgen-resume":""));
    rg.type="button";
    rg.title=interrupted?"服务重启中断了这条回复,点击继续生成":"重新生成";
    rg.setAttribute("aria-label",rg.title);
    rg.innerHTML=interrupted?"🔄 继续生成":ic("refresh");
    rg.onclick=()=>regenerateTurn(conv, turnId, rg);   // 手动点击要报错提示,不传 auto
    foot.append(rg);
  }
  return foot;
}
function copyMsgText(btn, text){
  if(!text || !navigator.clipboard) return;
  navigator.clipboard.writeText(text).then(()=>{
    const old=btn.innerHTML; btn.innerHTML=ic("save");
    setTimeout(()=>{ btn.innerHTML=old; }, 1200);
  }).catch(()=>{});
}
// 重新生成:删掉服务端最后一轮(仅允许对着最新一轮操作,见 delete_last_turn),
// 保留原有的用户气泡不动,原地起一个新的流式回复气泡再把同一句话发一遍——
// 视觉上等价于"AI 的这个回答被替换了",而不是在下面又新开一轮对话。
// auto=true 是重启后的自动恢复(见 maybeAutoResume):btn 可以为 null(无按钮可点),
// 失败静默 —— 双标签页/用户已发新消息等竞态下 409 不弹提示,气泡还在用户可手动点。
async function regenerateTurn(conv, turnId, btn, auto){
  if(btn && btn.disabled) return;
  if(btn) btn.disabled=true;
  let data=null;
  try{
    const r=await api("/turn/regenerate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({conv,id:turnId})});
    data=await r.json().catch(()=>null);
  }catch(e){
    if(auto) return;
    if(btn) btn.disabled=false;
    addBubble("ai","⚠️ 重新生成失败:"+e.message);
    return;
  }
  if(!data || !data.ok){
    if(auto) return;
    if(btn) btn.disabled=false;
    addBubble("ai","⚠️ "+((data&&data.error)||"重新生成失败,可能不是最新一条回复"));
    return;
  }
  delete S.histCache[conv]; idbDel("hist:"+conv);
  if(S.conv===conv){
    if(btn){
      const row=btn.closest(".row"); if(row) row.remove();
    }else{
      // 自动恢复(无按钮可点):按 turn id 定位消息块,删掉 AI 回复气泡(用户气泡原样保留)
      const blk=document.querySelector('#wrap .turn-block[data-tid="'+turnId+'"]');
      if(blk){ const rows=blk.querySelectorAll(".row"); const r=rows[rows.length-1]; if(r) r.remove(); }
    }
    S.localSent=true;   // 用户气泡原样保留,避免下面 /send 回显的 "user" 事件又重复冒一条
    ensureStream();
  }
  try{
    await api("/send",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({conv, text:data.text, images:[], audios:[], files:[]})});
  }catch(e){ if(!auto) addBubble("ai","⚠️ 发送失败:"+e.message); }
}
// 重启中断的回复 → 自动继续生成:该会话最新一条带 interrupted 标记(服务端
// recover_interrupted_turns 收尾时写入)就自动重发同一句话,实现接近语音通话的
// "无感恢复"。S.autoResumed 每会话只触发一次;用户已在这之后发了新消息(该轮不再
// 是最新)→ /turn/regenerate 409 → 静默放弃,气泡保留,用户可手动点「继续生成」。
function maybeAutoResume(conv, turns){
  if(!turns || !turns.length || !conv || S.autoResumed[conv]) return;
  const last=turns[turns.length-1];
  if(!last || last.pending || !(last.events||[]).some(x=>x.type==="interrupted")) return;
  S.autoResumed[conv]=true;
  setTimeout(()=>regenerateTurn(conv, last.id, null, true), 300);  // 让气泡先渲染出来再恢复
}
// 带鉴权头拉图并显示;token 只在头里传,不进 URL(不泄露到日志/历史/Referer)。
// thumb=true 时追加 &thumb=1 拉缩略图(聊天气泡默认用这个);查看原图走 loadFullImg。
// 实际取数据走 fetchCachedBlobUrl,同一 URL 本次标签页只 fetch 一次。
async function loadAuthedImg(im, url, thumb){
  const full = thumb ? url+(url.includes("?")?"&":"?")+"thumb=1" : url;
  const o = await fetchCachedBlobUrl(full);
  if(o) im.src=o;
}
function scrollDown(force){
  const m=$("#messages");
  requestAnimationFrame(()=>{
    if(force){ m.scrollTop=m.scrollHeight; updateScrollBtn(); return; }
    const dist=m.scrollHeight - m.scrollTop - m.clientHeight;
    if(dist < 80) m.scrollTop = m.scrollHeight;  // 距底<80px 才跟滚,否则用户在阅读历史
    updateScrollBtn();
  });
}
function updateScrollBtn(){
  const btn=$("#scrollDownBtn"); if(!btn) return;
  const m=$("#messages");
  const dist=m.scrollHeight - m.scrollTop - m.clientHeight;
  btn.classList.toggle("show", dist >= 80);
}
// 点↓按钮:平滑滚到底(流式跟滚仍用瞬时,否则追不上不断增长的内容)
function scrollDownSmooth(){
  const m=$("#messages");
  m.scrollTo({top:m.scrollHeight, behavior:"smooth"});
}

// ── SSE 流式接收 ─────────────────────────────────────────────────────────
// 用 fetch 流式读 SSE,替代原生 EventSource。原因:iOS Safari 只要有活跃的 EventSource
// 长连接,地址栏就【一直转圈】(把没结束的长连接当成"页面还在加载")。fetch 读流不占这个
// "页面加载"语义,转圈消失;且重连完全握在自己手里(回前台立即重连、断线退避重连)。
// 服务端仍是标准 SSE,无需改动。对外暴露与 EventSource 兼容的接口:readyState / close()。
class FetchSSE {
  constructor(url, token){
    this.url=url; this.token=token;
    this.readyState=0;        // 0=连接中 1=已连 2=已关闭
    this.lastId=null;         // 最后收到的 SSE id,重连时带 Last-Event-ID 让服务端补发漏掉的
    this.lastBeat=Date.now(); // 最后收到任何数据(含心跳)的时刻,看门狗据此判断假死
    this.onopen=null; this.onmessage=null;
    this._closed=false; this._ctrl=null; this._wake=null;
    this._loop();
  }
  async _loop(){
    let firstOpen=true;
    while(!this._closed){
      this.readyState=0; this._ctrl=new AbortController();
      try{
        const headers={"X-Auth-Token":this.token};
        if(this.lastId!=null) headers["Last-Event-ID"]=String(this.lastId);
        const resp=await fetch(this.url,{headers, signal:this._ctrl.signal, cache:"no-store"});
        if(resp.status===401){ this.close(); showLogin(); return; }
        if(!resp.ok || !resp.body) throw new Error("http "+resp.status);
        this.readyState=1; this.lastBeat=Date.now();
        if(this.onopen) this.onopen(firstOpen);   // 传首连标志:首连不补历史(openConv 已拉),重连才补
        firstOpen=false;
        const reader=resp.body.getReader(); const dec=new TextDecoder(); let buf="";
        while(!this._closed){
          const {value,done}=await reader.read();
          if(done) break;                          // 服务端关流 → 跳出去重连
          buf+=dec.decode(value,{stream:true});
          let sep;
          while((sep=buf.indexOf("\n\n"))>=0){ const block=buf.slice(0,sep); buf=buf.slice(sep+2); this._emit(block); }
        }
      }catch(e){ /* abort / 网络断,落到下面退避重连 */ }
      if(this._closed) break;
      this.readyState=0; await this._sleep(3000);  // 退避 3s(reconnectNow 可提前唤醒)
    }
    this.readyState=2;
  }
  _emit(block){
    this.lastBeat=Date.now();                      // 收到任何块(含 ": ping")= 连接活着
    let id=null,data=null;
    for(const line of block.split("\n")){
      if(line.startsWith(":")) continue;           // ": ping" / ": connected" 注释行,忽略
      if(line.startsWith("id:")) id=line.slice(3).trim();
      else if(line.startsWith("data:")) data=line.slice(5).trim();
    }
    if(id!=null) this.lastId=id;
    if(data!=null && this.onmessage) this.onmessage({data});
  }
  _sleep(ms){ return new Promise(res=>{ const t=setTimeout(res,ms); this._wake=()=>{ clearTimeout(t); this._wake=null; res(); }; }); }
  reconnectNow(){ if(this._closed) return; if(this._ctrl) try{this._ctrl.abort();}catch(e){} if(this._wake) this._wake(); } // 立刻断开重连(跳过退避)
  close(){ this._closed=true; this.readyState=2; if(this._ctrl) try{this._ctrl.abort();}catch(e){} if(this._wake) this._wake(); }
}
function connect(){
  if(S.es) S.es.close();
  const es = S.es = new FetchSSE("/events", S.token);
  es.onopen = (firstOpen)=>{
    // 首连/重连都拉一次历史:首连补回上次刷新前进行中轮的部分内容(draft_text);
    // 重连补回断连期间漏掉的消息(尤其服务端自我重启后注入的系统消息)。force:重连这一刻
    // 恰是最容易出现"流式气泡卡死"的时候(断线期间 done 被环形缓冲挤掉),顺带核对一次。
    reloadHistory(true);
  };
  es.onmessage = ev=>{ try{
    const d=JSON.parse(ev.data);
    if(d.id){ if(d.id<=S.lastId) return; S.lastId=d.id; }  // 补发/重复的老事件直接丢弃
    handleEvent(d);
  }catch(e){} };
}
// 手机救命稻草:iOS 切后台/锁屏会挂起连接,回前台后连接多半已【假死】(既不报错也不来数据)。
// 页面重新可见 / 重新联网 / 窗口聚焦时,主动 reconnectNow() 强制重连,并直接走 HTTP 拉一次历史
// (不等 SSE 恢复)。8s 看门狗兜底:超过 20s 没收到任何数据(含心跳)就判定假死并强制重连。
function reviveSSE(){
  if(document.hidden) return;
  if(!S.es) connect(); else S.es.reconnectNow();
  loadConvs();                   // 断线期间侧栏本身(新会话/标题/置顶等)也可能变了,一并核对
  reloadHistory(true);          // 强制核对:回前台/回网络时最可能出现"卡在思考中"的漏接
}
document.addEventListener("visibilitychange", ()=>{ if(!document.hidden) reviveSSE(); });
window.addEventListener("online", reviveSSE);
window.addEventListener("focus", reviveSSE);
setInterval(()=>{
  if(document.hidden || !S.es) return;
  if(S.es.readyState===2) connect();                                   // 被关掉了 → 重建
  else if(Date.now()-(S.es.lastBeat||0) > 20000) S.es.reconnectNow();  // 20s 无心跳=假死 → 强制重连
  // 连接活着,但当前会话的流式气泡超过 25s 没收到任何新事件 → 疑似卡住(如反复切换多个
  // 正在streaming的会话期间发生过短暂重连,漏接了这条的某帧),强制核对一次历史自愈
  else if(S.stream && Date.now()-(S.stream.lastEvt||S.stream.t0||0) > 25000) reloadHistory(true);
}, 8000);
// /history 可能已经用 draft 字段铺出一个"草稿气泡"(pending 轮、data-tid 挂在 turn-block 上)。
// 建真正的流式气泡前先把它摘掉,否则同一轮回复会同时挂着草稿气泡和流式气泡,看着像重复了两遍。
// 摘的判据是"带 .status 的 .row.ai"——正常已完成的历史气泡不带这个状态行。
function removeStalePendingBubble(){
  const rows=$("#wrap").querySelectorAll(".row.ai");
  for(let i=rows.length-1;i>=0;i--){
    if(rows[i].querySelector(".status")){ rows[i].remove(); break; }
  }
}
function ensureStream(t0){
  // 为当前会话准备一个流式 assistant 气泡(含状态行/思考/过程区)
  if(S.stream) return S.stream;
  removeStalePendingBubble();
  // 新一轮开始 = 上一轮不再是"最新一轮",之前挂的重新生成按钮跟着失效——
  // 摘掉它,不留一个点了只会收到 409 的过期按钮(正常 diff 重绘也会摘,这里
  // 是让画面立刻反映,不用等下次 /history 重拉)。
  $("#wrap").querySelectorAll(".tfregen").forEach(b=>b.remove());
  const row=el("div","row ai");
  const bubble=el("div","bubble");
  const status=el("div","status");
  const thinktog=el("div","thinktog"); const tcv=el("span","chev");
  thinktog.append(tcv, document.createTextNode("思考过程"));
  const think=el("div","think");
  // flow:文字段与工具卡按到达顺序交错追加(复刻 Claude Code 的边说边干)
  const flow=el("div","flow");
  bubble.append(thinktog,think,flow,status);  // 状态行(工作中…)放气泡底部,像 TG 的打字指示
  row.append(bubble); $("#wrap").append(row); updateEmpty();
  thinktog.onclick=()=>{ const on=think.classList.toggle("show"); tcv.classList.toggle("down", on); };
  S.stream={row,bubble,status,think,thinktog,flow,segs:{},tools:[],cards:{},subs:{},activeCard:null,tgroup:null,toolCount:0,t0:t0||Date.now(),lastEvt:Date.now()};
  resumeStreamTimer(S.stream);  // 状态行的耗时计数每秒走字;流被任何路径丢弃(完成/切会话)都会自行停表,不泄漏
  renderStatus();  // 初始即显示"💭 思考中…"动画
  updateSendBtn();
  scrollDown(true); return S.stream;
}
// 给流式状态对象挂一个走字计时器(新建/从快照接回来都用这个),旧表已在挂之前 clearInterval 过
function resumeStreamTimer(s){
  const tm=setInterval(()=>{ if(!S.stream||S.stream.timer!==tm){ clearInterval(tm); return; } renderStatus(); }, 1000);
  s.timer=tm;
}
// 取/建某文字段的 div(按首次出现顺序挂进 flow,与工具卡自然交错)
function segDiv(s, seg){
  seg=seg||0;
  let d=s.segs[seg];
  if(!d){ d=el("div","seg"); d.dataset.raw=""; s.segs[seg]=d; s.flow.append(d); }
  return d;
}
// 全部文字段拼起来(判断"这轮有没有正文"用)
function streamText(s){
  return Object.keys(s.segs).sort((a,b)=>a-b).map(k=>s.segs[k].dataset.raw).join("");
}
let renderPending=false; const dirtySegs=new Set();
function userSelecting(){
  const s=window.getSelection();
  return s && s.rangeCount>0 && !s.isCollapsed;
}
// 用户松手时触发:清理卡住的 renderPending,让积压的 dirtySegs 能渲出来
document.addEventListener("selectionchange", ()=>{
  if(!userSelecting() && renderPending && dirtySegs.size){
    renderPending=false;
    const first=dirtySegs.values().next().value;
    if(first) renderSeg(first);
  }
});
function renderSeg(d){
  dirtySegs.add(d);
  if(renderPending) return; renderPending=true;
  requestAnimationFrame(()=>{ renderPending=false;
    // 用户在选文字时不替换 innerHTML(会销毁选中 DOM,使 iOS 选择菜单闪退),
    // dirtySegs 不清,下次收到 text 事件会继续尝试。
    if(userSelecting()){ renderPending=true; return; }
    for(const x of dirtySegs) x.innerHTML=mdToHtml(x.dataset.raw);
    dirtySegs.clear(); scrollDown(); });
}
const DOTS='<span class="dots"><i></i><i></i><i></i></span>';
function renderStatus(){
  if(!S.stream) return;
  const t=S.stream.tools, hasBody=!!streamText(S.stream);
  // 工具 chip 已下放到 flow 卡片,这里不再重复;只留一条持续的"工作中/思考中"指示
  const secs=Math.max(0, Math.round((Date.now()-(S.stream.t0||Date.now()))/1000));
  const tick=secs>=1?(" · "+(secs>=60?Math.floor(secs/60)+"m"+(secs%60)+"s":secs+"s")):"";
  // 有正文 或 有工具在跑 → 工作中;纯思考(啥都没出)→ 思考中(始终带动画,像 TG)
  const working = hasBody || t.some(x=>!x.done);
  const label = S.stream.audioPending ? "音频转写中" : (working ? "工作中" : "思考中");
  S.stream.status.innerHTML = '<span class="chip live">'+label+DOTS+esc(tick)+'</span>'
    + '<button type="button" class="statusstop" title="停止回复" aria-label="停止回复"><span class="sq"></span></button>';
}
function finalizeStream(finalText, imgs, turnId){
  if(!S.stream) return;
  // 末尾那条普通命令以前等不到下一段正文/命令,会永久散落在过程区;收工时也要归组。
  if(S.stream.activeCard){ foldToGroup(S.stream, S.stream.activeCard); S.stream.activeCard=null; }
  if(S.stream.timer) clearInterval(S.stream.timer);
  S.stream.status.remove(); S.stream.thinktog.remove(); S.stream.think.remove();
  // 回合已结束:还没点的审批按钮全部失效(点了也只会收到"已过期")
  S.stream.bubble.querySelectorAll(".choice button").forEach(b=>{ b.disabled=true; });
  // 交错排布保持原样;只有整轮没渲出任何正文时才用 done 的全文兜底(防丢帧空泡)
  if(finalText!==undefined && !streamText(S.stream)){
    const d=segDiv(S.stream, 0); d.dataset.raw=finalText; d.innerHTML=mdToHtml(finalText);
  }else{
    for(const k in S.stream.segs){ const d=S.stream.segs[k]; d.innerHTML=mdToHtml(d.dataset.raw); }
  }
  appendImgs(S.stream.bubble, imgs);
  // 刚说完就地挂时间/复制/重新生成——不用等用户切走再切回、走 /history 重绘才补出来。
  // turnId 只有 done 事件(常规对话轮)才带,命令回复/cron 推送(message 事件)没有,
  // 此时不挂重新生成按钮(见 buildTurnFoot)。ts 用本地时间近似,跟服务端落库时刻
  // 只差一次网络往返,消息级"几点几分"的粒度感知不出来。
  S.stream.bubble.append(buildTurnFoot(S.conv, turnId, streamText(S.stream), Date.now()/1000, true));
  S.stream=null; updateSendBtn(); scrollDown();
}
// 构成一个"进行中回合"的流式事件类型(可重放以重建气泡)。done/message/choice 不在此列。
const STREAMY = ["user","start","thinking","text","tool_start","tool_input","tool_end","compact"];
// 每会话缓冲进行中回合的事件;text/thinking 是全量快照:thinking 全局只留最后一条,
// text 按段(seg)各留最后一条 —— 段的先后顺序就是与工具卡交错的顺序,不能压掉。
function bufPush(conv, e){
  const b = S.live[conv] || (S.live[conv]=[]);
  if(e.type==="start" && !e._t0) e._t0=Date.now(); // 记录真实开始时刻,切回重放时恢复计时
  if(e.type==="text"||e.type==="thinking"){
    for(let i=b.length-1;i>=0;i--){
      if(b[i].type!==e.type) continue;
      if(e.type==="text" && (b[i].seg||0)!==(e.seg||0)) continue;
      b[i]=e; return;
    }
  }
  b.push(e);
}
// 仅当"正在回复的会话集合"变化时才重绘列表(圆点靠 CSS 动画常闪,不需逐帧 JS)。
let _liveSig="";
function markLive(){
  const sig=Object.keys(S.live).sort().join(",");
  if(sig===_liveSig) return;
  _liveSig=sig; renderConvs();
}
function handleEvent(e){
  // 服务端进程重启标识:变了说明断线期间进程重启过,环形缓冲/_live 全清空了,
  // 靠事件补发这条路救不回来 —— 主动整体核对一次(侧栏 + 当前会话历史),别等用户自己发现内容旧了。
  // 关键:重启后服务端事件编号从 0 重新数,而 S.lastId 还停在旧进程的最大编号上,
  // 不重置的话新事件 id 全 ≤ 旧游标,被 onmessage 的去重判断当"补发的旧事件"丢弃,
  // 页面就此静默断流 —— 这就是"重启后必须手动刷新才能继续对话"的根因。
  if(e.type==="hello"){
    const prev=S.bootId; S.bootId=e.boot_id;
    if(prev && prev!==e.boot_id){
      if(typeof e.seq==="number") S.lastId=e.seq; else S.lastId=0;  // 去重游标对齐新进程编号空间
      if(S.es) S.es.lastId=null;                                    // Last-Event-ID 补发游标同样作废
      S.autoResumed={};                                             // 新进程再给一次自动恢复机会
      loadConvs(); reloadHistory(true);
    }
    return;
  }
  // 后台标题总结完成 → 刷新侧边栏/标题栏(loadConvs 会顺带同步顶栏标题),无论前后台会话
  if(e.type==="title"){ loadConvs(); return; }
  // 非 web 入口(cron/语音任务等)驱动的会话有新动静,见 core/task_events.py 主事件桥:
  // 那边没有 start/thinking 流式过程可镜像,直接拼气泡容易出现"回复凭空冒出来,
  // 缺前面那句提问"的错位画面——干脆让浏览器去核对真实数据最省心。
  if(e.type==="sync"){ loadConvs(); if(e.conv===S.conv) reloadHistory(true); return; }
  // send_image 工具中途发的图(mid_turn):这一轮还没结束,后面还会有正文/工具事件。
  // 按 STREAMY 事件一样只挂进气泡/回放缓冲,不能走下面"回合结束"那一套(未读标记/
  // 清空回放缓冲/刷新侧栏),否则会把还没说完的这一轮拦腰截断成两条气泡。
  if(e.type==="message" && e.mid_turn){
    bufPush(e.conv, e); markLive();
    // histLoading 窗口内(openConv 正在拉 /history)不直接建气泡,已缓冲,等 maybeReplayStream 统一重放
    if(e.conv===S.conv && S.histLoading!==e.conv){ applyStreamEvent(e); if(S.stream) S.stream.lastEvt=Date.now(); }
    return;
  }
  // 模型刚通过定时任务工具完成增删改时，立刻同步列表；不必等整轮回复结束。
  if(e.type==="tool_end" && e.ok && ["add_cron_job","delete_cron_job","set_cron_job_enabled"].includes(e.name)){
    loadCronSidebar();
  }
  // 会话有新回合 → 历史已变,作废其缓存(含磁盘),下次打开会重新拉取
  if(e.type==="done"||e.type==="message"){ delete S.histCache[e.conv]; idbDel("hist:"+e.conv); }
  // 维护每会话"进行中"缓冲:无论前台/后台都记录,切回该会话时可完整重建流式气泡
  if(STREAMY.indexOf(e.type)>=0){ bufPush(e.conv, e); markLive();
    // start = 这一轮刚起跑的时刻,也正是后端刚把 archived 清掉/状态改成 running 的
    // 时刻(见 run.py._handle / task_runner._start_one)。之前只在"侧栏还没这一行"
    // 时才刷新,续接一个侧栏里本来就存在的旧行(已归档/已完成)不会触发,archived
    // 和状态要等这一轮 done 才刷新——期间侧栏一直显示旧数据,长任务尤其明显。
    // 都是廉价的 GET,不必省这一次:start 无条件刷新对应侧栏。
    if(e.type==="start"){
      if(String(e.conv).startsWith("task:")) loadVoiceSidebar();
      else loadConvs();
    }
  }
  if(e.type==="done"||e.type==="message"){
    // 未读标记要在 delete S.live 触发的这次重绘【之前】就位,否则会先渲一帧"没有点"
    // 再渲一帧"灰点冒出来",看着像跳动;这里把橙点摘掉和灰点挂上做成同一次渲染。
    // 定时任务跑完推来的消息:顺带刷新任务列表(last_status/enabled 这些字段不在
    // /conversations 里,得单独拉 /cron/sidebar)。task: 前缀现在是语音/cron/chat
    // 三种后台任务共用的(2026-07-29 统一),前缀本身分不出是哪一种,索性都刷新一遍
    // ——都是廉价的 GET,刷新一个用不上的列表没有实际代价。
    const isTaskConv = String(e.conv).startsWith("task:");
    if(e.conv===S.conv){
      S.pendingReview[e.conv]=false; const c=S.convs.find(x=>x.conv===e.conv); if(c) c.pending_review=false;
      delete S.live[e.conv]; delete S.streamSnap[e.conv]; markLive();
      // 这一轮可能改了代码:顶栏 Git 状态原先只在切会话时拉一次,任务跑完不会自动
      // 更新,顶栏就会一直停留在"工作区干净"——这里补一次悄悄刷新(不重置按钮/不关弹层)
      if(e.type==="done" && typeof refreshGitQuiet==="function") refreshGitQuiet(e.conv);
      // 后端不知道"用户正盯着这个会话看",完成时统一先标为未读;这里既然正在看,
      // 主动清一次服务端标记再拉 loadConvs——顺序不能反,否则 loadConvs 抢先拉到
      // 服务端还没清的"未读",又把本地清零覆盖回去。
      api("/conv/read",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({conv:e.conv})})
        .catch(()=>{}).then(loadConvs);
    } else {
      S.pendingReview[e.conv]=true; const c=S.convs.find(x=>x.conv===e.conv); if(c) c.pending_review=true;
      delete S.live[e.conv]; delete S.streamSnap[e.conv]; markLive();
      // 后台任务行来自 /voice/sidebar(非 /conversations),done 后刷新 task_status
      if(isTaskConv){ loadVoiceSidebar(); } else { loadConvs(); }
    }
    if(isTaskConv) loadCronSidebar();
  }

  if(e.conv!==S.conv){ // 别的会话在后台的事件 → 不打断当前视图(已缓冲,圆点已更新)
    // 审批发给非当前会话:直接弹出全屏确认窗,不让用户漏掉
    if(e.type==="choice"){ S.pendingChoice[e.conv]=e; openChoiceModal(e.conv, e); return; }
    // 该会话这一轮结束了 → 之前暂存的审批已作废,清掉
    if(e.type==="done"||e.type==="message"){ delete S.pendingChoice[e.conv]; }
    if(e.type==="done") flushPending(e.conv);  // 后台会话任务完成 → 补发其待发送队列
    return;
  }
  // 当前会话本轮结束 → 清掉可能残留的暂存审批
  if(e.type==="done"||e.type==="message") delete S.pendingChoice[e.conv];
  if(STREAMY.indexOf(e.type)>=0){
    // histLoading 窗口内:上面已 bufPush 缓冲过,这里不直接建气泡——避免跟 openConv 里
    // /history 铺出来的草稿气泡各建一个,等 maybeReplayStream 统一从缓冲重放一次收尾。
    if(S.histLoading!==e.conv){ applyStreamEvent(e); if(S.stream) S.stream.lastEvt=Date.now(); }
    return;
  }
  switch(e.type){
    case "done": {
      const errBubble = S.stream ? S.stream.bubble : null;
      finalizeStream(e.text, undefined, e.turn_id);
      if(e.is_error){
        if(errBubble) errBubble.classList.add("err");
        // 有 api_error_status(429/529/5xx等)= 后端已确认是模型服务商那边的错,
        // 直接按状态码分类;没有才退回关键词猜测(网络/进程异常等)
        const st=e.api_error_status;
        if(st===429) setRateStatus("warn", "最近遇到限额/过载", e.error);
        else if(st===529) setRateStatus("warn", "Claude 服务过载", e.error);
        else if(st) setRateStatus("err", `Claude 服务出错(${st})`, e.error);
        else {
          const el=(e.error||"").toLowerCase();
          if(/rate|429|quota|limit|overloaded/i.test(el)) setRateStatus("warn", "最近遇到限额/过载", e.error);
          else setRateStatus("err", "", e.error);
        }
      }else{ setRateStatus("ok","",""); }
      setMeta(e); loadConvs(); flushPending(e.conv); break; }
    case "message": // 命令回复 / 报错 / cron 推送(AI 主动发图走 mid_turn 分支,不会到这里)
      if(S.stream && !streamText(S.stream)){ finalizeStream(e.text, e.images); }
      else { S.stream=null; addBubble("ai", e.text, e.images); }
      // 后端推送的报错也更新侧栏状态
      if(e.text&&e.text.includes("⚠️")){
        const el2=e.text.toLowerCase();
        if(/rate|429|quota|limit|overloaded/i.test(el2)) setRateStatus("warn", "最近遇到限额/过载", e.text);
        else setRateStatus("err", "", e.text);
      }
      loadConvs(); flushPending(e.conv); break;
    case "choice": delete S.pendingChoice[e.conv]; renderChoice(e); break;
  }
}
// 创建一个"已执行 N 条命令"可展开折叠组,内放 toolCards
function makeToolGroup(cards, count){
  const grp=el("div","tgroup");
  const head=el("div","tg-head");
  const chev=el("span","chev");
  const label=el("span","tg-label");
  label.textContent="已执行 "+(count||cards.length)+" 条命令";
  head.append(chev, label);
  const body=el("div","tg-body collapsed");
  for(const c of cards) body.append(c);
  grp.append(head, body);
  head.onclick=()=>{ const c=body.classList.toggle("collapsed"); chev.classList.toggle("down", !c); };
  grp._head=head; grp._body=body; grp._label=label; grp._chev=chev;
  return grp;
}
// 把已完成的指令卡片折叠进"已执行 N 条命令"可展开组
function foldToGroup(s, card){
  let grp = s.tgroup;
  if(!grp){
    // 记下卡在 flow 里的位置再移动,保证折叠组替换原位而非扔到末尾
    const anchor = card ? card.nextSibling : null;
    grp = makeToolGroup([], 0);
    s.tgroup = grp;
    if(card){
      card.classList.remove("tc-active");
      grp._body.append(card);
      s.toolCount = 1;
      grp._label.textContent = "已执行 1 条命令";
      if(!grp.parentNode){
        if(anchor && anchor.parentNode === s.flow){
          s.flow.insertBefore(grp, anchor);
        } else {
          s.flow.append(grp);
        }
      }
    }
  } else if(card){
    card.classList.remove("tc-active");
    grp._body.append(card);
    s.toolCount = (s.toolCount || 0) + 1;
    grp._label.textContent = "已执行 " + s.toolCount + " 条命令";
  }
}
// 把单个流式事件落到 DOM(供前台实时渲染 + 切回会话时按缓冲重放两用)
function applyStreamEvent(e){
  // 停止回复后的防重建短窗,见 stopReply 的注释:窗口内该会话的旧帧在流已收掉
  // 时不重建气泡(避免「点了还出现,要点第二次」);到期或命中后自动解除。
  const g = S.stopGuard;
  if(g){
    if(g.conv === e.conv){
      if(Date.now() > g.until) S.stopGuard = null;  // 窗口到期,恢复正常
      else if(!S.stream) return;                    // 窗口内、无流 → 在途旧帧,丢弃
    }
  }
  switch(e.type){
    case "user":
      if(S.localSent){ S.localSent=false; break; }  // 本客户端发的,气泡已在 send() 里加了
      // 无活跃流式气泡 = 不是本轮发送 → 历史已渲染过该消息,跳过避免重复
      if(!S.stream) break;
      addBubble("me", e.text, e.images); break;
    case "start":
      if(S.audioLoading){ S.audioLoading.remove(); S.audioLoading=null; }
      if(S.stream) S.stream.audioPending = false;  // 转写完成,状态行恢复正常文案
      ensureStream(e._t0); break;
    case "thinking": { const s=ensureStream(); s.thinktog.style.display="flex"; s.think.textContent=e.text; renderStatus(); break; }
    case "text": { const s=ensureStream(); const d=segDiv(s, e.seg); d.dataset.raw=e.text; renderSeg(d); if(s.activeCard){ foldToGroup(s, s.activeCard); s.activeCard=null; s.tgroup=null; s.toolCount=0; } renderStatus(); break; }
    case "tool_start": { const s=ensureStream();
      if(e.parent){ subUpsert(s, e.parent, {name:e.name, id:e.tool_id, done:false, ok:true}); }
      else s.tools.push({name:e.name, id:e.tool_id, done:false, ok:true, subN:0});
      renderStatus(); break; }
    case "tool_input": { const s=ensureStream();
      if(e.parent) break;   // 子代理内部工具的入参不展大卡片(动态在子代理卡里),避免刷屏
      const card=renderToolCard(e.name, e.input||{});
      if(card){
        if(e.tool_id) s.cards[e.tool_id]=card;
        // 上一条活跃指令折叠进组,新指令置为活跃(呼吸灯)
        if(s.activeCard && s.activeCard!==card){ foldToGroup(s, s.activeCard); }
        s.activeCard=card; card.classList.add("tc-active");
        s.flow.append(card); scrollDown();
        // 子代理卡片刚建好:若它的内部工具事件已先到(存在 s.subs),补渲染一次动态区
        if(card._sub && e.tool_id && s.subs[e.tool_id]) renderSub(s, e.tool_id);
      } break; }
    case "tool_end": { const s=ensureStream();
      if(e.parent){ subUpsert(s, e.parent, {name:e.name, id:e.tool_id, done:true, ok:e.ok}); renderStatus(); break; }
      // 优先按 tool_id 配对(并行同名工具不会错标),回退按名字
      const it=(e.tool_id&&s.tools.find(x=>x.id===e.tool_id&&!x.done))
        ||s.tools.find(x=>x.name===e.name&&!x.done)||s.tools[s.tools.length-1];
      if(it){it.done=true;it.ok=e.ok;}
      attachResult(s, e);
      // 呼吸灯不因工具结束而停——持续到下条指令或正文出现才移除
      renderStatus(); break; }
    case "compact": { const s=ensureStream();
      const d=el("div","compactline"); d.textContent=e.trigger==="manual"?"已手动压缩上下文,对话继续":"上下文已自动压缩,对话继续";
      s.flow.append(d); scrollDown(); break; }
    case "message": { // mid_turn 图片消息(send_image 工具),只在本轮回复过程中途出现
      const s=ensureStream();
      if(e.text){ const d=el("div","seg"); d.innerHTML=mdToHtml(e.text); s.flow.append(d); }
      appendImgs(s.flow, e.images);
      renderStatus(); scrollDown(); break; }
  }
}
// 切回某会话:若它有进行中的缓冲,重建流式气泡。命中快照(离开前摘下的 DOM)就直接接回来,
// 只补上离开期间新增的那几条事件;没有快照(如页面刚刷新)才从头重放整段缓冲。
function maybeReplayStream(conv){
  if(S.conv!==conv) return;
  const buf=S.live[conv]; if(!buf||!buf.length) return;
  // renderTurns 可能已用 draft 渲染了部分内容;不管接下来走哪条重建路径,都先清掉,
  // 避免出现两个 AI 气泡(草稿+流式)。restore-from-snap 分支下面是直接 append 现成的
  // S.stream.row、不经过 ensureStream(),所以这里仍需单独摘一次。
  removeStalePendingBubble();
  const snap=S.streamSnap[conv];
  delete S.streamSnap[conv];
  if(snap && snap.asOf<=buf.length){
    S.stream=snap.state;
    $("#wrap").append(S.stream.row);
    resumeStreamTimer(S.stream);
    for(let i=snap.asOf;i<buf.length;i++){         // 只补离开期间新增的事件,已建好的工具卡不重建
      const e=buf[i]; if(e.type==="user") continue;
      applyStreamEvent(e);
    }
    scrollDown(true);  // 接回来的内容跟在刚渲染完的历史后面,补一次滚到底
  }else{
    S.stream=null;
    for(const e of buf){
      if(e.type==="user") continue;   // 用户消息已由历史渲染,重放跳过避免重复气泡
      applyStreamEvent(e);
    }
  }
  if(S.stream) S.stream.lastEvt=Date.now();  // 按当前时刻起算卡住检测,不用缓冲里事件的旧时间
}
// 提示音:Web Audio 现生成两声轻响,不依赖外部音频文件;页面从没交互过(无 AudioContext
// 授权)时 resume 会静默失败,吞掉即可——反正这类场景系统推送那条通知本身也会响。
function playChime(){
  try{
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if(!Ctx) return;
    const ctx = new Ctx();
    const beep = (t, freq) => {
      const osc = ctx.createOscillator(), gain = ctx.createGain();
      osc.type = "sine"; osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(0.18, t + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.22);
      osc.connect(gain); gain.connect(ctx.destination);
      osc.start(t); osc.stop(t + 0.24);
    };
    const t0 = ctx.currentTime;
    beep(t0, 880); beep(t0 + 0.14, 1175);
    setTimeout(()=>ctx.close(), 500);
  }catch(e){}
}
// 跨会话审批/确认:弹出全屏 modal,显示会话标题、问题内容和可点选项
function openChoiceModal(conv, e){
  // 工作台独立窗口只处理任务,不能被其他会话的确认选项盖住。
  if(S.standaloneWorkbench) return;
  const modal=$("#choiceModal"); if(!modal) return;
  const titleEl=$("#choiceModalTitle"); const promptEl=$("#choiceModalPrompt"); const optsEl=$("#choiceModalOpts");
  const c = S.convs.find(x=>x.conv===conv);
  titleEl.textContent = c ? (c.title || "新对话") : (conv==="main" ? "主会话" : "新对话");
  promptEl.innerHTML = mdToHtml(e.prompt || "");
  optsEl.innerHTML = "";
  playChime();
  const btns=[];
  (e.options || []).forEach(([cmd,label],i)=>{
    const btn=el("button");
    btn.innerHTML = '<span class="arw">❯</span> <span class="num">'+(i+1)+".</span> <span class=\"lbl\">"+esc(label)+"</span>";
    btn.onclick=()=>{
      btns.forEach(b=>{ b.disabled=true; if(b!==btn) b.classList.add("dimmed"); });
      btn.classList.add("selected");
      sendCmd(cmd, conv);
      modal.hidden=true;
      // 用户已响应,切回该会话时不再补出旧审批
      delete S.pendingChoice[conv];
    };
    optsEl.append(btn); btns.push(btn);
  });
  modal.hidden=false;
}
function renderChoice(e){
  const box=el("div","choice");
  const opts=el("div","opts"); box.append(opts);
  const btns=[];
  e.options.forEach(([cmd,label],i)=>{
    const btn=el("button");
    const arw=el("span","arw"); arw.textContent="❯";
    const num=el("span","num"); num.textContent=(i+1)+".";
    const lbl=el("span","lbl"); lbl.textContent=label;
    btn.append(arw,num,lbl);
    btn.onclick=()=>{
      btns.forEach(b=>{ b.disabled=true; if(b!==btn) b.classList.add("dimmed"); });
      btn.classList.add("selected"); sendCmd(cmd);
    };
    opts.append(btn); btns.push(btn);
  });
  if(S.stream){
    // 回合进行中(审批闸/追问):嵌进当前流式气泡的过程区,绝不能删掉正在渲染的工具卡
    const card=el("div","toolcard");
    const head=el("div","tc-head tc-warn"); head.innerHTML=ic("lock")+" 需要你的确认";
    const bodyw=el("div","tc-body");
    const p=el("div"); p.innerHTML=mdToHtml(e.prompt);
    bodyw.append(p, box);
    card.append(head, bodyw);
    S.stream.flow.append(card); scrollDown();
    return;
  }
  // 无进行中的回合(如 /model 选择):独立气泡
  const b=addBubble("ai", e.prompt);
  b.append(box); scrollDown();
}
