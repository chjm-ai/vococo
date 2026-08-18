"use strict";
// 2026-08-14 从 index.html 拆出(前端模块化):项目 Git 状态 / 会话列表 renderConvs / 历史预热 / 项目 / 语音任务与定时任务分组 / cron 弹窗。
// 与内联脚本同属全局作用域(无构建步骤),加载顺序见 index.html。

// ── 项目 Git 状态 ─────────────────────────────────────────────────────────
// 顶栏那颗 ⎇ 按钮:仅项目会话显示;点开弹层看改动 + 一键建分支
function gitBtnLabel(d){
  let s='⎇ '+esc(d.branch);
  if(d.unmerged) s+=' <span class="gahb">↑'+d.unmerged+'</span>';   // 未合并提交(相对 main):标题栏直接可见
  if(d.dirty) s+=' •'+d.dirty;
  if(d.added||d.removed){
    s+=' <span style="color:#3fb950">+'+d.added+'</span>'
      +' <span style="color:#f85149">-'+d.removed+'</span>';
  }
  return s;
}
async function refreshGit(conv){
  const btn=$("#convGit"), pbtn=$("#convProjName");
  $("#gitPop").hidden=true; $("#projPop").hidden=true;
  btn.hidden=false; btn.classList.remove("dirty"); btn.textContent="⎇ …"; pbtn.hidden=true;
  try{
    const r=await api("/conv/git?conv="+encodeURIComponent(conv)); const d=await r.json();
    if(S.conv!==conv) return;                                      // 期间切走了,丢弃结果
    S.git=d;
    if(!d.is_project){ btn.hidden=true; return; }
    pbtn.textContent=d.name; pbtn.hidden=false;
    if(!d.is_repo){ btn.textContent="⎇ 非 git 仓库"; return; }
    btn.innerHTML=gitBtnLabel(d); btn.classList.toggle("dirty", (d.dirty>0)||(d.unmerged>0));
  }catch(e){ btn.hidden=true; pbtn.hidden=true; }
}
// 项目名 pill:点开看完整路径(project_path 是仓库根,不是本会话的 worktree cwd)
function renderProjPop(){
  const d=S.git, pop=$("#projPop");
  if(!d || !d.is_project){ pop.hidden=true; return; }
  pop.innerHTML='<div class="ppath">'+esc(d.project_path||d.path||"")+'</div>';
}
$("#convProjName").onclick=e=>{ e.stopPropagation(); const p=$("#projPop");
  if(p.hidden){ renderProjPop(); p.hidden=false; } else p.hidden=true; };
document.addEventListener("click", e=>{ const b=$("#projBox"); if(b && !b.contains(e.target)) $("#projPop").hidden=true; });
// 默认分支名:vococo/月日-时分,方便一次会话开一条隔离分支
function defaultBranchName(){ const d=new Date(), p=n=>String(n).padStart(2,"0");
  return "vococo/"+p(d.getMonth()+1)+p(d.getDate())+"-"+p(d.getHours())+p(d.getMinutes()); }
function renderGitPop(){
  const d=S.git, pop=$("#gitPop");
  if(!d || !d.is_project){ pop.hidden=true; return; }
  if(!d.is_repo){ pop.innerHTML='<div class="gph"><span class="gbr">'+esc(d.name)+'</span></div>'+
    '<div class="gempty">该项目目录不是 git 仓库</div>'; return; }
  const ah=[]; if(d.ahead) ah.push("↑"+d.ahead); if(d.behind) ah.push("↓"+d.behind);
  const files = d.dirty
    ? '<div class="gfiles">'+d.files.map(f=>'<div class="gf"><span class="gx">'+esc((f.x||"").trim()||"?")+'</span><span class="gp">'+esc(f.path)+'</span></div>').join("")+'</div>'
    : '<div class="gclean">✓ 工作区干净</div>';
  pop.innerHTML=
    '<div class="gph"><span class="gbr">⎇ '+esc(d.branch)+'</span>'+(ah.length?'<span class="gah">'+ah.join(" ")+'</span>':'')+'</div>'+
    '<div class="gsub">'+esc(d.path)+'</div>'+
    files+
    '<div class="gnew"><input id="gitBrName" type="text" placeholder="新分支名" value="'+esc(defaultBranchName())+'"><button id="gitBrGo" type="button">新建并切换</button></div>'+
    '<div class="gerr" id="gitErr" hidden></div>';
  $("#gitBrGo").onclick=createGitBranch;
  $("#gitBrName").onkeydown=e=>{ if(e.key==="Enter") createGitBranch(); };
}
async function createGitBranch(){
  const name=$("#gitBrName").value.trim(), err=$("#gitErr"), btn=$("#gitBrGo");
  err.hidden=true;
  if(!name){ err.textContent="请填写分支名"; err.hidden=false; return; }
  btn.disabled=true; btn.textContent="创建中…";
  try{
    const r=await api("/conv/git/branch",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({conv:S.conv, name})});
    const d=await r.json();
    if(!r.ok){ err.textContent=d.error||"创建失败"; err.hidden=false; btn.disabled=false; btn.textContent="新建并切换"; return; }
    S.git=d; renderGitPop();                                       // 重绘:分支名已切到新分支
    const b=$("#convGit"); b.innerHTML=gitBtnLabel(d); b.classList.toggle("dirty", (d.dirty>0)||(d.unmerged>0));
  }catch(e){ err.textContent="网络错误"; err.hidden=false; btn.disabled=false; btn.textContent="新建并切换"; }
}
$("#convGit").onclick=e=>{ e.stopPropagation(); const p=$("#gitPop");
  if(p.hidden){ renderGitPop(); p.hidden=false; } else p.hidden=true; };
document.addEventListener("click", e=>{ const b=$("#gitBox"); if(b && !b.contains(e.target)) $("#gitPop").hidden=true; });

// ── 会话列表 ─────────────────────────────────────────────────────────────
function fmtTime(ts){ if(!ts) return ""; const d=new Date(ts*1000), n=new Date();
  const sameDay=d.toDateString()===n.toDateString();
  return sameDay ? d.toTimeString().slice(0,5) : (d.getMonth()+1)+"/"+d.getDate(); }
async function loadPrefs(){
  try{
    const r = await api("/prefs"); const p = await r.json();
    // 服务端有明确值才覆盖本地;null = 不动(本地 localStorage 是当前设备的主)
    if(p.theme && ["light","dark"].includes(p.theme) && p.theme !== localStorage.getItem("vococo_theme")){
      document.documentElement.setAttribute("data-theme", p.theme);
      try{ localStorage.setItem("vococo_theme", p.theme); }catch(e){}
      syncTheme();
    }
    if(p.conv_filter) S.convFilter = p.conv_filter;
  }catch(e){}
}
// 用一份 /conversations 响应数据重建 S.convs(供 loadConvs() 与登录时的首次拉取共用,
// 登录那次不用再多打一次 /conversations)。
function applyConvs(d){
  const main = d.main; main.conv="main"; main.title="主会话"; main.pinned=true;
  // 新建但还没发出第一条消息的本地会话(conv=local-xxx)后端还不认识,不在返回列表里;
  // 合并时保留它,否则录音/转写等待期间只要有别的会话触发这次刷新,这条新会话就会被冲没
  const locals = S.convs.filter(c=>String(c.conv).startsWith("local-"));
  // 全部会话(含主会话)按最近活跃排序,最新的排最前面
  S.convs = [main, ...d.conversations, ...locals].sort((a,b)=>(b.last_ts||0)-(a.last_ts||0));
  renderConvs();   // 会话刷新后同步标题栏
}
async function loadConvs(){
  const r = await api("/conversations"); const d = await r.json();
  applyConvs(d);
  idbSet("convs", d);
  prefetchHistories();
}
// ── 后台预热最近会话的历史 ────────────────────────────────────────────────
// 刷新/新开页面后,IndexedDB 里可能没有(或已过期)最近会话的历史,第一次点开要等一趟
// 跨境网络(重会话 gzip 后 70~145KB,实测 4s+)。这里在首屏安顿好之后,悄悄把最近几个
// 会话的 /history 拉进缓存——配合服务端 ETag/304,已缓存且没变的只花一个空包,成本很低。
// 每次页面加载只跑一轮;串行拉,别跟当前会话/SSE 抢那 ~50KB/s 的隧道带宽。
var _prefetchedHist = false;
async function prefetchHistories(){
  if(_prefetchedHist) return;
  _prefetchedHist = true;
  await new Promise(r=>setTimeout(r, 2500));   // 让当前会话历史 + SSE 先就位
  const tops = S.convs.filter(c=>c.turns>0 && !String(c.conv).startsWith("local-")).slice(0,5);
  for(const c of tops){
    if(S.conv===c.conv) continue;              // 正开着的会话 openConv 自己拉过了
    try{
      const r=await api("/history?conv="+encodeURIComponent(c.conv));
      const d=await r.json();
      S.histCache[c.conv]=d.turns;
      idbSetHist(c.conv, d.turns);
    }catch(e){ break; }                        // 网络不给力就收手,下次刷新再预热
  }
}
// 从 conv id 解析它属于哪个项目:p<hash>:<convid> → hash;否则 null(默认项目)
function convProject(conv){
  const m = String(conv).replace(/^local-/,"").match(/^p([0-9a-f]+):/);
  return m ? m[1] : null;
}
// 取所有置顶行(含普通会话与语音任务;不含主会话——主会话独立渲染,不归置顶分组)。
// 返回 {ts,build} 而非会话对象本身,因为语音任务要用 buildVoiceTaskRow 渲染,跟
// 普通会话的 buildConvRow 不是同一个函数(2026-07-29:语音任务开放置顶后补上,
// 否则点了置顶按钮却不会真的挪进置顶区,是个死功能)。
function pinnedConvs(inCall){
  const convItems = S.convs.filter(c=>c.pinned && c.conv!=="main").map(c=>({ts:c.last_ts||0, build:()=>buildConvRow(c, inCall)}));
  const taskItems = ((S.voiceSidebar&&S.voiceSidebar.tasks)||[]).filter(t=>t.pinned).map(t=>({ts:t.last_ts||0, build:()=>buildVoiceTaskRow(t, inCall)}));
  return [...convItems, ...taskItems].sort((a,b)=>(b.ts||0)-(a.ts||0));
}
function convsInGroup(hash){
  return S.convs.filter(c => c.conv!=="main" && !c.pinned && convProject(c.conv)===hash);
}
// 分组的存储键:默认项目用固定串,项目用其 hash
function grpKey(hash){ return hash===null ? "__default__" : hash; }
function saveExpanded(){ try{ localStorage.setItem("vococo_expanded", JSON.stringify([...S.expanded])); }catch(e){} }
function loadExpanded(){ try{ S.expanded=new Set(JSON.parse(localStorage.getItem("vococo_expanded")||"[]")); }catch(e){ S.expanded=new Set(); } }

function projName(hash){
  if(!hash) return "默认项目";
  const p=(S.projects||[]).find(x=>x.hash===hash);
  return p ? p.name : "默认项目";
}
// 项目选择胶囊只在草稿态(local- 会话,还没发过消息)出现:发出第一条后这会话已归属到
// 侧栏对应项目分组,不再需要重复标记(见 send()/sendCmd() 里转正后的 renderProjSelChip 调用)
function renderProjSelChip(){
  const box=$("#projSel"); if(!box) return;
  const show = String(S.conv||"").startsWith("local-");
  box.hidden = !show;
  if(show) $("#projSelName").textContent = projName(S.project);
}
function renderProjSelPop(){
  const pop=$("#projSelPop"), cur=S.project;
  const items=[{hash:null,name:"默认项目"}, ...(S.projects||[])];
  pop.innerHTML=items.map(p=>
    '<button type="button" class="mi'+(p.hash===cur?" on":"")+'" data-h="'+esc(p.hash||"")+'">'+
    '<span class="ml">'+esc(p.name)+'</span>'+(p.hash===cur?'<span class="mk">✓</span>':'')+'</button>'
  ).join("");
  pop.querySelectorAll(".mi").forEach(b=>{ b.onclick=()=>pickDraftProject(b.dataset.h||null); });
}
// 切换草稿会话归属的项目:conv id 里的 p<hash>: 前缀重新拼一遍,内容(时间戳+随机串)不变
function pickDraftProject(hash){
  $("#projSelPop").hidden=true;
  if(!String(S.conv||"").startsWith("local-")) return;  // 安全兜底:已转正的会话不可切项目
  if(hash===S.project) return;
  const idPart = S.conv.replace(/^local-(?:p[0-9a-f]+:)?/, "");
  const newConv = "local-"+(hash?("p"+hash+":"):"")+idPart;
  const idx = S.convs.findIndex(x=>x.conv===S.conv);
  if(idx>=0) S.convs[idx]={...S.convs[idx], conv:newConv};
  S.project=hash; S.conv=newConv;
  S.expanded.add(grpKey(hash)); saveExpanded();
  renderConvs(); renderProjSelChip();
}
$("#projSelBtn").onclick=e=>{ e.stopPropagation(); const p=$("#projSelPop");
  if(p.hidden){ renderProjSelPop(); p.hidden=false; } else p.hidden=true; };
document.addEventListener("click", e=>{ const b=$("#projSel"); if(b && !b.contains(e.target)) $("#projSelPop").hidden=true; });

function convsInGroup(hash){
  return S.convs.filter(c => c.pinned ? hash===null : convProject(c.conv)===hash);
}
// 语音通话主行(通话入口),与主会话一起置顶在根目录最前(不属于任何分组,数据来自 /voice/sidebar)。
// 通话视图开着时不看 S.conv(那还停在离开前的聊天会话上没变),由调用方传进来的 inCall
// (= #callView 是否显示)决定「语音通话」这一行是否高亮成"你现在就在这儿"。
function buildVoiceMainRow(inCall){
  // 主会话是固定入口,不能依赖 /voice/sidebar 请求成功才显示。该请求只负责
  // 后台任务元数据;网络失败时仍必须保留入口,让用户至少能继续发消息。
  const mainRow=el("div","conv voicemain"+(inCall?" active":""));
  const mainBody=el("div","cvbody");
  mainBody.innerHTML=ic("mic");
  const mainCt=el("div","ct"); mainCt.textContent="主会话"; mainBody.append(mainCt);
  // 主行不显示时刻(2026-08-04:与其他会话行区分,保持简洁)
  mainRow.append(mainBody);
  mainRow.onclick=()=>{ openCallView(); };
  return mainRow;
}
// 语音后台任务的单条行(与普通会话行同构);archived 筛选不通过时返回 null。
// 拆成单条构建是为了跟普通会话按时间混排(见 renderConvs 里默认项目分组的合并排序)。
// 归档/删除按钮 + 左滑手势:普通会话行(非置顶)与语音任务行共用的一段
// (2026-07-23 从 buildConvRow/buildVoiceTaskRow 里近乎逐行重复的代码收口)
function bindRowSwipeActions(row, body, conv, archived){
  const act=el("div","cvact");
  const archBtn=el("button","cvarch"); archBtn.innerHTML=ic("folder"); archBtn.title=archived?"取消归档":"归档";
  archBtn.onclick=ev=>{ ev.stopPropagation(); closeSwipe(row); toggleArchive(conv); };
  const del=el("button","cvdel"); del.innerHTML=ic("trash"); del.title="删除";
  del.onclick=ev=>{ ev.stopPropagation(); closeSwipe(row); delConv(conv); };
  // 置顶按钮:语音任务改名 task: 前缀后不再靠字符串猜类型排除——2026-07-29 起语音任务
  // 也支持置顶(后端 pinned 字段本就通用,之前只是前端没开放入口)
  const convObj=findConv(conv);
  const isPinned=!!(convObj&&convObj.pinned);
  const pinBtn=el("button","cvpin"); pinBtn.innerHTML=ic("pin"); pinBtn.title=isPinned?"取消置顶":"置顶";
  pinBtn.onclick=ev=>{ ev.stopPropagation(); closeSwipe(row); pinConv(conv, !isPinned); };
  act.append(archBtn,pinBtn,del);
  row.append(act);
  bindSwipe(row, body, act);
}
function buildVoiceTaskRow(t, inCall){
  const arch=!!t.archived;
  if(S.convFilter==="archived"&&!arch) return null;
  if(S.convFilter==="active"&&arch) return null;
  const row=el("div","conv ingroup"+(!inCall && t.conv===S.conv?" active":""));
  row.dataset.conv=t.conv;
  const body=el("div","cvbody");
  // 任务还在跑(queued/running)→ 复用普通会话行的闪烁圆点;终态未读 → 灰点,跟
  // 普通会话/定时任务行同一套 pending_review 语义(2026-07-29:推翻 ab77594 当时
  // "终态不挂点"的简化决定,统一三类侧栏行的完成态标记)。
  if(S.live[t.conv]){
    const dot=el("span","livedot"); dot.title="AI 正在回复中"; body.append(dot);
  } else if(t.task_status==="queued"||t.task_status==="running"){
    const dot=el("span","livedot"); dot.title=t.task_status==="running"?"任务进行中":"排队中"; body.append(dot);
  } else if(t.pending_review || S.pendingReview[t.conv]){
    const dot=el("span","reviewdot"); dot.title="有新内容"; body.append(dot);
  }
  const ct=el("div","ct"); ct.textContent=t.title||"新对话"; body.append(ct);
  const tm=fmtTime(t.last_ts); if(tm){ const tmEl=el("span","ctime"); tmEl.textContent=tm; body.append(tmEl); }
  // 任务还在跑(queued/running)→ 行内直接给「停止」按钮,点一下真停(调 /voice/tasks/cancel,
  // 跟语音喊停 voice_cancel_task 同一套逻辑);终态任务不显示
  if(t.task_status==="queued"||t.task_status==="running"){
    const stop=el("button","cvstop"); stop.innerHTML=ic("stop"); stop.title="停止任务";
    stop.onclick=ev=>{ev.stopPropagation();stopVoiceTask(t);};
    body.append(stop);
  }
  const more=el("button","more"); more.textContent="⋯"; more.title="更多";
  more.onclick=ev=>{ev.stopPropagation();openConvMenu(more,t.conv);}; body.append(more);
  row.append(body);
  bindRowSwipeActions(row, body, t.conv, arch);
  if(t.conv===S.swipedConv) row.classList.add("swiped");
  row.onclick=()=>{
    if(row._justSwiped){ row._justSwiped=false; return; }
    if(row.classList.contains("swiped")){ closeSwipe(row); return; }
    openConv(t.conv);
  };
  return row;
}
// 单条会话行(主会话与普通会话/置顶会话通用)。主会话独立置顶且不提供菜单/滑动手势;
// 置顶会话除了所在分组不同,长相(缩进、状态灯)跟普通分组内的会话行完全一样。
function buildConvRow(c, inCall){
  const isMain=c.conv==="main";
  const e=el("div","conv"+(isMain?"":" ingroup")+(!inCall && c.conv===S.conv?" active":""));
  e.dataset.conv=c.conv;
  const body=el("div","cvbody");
  if(isMain) body.innerHTML=ic("star");   // 主会话:左侧星标图标,跟紧邻的语音通话行(mic 图标)对齐
  if(S.live[c.conv]){ const dot=el("span","livedot"); dot.title="AI 正在回复中"; body.append(dot); }
  else if(c.pending_review || S.pendingReview[c.conv]){ const dot=el("span","reviewdot"); dot.title="有新内容"; body.append(dot); }   // 完成未读:灰色圆点
  const ct=el("div","ct"); ct.textContent=c.title||"新对话"; body.append(ct);
  if(!isMain){ const tm=fmtTime(c.last_ts); if(tm){ const tmEl=el("span","ctime"); tmEl.textContent=tm; body.append(tmEl); } }   // 会话时刻:名称右侧(主会话不显示)
  if(!isMain){ const more=el("button","more"); more.textContent="⋯"; more.title="更多"; more.onclick=ev=>{ev.stopPropagation();openConvMenu(more,c.conv);}; body.append(more); }
  e.append(body);
  if(!isMain){
    bindRowSwipeActions(e, body, c.conv, c.archived);   // 触屏:左滑露出归档/删除,不用长按(iOS 用户少用长按)
    if(c.conv===S.swipedConv) e.classList.add("swiped");   // 重建 DOM 后补回展开状态,别被后台事件触发的重渲染冲掉
  }
  e.onclick=()=>{
    if(e._justSwiped){ e._justSwiped=false; return; }   // 刚滑动完的这次 click 不触发切换
    if(e.classList.contains("swiped")){ closeSwipe(e); return; }   // 已展开:点一下先收起
    openConv(c.conv);
  };
  return e;
}
const CONV_SHOW_MAX = 7;   // 每个项目分组默认最多展示的会话数,超出折进「展开更多」
                          // (2026-08-04 主人确认 5→7:并发上限放开到 7 后列表也该能展示 7 条;
                          //  终态任务只在语音界面顶部状态条满 10 分钟自动隐藏(见 taskBarDoneHidden),
                          //  侧边栏/最近列表全量可见;老任务时间久了自然沉底,不霸占首屏)
const TAB_PAGE_SIZE = 20;   // 「置顶」「最近」Tab 分页粒度:默认 20 条,点「更多」每次再加 20 条
const SIDE_TABS = [{key:"projects",label:"项目"},{key:"cron",label:"定时"},{key:"pinned",label:"置顶"},{key:"recent",label:"最近"}];
// 侧栏第二层 Tab:项目/定时/置顶/最近,切 Tab 记住选择,不影响下方各分组自己的展开态
function renderSideTabs(box){
  const bar=el("div","sidetabs");
  for(const t of SIDE_TABS){
    const b=el("div","sidetab"+(S.sideTab===t.key?" active":"")); b.textContent=t.label;
    b.onclick=()=>{
      if(S.sideTab===t.key) return;
      S.sideTab=t.key; try{ localStorage.setItem("vococo_sidetab", t.key); }catch(e){}
      renderConvs();
    };
    bar.append(b);
  }
  box.append(bar);
}
function sideTabEmpty(text){ const e=el("div","tabempty"); e.textContent=text; return e; }
// 「置顶」Tab:扁平列表(不再是可折叠分组,Tab 本身就是入口),默认 20 条,点「更多」每次 +20
function renderPinnedTab(box, inCall){
  const pc=pinnedConvs(inCall);
  if(!pc.length){ box.append(sideTabEmpty("暂无置顶会话")); return; }
  const shownN=Math.min(S.tabShown.pinned, pc.length);
  for(const it of pc.slice(0, shownN)){ const r=it.build(); if(r) box.append(r); }
  if(pc.length > shownN){
    const more=el("div","conv ingroup convmore");
    const mct=el("div","ct"); mct.textContent="更多"; more.append(mct);
    more.onclick=()=>{ S.tabShown.pinned += TAB_PAGE_SIZE; renderConvs(); };
    box.append(more);
  }
}
// 「最近」Tab:汇总所有项目(含语音任务)的会话,按最后活跃时间混排,不看归属项目、数量不封顶,
// 默认 20 条,点「更多」每次 +20(置顶与最近是正交维度,置顶项目若时间够新也会出现在这里)
function renderRecentTab(box, inCall){
  const passesArchFilter = arch => !(S.convFilter==="archived"&&!arch) && !(S.convFilter==="active"&&arch);
  const taskItems = ((S.voiceSidebar&&S.voiceSidebar.tasks)||[]).map(t=>({ts:t.last_ts||0, build:()=>buildVoiceTaskRow(t, inCall)}));
  const convItems = S.convs.filter(c=>c.conv!=="main" && passesArchFilter(!!c.archived)).map(c=>({ts:c.last_ts||0, build:()=>buildConvRow(c, inCall)}));
  const rows = [...taskItems, ...convItems].sort((a,b)=>(b.ts||0)-(a.ts||0));
  if(!rows.length){ box.append(sideTabEmpty("暂无最近会话")); return; }
  const shownN=Math.min(S.tabShown.recent, rows.length);
  for(const it of rows.slice(0, shownN)){ const r=it.build(); if(r) box.append(r); }
  if(rows.length > shownN){
    const more=el("div","conv ingroup convmore");
    const mct=el("div","ct"); mct.textContent="更多"; more.append(mct);
    more.onclick=()=>{ S.tabShown.recent += TAB_PAGE_SIZE; renderConvs(); };
    box.append(more);
  }
}
// 「定时」Tab:两类任务各自一个可折叠分组:
// 1) VOCOCO 定时任务:在 vococo 内创建/编辑/启停,有专属会话和运行记录;
// 2) 本机系统任务:Mac 的 launchd/crontab,只读展示脚本与状态。
// 默认展开 VOCOCO、收起本机任务;折叠状态记在 localStorage,刷新后保持。
function buildCronGroupHeader(key, title){
  const open=!!S.cronGroups[key];
  const h=el("div","projgrp crongrp");
  h.setAttribute("role","button");
  h.tabIndex=0;
  h.setAttribute("aria-expanded",String(open));
  const nm=el("span","pgname"); nm.textContent=title; h.append(nm);
  const caret=el("span","pgcaret chev"+(open?" down":"")); h.append(caret);
  const toggle=()=>{
    S.cronGroups[key]=!S.cronGroups[key];
    localStorage.setItem(`vococo_cron_group_${key}`,S.cronGroups[key]?"1":"0");
    renderConvs();
  };
  h.onclick=toggle;
  h.onkeydown=ev=>{ if(ev.key==="Enter"||ev.key===" "){ ev.preventDefault(); toggle(); } };
  return h;
}
function renderCronGroupRows(box, rows, key){
  const shown=S.moreShown.has(key)?rows:rows.slice(0,CONV_SHOW_MAX);
  for(const r of shown) box.append(r);
  if(rows.length>shown.length){
    const more=el("div","conv ingroup convmore");
    const mct=el("div","ct"); mct.textContent="展开更多"; more.append(mct);
    more.onclick=()=>{ S.moreShown.add(key); renderConvs(); };
    box.append(more);
  }
}
function renderCronTab(box, inCall){
  const jobs=S.cronJobs||[];
  const systemTasks=S.systemTasks||[];

  // vococo 自己管理的任务:可编辑、可启停、可进入专属会话查看历史。
  box.append(buildCronGroupHeader("managed","定时任务"));
  if(S.cronGroups.managed){
    if(jobs.length) renderCronGroupRows(box,jobs.map(buildCronJobRow),"__cron_managed__");
    else box.append(sideTabEmpty("暂无定时任务"));
    const add=el("div","projgrp projadd"); add.textContent="＋ 新建定时任务…";
    add.onclick=()=>openCronModal(null); box.append(add);
  }

  // Mac launchd/crontab 任务:只读查看,增删改仍在系统配置里完成。
  if(systemTasks.length){
    box.append(buildCronGroupHeader("system","本机系统任务"));
    if(S.cronGroups.system){
      for(const t of systemTasks) box.append(buildSystemTaskRow(t));
    }
  }
}
// 「项目」Tab:手风琴——默认项目 + 每个项目一个可折叠分组,展开后列出其会话;末尾一行「新建项目」
function renderProjectsTab(box, inCall){
  const groups=[{hash:null,name:"默认项目"}].concat(S.projects.map(p=>({hash:p.hash,name:p.name})));
  for(const g of groups) renderProjGroup(box, g, inCall);
  const addp=el("div","projgrp projadd"); addp.textContent="＋ 新建项目…"; addp.onclick=openProjModal; box.append(addp);
}
function renderConvs(){
  const inCall = !$("#callView").hidden;  // 统一对话视图打开时,侧栏只高亮「对话」入口
  // 顶部固定区只保留新对话按钮和统一对话入口,不再显示重复的主会话行。
  $("#newBtn").innerHTML = '<div class="cvbody">'+ic("edit")+'<div class="ct">新对话</div></div>';
  const top=$("#convTopRows"); top.innerHTML="";
  const vm=buildVoiceMainRow(inCall); if(vm) top.append(vm);
  // 列表区:Tab 栏固定(#sideTabs),只有分组内容(#convBody)滚动;首次渲染会清掉骨架行
  const tabs=$("#sideTabs"); tabs.innerHTML=""; renderSideTabs(tabs);
  const box=$("#convBody"); box.innerHTML="";
  if(S.sideTab==="cron") renderCronTab(box, inCall);
  else if(S.sideTab==="pinned") renderPinnedTab(box, inCall);
  else if(S.sideTab==="recent") renderRecentTab(box, inCall);
  else renderProjectsTab(box, inCall);
  // 同步标题栏:loadConvs/loadVoiceSidebar/loadCronSidebar 刷新各自列表后都会调 renderConvs,
  // 标题(含改名)可能已更新。语音任务/定时任务会话不在 S.convs 里(那是 /conversations 拉的),
  // 查找逻辑跟 openConv 保持一致——否则在任务自己的会话里改名,顶部标题栏不会跟着刷新
  // (侧栏那行没事,因为它是整个重建的;标题栏是这里单独同步的,漏了这几个来源就会显示旧名字)。
  const activeConv=S.convs.find(c=>c.conv===S.conv)
    || (S.voiceSidebar.main && S.voiceSidebar.main.conv===S.conv ? S.voiceSidebar.main : null)
    || S.voiceSidebar.tasks.find(x=>x.conv===S.conv)
    || S.cronJobs.find(x=>x.conv===S.conv);
  if(activeConv) $("#convTitle").textContent=activeConv.title||"新对话";
}

// 渲染单个项目分组(手风琴头 + 展开后的会话行);置顶节与常规节共用同一份逻辑。
function renderProjGroup(box, g, inCall){
  const convs=convsInGroup(g.hash);
  const open=S.expanded.has(grpKey(g.hash));
  const h=el("div","projgrp");
  const nm=el("span","pgname"); nm.textContent=g.name; h.append(nm);
  const caret=el("span","pgcaret chev"+(open?" down":"")); h.append(caret);   // 折线箭头,展开时旋转朝下
  if(g.hash!==null){
    const more=el("button","pgmore"); more.textContent="⋯"; more.title="更多";
    more.onclick=ev=>{ ev.stopPropagation(); openProjMenu(more, g.hash); }; h.append(more);
  }
  const add=el("button","pgadd"); add.textContent="＋"; add.title="在此项目下新建会话";
  add.onclick=ev=>{ ev.stopPropagation(); newChatIn(g.hash); }; h.append(add);
  if(g.hash!==null){
    bindProjDrag(h, g.hash);   // 默认项目/末尾「新建项目」不可拖,仅真实项目分组可排序
  }
  h.onclick=()=>{ if(S.dragMoved){ S.dragMoved=false; return; } toggleGroup(g.hash); };  // 拖拽落地那一下别顺带触发展开/收起
  box.append(h);
  if(!open) return;
  let rows;
  if(g.hash===null){
    // 默认项目:语音后台任务不区分项目,统一落这里,跟普通会话按最后活跃时间混排
    // 再截前 CONV_SHOW_MAX 条——避免"语音任务永远排最前"把新会话挤出首屏
    // (2026-07-22 反馈:几十条老语音任务霸占默认项目前 5 条,当天新会话反而看不见)。
    const passesArchFilter = arch => !(S.convFilter==="archived"&&!arch) && !(S.convFilter==="active"&&arch);
    // 已置顶的语音任务归"置顶"分组,这里要滤掉,不然置顶了还继续在默认项目里重复出现
    const taskItems = ((S.voiceSidebar&&S.voiceSidebar.tasks)||[]).filter(t=>!t.pinned).map(t=>({ts:t.last_ts||0, build:()=>buildVoiceTaskRow(t, inCall)}));
    const convItems = convs.filter(c=>!c.pinned && passesArchFilter(!!c.archived)).map(c=>({ts:c.last_ts||0, build:()=>buildConvRow(c, inCall)}));
    rows = [...taskItems, ...convItems].sort((a,b)=>b.ts-a.ts).map(it=>it.build()).filter(Boolean);
  } else {
    rows=[];
    for(const c of convs){
      if(c.pinned) continue;
      const arch=!!c.archived;
      if(S.convFilter==="archived"&&!arch)continue;
      if(S.convFilter==="active"&&arch)continue;
      rows.push(buildConvRow(c, inCall));
    }
  }
  // 每组默认最多展示 CONV_SHOW_MAX 条,余下折进「展开更多」(点开后本次会话内保持展开)
  const shown = S.moreShown.has(grpKey(g.hash)) ? rows : rows.slice(0, CONV_SHOW_MAX);
  for(const r of shown) box.append(r);
  if(rows.length > shown.length){
    const k=grpKey(g.hash);
    const more=el("div","conv ingroup convmore");
    const mct=el("div","ct"); mct.textContent="展开更多"; more.append(mct);
    more.onclick=()=>{ S.moreShown.add(k); renderConvs(); };
    box.append(more);
  }
}

// ── 项目 ─────────────────────────────────────────────────────────────────
async function loadProjects(){
  try{ const r=await api("/projects"); S.projects=(await r.json()).projects||[]; }
  catch(e){}  // 保留最后一次成功数据，离线状态由同步标记明确提示
}

// ── 语音任务:侧边栏固定分组(主语音会话 + 各后台任务会话)────────────────────
// 「终态任务满 10 分钟自动隐藏」只作用于语音界面顶部的任务状态条(renderTaskBar):
// 顶部常驻横幅最多 7 条,终态(done/failed/cancelled)任务挂满 10 分钟自动消失、
// 不再占位置——隐藏≠删除,数据还在,任务抽屉/侧边栏/最近列表照常全量可见。
const DONE_HIDE_SEC = 600;
// 活跃(未终态)状态集合,任务状态条与侧边栏共用(2026-08-08 引入):后台任务
// (queued/running)与 SDK 待办(pending/in_progress/paused)两套状态一份定义,
// isTaskDone/taskBarDoneHidden/scheduleDoneHide 都按它判断。
// 必须放顶层:taskBarDoneHidden/scheduleDoneHide 是顶层函数,引用 IIFE 内常量会
// ReferenceError(2026-08-10 事故:任务栏整体渲染不出来、pageerror 刷屏)。
const TASK_ACTIVE_STATUSES = ["queued","pending","running","in_progress","paused"];
// 终态判定与状态条一致:非「排队中/进行中」即终态。updated_at 是任务终态落库时间
// (秒级时间戳),缺时间戳(幽灵数据)不隐藏。
function taskBarDoneHidden(t){
  if(TASK_ACTIVE_STATUSES.includes(t.status)) return false;  // 活跃任务(含 SDK in_progress/paused)永不隐藏
  const ts = t.updated_at ?? t.created_at;
  if(!ts) return false;
  return (Date.now()/1000 - ts) > DONE_HIDE_SEC;
}
// 「满 10 分钟自动隐藏」的到点调度:算状态条里最近一个还没到期的终态任务还剩多久,
// 到点触发 renderTaskBar 让 taskBarDoneHidden 真正把它滤掉(渲染过滤是主机制,
// 定时器只是保证"到点真的消失",页面挂后台也会在到点那刻触发)。
let _doneHideTimer = null;
function scheduleDoneHide(){
  if(_doneHideTimer){ clearTimeout(_doneHideTimer); _doneHideTimer=null; }
  const now=Date.now()/1000;
  const soonest = [...barTasks.values()]
    .map(t=>{
      if(TASK_ACTIVE_STATUSES.includes(t.status)) return null;
      const ts = t.updated_at ?? t.created_at;
      if(!ts) return null;
      const remain = DONE_HIDE_SEC - (now - ts);
      return remain > 0 ? remain : null;
    })
    .filter(v=>v!==null)
    .sort((a,b)=>a-b)[0];
  if(soonest!==undefined){
    _doneHideTimer=setTimeout(()=>{ window.refreshTaskBar?.(); }, Math.max(1000, soonest*1000));
  }
}
async function loadVoiceSidebar(){
  try{ const r=await api("/voice/sidebar"); S.voiceSidebar=await r.json(); }
  catch(e){}  // 不能把旧数据清空伪装成“服务端没有任务”
  S.voiceSidebarLoaded=true;   // 拉完(成功/失败)都算到位,「语音通话」骨架行退场
  renderConvs();
  if($("#empty").style.display==="flex") renderEmptyRecent();  // 欢迎屏正开着 → 语音任务到位后补一次最近对话
  refillCurrentMeta();   // 打开中的会话可能是语音主会话/任务:数据到位后补一次模型回填
}
// 侧边栏「语音任务」行上的停止按钮:调 /voice/tasks/cancel(跟语音喊停
// voice_cancel_task 同一套 cancel 逻辑),排队中/运行中都能停,停完刷新侧栏状态点
async function stopVoiceTask(t){
  if(!confirm(`确定要停止任务「${t.title||""}」吗?已做完的部分不会丢,只是不再继续。`)) return;
  let j;
  try{ const r=await api("/voice/tasks/cancel",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({conv:t.conv})}); j=await r.json(); }
  catch(e){ alert("操作失败"); return; }
  if(!j.ok){ alert(j.message||"取消失败"); return; }
  await loadVoiceSidebar();  // 停掉后状态点/停止按钮随之消失
}
// SSE task_update → 同步侧边栏任务行的状态点:状态没变(纯进度文案)不重绘,
// 新派发的任务侧栏还没有这行则整个重拉一次
function syncSidebarTaskStatus(t){
  const row=(S.voiceSidebar.tasks||[]).find(x=>x.conv==="task:"+t.id);
  if(!row){ loadVoiceSidebar(); return; }
  if(row.task_status===t.status) return;
  row.task_status=t.status; renderConvs();
}
// ── 定时任务:侧边栏固定分组(每个任务一条专属会话)────────────────────────────
async function loadCronSidebar(){
  try{ const r=await api("/cron/sidebar"); S.cronJobs=(await r.json()).jobs||[]; }
  catch(e){}  // 请求失败时保留上次成功列表，并由“服务不可达”状态说明
  renderConvs();
  syncCronHeader();   // 正开着某条任务会话时,启停开关的状态要跟上最新数据
  refillCurrentMeta();   // 同 loadVoiceSidebar:数据到位后补一次模型回填
}
// 「定时」Tab 里的「本机系统任务」区块:本机 launchd/crontab 里真正带调度周期的任务
// (只读,见 web.py /system/tasks、cron/system_tasks.py 模块头的识别标准),跟上面
// vococo 自己管的 cron 任务是两回事——这里纯展示"我以为在跑的脚本是不是真的还在跑"。
async function loadSystemTasks(){
  try{
    const r=await api("/system/tasks"); const d=await r.json();
    S.systemHostname=d.hostname||""; S.systemTasks=d.tasks||[];
  }catch(e){}  // 同上,失败保留上次成功列表
  renderConvs();
}
// 标题栏右侧的任务操作区(启停开关/编辑/「⋯」):只在 cron-task 会话显示,开关状态取自 S.cronJobs
function syncCronHeader(){
  // 直接按 conv 精确匹配 S.cronJobs(不再靠前缀字符串猜——2026-07-29 起 cron 任务
  // 跟语音/chat 后台任务共用同一个 task: 前缀,前缀本身分不出是不是 cron 任务了)。
  const job=(S.cronJobs||[]).find(x=>x.conv===S.conv) || null;
  const hide=!job;
  $("#convCronToggle").hidden=$("#convCronEditBtn").hidden=$("#convCronMore").hidden=hide;
  if(job){
    $("#convCronToggle").classList.toggle("on", !!job.enabled);
    $("#convCronToggle").title=job.enabled?"已启用 · 点击停用":"已停用 · 点击启用";
  }
}
// 标题栏最右侧的「⋯」:普通会话(非主会话、非定时任务——那个已有专属按钮)才显示,
// 点开跟侧栏列表同一套 openConvMenu(置顶/归档/删除)。直接按 conv 精确匹配 S.cronJobs
// (不再靠前缀字符串猜,理由同 syncCronHeader——task: 前缀统一后猜不出类型了)。
function syncMoreHeader(){
  const conv=String(S.conv||"");
  const isCron=(S.cronJobs||[]).some(x=>x.conv===conv);
  $("#convMoreBtn").hidden = !conv || conv==="main" || isCron;
}
// 单条定时任务行:点进去看该任务的历次运行记录(它就是一条普通会话);「⋯」跟普通
// 会话行同一套 openConvMenu,点开是编辑/启停/删除(见 openConvMenu 里的 isCron 分支)
function buildCronJobRow(j){
  const row=el("div","conv ingroup"+(j.conv===S.conv?" active":""));
  row.dataset.conv=j.conv;
  const body=el("div","cvbody");
  if(j.pending_review || S.pendingReview[j.conv]) { const dot=el("span","reviewdot"); dot.title="有新内容"; body.append(dot); }
  const ct=el("div","ct"); ct.textContent=j.title||"定时任务";
  if(!j.enabled) ct.style.opacity="0.5";
  body.append(ct);
  const tm=fmtTime(j.last_ts); if(tm){ const tmEl=el("span","ctime"); tmEl.textContent=tm; body.append(tmEl); }  // 最近一次运行时刻,与普通会话行统一
  const more=el("button","more"); more.textContent="⋯"; more.title="更多";
  more.onclick=ev=>{ev.stopPropagation();openConvMenu(more,j.conv);}; body.append(more);
  row.append(body);
  row.onclick=()=>openConv(j.conv);
  bindCronDrag(row, j.job_id);
  return row;
}
// 定时任务拖拽排序(复用项目拖拽模式:HTML5 DnD,拖起半透明,落地蓝线提示)
function bindCronDrag(row, jobId){
  row.draggable=true;
  row.ondragstart=ev=>{
    if(ev.target.closest(".more")){ev.preventDefault();return;}
    ev.dataTransfer.effectAllowed="move";
    ev.dataTransfer.setData("text/plain",jobId);
    S._cronDragSrc=jobId;
    setTimeout(()=>row.classList.add("dragging"),0);
  };
  row.ondragend=()=>{row.classList.remove("dragging");S._cronDragSrc=null;};
  row.ondragover=ev=>{if(!S._cronDragSrc||S._cronDragSrc===jobId)return;ev.preventDefault();ev.dataTransfer.dropEffect="move";row.classList.add("dragover");};
  row.ondragleave=()=>row.classList.remove("dragover");
  row.ondrop=ev=>{
    ev.preventDefault();row.classList.remove("dragover");
    const src=ev.dataTransfer.getData("text/plain");
    if(src&&src!==jobId) reorderCronJob(src,jobId);
  };
}
function reorderCronJob(srcId,targetId){
  const arr=S.cronJobs;
  const si=arr.findIndex(j=>j.job_id===srcId), ti=arr.findIndex(j=>j.job_id===targetId);
  if(si<0||ti<0)return;
  const [item]=arr.splice(si,1); arr.splice(ti,0,item);
  renderConvs();
  api("/cron/jobs/reorder",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({order:arr.map(j=>j.job_id)})}).catch(()=>{});
}
// 单条本机系统任务行(launchd/crontab,只读):点进去看脚本内容 + 日志尾部(openSystemTaskModal)。
// 没有专属会话,不走 openConv;没有「⋯」菜单——增删改去改 plist/crontab 本身,这里只读。
function buildSystemTaskRow(t){
  const row=el("div","conv ingroup systask");
  const body=el("div","cvbody");
  const ct=el("div","ct"); ct.textContent=t.name||"系统任务";
  body.append(ct);
  // resident(常驻守护进程):没在跑才是异常——launchctl 里查不到 PID 意味着该常驻的
  // 进程挂了。scheduled(定时触发):没在跑是常态(等下次触发),只在"未加载"或
  // "上次退出码非 0"时才提示,不对 running 状态本身做判断(触发间隙必然是 false)。
  if(t.enabled===false){
    const b=el("span","stbadge sterr"); b.textContent="未加载"; body.append(b);
  }else if(t.task_type==="resident"){
    const ok=t.running===true;
    const b=el("span","stbadge "+(ok?"stok":"sterr")); b.textContent=ok?"运行中":"已停止"; body.append(b);
  }else if(t.last_exit_code){
    const b=el("span","stbadge sterr"); b.textContent="上次失败"; body.append(b);
  }
  const tm=el("span","ctime"); tm.textContent=t.schedule_desc||""; body.append(tm);
  row.append(body);
  row.onclick=()=>openSystemTaskModal(t);
  return row;
}
// 项目分组拖拽排序(原生 HTML5 DnD:桌面拖起手感好,鼠标移动量够才触发,不影响原有点击展开)
function bindProjDrag(h, hash){
  h.draggable = true;
  h.ondragstart = ev=>{
    if(ev.target.closest(".pgadd,.pgmore")){ ev.preventDefault(); return; }   // 加号/更多按钮不触发拖拽
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", hash);
    S.dragSrc = hash;
    setTimeout(()=>h.classList.add("dragging"), 0);   // 下一 tick 加,免得拖拽预览图也变半透明
  };
  h.ondragend = ()=>{ h.classList.remove("dragging"); S.dragSrc=null; };
  h.ondragover = ev=>{ if(!S.dragSrc || S.dragSrc===hash) return; ev.preventDefault(); ev.dataTransfer.dropEffect="move"; h.classList.add("dragover"); };
  h.ondragleave = ()=> h.classList.remove("dragover");
  h.ondrop = ev=>{
    ev.preventDefault(); h.classList.remove("dragover");
    const src = ev.dataTransfer.getData("text/plain");
    S.dragMoved = true;   // 拖拽落地这一下,别顺带触发 h.onclick 里的展开/收起
    if(src && src!==hash) reorderProject(src, hash);
  };
}
function reorderProject(srcHash, targetHash){
  const arr=S.projects;
  const si=arr.findIndex(p=>p.hash===srcHash), ti=arr.findIndex(p=>p.hash===targetHash);
  if(si<0 || ti<0) return;
  const [item]=arr.splice(si,1); arr.splice(ti,0,item);
  renderConvs();
  api("/projects/reorder",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({order:arr.map(p=>p.hash)})}).catch(()=>{});
}
function toggleGroup(hash){
  const k=grpKey(hash);
  if(S.expanded.has(k)){ S.expanded.delete(k); S.moreShown.delete(k); }  // 折叠时清掉「展开更多」态,下次打开回到默认 5 条
  else S.expanded.add(k);
  saveExpanded(); renderConvs();
}
// 在指定项目下开新会话(顶部 ＋新对话 用当前活跃项目;分组 ＋ 用该分组)。
// focus=false 用于 APP 启动时静默落地到一个新对话——不弹键盘打扰用户。
// expand=false 同样只用于 APP 启动时的静默落地:别把用户上次手动折叠的分组重新展开。
function newChatIn(hash, focus, expand){
  S.project=hash;
  if(expand!==false){ S.expanded.add(grpKey(hash)); saveExpanded(); }
  const id=Date.now().toString(36)+Math.random().toString(36).slice(2,6);
  const conv="local-"+(hash?("p"+hash+":"):"")+id;
  S.convs.unshift({conv,title:"新对话",turns:0,last_ts:null});
  openConv(conv, expand===false);
  if(focus!==false) $("#ta").focus();
}
function newChat(){ newChatIn(S.project); }

async function removeProject(hash){
  try{ await api("/projects/remove",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({hash})}); }
  catch(e){ alert("移除失败"); return; }
  S.expanded.delete(hash); saveExpanded();
  const activeGone = convProject(S.conv)===hash;   // 当前会话属于被移除项目 → 回落主会话
  if(S.project===hash) S.project=null;
  await loadProjects();
  if(activeGone){ openConv("main"); } else { renderConvs(); }
}

async function pinConv(conv, pinned){
  const c=findConv(conv); if(c) c.pinned=pinned;  // 乐观更新,立即重渲
  renderConvs();
  try{ await api("/conv/pin",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({conv,pinned})}); }
  catch(e){ /* 失败也不回滚,下次 loadConvs 会自然纠正 */ }
  await loadConvs(); renderConvs();
}

// 目录浏览器弹窗
async function openProjModal(){ $("#projModal").hidden=false; await browseTo(""); }
function closeProjModal(){ $("#projModal").hidden=true; }
async function browseTo(dir){
  let d;
  try{ const r=await api("/browse"+(dir?("?dir="+encodeURIComponent(dir)):"")); d=await r.json(); }
  catch(e){ return; }
  S.browseDir=d.dir; $("#pmPath").value=d.dir;
  const box=$("#pmList"); box.innerHTML="";
  if(d.parent){ const up=el("div","pmrow pmup"); up.textContent="⬆ 上一级"; up.onclick=()=>browseTo(d.parent); box.append(up); }
  for(const e of d.entries){ const row=el("div","pmrow"); row.innerHTML=ic("folder")+" "+esc(e.name); row.onclick=()=>browseTo(e.path); box.append(row); }
  if(!d.entries.length){ const empty=el("div","pmrow pmup"); empty.textContent="(无子文件夹)"; box.append(empty); }
}
async function createProject(path){
  path=(path||"").trim(); if(!path){ alert("请先选或输入一个目录"); return; }
  try{
    const r=await api("/projects/create",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({path})});
    const d=await r.json();
    if(d.error){ alert(d.error); return; }
    closeProjModal(); await loadProjects(); newChatIn(d.project.hash);  // 建好即在其下开一个新会话
  }catch(e){ alert("建项目失败"); }
}
$("#pmGo").onclick = ()=>browseTo($("#pmPath").value.trim());
$("#pmPath").onkeydown = e=>{ if(e.key==="Enter") browseTo($("#pmPath").value.trim()); };
$("#pmCancel").onclick = closeProjModal;
$("#pmCreate").onclick = ()=>createProject($("#pmPath").value);
$("#projModal").onclick = e=>{ if(e.target===$("#projModal")) closeProjModal(); };

// ── 定时任务详情/编辑弹窗(点即生效,不走审批——见 cron/scheduler.py 注释)───────
// job 不传 = 新建模式;传了 = 编辑模式(表单回填该任务的字段,保存时打 update 接口)。
// 列表在侧栏「定时任务」分组里看,启停/删除在该任务的「⋯」菜单里操作,这个弹窗只管
// 「新建一条」或「编辑一条」的详情表单。
function autoResizeTA(ta){ta.style.height="auto";ta.style.height=ta.scrollHeight+"px";}
for(const ta of document.querySelectorAll("#cronForm textarea")) ta.addEventListener("input",()=>autoResizeTA(ta));
let _cronModelCache=null;
async function populateCronModelSelect(selected){
  const sel=$("#cfModel");
  if(!_cronModelCache){
    try{
      const r=await api("/models"); const d=await r.json();
      _cronModelCache=d.choices||[];
    }catch(e){ _cronModelCache=[]; }
  }
  sel.innerHTML='<option value="">默认模型</option>';
  for(const [v,label] of _cronModelCache){
    const o=document.createElement("option"); o.value=v; o.textContent=label; sel.appendChild(o);
  }
  sel.value=selected||"";
}
function openCronModal(job){
  $("#cronModal").hidden=false;
  showCronForm(job);
}
function closeCronModal(){ $("#cronModal").hidden=true; S.cronEditId=null; }
function syncCronMode(){
  const script=$("#cfMode").value==="script";
  $("#cfScriptFields").hidden=!script;
  $("#cfPrompt").placeholder=script
    ? "脚本用途说明,如「检查外贸邮件和退信情况」"
    : "到点时要执行的指令,如「查一下今天的日历和待办,简短汇总」";
}
function showCronForm(job){
  S.cronEditId = job ? job.job_id : null;
  $("#cronModalTitle").textContent = job ? "编辑定时任务" : "＋ 新建定时任务";
  $("#cfSave").textContent = job ? "✓ 保存" : "✓ 创建";
  $("#cfName").value = job ? (job.name || job.title || "") : "";
  $("#cfMode").value = job && job.mode==="script" ? "script" : "agent";
  $("#cfPrompt").value = job ? (job.prompt || "") : "";
  $("#cfCommand").value = job ? (job.command || "") : "";
  $("#cfSummarizePrompt").value = job ? (job.summarize_prompt || "") : "";
  syncCronMode();
  // 编辑表单只支持 cron 表达式调度(跟新建一致);任务若是 interval/once 调度(只能靠
  // 工具/建议创建),这里留空,保存时必须重新选一个频率,不会静默把调度类型改掉。
  const expr = (job && job.schedule && job.schedule.kind==="cron") ? (job.schedule.expr||"") : "";
  const presetOpts=[...$("#cfPreset").options].map(o=>o.value);
  $("#cfPreset").value = expr && presetOpts.includes(expr) ? expr : "custom";
  $("#cfCron").value = expr;
  $("#cfCwd").value = job ? (job.cwd || "") : "";
  populateCronModelSelect(job ? (job.model || "") : "");
  requestAnimationFrame(()=>{for(const ta of document.querySelectorAll("#cronForm textarea")) autoResizeTA(ta);});
  if(!job) $("#cfName").focus();
}
async function saveCronJob(){
  const name=$("#cfName").value.trim(), prompt=$("#cfPrompt").value.trim();
  const cron=$("#cfCron").value.trim(), cwd=$("#cfCwd").value.trim();
  const mode=$("#cfMode").value, command=$("#cfCommand").value.trim();
  const summarize_prompt=$("#cfSummarizePrompt").value.trim();
  if(!name || !prompt){ alert("任务名称和执行说明不能为空"); return; }
  if(mode==="script" && !command){ alert("脚本任务需要填写要执行的命令"); return; }
  if(!cron){ alert("请选一个预设频率,或填自定义 cron 表达式"); return; }
  const editId=S.cronEditId;
  const model=$("#cfModel").value;
  const body={name, prompt, schedule:{kind:"cron", expr:cron}, cwd, model, mode, command, summarize_prompt};
  if(editId) body.id=editId;
  let d;
  try{
    const r=await api(editId?"/cron/jobs/update":"/cron/jobs/create",
      {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    d=await r.json();
  }catch(e){ alert(editId?"保存失败":"创建失败"); return; }
  if(d.error){ alert(d.error); return; }
  closeCronModal(); await loadCronSidebar();
}
async function toggleCronJob(id, enabled){
  try{ await api("/cron/jobs/enable",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id,enabled})}); }
  catch(e){ alert("操作失败"); return; }
  await loadCronSidebar();
}
async function deleteCronJob(id, title, conv){
  if(!confirm(`删除定时任务「${title||""}」?这条任务的历史记录也会一起删掉,不可恢复。`)) return;
  const wasActive = conv && S.conv===conv;
  const previous=S.cronJobs;
  S.cronJobs=S.cronJobs.filter(j=>j.job_id!==id);  // 乐观删除：点击后立刻从侧栏消失
  renderConvs(); syncCronHeader(); syncMoreHeader();
  if(wasActive) openConv("main");   // 删的是当前正开着的任务会话 → 切回主会话
  try{
    const r=await api("/cron/jobs/delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id})});
    const d=await r.json();
    if(!r.ok || d.error) throw new Error(d.error||"删除失败");
  }catch(e){
    S.cronJobs=previous;  // 服务端拒绝/网络失败：原样回滚
    renderConvs(); syncCronHeader(); syncMoreHeader();
    alert(e.message||"删除失败"); return;
  }
  await loadCronSidebar();  // 成功后以服务端数据最终校准
}
$("#cfPreset").onchange = ()=>{ const v=$("#cfPreset").value; if(v!=="custom") $("#cfCron").value=v; };
$("#cfMode").onchange = syncCronMode;
$("#cfCancel").onclick = closeCronModal;
$("#cfSave").onclick = saveCronJob;
$("#cronModal").onclick = e=>{ if(e.target===$("#cronModal")) closeCronModal(); };
// 任务会话标题栏右侧的操作区(打开这条 cron-task 会话时才显示,见 syncCronHeader):
// 启停开关和编辑图标直出;「⋯」里只剩删除(slim 模式),编辑/启停不用多点一层
$("#convCronEditBtn").innerHTML = ic("edit");
$("#convCronToggle").onclick = ()=>{ const j=S.cronJobs.find(x=>x.conv===S.conv); if(j) toggleCronJob(j.job_id, !j.enabled); };
$("#convCronEditBtn").onclick = ()=>{ const j=S.cronJobs.find(x=>x.conv===S.conv); if(j) openCronModal(j); };
$("#convCronMore").onclick = ev=>{ ev.stopPropagation(); openConvMenu($("#convCronMore"), S.conv, true); };
$("#convMoreBtn").onclick = ev=>{ ev.stopPropagation(); openConvMenu($("#convMoreBtn"), S.conv, false); };

// ── 本机系统任务详情弹窗(只读)────────────────────────────────────────────
// 点开一行,按需拉 /system/tasks/detail(脚本内容+日志尾部不放列表接口里,见 web.py 注释)。
async function openSystemTaskModal(t){
  $("#sysTaskModal").hidden=false;
  $("#stModalTitle").textContent=t.name||"系统任务";
  let statusLabel = "";
  if(t.source==="launchd"){
    if(t.enabled===false) statusLabel="未加载";
    else if(t.task_type==="resident") statusLabel=t.running?"运行中":"已停止";
    else statusLabel=t.running?"运行中":"待触发";
    // resident 类型当前健康(running)时,历史退出码只是噪音(可能是很久前一次正常重载
    // 留下的);只有"已停止"或 scheduled 类型才值得带出来当诊断线索
    const showExitCode = t.last_exit_code && !(t.task_type==="resident" && t.running);
    if(showExitCode) statusLabel+=` · 上次退出码 ${t.last_exit_code}`;
  }
  $("#stMeta").textContent=`来源:${t.source==="launchd"?"launchd":"crontab"} · 调度:${t.schedule_desc||"?"}`
    +(statusLabel ? ` · ${statusLabel}` : "");
  $("#stCommand").textContent=t.command||"";
  $("#stScript").textContent="加载中…";
  $("#stLogs").innerHTML="";
  try{
    const r=await api("/system/tasks/detail?id="+encodeURIComponent(t.id));
    const d=(await r.json()).task;
    $("#stScript").textContent=d.script_content || d.script_error || "(无脚本内容)";
    const logEntries=Object.entries(d.logs||{});
    if(!logEntries.length){
      const empty=el("div","stlogempty"); empty.textContent="(无日志文件)"; $("#stLogs").append(empty);
    }
    for(const [path,tail] of logEntries){
      const h=el("div","stlogpath"); h.textContent=path; $("#stLogs").append(h);
      const pre=el("pre","stpre"); pre.textContent=tail||"(空)"; $("#stLogs").append(pre);
    }
  }catch(e){ $("#stScript").textContent="加载失败"; }
}
function closeSystemTaskModal(){ $("#sysTaskModal").hidden=true; }
$("#stClose").onclick = closeSystemTaskModal;
$("#sysTaskModal").onclick = e=>{ if(e.target===$("#sysTaskModal")) closeSystemTaskModal(); };
