"use strict";
// 默认工作台只读概览；旧待办保留，所有任务管理仍使用原来的接口与控件。
const AWB = {mode:"automation", data:null, error:"", loading:false, version:0,
  open:new Set(), folds:new Set(), jobs:new Set(), products:new Set(), histories:new Map(), menuTrigger:null,
  search:"", showDisabled:true, sourceRequest:0};
function automationWorkbenchActive(){ return AWB.mode==="automation"; }
function awbVisible(){ return automationWorkbenchActive() && S.surface==="workbench" && !$("#workbenchView").hidden; }
async function awbJson(url){
  const r=await api(url,{cache:"no-store"}), d=await r.json();
  if(!r.ok || d.error) throw new Error(d.error||"加载失败，请重试");
  return d;
}
async function loadAutomationWorkbench(){
  const version=++AWB.version;
  AWB.loading=true; AWB.error=""; renderAutomationWorkbench();
  try{
    const data=await awbJson("/workbench/automation");
    if(version!==AWB.version) return;
    AWB.data=data;
  }catch(e){ if(version!==AWB.version) return; AWB.error=e.message; }
  AWB.loading=false; if(awbVisible()) renderAutomationWorkbench();
  if(!AWB.error) await awbRefreshHistories();
}
async function awbRefreshHistories(){
  const owners=new Set(AWB.data.agents.map(a=>a.id)),pending=[];
  const previous=AWB.histories; AWB.histories=new Map();
  for(const job of AWB.data.jobs){
    if(!AWB.jobs.has(job.id)) continue;
    const owner=owners.has(job.agent_id)?job.agent_id:"",key=awbHistoryKey(owner,job.id),old=previous.get(key);
    if(old) AWB.histories.set(key,old);
    pending.push(awbLoadJobHistory(owner,job.id,old?.page||0));
  }
  for(const owner of AWB.products) if(owners.has(owner)) pending.push(awbLoadProducts(owner));
  await Promise.all(pending);
}
function refreshAutomationWorkbench(){ if(awbVisible()) return loadAutomationWorkbench(); }
function awbNumber(n){ return new Intl.NumberFormat("zh-CN").format(n||0); }
function awbTokens(n){ return n>=10000 ? new Intl.NumberFormat("zh-CN",{minimumFractionDigits:1,maximumFractionDigits:1}).format(n/10000)+" 万" : awbNumber(n); }
function awbDuration(n){ return n==null || !(n>0) ? "未记录" : n>=60 ? Math.round(n/60*10)/10+" 分钟" : Math.round(n)+" 秒"; }
function awbRate(s){ return s.runs ? (100*s.ok/s.runs).toFixed(1)+"%" : "—"; }
function awbTime(ts){ return new Date(ts*1000).toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}); }
function awbMetricText(s){
  return (s.token_samples ? awbTokens(s.tokens)+" Token" : "Token 未记录")+" · 中位 "+awbDuration(s.median_duration);
}
function awbStatus(j){
  if(j.activity==="running") return ["running","运行中"];
  if(j.activity==="queued") return ["queued","排队中"];
  if(!j.enabled) return ["paused","已停用"];
  if(j.last_status && !j.last_status.startsWith("success")) return ["error","上次异常"];
  return ["idle","待触发"];
}
function awbSchedule(j){
  const s=j.schedule||{}, m=(s.expr||"").match(/^(\d+) (\d+) \* \* \*$/);
  return m ? "每天 "+m[2].padStart(2,"0")+":"+m[1].padStart(2,"0") : j.schedule_desc||s.expr||"未设置计划";
}
function awbButton(action,label,id="",cls=""){ return '<button type="button" class="'+cls+'" data-awb="'+action+'" data-id="'+esc(id)+'">'+label+'</button>'; }
function awbHeader(){
  return '<header class="wb-toolbar"><div class="wb-title-row"><div class="wb-title">'+
    '<button class="wb-hamb" data-sidebar aria-label="打开侧栏">'+ic("panel")+'</button><h1>工作台</h1></div><div class="wb-title-actions">'+
    awbButton("refresh",AWB.loading?"刷新中":"刷新","","awb-link")+
    awbButton("todos","待办","","awb-link")+
    '<button class="wb-win-btn" data-workbench-win title="独立窗口" aria-label="在独立窗口打开工作台">'+ic("newwin")+'</button></div></div>'+
    '<div class="awb-controls"><label class="awb-search"><input id="awbSearch" aria-label="搜索 Agent 或定时任务" type="search" value="'+esc(AWB.search)+'" placeholder="搜索 Agent、定时任务"></label><label class="awb-show-disabled"><input id="awbShowDisabled" type="checkbox"'+(AWB.showDisabled?' checked':'')+'>显示已停用</label></div></header>';
}
function awbStats(){
  const d=AWB.data,s=d.stats;
  return '<div class="apstats awb-stats">'+[[awbNumber(d.jobs.length),"定时任务"],[awbNumber(d.runs),"累计运行"],
    [s.token_samples?awbTokens(s.tokens):"未记录","近 7 天 Token"],[awbDuration(s.median_duration),'近 7 天<span class="awb-stat-label">中位耗时</span>'],
    [awbRate(s),'近 7 天<span class="awb-stat-label">调度成功率</span>'],[awbNumber(d.artifacts)+" / "+awbNumber(d.works),"附件 / 作品"]]
    .map(([n,t])=>'<div class="apstat"><b>'+esc(String(n))+'</b><span>'+t+'</span></div>').join("")+'</div>'+
    '<details class="awb-note"><summary>近 7 天 · 指标口径与覆盖</summary><p>Token 有记录 '+s.token_samples+'/'+s.runs+' 次，耗时有记录 '+s.duration_samples+'/'+s.runs+' 次；未记录不算零。Token 沿用保存口径，不是精确账单，也不据此推算费用。</p><p>运行包含历史任务与独立后台任务。调度成功不等于业务目标完成；工具轨迹不是业务阶段。附件是保存的快照，网站作品指向当前版本。</p></details>';
}
function awbMatches(j){ return AWB.showDisabled || j.enabled; }
function awbDayTitle(day){
  if(!day.runs) return day.date+'\n无运行记录；可能未触发或历史未记录，不能确认触发条件是否满足。';
  return day.date+'\n'+day.runs+' 次运行 · 成功 '+day.ok+' · 失败 '+day.failed+(day.unknown?' · 状态未确认 '+day.unknown:'')+
    '\n'+awbMetricText(day)+'\n最近运行 '+awbTime(day.last_ts)+'\n调度成功不等于业务目标完成';
}
let awbTip, awbTipTick, awbTipKeyboard=false;
function awbHideTip(){
  if(awbTip) awbTip.hidden=true;
  awbTipTick=null; awbTipKeyboard=false;
  document.querySelector('[aria-describedby="awbTip"]')?.removeAttribute("aria-describedby");
}
function awbScrollTip(){
  const tick=awbTipTick;
  if(!awbTipKeyboard || !tick?.isConnected){ awbHideTip(); return; }
  const rect=tick.getBoundingClientRect();
  if(rect.top<0 || rect.bottom>innerHeight){ awbHideTip(); return; }
  awbShowTip(tick,true);
}
function awbShowTip(tick,keyboard=false){
  if(!tick) return;
  if(!awbTip){
    awbTip=document.createElement("div"); awbTip.id="awbTip"; awbTip.className="sttip";
    awbTip.setAttribute("role","tooltip"); $("#workbenchView").append(awbTip);
  }
  awbHideTip(); awbTipTick=tick; awbTipKeyboard=keyboard;
  awbTip.textContent=tick.dataset.awbTip; awbTip.hidden=false;
  tick.closest("button").setAttribute("aria-describedby","awbTip");
  const gap=parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--sp-3"))||8;
  const cell=tick.getBoundingClientRect(),rect=awbTip.getBoundingClientRect();
  awbTip.style.left=Math.max(gap,Math.min(cell.left,innerWidth-rect.width-gap))+"px";
  awbTip.style.top=Math.max(gap,cell.bottom+gap+rect.height<=innerHeight-gap?cell.bottom+gap:cell.top-rect.height-gap)+"px";
}
$("#workbenchView").addEventListener("pointerover",event=>{
  if(event.pointerType==="touch" || !awbVisible()) return;
  const tick=event.target.closest("[data-awb-tip]");
  if(tick) awbShowTip(tick);
  else if(!awbTipTick || event.target.closest(".awb-spark")!==awbTipTick.closest(".awb-spark")) awbHideTip();
});
$("#workbenchView").addEventListener("pointerleave",awbHideTip);
$("#workbenchView").addEventListener("focusin",event=>{
  const button=event.target.closest(".awb-recent,.awb-job-recent");
  if(button?.matches(":focus-visible")) awbShowTip(button.querySelector("[data-awb-tip]:last-child"),true);
});
$("#workbenchView").addEventListener("focusout",awbHideTip);
$("#workbenchView").addEventListener("click",awbHideTip);
document.addEventListener("scroll",awbScrollTip,true);
document.addEventListener("keydown",event=>{ if(event.key==="Escape") awbHideTip(); });
window.addEventListener("resize",awbHideTip);
window.addEventListener("blur",awbHideTip);
function awbRecentRuns(a,owner=null){
  const days=a.run_days||[],latest=(a.recent_runs||[]).at(-1);
  const marks=days.map(day=>'<span class="awb-tick '+day.status+'" data-date="'+esc(day.date)+'" data-awb-tip="'+esc(awbDayTitle(day))+'"></span>').join('');
  const isJob=owner!==null;
  return '<button type="button" class="'+(isJob?'awb-job-recent':'awb-recent')+'" data-awb="'+(isJob?'job-runs':'all-runs')+'" data-id="'+esc(a.id)+'"'+(isJob?' data-owner="'+esc(owner)+'"':'')+' aria-label="查看 '+esc(a.name)+' 的运行记录，最近 30 天，每格一天；绿色成功，红色失败，灰色无运行记录或状态未确认。按服务器本地日期统计。">'+
    '<span class="awb-spark" aria-hidden="true">'+marks+'</span>'+(isJob?'':'<small>'+awbNumber(a.runs)+' 运行 · '+(latest?awbTime(latest.ts):'暂无记录')+'</small>')+'</button>';
}
function awbOwner(a,jobs){
  const open=AWB.open.has(a.id);
  return '<section class="awb-owner" data-owner="'+esc(a.id)+'"><div class="awb-owner-row">'+
    '<button type="button" class="awb-owner-toggle" data-awb="owner" data-id="'+esc(a.id)+'" aria-expanded="'+open+'"><span class="awb-chevron" aria-hidden="true">'+(open?"⌄":"›")+'</span>'+avatarSvg(a.avatar)+
    '<span class="awb-owner-name"><strong>'+esc(a.name)+'</strong><small>'+awbNumber(jobs.length)+' 定时任务</small></span></button>'+awbRecentRuns(a)+
    '<span class="awb-owner-metrics">'+esc(awbMetricText(a.stats))+'</span>'+awbButton("products",awbNumber(a.artifacts)+' 附件 · '+awbNumber(a.works)+' 作品',a.id,"awb-link")+'</div>'+
    (open?'<div class="awb-owner-body">'+awbOwnerBody(a,jobs)+'</div>':"")+ '</section>';
}
function awbOwnerBody(a,jobs){
  return (AWB.products.has(a.id)?awbProductsHtml(a.id):"")+'<div class="awb-jobs">'+
    (jobs.length?jobs.map(j=>awbJobHtml(a.id,j)).join(""):'<p class="awb-empty">暂无定时任务</p>')+
    awbButton("create","＋ 新建定时任务",a.id,"awb-link")+'</div>';
}
function awbJobHtml(owner,j){
  const [status,label]=awbStatus(j),open=AWB.jobs.has(j.id);
  return '<section class="awb-task" data-job="'+esc(j.id)+'"><div class="aptask awb-job"><button type="button" class="aptmain" data-awb="job-runs" data-id="'+esc(j.id)+'" data-owner="'+esc(owner)+'" aria-expanded="'+open+'" title="'+esc(j.name+' · '+awbSchedule(j))+'"><span class="awb-chevron" aria-hidden="true">'+(open?'⌄':'›')+'</span><span class="awb-job-title"><span class="aptname">'+esc(j.name)+'</span><span class="awb-job-status '+status+'">'+(status==='idle'?'':label)+'</span></span></button>'+awbRecentRuns(j,owner)+
    '<span class="awb-job-count" title="累计运行 '+awbNumber(j.runs)+' 次">'+awbNumber(j.runs)+' 运行</span><button type="button" class="more" data-awb="menu" data-id="'+esc(j.id)+'" aria-label="管理定时任务 '+esc(j.name)+'" aria-haspopup="menu">⋯</button></div>'+
    (open?awbJobHistoryHtml(owner,j.id):"")+'</section>';
}
function awbHistoryKey(agent,job){ return JSON.stringify([agent,job]); }
function awbJobHistoryHtml(agent,job){
  const h=AWB.histories.get(awbHistoryKey(agent,job));
  if(!h) return '<p class="awb-empty" role="status">正在读取…</p>';
  const pageButton=(action,label,disabled)=>'<button type="button" class="awb-link" data-awb="'+action+'" data-id="'+esc(job)+'"'+(disabled?' disabled':'')+'>'+label+'</button>';
  return '<div class="awb-job-history" data-history-owner="'+esc(agent)+'" aria-label="运行记录">'+
    (h.loading?'<p class="awb-empty" role="status">正在读取…</p>':h.error?'<p class="awb-error" role="alert">'+esc(h.error)+'</p>'+pageButton('job-retry','重试',false):
      '<div class="awb-history">'+(h.items.length?'<div class="awb-history-head" aria-hidden="true"><span>运行时间</span><span>运行结果</span><span>Token</span><span>耗时</span><span>会话</span></div>'+h.items.map(awbRunHtml).join(""):'<p class="awb-empty">暂无运行记录</p>')+'</div>')+
    '<nav class="awb-pager" aria-label="运行记录翻页">'+pageButton('job-prev','上一页',h.loading||h.page===0)+'<span>第 '+(h.page+1)+' 页</span>'+pageButton('job-next','下一页',h.loading||!!h.error||!h.next)+'</nav></div>';
}
function awbProductsHtml(agent){
  const h=AWB.histories.get(awbHistoryKey(agent,"products"));
  const head='<div class="awb-actions">'+awbButton("library","图片和网站作品库",agent,"awb-link")+'<span>附件</span></div>';
  if(!h) return head+'<p class="awb-empty" role="status">正在读取…</p>';
  return '<div class="awb-products">'+head+h.items.map(awbArtifactHtml).join("")+
    (h.error?'<p class="awb-error" role="alert">'+esc(h.error)+'</p>':"")+
    (!h.items.length&&!h.loading&&!h.error?'<p class="awb-empty">暂无附件</p>':"")+
    (h.loading?'<p class="awb-empty" role="status">正在读取…</p>':h.next||h.error?awbButton("more-products",h.error?"重试":"加载更多",agent,"awb-link"):"")+'</div>';
}
function awbRunHtml(r){
  const ok=r.status.startsWith("success"),failed=r.status.startsWith("error")||r.status==="failed",linked=!!r.target?.conv;
  const tokens=r.tokens>0?awbTokens(r.tokens):"未记录",duration=awbDuration(r.duration);
  return '<article class="aprun"><button type="button" class="awb-run-link" data-awb="source-run" data-id="'+r.id+'"'+(linked?'':' disabled')+'><time>'+awbTime(r.ts)+'</time><span class="awb-run-name"><span class="awb-dot '+(ok?"ok":failed?"error":"unknown")+'" aria-hidden="true"></span>'+(ok?"调度成功":failed?"运行异常":"状态未确认")+'</span>'+
    '<span class="awb-run-tokens" aria-label="Token '+esc(tokens)+'" title="Token '+esc(tokens)+'">'+esc(tokens)+'<span class="awb-token-unit"> Token</span></span><span class="awb-run-duration" aria-label="耗时 '+esc(duration)+'" title="耗时 '+esc(duration)+'">'+esc(duration)+'</span><span class="awb-run-action">'+(linked?'查看会话 ›':'未关联会话')+'</span></button></article>';
}
function awbArtifactHtml(e){
  return '<div class="artifact-card awb-artifact"><div><strong>'+esc(e.title||e.filename)+'</strong><small>'+esc(e.filename)+' · '+awbNumber(e.size)+' 字节</small></div>'+
    (e.conv?'<button type="button" data-awb="preview" data-artifact-id="'+esc(e.id)+'" data-conv="'+esc(e.conv)+'" data-filename="'+esc(e.filename)+'">预览</button><button type="button" data-awb="download" data-artifact-id="'+esc(e.id)+'" data-conv="'+esc(e.conv)+'" data-filename="'+esc(e.filename)+'">下载</button>':'<span>来源不可定位</span>')+'</div>';
}
function renderAutomationWorkbench(){
  awbHideTip();
  const root=$("#wbContent"); if(!root || !automationWorkbenchActive()) return;
  $("#workbenchView").classList.add("automation-workbench");
  $("#wbFab").classList.add("wb-fab-off"); $("#wbDock").innerHTML="";
  $("#wbMultiBar").hidden=true; document.body.classList.remove("wb-multiselect-mode");
  root.innerHTML=awbHeader()+(AWB.error?'<p role="alert" class="awb-error">'+esc(AWB.error)+'</p>':"")+(!AWB.data?'<p role="status" class="awb-empty">'+(AWB.loading?"正在读取工作台…":"工作台暂无数据")+'</p>':awbStats()+'<div id="awbOwners"></div>');
  if(AWB.data) awbRenderOwners();
}
function awbRenderOwners(){
  awbHideTip();
  const root=$("#awbOwners"); if(!root) return;
  const focused=document.activeElement, focusData=root.contains(focused)?{...focused.dataset}:null;
  const d=AWB.data,q=AWB.search.trim().toLowerCase(), active=[], empty=[];
  for(const a of d.agents){
    const all=d.jobs.filter(j=>j.agent_id===a.id);
    const jobs=all.filter(j=>awbMatches(j) && (!q||a.name.toLowerCase().includes(q)||j.name.toLowerCase().includes(q)));
    if(all.length && jobs.length) active.push(awbOwner(a,jobs));
    else if(!all.length && (!q||a.name.toLowerCase().includes(q))) empty.push(awbOwner(a,[]));
  }
  const known=new Set(d.agents.map(a=>a.id)), orphan=d.jobs.filter(j=>!known.has(j.agent_id)&&awbMatches(j)&&(!q||j.name.toLowerCase().includes(q)));
  root.innerHTML=active.join("")+(!active.length?'<p class="awb-empty">没有符合条件的定时任务</p>':"")+
    (orphan.length?'<details class="awb-fold" data-awb-fold="orphan" '+(AWB.folds.has("orphan")?'open':'')+'><summary>历史 / 未归属 · '+orphan.length+' 定时任务</summary>'+awbOwnerBody({id:""},orphan)+'</details>':"")+
    (empty.length?'<details class="awb-fold" data-awb-fold="empty" '+(AWB.folds.has("empty")?'open':'')+'><summary>暂无定时任务的 Agent · '+empty.length+'</summary>'+empty.join("")+'</details>':"");
  if(focusData?.awb){
    [...root.querySelectorAll("[data-awb]")].find(b=>b.dataset.awb===focusData.awb && b.dataset.id===focusData.id && b.dataset.owner===focusData.owner)?.focus({preventScroll:true});
  }
}
function awbExpandOwner(agent){
  AWB.open.add(agent); if(!agent) AWB.folds.add("orphan");
}
async function awbToggleJob(agent,job){
  awbExpandOwner(agent);
  if(AWB.jobs.has(job)){ AWB.jobs.delete(job); awbRenderOwners(); return; }
  AWB.jobs.add(job); await awbLoadJobHistory(agent,job);
}
async function awbLoadJobHistory(agent,job,page=0){
  const key=awbHistoryKey(agent,job),prev=AWB.histories.get(key);
  const cursors=prev?.cursors.slice()||[0];
  if(page<0 || (page>0 && !cursors[page])) return;
  const state={items:[],next:null,page,cursors,loading:true,error:""};
  AWB.histories.set(key,state); awbRenderOwners();
  try{
    const query=new URLSearchParams({agent,job,before:String(cursors[page]),limit:"5"});
    const data=await awbJson("/workbench/automation/runs?"+query);
    if(AWB.histories.get(key)!==state) return;
    state.items=data.items; state.next=data.next;
    state.cursors=state.cursors.slice(0,page+1); if(data.next) state.cursors.push(data.next);
  }catch(e){ if(AWB.histories.get(key)!==state) return; state.error=e.message; }
  state.loading=false; if(awbVisible()) awbRenderOwners();
}
async function awbLoadProducts(agent,more=false){
  const key=awbHistoryKey(agent,"products"),prev=AWB.histories.get(key);
  if(more && prev?.loading) return;
  const state={items:more?(prev?.items||[]):[],next:more?prev?.next:null,loading:true,error:""};
  AWB.histories.set(key,state); awbRenderOwners();
  try{
    const data=await awbJson("/workbench/automation/artifacts?"+new URLSearchParams({agent,before:String(state.next||0)}));
    if(AWB.histories.get(key)!==state) return;
    const ids=new Set(state.items.map(i=>i.id)); state.items.push(...data.items.filter(i=>!ids.has(i.id))); state.next=data.next;
  }catch(e){ if(AWB.histories.get(key)!==state) return; state.error=e.message; }
  state.loading=false; if(awbVisible()) awbRenderOwners();
}
async function awbOpenRun(id,button){
  const request=++AWB.sourceRequest,convSeq=_convOpenSeq;
  button.disabled=true; button.setAttribute("aria-busy","true");
  try{
    const d=await awbJson("/workbench/automation/run?id="+id);
    // 历史加载会重绘按钮，但不应取消跳转；只丢弃后点的记录或切换会话前的旧请求。
    if(request!==AWB.sourceRequest || convSeq!==_convOpenSeq || !awbVisible()) return;
    if(!d.target?.conv){ alert("对应会话已清空或未关联，无法跳转"); return; }
    await openConvAtTurn(d.target.conv,d.target.turn_id,d.target.ts);
  }finally{ button.disabled=false; button.removeAttribute("aria-busy"); }
}
async function awbJobConfig(id){
  const d=await awbJson("/cron/sidebar"),j=d.jobs.find(j=>j.job_id===id);
  if(!j) throw new Error("定时任务已不存在，请刷新");
  return j;
}
async function awbManage(btn,id){
  try{
    const j=await awbJobConfig(id); if(!btn.isConnected||!awbVisible()) return;
    const perform=fn=>async()=>{try{await fn();}catch(e){alert(e.message);}finally{refreshAutomationWorkbench();}};
    apMenuAt(btn,[{label:"编辑 / 重命名",run:()=>openCronModal(j)},
      {label:j.enabled?"停用":"启用",run:perform(()=>toggleCronJob(id,!j.enabled))},
      {label:"试跑",run:perform(async()=>{if(confirm("立即运行「"+j.title+"」？会执行真实任务。")) await agentPost("/cron/jobs/run",{id});})},
      {label:"复制到…",run:()=>pickAgentMenu(btn,S.agents,performTarget=>perform(()=>copyJobTo(j,performTarget))())},
      {label:"移动到…",run:()=>pickAgentMenu(btn,S.agents.filter(a=>a.id!==j.agent_id),t=>perform(()=>moveJobTo(j,t))())},
      {label:"删除",danger:true,run:perform(()=>deleteCronJob(id,j.title,j.conv))}]);
    AWB.menuTrigger=id;
    const menu=apMenuEl; menu?.setAttribute("role","menu");
    menu?.querySelectorAll("button").forEach(b=>b.setAttribute("role","menuitem"));
    menu?.querySelector("button")?.focus();
  }catch(e){ alert(e.message); }
}
async function awbClick(event){
  const b=event.target.closest("[data-awb]"); if(!b) return;
  event.stopPropagation(); const action=b.dataset.awb,id=b.dataset.id||"";
  if(action==="refresh"){ await loadAutomationWorkbench(); return; }
  if(action==="todos"){
    AWB.mode="todos"; $("#workbenchView").classList.remove("automation-workbench");
    if(!WB.anchor) WB.anchor=workbenchToday(); await loadWorkbenchData(); renderWorkbench(); return;
  }
  if(action==="owner"){ AWB.open.has(id)?AWB.open.delete(id):AWB.open.add(id); awbRenderOwners(); return; }
  if(action==="products"){
    awbExpandOwner(id);
    if(AWB.products.has(id)){AWB.products.delete(id);awbRenderOwners();}
    else {AWB.products.add(id);await awbLoadProducts(id);} return;
  }
  if(action==="job-runs"){await awbToggleJob(b.dataset.owner,id);return;}
  if(action==="all-runs"){awbExpandOwner(id);awbRenderOwners();return;}
  if(action==="more-products"){await awbLoadProducts(id,true);return;}
  if(["job-prev","job-next","job-retry"].includes(action)){
    const owner=b.closest("[data-history-owner]").dataset.historyOwner,h=AWB.histories.get(awbHistoryKey(owner,id));
    if(!h || h.loading) return;
    await awbLoadJobHistory(owner,id,h.page+(action==="job-prev"?-1:action==="job-next"?1:0)); return;
  }
  if(action==="source-run"){await awbOpenRun(id,b);return;}
  if(action==="menu"){await awbManage(b,id);return;}
  if(action==="create"){openCronModal(null);S.cronAgentId=id||null;$("#cfCwd").value=agentById(id)?.workdir||"";return;}
  if(action==="library"){
    await vococoLazy.run("library", async()=>{
      LIB.filters.agent_id=id;LIB.filters.kind="";LIB.filters.state="visible";LIB.filters.search="";
      await openLibrary();
    });
    return;
  }
  if(action==="preview"){
    const params=new URLSearchParams({kind:"artifact",target:b.dataset.artifactId,title:b.dataset.filename,conv:b.dataset.conv});
    window.open("/doc-preview.html?"+params,"_blank","noopener");return;
  }
  if(action==="download"){
    const status=el("span");b.after(status);await downloadArtifact({id:b.dataset.artifactId,filename:b.dataset.filename},b.dataset.conv,b,status);
  }
}
$("#workbenchView").addEventListener("click",event=>{
  if(automationWorkbenchActive()) awbClick(event).catch(e=>alert(e.message));
});
$("#workbenchView").addEventListener("toggle",event=>{
  const key=event.target.dataset.awbFold; if(!key||!event.target.isConnected) return;
  event.target.open?AWB.folds.add(key):AWB.folds.delete(key);
},true);
$("#workbenchView").addEventListener("input",event=>{
  if(event.target.id!=="awbSearch") return; AWB.search=event.target.value; awbRenderOwners();
});
$("#workbenchView").addEventListener("change",event=>{
  if(event.target.id!=="awbShowDisabled") return; AWB.showDisabled=event.target.checked; awbRenderOwners();
});
document.addEventListener("keydown",event=>{
  if(!awbVisible()||!apMenuEl) return;
  if(event.key==="Escape"){event.preventDefault();closeApMenu();document.querySelector('[data-awb="menu"][data-id="'+CSS.escape(AWB.menuTrigger||"")+'"]')?.focus();}
  if(!["ArrowDown","ArrowUp","Home","End"].includes(event.key)) return;
  const items=[...apMenuEl.querySelectorAll("button")],index=items.indexOf(document.activeElement);
  event.preventDefault(); items[event.key==="Home"?0:event.key==="End"?items.length-1:(index+(event.key==="ArrowUp"?-1:1)+items.length)%items.length]?.focus();
});
