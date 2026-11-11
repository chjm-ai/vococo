"use strict";
// 默认工作台只读概览；旧待办保留，所有任务管理仍使用原来的接口与控件。
const AWB = {mode:"automation", data:null, error:"", loading:false, version:0,
  open:new Set(), folds:new Set(), tabs:new Map(), histories:new Map(), details:new Map(), menuTrigger:null,
  search:"", filter:"all", request:0};
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
}
function refreshAutomationWorkbench(){ if(awbVisible()) return loadAutomationWorkbench(); }
function awbNumber(n){ return new Intl.NumberFormat("zh-CN").format(n||0); }
function awbTokens(n){ return n>=10000 ? (n/10000).toFixed(1)+" 万" : awbNumber(n); }
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
    '<div class="awb-controls"><label class="awb-search"><input id="awbSearch" aria-label="搜索 Agent 或定时任务" type="search" value="'+esc(AWB.search)+'" placeholder="搜索 Agent、定时任务"></label><div class="wb-switch" role="group" aria-label="筛选定时任务">'+
    [["all","全部"],["active","运行中"],["error","异常"]].map(([v,t])=>'<button type="button" class="'+(AWB.filter===v?"on":"")+'" data-awb="filter" data-id="'+v+'" aria-pressed="'+(AWB.filter===v)+'">'+t+'</button>').join("")+ '</div></div></header>';
}
function awbStats(){
  const d=AWB.data,s=d.stats;
  return '<div class="apstats awb-stats">'+[[d.jobs.length,"定时任务"],[awbNumber(d.runs),"累计运行"],
    [s.token_samples?awbTokens(s.tokens):"未记录","近 7 天 Token"],[awbDuration(s.median_duration),"中位耗时"],
    [awbRate(s),"调度成功率"],[awbNumber(d.artifacts)+" / "+awbNumber(d.works),"附件 / 作品"]]
    .map(([n,t])=>'<div class="apstat"><b>'+esc(String(n))+'</b><span>'+t+'</span></div>').join("")+'</div>'+
    '<details class="awb-note"><summary>近 7 天 · 指标口径与覆盖</summary><p>Token 有记录 '+s.token_samples+'/'+s.runs+' 次，耗时有记录 '+s.duration_samples+'/'+s.runs+' 次；未记录不算零。Token 沿用保存口径，不是精确账单，也不据此推算费用。</p><p>运行包含历史任务与独立后台任务。调度成功不等于业务目标完成；工具轨迹不是业务阶段。附件是保存的快照，网站作品指向当前版本。</p></details>';
}
function awbMatches(j){
  if(AWB.filter==="active") return ["running","queued"].includes(j.activity);
  if(AWB.filter==="error") return awbStatus(j)[0]==="error";
  return true;
}
function awbRecentRuns(a){
  const runs=a.recent_runs||[],latest=runs.at(-1);
  const marks=runs.map(r=>{
    const status=r.status.startsWith("success")?"ok":r.status.startsWith("error")||r.status==="failed"?"error":"unknown";
    const label=status==="ok"?"调度成功":status==="error"?"运行异常":"状态未确认";
    return '<span class="awb-tick '+status+'" title="'+esc(awbTime(r.ts)+' · '+label)+'"></span>';
  }).join("");
  return '<button type="button" class="awb-recent" data-awb="all-runs" data-id="'+esc(a.id)+'" aria-label="查看 '+esc(a.name)+' 的运行记录" title="最近 '+runs.length+' 次运行，从左到右由旧到新；绿色为调度成功，红色为异常，灰色为未确认">'+
    (marks?'<span class="awb-spark" aria-hidden="true">'+marks+'</span>':"")+'<small>'+awbNumber(a.runs)+' 运行 · '+(latest?awbTime(latest.ts):"暂无记录")+'</small></button>';
}
function awbOwner(a,jobs){
  const tab=AWB.tabs.get(a.id)||{tab:"tasks",job:""}, open=AWB.open.has(a.id);
  return '<section class="awb-owner" data-owner="'+esc(a.id)+'"><div class="awb-owner-row">'+
    '<button type="button" class="awb-owner-toggle" data-awb="owner" data-id="'+esc(a.id)+'" aria-expanded="'+open+'">'+avatarSvg(a.avatar)+
    '<span><strong>'+esc(a.name)+'</strong><small>'+jobs.length+' 定时任务</small></span><span class="awb-chevron">'+(open?"⌄":"›")+'</span></button>'+awbRecentRuns(a)+
    '<span class="awb-owner-metrics">'+esc(awbMetricText(a.stats))+'</span>'+awbButton("products",a.artifacts+' 附件 · '+a.works+' 作品',a.id,"awb-link")+'</div>'+
    (open?'<div class="awb-owner-body"><nav class="wb-switch awb-tabs" aria-label="'+esc(a.name)+' 工作内容">'+
      [["tasks","定时任务"],["runs","运行"],["products","产出"]].map(([v,t])=>'<button type="button" data-awb="tab" data-id="'+esc(a.id)+'" data-tab="'+v+'" class="'+(tab.tab===v?"on":"")+'">'+t+'</button>').join("")+ '</nav>'+awbOwnerBody(a,jobs,tab)+'</div>':"")+ '</section>';
}
function awbOwnerBody(a,jobs,tab){
  if(tab.tab!=="tasks") return awbHistoryHtml(a.id,tab);
  return '<div class="awb-jobs">'+(jobs.length?jobs.map(j=>{
    const [status,label]=awbStatus(j);
    return '<div class="aptask awb-job"><span class="awb-dot '+status+'" title="'+label+'"></span><button type="button" class="aptmain" data-awb="job-runs" data-id="'+esc(j.id)+'" data-owner="'+esc(a.id)+'"><span class="aptname">'+esc(j.name)+'</span><span class="aptsub">'+esc(awbSchedule(j))+' · '+(j.mode==="script"?"脚本":"AI")+' · '+label+'</span></button><span class="awb-job-metrics">'+j.runs+' 运行<br>'+esc(awbMetricText(j.stats))+'</span><button type="button" class="more" data-awb="menu" data-id="'+esc(j.id)+'" aria-label="管理定时任务 '+esc(j.name)+'" aria-haspopup="menu">⋯</button></div>';
  }).join(""):'<p class="awb-empty">暂无定时任务</p>')+awbButton("create","＋ 新建定时任务",a.id,"awb-link")+'</div>';
}
function awbHistoryKey(a,tab){ return [a,tab.tab,tab.job||""].join(":"); }
function awbHistoryHtml(a,tab){
  const h=AWB.histories.get(awbHistoryKey(a,tab));
  const job=AWB.data.jobs.find(j=>j.id===tab.job);
  const head=tab.tab==="products"?'<div class="awb-actions">'+awbButton("library","图片和网站作品库",a,"awb-link")+'<span>以下为附件快照，可预览或下载复用</span></div>':
    (job?'<div class="awb-actions"><strong>'+esc(job.name)+'</strong>'+(a?awbButton("all-runs","查看全部运行",a,"awb-link"):'<button type="button" data-awb="tab" data-id="" data-tab="tasks" class="awb-link">返回定时任务</button>')+'</div>':"");
  if(!h) return head+'<p class="awb-empty" role="status">正在读取…</p>';
  const body=tab.tab==="products"?h.items.map(awbArtifactHtml).join(""):h.items.map(awbRunHtml).join("");
  return head+'<div class="awb-history">'+body+'</div>'+(h.error?'<p class="awb-error" role="alert">'+esc(h.error)+'</p>':"")+
    (!h.items.length&&!h.loading&&!h.error?'<p class="awb-empty">暂无'+(tab.tab==="products"?"附件":"运行记录")+'</p>':"")+
    (h.loading?'<p class="awb-empty" role="status">正在读取…</p>':(h.next||h.error?awbButton("more",h.error?"重试":"加载更多",a,"awb-link"):""));
}
function awbRunHtml(r){
  const open=AWB.details.has(r.id), detail=AWB.details.get(r.id);
  return '<article class="aprun"><button type="button" class="awb-run-head" data-awb="run" data-id="'+r.id+'" aria-expanded="'+open+'"><span class="awb-dot '+(r.status.startsWith("success")?"ok":"error")+'"></span><span class="awb-run-name">'+esc(r.job_name||"运行记录")+'</span><time>'+awbTime(r.ts)+'</time><span>'+ (r.status.startsWith("success")?"调度成功":"运行异常")+'</span></button><div class="aptsub">'+esc((r.tokens>0?awbTokens(r.tokens)+" Token":"Token 未记录")+' · '+awbDuration(r.duration))+'</div>'+
    (open?awbDetailHtml(r,detail):'<p class="awb-excerpt">'+esc(r.text.slice(0,160))+'</p>')+'</article>';
}
function awbDetailHtml(r,d){
  if(d.loading) return '<p role="status">正在读取运行详情…</p>';
  if(d.error) return '<p class="awb-error" role="alert">'+esc(d.error)+'</p>'+awbButton("retry-run","重试",String(r.id),"awb-link");
  return '<div class="awb-detail"><div class="awb-actions">'+(d.target&&d.target.conv?awbButton("source","来源会话",String(r.id),"awb-link"):"")+awbButton("copy-result","复制结果",String(r.id),"awb-link")+'</div><pre class="awb-result">'+esc(d.text||"未保存结果正文")+'</pre>'+
    '<h3>工具轨迹 <span>'+d.tools.length+' 个节点</span></h3>'+(d.source_linked?(d.tools.length?'<ol class="awb-trace">'+d.tools.map((t,i)=>'<li class="'+t.status+'"><span>'+String(i+1).padStart(2,"0")+'</span><strong>'+esc(t.name)+'</strong><small>'+(t.status==="ok"?"已完成":t.status==="error"?"异常":"未记录完成状态")+(t.children?' · '+t.children+' 子步骤':"")+'</small></li>').join("")+'</ol>':'<p class="awb-empty">这次运行没有保存工具轨迹</p>'):'<p class="awb-empty">历史记录未关联原始执行轮，无法确认工具过程和附件。</p>')+
    d.artifacts.map(e=>awbArtifactHtml({...e,conv:d.artifact_conv})).join("")+'</div>';
}
function awbArtifactHtml(e){
  return '<div class="artifact-card awb-artifact"><div><strong>'+esc(e.title||e.filename)+'</strong><small>'+esc(e.filename)+' · '+awbNumber(e.size)+' 字节</small></div>'+
    (e.conv?'<button type="button" data-awb="preview" data-artifact-id="'+esc(e.id)+'" data-conv="'+esc(e.conv)+'" data-filename="'+esc(e.filename)+'">预览</button><button type="button" data-awb="download" data-artifact-id="'+esc(e.id)+'" data-conv="'+esc(e.conv)+'" data-filename="'+esc(e.filename)+'">下载</button>':'<span>来源不可定位</span>')+'</div>';
}
function renderAutomationWorkbench(){
  const root=$("#wbContent"); if(!root || !automationWorkbenchActive()) return;
  $("#workbenchView").classList.add("automation-workbench");
  $("#wbFab").classList.add("wb-fab-off"); $("#wbDock").innerHTML="";
  $("#wbMultiBar").hidden=true; document.body.classList.remove("wb-multiselect-mode");
  root.innerHTML=awbHeader()+(AWB.error?'<p role="alert" class="awb-error">'+esc(AWB.error)+'</p>':"")+(!AWB.data?'<p role="status" class="awb-empty">'+(AWB.loading?"正在读取工作台…":"工作台暂无数据")+'</p>':awbStats()+'<div id="awbOwners"></div>');
  if(AWB.data) awbRenderOwners();
}
function awbRenderOwners(){
  const root=$("#awbOwners"); if(!root) return;
  const focused=document.activeElement, focusData=root.contains(focused)?{...focused.dataset}:null;
  const d=AWB.data,q=AWB.search.trim().toLowerCase(), active=[], empty=[];
  for(const a of d.agents){
    const all=d.jobs.filter(j=>j.agent_id===a.id);
    const jobs=all.filter(j=>awbMatches(j) && (!q||a.name.toLowerCase().includes(q)||j.name.toLowerCase().includes(q)));
    if(all.length && jobs.length) active.push(awbOwner(a,jobs));
    else if(!all.length && AWB.filter==="all" && (!q||a.name.toLowerCase().includes(q))) empty.push(awbOwner(a,[]));
  }
  const known=new Set(d.agents.map(a=>a.id)), orphan=d.jobs.filter(j=>!known.has(j.agent_id)&&awbMatches(j)&&(!q||j.name.toLowerCase().includes(q)));
  root.innerHTML=active.join("")+(!active.length?'<p class="awb-empty">没有符合条件的定时任务</p>':"")+
    (orphan.length?'<details class="awb-fold" data-awb-fold="orphan" '+(AWB.folds.has("orphan")?'open':'')+'><summary>历史 / 未归属 · '+orphan.length+' 定时任务</summary>'+awbOwnerBody({id:""},orphan,AWB.tabs.get("")||{tab:"tasks"})+'</details>':"")+
    (empty.length?'<details class="awb-fold" data-awb-fold="empty" '+(AWB.folds.has("empty")?'open':'')+'><summary>暂无定时任务的 Agent · '+empty.length+'</summary>'+empty.join("")+'</details>':"");
  if(focusData?.awb){
    [...root.querySelectorAll("[data-awb]")].find(b=>b.dataset.awb===focusData.awb && b.dataset.id===focusData.id && b.dataset.tab===focusData.tab)?.focus({preventScroll:true});
  }
}
async function awbSelect(agent,tab="tasks",job=""){
  AWB.open.add(agent); if(!agent) AWB.folds.add("orphan");
  AWB.tabs.set(agent,{tab,job}); awbRenderOwners();
  if(tab!=="tasks") await awbLoadHistory(agent);
}
async function awbLoadHistory(agent,more=false){
  const tab=AWB.tabs.get(agent)||{tab:"runs"},key=awbHistoryKey(agent,tab),prev=AWB.histories.get(key);
  if(more && prev?.loading) return;
  const state={items:more?(prev?.items||[]):[],next:more?prev?.next:null,loading:true,error:"",version:++AWB.request};
  AWB.histories.set(key,state); awbRenderOwners();
  try{
    const query=new URLSearchParams({agent,job:tab.job||"",before:String(state.next||0)});
    const data=await awbJson("/workbench/automation/"+(tab.tab==="products"?"artifacts":"runs")+"?"+query);
    if(AWB.histories.get(key)!==state) return;
    const ids=new Set(state.items.map(i=>i.id)); state.items.push(...data.items.filter(i=>!ids.has(i.id))); state.next=data.next;
  }catch(e){ if(AWB.histories.get(key)!==state) return; state.error=e.message; }
  state.loading=false; if(awbVisible()) awbRenderOwners();
}
async function awbLoadRun(id){
  const state={loading:true}; AWB.details.set(id,state); awbRenderOwners();
  try{ const d=await awbJson("/workbench/automation/run?id="+id); if(AWB.details.get(id)===state) AWB.details.set(id,d); }
  catch(e){ if(AWB.details.get(id)===state) AWB.details.set(id,{error:e.message}); }
  if(awbVisible()) awbRenderOwners();
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
  if(action==="filter"){AWB.filter=id;renderAutomationWorkbench();return;}
  if(action==="owner"){ AWB.open.has(id)?AWB.open.delete(id):AWB.open.add(id); awbRenderOwners(); return; }
  if(action==="products"||action==="tab"){await awbSelect(id,action==="products"?"products":b.dataset.tab);return;}
  if(action==="job-runs"){await awbSelect(b.dataset.owner,"runs",id);return;}
  if(action==="all-runs"){await awbSelect(id,"runs");return;}
  if(action==="more"){await awbLoadHistory(id,true);return;}
  if(action==="run"||action==="retry-run"){
    const n=Number(id); if(action==="run"&&AWB.details.has(n)){AWB.details.delete(n);awbRenderOwners();}else await awbLoadRun(n); return;
  }
  if(action==="menu"){await awbManage(b,id);return;}
  if(action==="create"){openCronModal(null);S.cronAgentId=id||null;$("#cfCwd").value=agentById(id)?.workdir||"";return;}
  if(action==="library"){LIB.filters.agent_id=id;LIB.filters.kind="";LIB.filters.state="visible";LIB.filters.search="";await openLibrary();return;}
  if(action==="source"){const d=AWB.details.get(Number(id));if(d?.target?.conv) await openConvAtTurn(d.target.conv,d.target.turn_id,d.target.ts);return;}
  if(action==="copy-result"){
    try{await navigator.clipboard.writeText(AWB.details.get(Number(id))?.text||"");b.textContent="已复制";}catch(e){alert("复制失败，请手动选择结果正文");}return;
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
document.addEventListener("keydown",event=>{
  if(!awbVisible()||!apMenuEl) return;
  if(event.key==="Escape"){event.preventDefault();closeApMenu();document.querySelector('[data-awb="menu"][data-id="'+CSS.escape(AWB.menuTrigger||"")+'"]')?.focus();}
  if(!["ArrowDown","ArrowUp","Home","End"].includes(event.key)) return;
  const items=[...apMenuEl.querySelectorAll("button")],index=items.indexOf(document.activeElement);
  event.preventDefault(); items[event.key==="Home"?0:event.key==="End"?items.length-1:(index+(event.key==="ArrowUp"?-1:1)+items.length)%items.length]?.focus();
});
