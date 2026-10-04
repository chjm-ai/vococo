"use strict";
// 2026-10-03 会话内查找(标题栏放大镜 / 会话里按 ⌘F):类似浏览器页内查找,但支持多个关键词。
// 空格分隔多个词,每个词一种高亮色;点词条只在该词的命中间跳转,再点一次回到「全部」。
// 高亮用 CSS Custom Highlight API(::highlight),不改消息 DOM,和 renderTurns 的增量 diff、
// 流式气泡互不干扰。只搜已渲染出来的轮次;更早的分页没加载时给「加载更早消息一起搜」。
// 与内联脚本同属全局作用域,依赖 app-core / stream / index.html 里的 $、el、esc、ic、S、loadEarlierHistory。

const FIND = {
  open:false, terms:[], hits:[], cur:-1, focus:-1,   // focus:-1=全部词,否则只在 terms[focus] 的命中间跳
  timer:null, moTimer:null, mo:null, loadingAll:false, truncated:false, chipSig:"",
};
const FIND_MAX_TERMS = 5;      // 对应 styles.css 里 fh-0..fh-4 五种颜色
const FIND_MAX_HITS = 2000;    // 超长会话兜底,防止一次造几万个 Range 卡住;超出时丢最旧的,保住最新消息
const FIND_OK = typeof CSS!=="undefined" && CSS.highlights && typeof Highlight!=="undefined";
// 不参与查找的区域:按钮/图标是界面文字,不是对话内容
const FIND_SKIP = "button,svg,script,style,.findbar";

function findTerms(q){
  const seen=new Set(), out=[];
  for(const w of q.split(/\s+/)){
    const k=w.toLowerCase();
    if(!k || seen.has(k)) continue;
    seen.add(k); out.push(w);
    if(out.length>=FIND_MAX_TERMS) break;
  }
  return out;
}
// 文本节点是否真的显示着(折叠的工具卡、隐藏块里的字不算命中,否则跳过去什么也看不见)
function findVisible(node){
  const p=node.parentElement;
  if(!p || p.closest(FIND_SKIP)) return false;
  if(p.checkVisibility) return p.checkVisibility();
  return p.getClientRects().length>0;
}
// 中英文分开匹配:中文没有词边界,按片段命中;英文/数字要整词命中,搜「AI」不该亮 email、wait 里的 ai。
// 只看词两端:首字符是字母数字 → 前一个字符不能是字母数字;尾字符同理。中英混排(「用AI做」「AI编程」)照常命中
const FIND_WORD = /[A-Za-z0-9_]/;
function findBoundaryOk(text, start, end, term){
  if(FIND_WORD.test(term[0]) && start>0 && FIND_WORD.test(text[start-1])) return false;
  if(FIND_WORD.test(term[term.length-1]) && end<text.length && FIND_WORD.test(text[end])) return false;
  return true;
}
// 扫 #wrap 里所有可见文本节点,按文档顺序收集各词命中。同一处被多个词覆盖时各记各的。
// 先 indexOf 再判可见:绝大多数文本节点没命中,省掉逐个量布局。Range 最后只给保留的那部分建。
function findCollect(terms){
  let hits=[];
  FIND.truncated=false;
  if(!terms.length) return hits;
  const lows=terms.map(t=>t.toLowerCase());
  const walker=document.createTreeWalker($("#wrap"), NodeFilter.SHOW_TEXT);
  let n;
  while((n=walker.nextNode())){
    const text=n.nodeValue;
    if(!text || !text.trim()) continue;
    const low=text.toLowerCase();
    const local=[];
    lows.forEach((w,ti)=>{
      let i=low.indexOf(w);
      while(i>=0){
        // 命中就跳过整个词(不重叠);被词边界否掉的只挪一格,别漏掉紧跟着的真命中
        if(findBoundaryOk(low, i, i+w.length, w)){ local.push({node:n, start:i, end:i+w.length, term:ti}); i=low.indexOf(w, i+w.length); }
        else i=low.indexOf(w, i+1);
      }
    });
    if(!local.length || !findVisible(n)) continue;
    local.sort((a,b)=>a.start-b.start || a.term-b.term);
    for(const h of local) hits.push(h);
  }
  if(hits.length>FIND_MAX_HITS){ FIND.truncated=true; hits=hits.slice(-FIND_MAX_HITS); }
  for(const h of hits){
    const r=document.createRange();
    r.setStart(h.node, h.start); r.setEnd(h.node, h.end);
    h.range=r;
  }
  return hits;
}
// 当前过滤下可跳转的命中(全部 or 只某一个词)
function findPool(){
  return FIND.focus<0 ? FIND.hits : FIND.hits.filter(h=>h.term===FIND.focus);
}
function findPaint(){
  if(!FIND_OK) return;
  for(let i=0;i<FIND_MAX_TERMS;i++) CSS.highlights.delete("fh-"+i);
  CSS.highlights.delete("fh-cur");
  if(!FIND.open) return;
  const groups=FIND.terms.map(()=>[]);
  FIND.hits.forEach(h=>groups[h.term].push(h.range));
  groups.forEach((rs,i)=>{ if(rs.length) CSS.highlights.set("fh-"+i, new Highlight(...rs)); });
  const pool=findPool(), h=pool[FIND.cur];
  if(h){ const cur=new Highlight(h.range); cur.priority=1; CSS.highlights.set("fh-cur", cur); }
}
function findRenderBar(){
  const pool=findPool();
  $("#findCount").textContent = FIND.terms.length
    ? (pool.length ? (FIND.cur+1)+"/"+pool.length+(FIND.truncated?"+":"") : "0/0") : "";
  $("#findCount").title = FIND.truncated ? "命中太多,只保留了最近的 "+FIND_MAX_HITS+" 处" : "";
  $("#findCount").classList.toggle("none", FIND.terms.length>0 && !pool.length);
  $("#findPrev").disabled = $("#findNext").disabled = !pool.length;
  // 词条:2 个词以上才显示(单个词时和计数重复);每条带该词命中数,点了只在它的命中间跳
  const chips=$("#findChips");
  const counts=FIND.terms.map(()=>0);
  FIND.hits.forEach(h=>counts[h.term]++);
  const conv=S.conv, st=S.histPaging[conv];
  const more = FIND.terms.length && conv && !String(conv).startsWith("local-") && (!st || st.hasMore!==false)
    && (S.histCache[conv]||[]).length>=40;
  // 内容没变就不重建:流式期间会频繁重扫,每次都换掉按钮会吞掉用户正按着的那次点击
  const sig=JSON.stringify([FIND.terms, counts, FIND.hits.length, FIND.focus, !!more, FIND.loadingAll]);
  if(sig===FIND.chipSig) return;
  FIND.chipSig=sig; chips.innerHTML="";
  if(FIND.terms.length>1){
    const all=el("button","fchip"+(FIND.focus<0?" on":""));
    all.type="button"; all.textContent="全部 "+FIND.hits.length;
    all.onclick=()=>findSetFocus(-1);
    chips.append(all);
    FIND.terms.forEach((t,i)=>{
      const c=el("button","fchip fc-"+i+(FIND.focus===i?" on":"")+(counts[i]?"":" zero"));
      c.type="button";
      c.innerHTML='<i></i>'+esc(t)+' <span>'+counts[i]+'</span>';
      c.onclick=()=>findSetFocus(FIND.focus===i ? -1 : i);
      chips.append(c);
    });
  }
  // 更早的分页还没拉下来:提示用户一键全量加载再搜(长会话才会有)
  if(more || FIND.loadingAll){
    const b=el("button","fmore"); b.type="button";
    b.textContent = FIND.loadingAll ? "正在加载更早消息…" : "只搜了已加载的消息 · 加载更早消息一起搜";
    b.disabled=FIND.loadingAll;
    b.onclick=()=>findLoadAll();
    chips.append(b);
  }
  chips.hidden=!chips.children.length;
}
// 消息区可见部分的上沿 = 顶栏(含查找条)底边。直接量顶栏而不读 --header-h:查找条刚展开时
// ResizeObserver 还没回调,变量是旧值,会把顶栏底下被盖住的那处当成「看得见」
function findVisTop(){ return $("header.bar").getBoundingClientRect().bottom; }
// 把当前命中滚到消息区上 1/3 处(顶栏是浮在上面的,滚到正中偏上最不容易被挡)
function findScrollTo(h){
  if(!h) return;
  const box=$("#messages");
  const r=h.range.getBoundingClientRect(), b=box.getBoundingClientRect();
  if(!r.height && !r.width) return;
  const visTop=findVisTop(), visBot=b.bottom-80;
  if(r.top>=visTop+8 && r.bottom<=visBot) return;   // 已经在视野内就不动,避免来回跳
  box.scrollTop += r.top - (visTop + (visBot-visTop)/3);
  if(typeof updateScrollBtn==="function") updateScrollBtn();
}
// 像浏览器页内查找:从当前看到的位置往下找第一个,下面没有了再取最后一个
function findNearIdx(pool){
  const top=findVisTop();
  const i=pool.findIndex(h=>h.range.getBoundingClientRect().top>=top);
  return i<0 ? pool.length-1 : i;
}
function findGo(step){
  const pool=findPool();
  if(!pool.length) return;
  FIND.cur = FIND.cur<0 ? (step>0?0:pool.length-1) : (FIND.cur+step+pool.length)%pool.length;
  findPaint(); findRenderBar(); findScrollTo(pool[FIND.cur]);
}
function findSetFocus(i){
  const prev=findPool()[FIND.cur];
  FIND.focus=i;
  const pool=findPool();
  // 切词时尽量停在当前位置之后最近的那个命中,而不是每次都从头
  let idx=prev ? pool.findIndex(h=>h.range.compareBoundaryPoints(Range.START_TO_START, prev.range)>=0) : 0;
  if(idx<0) idx=pool.length-1;
  FIND.cur=pool.length?idx:-1;
  findPaint(); findRenderBar(); findScrollTo(pool[FIND.cur]);
  $("#findInput").focus();
}
// 重新扫一遍。opts.keep:DOM 变了(流式/翻页)时尽量保住当前所在的那个命中;
// opts.jump:"near"(视野里/视野下方第一个)/"last"/false,是否跳过去
function findRun(opts={}){
  if(!FIND.open) return;
  const prev=findPool()[FIND.cur];
  FIND.terms=findTerms($("#findInput").value);
  if(FIND.focus>=FIND.terms.length) FIND.focus=-1;
  FIND.hits=findCollect(FIND.terms);
  const pool=findPool();
  let idx=-1;
  if(opts.keep && prev){
    idx=pool.findIndex(h=>h.node===prev.node && h.start===prev.start && h.term===prev.term);
    if(idx<0) idx=Math.min(FIND.cur, pool.length-1);
  }else if(pool.length){
    idx = opts.jump==="last" ? pool.length-1 : findNearIdx(pool);
  }
  FIND.cur=idx;
  findPaint(); findRenderBar();
  if(opts.jump && idx>=0) findScrollTo(pool[idx]);
}
// 循环把更早分页拉下来(每页 40 轮,见 loadEarlierHistory),拉完重搜。
// stopOnHit:从全局搜索跳进来时用,一出现命中就停,不必把超长会话整个拉完;停下后跳到最近一处
async function findLoadAll(stopOnHit){
  const conv=S.conv;
  if(FIND.loadingAll || !conv) return;
  const hadHits=FIND.hits.length>0;
  FIND.loadingAll=true; findRenderBar();
  try{
    for(let guard=0; guard<200; guard++){
      const st=S.histPaging[conv];
      if(S.conv!==conv || !FIND.open || (st && st.hasMore===false)) break;
      const before=(S.histCache[conv]||[]).length;
      await loadEarlierHistory();
      if((S.histCache[conv]||[]).length===before) break;   // 没进展(请求失败/到顶)就停,别死循环
      if(stopOnHit && findCollect(FIND.terms).length) break;
    }
  }finally{
    FIND.loadingAll=false;
    clearTimeout(FIND.moTimer);
    // 之前一处都没命中:跳到最近一处(观察器中途可能已重扫过,不能拿「现在有没有命中」判断);
    // 之前就有命中(用户手动点加载更早):保住当前位置不动
    if(S.conv===conv && FIND.open) findRun(hadHits ? {keep:true} : {jump:"last"});
  }
}
// q:预填关键词(从全局搜索结果点进来时带上);opts.jump 同 findRun
function openFind(q, opts={}){
  const bar=$("#findBar");
  FIND.open=true; bar.hidden=false;
  $("#convFindBtn").classList.add("on");
  const inp=$("#findInput");
  if(typeof q==="string") inp.value=q;
  inp.focus(); inp.select();
  if(!FIND.mo){
    // 流式输出、翻页、切会话都会改 #wrap:防抖重扫,保住当前命中
    // 流式状态行(.status「工作中 12s」)每秒走字,只有它变时不重扫;计时器和输入框的分开,互不顶掉
    FIND.mo=new MutationObserver(recs=>{
      const st=S.stream && S.stream.status;
      if(st && recs.every(r=>st.contains(r.target))) return;
      clearTimeout(FIND.moTimer); FIND.moTimer=setTimeout(()=>findRun({keep:true}), 300);
    });
  }
  FIND.mo.observe($("#wrap"), {childList:true, subtree:true, characterData:true});
  findRun({jump:opts.jump||"near"});
  // 从全局搜索点进来、已加载部分没命中:命中多半在更早的分页里,自动去拉
  if(opts.autoLoad && !FIND.hits.length) findLoadAll(true);
}
function closeFind(){
  if(!FIND.open) return;
  FIND.open=false;
  $("#findBar").hidden=true;
  $("#convFindBtn").classList.remove("on");
  if(FIND.mo) FIND.mo.disconnect();
  clearTimeout(FIND.timer); clearTimeout(FIND.moTimer);
  FIND.hits=[]; FIND.cur=-1; FIND.focus=-1; FIND.chipSig="";
  findPaint();
}

$("#convFindBtn").innerHTML=ic("search");
$("#convFindBtn").onclick=e=>{ e.stopPropagation(); FIND.open ? closeFind() : openFind(); };
$("#findPrev").innerHTML=ic("chevronUp");
$("#findNext").innerHTML=ic("chevronDown");
$("#findClose").innerHTML=ic("close");
$("#findPrev").onclick=()=>findGo(-1);
$("#findNext").onclick=()=>findGo(1);
$("#findClose").onclick=closeFind;
// 拼音输入法选字过程中框里是拼音字母(iOS 还会自动加空格,被拆成好几个英文词),这时不搜,选完字再搜
$("#findInput").oninput=e=>{
  if(e.isComposing) return;
  clearTimeout(FIND.timer); FIND.timer=setTimeout(()=>findRun({jump:"near"}), 120);
};
$("#findInput").addEventListener("compositionend", ()=>{
  clearTimeout(FIND.timer); FIND.timer=setTimeout(()=>findRun({jump:"near"}), 120);
});
$("#findInput").onkeydown=e=>{
  if(e.key==="Enter"){
    e.preventDefault();
    if(e.isComposing || e.keyCode===229) return;   // 中文输入法选词的回车不算跳转(Safari 只认 229,同 composer.js)
    clearTimeout(FIND.timer);
    // 刚改完还没等到防抖:先按新词扫一遍再跳
    if(findTerms(e.target.value).join("\u0000")!==FIND.terms.join("\u0000")) findRun({jump:"near"});
    else findGo(e.shiftKey?-1:1);
  }else if(e.key==="Escape"){ e.preventDefault(); closeFind(); }
};
