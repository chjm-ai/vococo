"use strict";
// 2026-09-29 Agent(项目升级版,后端见 memory/agents.py):侧栏「Agent」Tab + 右侧 Agent 面板。
// 与「项目」「定时」Tab 并存,方案定了再下架那两个。
// - 侧栏:每个 Agent 一行(像素头像 + 名称 + 折线箭头),点行 = 打开它的主会话(定时结果都推到这里),
//   箭头展开(第一行固定是主会话,下面是名下独立会话),hover「＋」在它名下开新会话;状态做在头像上(见 agentWorking/agentUnread)
// - 右侧面板(标题栏按钮开关):动态 / 定时 / 目标 / 设定 / 文件

// ── 像素头像:风格取自 logo「小幽」——crispEdges 方块、深色眼睛、左上一格高光 ──────────
// 形状 = 每行的实心区间 [起列, 止列];eye = 眼睛所在行。键名与后端 AVATAR_SHAPES 一致
const AV_SHAPES = {
  xiaoyou: {name:"小幽", eye:5, rows:{0:[[4,7]],1:[[3,8]],2:[[2,9]],3:[[2,9]],4:[[1,10]],5:[[1,10]],6:[[1,10]],7:[[1,10]],8:[[1,10]],9:[[1,10]],10:[[1,2],[4,5],[6,7],[9,10]]}},
  ghost:   {name:"幽灵", eye:5, rows:{0:[[4,7]],1:[[3,8]],2:[[2,9]],3:[[2,9]],4:[[1,10]],5:[[1,10]],6:[[1,10]],7:[[1,10]],8:[[2,9]],9:[[2,9]],10:[[3,8]]}},
  blob:    {name:"圆团", eye:5, rows:{1:[[4,7]],2:[[2,9]],3:[[1,10]],4:[[1,10]],5:[[1,10]],6:[[1,10]],7:[[1,10]],8:[[1,10]],9:[[2,9]],10:[[4,7]]}},
  square:  {name:"方块", eye:5, rows:{0:[[5,6]],1:[[5,6]],2:[[1,10]],3:[[1,10]],4:[[1,10]],5:[[1,10]],6:[[1,10]],7:[[1,10]],8:[[1,10]],9:[[1,10]],10:[[2,3],[8,9]]}},
  cat:     {name:"猫耳", eye:5, rows:{1:[[2,3],[8,9]],2:[[1,4],[7,10]],3:[[1,10]],4:[[1,10]],5:[[1,10]],6:[[1,10]],7:[[1,10]],8:[[1,10]],9:[[2,9]],10:[[3,8]]}},
  drop:    {name:"水滴", eye:6, rows:{0:[[5,6]],1:[[5,6]],2:[[4,7]],3:[[3,8]],4:[[2,9]],5:[[1,10]],6:[[1,10]],7:[[1,10]],8:[[1,10]],9:[[2,9]],10:[[3,8]]}},
};
const AV_COLORS = {orange:"蜜橙", coral:"珊瑚", lime:"青柠", lake:"湖蓝", grape:"葡萄", pink:"樱粉", gold:"金黄", teal:"青碧"};
const AV_EYES = {
  dot:    {name:"圆眼", cells:r=>[[r,3],[r,4],[r,7],[r,8],[r+1,3],[r+1,4],[r+1,7],[r+1,8]]},
  small:  {name:"豆眼", cells:r=>[[r,4],[r+1,4],[r,7],[r+1,7]]},
  squint: {name:"眯眼", cells:r=>[[r+1,3],[r+1,4],[r+1,7],[r+1,8]]},
};
function avatarSvg(av){
  av=av||{};
  const sh=AV_SHAPES[av.shape]||AV_SHAPES.blob;
  const color="var(--av-"+(AV_COLORS[av.color]?av.color:"orange")+")";
  let out="", first=null;
  for(const [y,spans] of Object.entries(sh.rows)){
    for(const [a,b] of spans){
      out+='<rect x="'+a+'" y="'+y+'" width="'+(b-a+1)+'" height="1" fill="'+color+'"/>';
      if(first===null) first=[+y,a];
    }
  }
  if(first) out+='<rect x="'+(first[1]+1)+'" y="'+(first[0]+2)+'" width="1" height="1" fill="var(--wh-2)"/>';   // 受光面高光
  for(const [y,x] of (AV_EYES[av.eyes]||AV_EYES.dot).cells(sh.eye)) out+='<rect x="'+x+'" y="'+y+'" width="1" height="1" fill="var(--av-eye)"/>';
  return '<svg class="agav" viewBox="0 0 12 11" shape-rendering="crispEdges" aria-hidden="true">'+out+'</svg>';
}

// ── 数据 ────────────────────────────────────────────────────────────────
S.agents = [];
S.agentPanelTab = localStorage.getItem("vococo_agent_tab") || "feed";
S.agentPanelOn = localStorage.getItem("vococo_agent_panel") === "1";   // 面板开关记住上次选择(跨会话)
async function loadAgents(){
  try{ const r=await api("/agents"); S.agents=(await r.json()).agents||[]; }
  catch(e){}   // 失败保留上次成功列表
  patchAgentTitles(S.convs);
  renderConvs(); syncAgentHeader();
  if(typeof updateEmpty==="function" && $("#empty").style.display==="flex") updateEmpty();   // 欢迎屏可能先按默认样子画了
}
function agentById(id){ return S.agents.find(a=>a.id===id) || null; }
function agentByMainConv(conv){ return S.agents.find(a=>a.main_conv===conv) || null; }
// 会话属于哪个 Agent:项目会话按项目哈希;定时任务会话按任务的 agent_id;主会话 = 通用
function agentForConv(conv){
  conv=String(conv||"").replace(/^local-/,"");
  if(!conv) return null;
  const h=convProject(conv);
  if(h) return S.agents.find(a=>a.project_hash===h) || null;
  const job=(S.cronJobs||[]).find(j=>j.conv===conv);
  if(job) return job.agent_id ? agentById(job.agent_id) : null;
  if(conv==="main") return agentById("general");
  return null;
}
// Agent 主会话在 /conversations 里的标题是第一条消息(多半是「⏰ 任务名」),显示成 Agent 名字
function patchAgentTitles(list){
  for(const c of list||[]){ const a=agentByMainConv(c.conv); if(a && a.id!=="general") c.title=a.name; }
}
async function agentPost(path, body){
  const r=await api(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const d=await r.json().catch(()=>({}));
  if(!r.ok || d.error) throw new Error(d.error||"操作失败");
  return d;
}

// ── 侧栏「Agent」Tab ────────────────────────────────────────────────────
// 子会话 = 该 Agent 名下的独立会话(不含主会话——它固定在展开后第一行,见 buildAgentMainRow);通用 Agent 收不属于任何项目的会话
function agentOwnsConv(a, conv){
  if(conv==="main" || conv===a.main_conv) return false;
  const h=convProject(conv);
  return a.id==="general" ? h===null : h===a.project_hash;
}
function agentConvs(a){
  return S.convs.filter(c=>{
    if(!agentOwnsConv(a, c.conv)) return false;
    if(S.convFilter==="archived" && !c.archived) return false;
    if(S.convFilter==="active" && c.archived) return false;
    return true;
  });
}
// Agent 行的两种状态提示(样式见 styles.css 的 .projgrp.agrow):
//   工作中 = 头像弹跳:主会话、名下任一会话或(总助理名下的)语音任务在跑,收起/展开都照实反映
//   未读   = 头像右上角红色数字:有几个会话留着没看过的完成结果(打开那个会话就减一;归档的不算)
// 统计范围和展开后的列表一致(置顶的语音任务列表里不出,这里也不算)。已知局限:底部筛选切到「归档」时
// 后端只回归档会话,未归档子会话不在 S.convs 里,这期间数字只剩主会话和语音任务。
// 2026-09-30 主人定案:未读不用动效——动的东西扫一眼分不清哪行有未读,静态数字才一目了然。
function agentOwnItems(a){
  const main=S.convs.find(c=>c.conv===a.main_conv)||{conv:a.main_conv};
  const tasks=a.id==="general" ? ((S.voiceSidebar&&S.voiceSidebar.tasks)||[]).filter(t=>!t.pinned) : [];
  return [main, ...S.convs.filter(c=>agentOwnsConv(a, c.conv)), ...tasks];
}
const agentItemBusy=c=>S.live[c.conv] || c.task_status==="queued" || c.task_status==="running";   // 同行内显示橙色闪点的条件
function agentWorking(a){ return agentOwnItems(a).some(agentItemBusy); }
// 还在跑的不算未读:那一行显示的是闪点,数字跟着行走
function agentUnread(a){
  return agentOwnItems(a).filter(c=>!c.archived && !agentItemBusy(c) && (c.pending_review || S.pendingReview[c.conv])).length;
}
function agentKey(a){ return "agent:"+a.id; }
function renderAgentsTab(box, inCall){
  if(!S.agents.length){ box.append(sideTabEmpty("加载中…")); return; }
  for(const a of S.agents) renderAgentGroup(box, a, inCall);
  const add=el("div","projgrp projadd"); add.textContent="＋ 新建 Agent…"; add.onclick=openAgentModal; box.append(add);
}
function renderAgentGroup(box, a, inCall){
  const convs=agentConvs(a);
  // 语音后台任务不分项目,归总助理(原「项目」Tab 的默认项目也是这么放的),和会话按最后活跃时间混排
  const tasks=a.id==="general" ? ((S.voiceSidebar&&S.voiceSidebar.tasks)||[]).filter(t=>!t.pinned
    && (S.convFilter==="archived" ? t.archived : S.convFilter==="active" ? !t.archived : true)) : [];
  // 名下只有主会话时不给展开:点 Agent 行就进主会话,展开出来也只有孤零零一行
  const hasChildren=convs.length+tasks.length>0;
  const k=agentKey(a), open=hasChildren && S.expanded.has(k);
  // 展开时高亮落在下面的「主会话」行上,Agent 行只在收起时代它高亮
  const h=el("div","projgrp agrow"+(!inCall && !open && S.conv===a.main_conv?" active":""));
  const working=agentWorking(a), unread=agentUnread(a);
  if(working) h.dataset.state="working";
  const tips=[working?"AI 正在工作中":"", unread?unread+" 个会话有未读结果":""].filter(Boolean);
  if(tips.length) h.title=tips.join(" · ");
  // 头像外包一层:角标定位在它右上角,且不跟着头像的弹跳动效一起动
  const av=el("span","agavwrap"); av.innerHTML=avatarSvg(a.avatar);
  if(unread){ const b=el("span","agbadge"); b.textContent=unread>9?"9+":String(unread); av.append(b); }
  h.append(av);
  const nm=el("span","pgname"); nm.textContent=a.name; h.append(nm);
  // 箭头只在有子会话时显示;隐藏时仍占位(visibility),右侧「＋」不跟着挪
  const caret=el("span","pgcaret"+(hasChildren?"":" agnone"));
  caret.append(el("span","chev"+(open?" down":"")));
  caret.title=open?"收起会话":"展开会话";
  caret.onclick=ev=>{ ev.stopPropagation(); if(!hasChildren) return;
    if(open){ S.expanded.delete(k); S.moreShown.delete(k); } else S.expanded.add(k);
    saveExpanded(); renderConvs(); };
  h.append(caret);
  const add=el("button","pgadd"); add.textContent="＋"; add.title="新会话";
  add.onclick=ev=>{ ev.stopPropagation(); S.expanded.add(k); newChatIn(a.project_hash||null); };
  h.append(add);
  if(a.project_hash) bindProjDrag(h, a.project_hash);   // 拖动排序;总助理固定在最前
  h.onclick=()=>{ if(S.dragMoved){ S.dragMoved=false; return; } openAgentMain(a); };   // 拖拽落地那一下别顺带打开
  box.append(h);
  if(!open) return;
  box.append(buildAgentMainRow(a, inCall));
  const rows=[...tasks.map(t=>({ts:t.last_ts||0, build:()=>buildVoiceTaskRow(t, inCall)})),
              ...convs.map(c=>({ts:c.last_ts||0, build:()=>buildConvRow(c, inCall)}))]
    .sort((x,y)=>y.ts-x.ts).map(it=>it.build()).filter(Boolean);
  const shown=S.moreShown.has(k)?rows:rows.slice(0, CONV_SHOW_MAX);
  for(const r of shown) box.append(r);
  if(rows.length>shown.length){
    const more=el("div","conv ingroup convmore");
    const mct=el("div","ct"); mct.textContent="展开更多"; more.append(mct);
    more.onclick=()=>{ S.moreShown.add(k); renderConvs(); };
    box.append(more);
  }
}
// 展开后的第一行:主会话,和子会话列在同一层,结构上一眼看出「Agent = 主会话 + 若干子会话」。
// 点 Agent 行本身照样进主会话,这一行只是把它摆出来;主会话不能归档/删除,所以不给 ⋯ 菜单和滑动手势。
function buildAgentMainRow(a, inCall){
  const c=S.convs.find(x=>x.conv===a.main_conv)||{conv:a.main_conv};
  const e=el("div","conv ingroup agmain"+(!inCall && S.conv===a.main_conv?" active":""));
  e.dataset.conv=a.main_conv;
  const body=el("div","cvbody");
  if(S.live[c.conv]){ const dot=el("span","livedot"); dot.title="AI 正在回复中"; body.append(dot); }
  else if(c.pending_review || S.pendingReview[c.conv]){ const dot=el("span","reviewdot"); dot.title="有新内容"; body.append(dot); }
  const ct=el("div","ct"); ct.textContent="主会话"; body.append(ct);
  const tm=fmtTime(c.last_ts); if(tm){ const tmEl=el("span","ctime"); tmEl.textContent=tm; body.append(tmEl); }
  e.append(body);
  e.onclick=()=>openAgentMain(a);
  return e;
}
function openAgentMain(a){
  // 主会话还没有任何记录时,/conversations 里没有它;先放一条占位,标题/项目归属才对得上
  if(a.main_conv!=="main" && !S.convs.some(c=>c.conv===a.main_conv))
    S.convs.push({conv:a.main_conv, title:a.name, turns:0, last_ts:null});
  openConv(a.main_conv);
}

// ── 新建 Agent 弹窗:名字 + 目录(默认新建 / 用已有目录)────────────────────────
function openAgentModal(){
  $("#agName").value=""; $("#agPath").value="";
  $("#agDirNew").checked=true; syncAgentDirMode();
  $("#agentModal").hidden=false; $("#agName").focus();
}
function closeAgentModal(){ $("#agentModal").hidden=true; }
function syncAgentDirMode(){ $("#agPathRow").hidden=!$("#agDirPick").checked; }
async function createAgent(){
  const name=$("#agName").value.trim();
  const path=$("#agDirPick").checked ? $("#agPath").value.trim() : "";
  if(!name){ alert("给它起个名字"); return; }
  if($("#agDirPick").checked && !path){ alert("选一个目录"); return; }
  let d;
  try{ d=await agentPost("/agents/create",{name, path}); }catch(e){ alert(e.message); return; }
  closeAgentModal();
  await Promise.all([loadAgents(), loadProjects()]);
  const a=agentById(d.agent.id)||d.agent;
  S.agentPanelOn=true; S.agentPanelTab="setup"; saveAgentPanelPref();   // 新建完直接落到「设定」,接着定人格和目标
  openAgentMain(a);
}
$("#agDirNew").onchange=syncAgentDirMode;
$("#agDirPick").onchange=syncAgentDirMode;
$("#agBrowse").onclick=()=>openDirPicker("选一个文件夹当 Agent 的工作目录","✓ 用这个目录", p=>{ $("#agPath").value=p; });
$("#agCancel").onclick=closeAgentModal;
$("#agCreate").onclick=createAgent;
$("#agName").onkeydown=e=>{ if(e.key==="Enter") createAgent(); };
$("#agentModal").onclick=e=>{ if(e.target===$("#agentModal")) closeAgentModal(); };

// ── 标题栏按钮 + 右侧面板 ──────────────────────────────────────────────
function saveAgentPanelPref(){
  try{ localStorage.setItem("vococo_agent_panel", S.agentPanelOn?"1":"0"); localStorage.setItem("vococo_agent_tab", S.agentPanelTab); }catch(e){}
}
function currentAgent(){ return S.surface==="chat" ? agentForConv(S.conv) : null; }
// 切会话/数据刷新后调:按钮只在 Agent 名下的会话出现;面板开着就跟到新 Agent
function syncAgentHeader(){
  const a=currentAgent(), btn=$("#convAgentBtn");
  btn.hidden=!a;
  if(a){ btn.innerHTML=avatarSvg(a.avatar); btn.title=a.name+" · 动态 / 定时 / 目标 / 设定 / 文件"; btn.classList.toggle("on", S.agentPanelOn); }
  // Agent 主会话标题 = Agent 名字(副标题标一下是主会话)
  const am=agentByMainConv(S.conv);
  if(am && S.surface==="chat") $("#convTitle").textContent=am.name;
  const show=!!a && S.agentPanelOn && $("#docPreview").hidden;
  const panel=$("#agentPanel");
  if(!show){ panel.hidden=true; return; }
  if(panel.hidden || panel.dataset.agent!==a.id){ panel.hidden=false; panel.dataset.agent=a.id; renderAgentPanel(); }
  else renderApInfo(a);   // 同一 Agent 下切会话(主↔子)/改了名字:只刷身份卡,别冲掉正文里没保存的输入
}
function toggleAgentPanel(){
  S.agentPanelOn=!S.agentPanelOn; saveAgentPanelPref();
  if(S.agentPanelOn && typeof closeDocPreview==="function") closeDocPreview();   // 两个右侧栏不同时占位
  $("#agentPanel").dataset.agent="";
  syncAgentHeader();
}
function hideAgentPanel(){ $("#agentPanel").hidden=true; $("#agentPanel").dataset.agent=""; }
$("#convAgentBtn").onclick=toggleAgentPanel;
$("#apClose").onclick=()=>{ S.agentPanelOn=false; saveAgentPanelPref(); syncAgentHeader(); };

const AP_TABS=[{key:"feed",label:"动态"},{key:"tasks",label:"定时"},{key:"goal",label:"目标"},{key:"setup",label:"设定"},{key:"files",label:"文件"}];
function renderAgentPanel(){
  const a=agentById($("#agentPanel").dataset.agent); if(!a) return;
  renderApInfo(a);
  const tabs=$("#apTabs"); tabs.innerHTML="";
  for(const t of AP_TABS){
    const b=el("div","sidetab"+(S.agentPanelTab===t.key?" active":"")); b.textContent=t.label;
    b.onclick=()=>{ S.agentPanelTab=t.key; saveAgentPanelPref(); renderAgentPanel(); };
    tabs.append(b);
  }
  const body=$("#apBody"); body.innerHTML=""; body.scrollTop=0;
  const fn={feed:renderApFeed, tasks:renderApTasks, goal:renderApGoal, setup:renderApSetup, files:renderApFiles}[S.agentPanelTab]||renderApFeed;
  fn(body, a);
}
// 面板顶部的身份卡:子会话打开面板时,第一眼要知道它属于哪个 Agent、这个 Agent 是干什么的
function renderApInfo(a){
  const box=$("#apInfo"); box.innerHTML="";
  const av=el("div","apiav"); av.innerHTML=avatarSvg(a.avatar);
  const main=el("div","apimain");
  const nm=el("div","apiname"); nm.textContent=a.name;
  const sub=el("div","apisub"); sub.textContent=a.summary || "还没写职责";
  main.append(nm, sub);
  if(a.goal){
    const g=el("button","apigoal"); g.type="button"; g.title="看目标和计划";
    const lb=el("span","aggl"); lb.textContent="目标";
    const t=el("span","aggt"); t.textContent=a.goal;
    g.append(lb, t); g.onclick=()=>{ S.agentPanelTab="goal"; saveAgentPanelPref(); renderAgentPanel(); };
    main.append(g);
  }
  box.append(av, main);
  if(S.conv!==a.main_conv){
    const go=el("button","apimainbtn"); go.type="button"; go.textContent="主会话"; go.title="回到「"+a.name+"」的主会话";
    go.onclick=()=>openAgentMain(a);
    box.append(go);
  }
}
// cron 表达式 → 人话:直接借定时任务弹窗里的预设文案(「每周一早 9 点」),没命中就原样显示
function scheduleText(sch, fallback){
  const expr=sch && sch.kind==="cron" ? sch.expr : "";
  const opt=expr && [...$("#cfPreset").options].find(o=>o.value===expr);
  return opt ? opt.textContent : (fallback||expr||"");
}
// 目标/计划文档:去掉顶上「# 目标」这种重复标题,空小节不显示
function docHtml(md){
  const parts=String(md||"").replace(/^# .*\n?/, "").split(/^(?=## )/m);
  return mdToHtml(parts.filter(p=>!/^## /.test(p) || p.replace(/^## .*$/m,"").trim()).join("").trim());
}
function apEmpty(text){ const e=el("div","apempty"); e.textContent=text; return e; }
function apSection(title){ const e=el("div","apsec"); e.textContent=title; return e; }
// 面板异步拉数据期间可能切了 Agent/Tab:回来时对不上就丢弃
function apStale(a, tab){ return $("#agentPanel").dataset.agent!==a.id || S.agentPanelTab!==tab; }

// 动态:定时任务运行结果的精简列表(完整内容在主会话里)
async function renderApFeed(body, a){
  body.append(apEmpty("加载中…"));
  let runs=[], stats=null;
  try{ const d=await (await api("/agents/runs?id="+encodeURIComponent(a.id))).json(); runs=d.runs||[]; stats=d.stats||null; }catch(e){}
  if(apStale(a,"feed")) return;
  body.innerHTML="";
  if(!runs.length){ body.append(apEmpty("还没有运行记录。定时任务和从这里派出的后台任务跑完会出现在这里。")); return; }
  if(stats && stats.runs) body.append(apStatsCard(stats));
  for(const r of runs){
    const row=el("div","aprun");
    const head=el("div","aprhead");
    const dot=el("span","aprdot "+(String(r.status||"").startsWith("success")?"ok":r.status==="skipped"?"":"err"));
    const nm=el("span","aprname"); nm.textContent=r.job_name;
    const tm=el("span","aprtime"); tm.textContent=fmtTime(r.ts);
    head.append(dot,nm,tm);
    const tx=el("div","aprtext"); tx.textContent=(r.text||"").replace(/\s+/g," ").slice(0,160);
    row.append(head,tx);
    const mt=runMetricsText(r);
    if(mt){ const m=el("div","aprmeta"); m.textContent=mt; row.append(m); }
    // 定时任务 → 它的会话;派出的后台任务(没复制进主会话,turn_id 为空)→ 任务会话
    row.onclick=()=>{ const j=(S.cronJobs||[]).find(x=>x.job_id===r.job_id); openConv(j?j.conv:(r.turn_id==null&&r.job_id?"task:"+r.job_id:a.main_conv)); };
    body.append(row);
  }
}

function fmtTokens(n){ n=+n||0; return n>=10000 ? (n/10000).toFixed(1)+" 万" : String(n); }
function fmtDur(s){ s=+s||0; return s>=60 ? (s/60).toFixed(1)+" 分钟" : Math.round(s)+" 秒"; }
function runMetricsText(r){
  if(!(r.tokens||r.duration||r.tool_calls)) return "";
  return fmtTokens(r.tokens)+" token · "+fmtDur(r.duration)+" · "+(r.tool_calls||0)+" 次工具";
}
// 动态顶部:近 7 天汇总(次数 / 成功率 / token / 平均耗时)
function apStatsCard(st){
  const box=el("div","apstats");
  const cell=(v,l)=>{ const c=el("div","apstat"); const b=el("b"); b.textContent=v; const s=el("span"); s.textContent=l; c.append(b,s); return c; };
  box.append(
    cell(st.runs+" 次", "近 "+st.days+" 天"),
    cell(st.success_rate==null?"—":Math.round(st.success_rate*100)+"%", "成功率"),
    cell(fmtTokens(st.tokens), "token"),
    cell(fmtDur(st.avg_duration), "平均耗时"),
  );
  return box;
}

// 定时:挂在这个 Agent 名下的任务;点行看该任务自己的会话
function renderApTasks(body, a){
  const jobs=(S.cronJobs||[]).filter(j=>j.agent_id===a.id);
  if(!jobs.length) body.append(apEmpty("还没有定时任务。"));
  for(const j of jobs){
    const row=el("div","aptask"+(S.conv===j.conv?" active":""));
    const main=el("div","aptmain");
    const nm=el("div","aptname"); nm.textContent=j.title||"定时任务"; if(!j.enabled) nm.classList.add("off");
    const sub=el("div","aptsub"); sub.textContent=scheduleText(j.schedule, j.schedule_desc)+(j.last_ts?" · 上次 "+fmtTime(j.last_ts):"");
    main.append(nm,sub);
    const sw=el("button","hswitch"+(j.enabled?" on":"")); sw.type="button"; sw.title=j.enabled?"已启用 · 点击停用":"已停用 · 点击启用";
    sw.onclick=async ev=>{ ev.stopPropagation(); await toggleCronJob(j.job_id, !j.enabled); renderAgentPanel(); };
    const more=el("button","more"); more.textContent="⋯"; more.title="更多";
    more.onclick=ev=>{ ev.stopPropagation(); openApTaskMenu(more, j, a); };
    row.append(main,sw,more);
    row.onclick=()=>openConv(j.conv);
    body.append(row);
  }
  const add=el("div","apadd"); add.textContent="＋ 新定时任务";
  add.onclick=()=>{ openCronModal(null); S.cronAgentId=a.id; if(a.workdir) $("#cfCwd").value=a.workdir; };
  body.append(add);
}
let apMenuEl=null;
function closeApMenu(){ if(apMenuEl){ apMenuEl.remove(); apMenuEl=null; } }
document.addEventListener("click", closeApMenu);
function apMenuAt(btn, items){
  closeApMenu();
  const m=el("div","convmenu"); m.style.visibility="hidden";
  for(const it of items){
    const b=el("button","cmitem"+(it.danger?" danger":"")); b.innerHTML=(it.icon?ic(it.icon)+" ":"")+esc(it.label);
    if(it.sub) b.classList.add("cmsub");
    b.onclick=ev=>{ ev.stopPropagation(); closeApMenu(); it.run(); };
    m.append(b);
  }
  document.body.append(m); apMenuEl=m;
  const r=btn.getBoundingClientRect();
  let top=r.bottom+4, left=r.right-m.offsetWidth;
  if(top+m.offsetHeight>window.innerHeight) top=r.top-4-m.offsetHeight;
  m.style.top=Math.max(6,top)+"px"; m.style.left=Math.max(6,left)+"px"; m.style.visibility="visible";
}
function openApTaskMenu(btn, j, a){
  const others=S.agents.filter(x=>x.id!==a.id);
  apMenuAt(btn, [
    {icon:"edit", label:"编辑", run:()=>openCronModal(j)},
    {icon:"zap", label:"试跑", run:()=>runCronNow(j)},
    {icon:"copy", label:"复制到…", run:()=>pickAgentMenu(btn, others, t=>copyJobTo(j, t))},
    {icon:"folder", label:"移动到…", run:()=>pickAgentMenu(btn, others, t=>moveJobTo(j, t))},
    {icon:"trash", label:"删除", danger:true, run:async()=>{ await deleteCronJob(j.job_id, j.title, j.conv); renderAgentPanel(); }},
  ]);
}
function pickAgentMenu(btn, list, fn){
  setTimeout(()=>apMenuAt(btn, list.map(t=>({label:t.name, run:()=>fn(t)}))), 0);   // 等上一个菜单的 click 冒泡完再开
}
async function runCronNow(j){
  try{ await agentPost("/cron/jobs/run",{id:j.job_id}); }catch(e){ alert(e.message); return; }
  openConv(j.conv);
}
function jobBody(j, agentId){
  return {name:j.name||j.title, prompt:j.prompt, schedule:j.schedule, cwd:j.cwd||"", model:j.model||"",
          mode:j.mode, command:j.command||"", summarize_prompt:j.summarize_prompt||"", agent_id:agentId};
}
async function copyJobTo(j, t){
  const b=jobBody(j, t.id);
  if(b.schedule && (b.schedule.kind==="webhook")) b.schedule={kind:"webhook"};   // 密钥不复制,新任务自己生成
  if(t.workdir && j.cwd===(agentById(j.agent_id)||{}).workdir) b.cwd=t.workdir;   // 原来跟着 Agent 工作目录走的,换成目标 Agent 的
  try{ await agentPost("/cron/jobs/create", b); }catch(e){ alert(e.message); return; }
  await loadCronSidebar(); await loadAgents(); renderAgentPanel();
}
async function moveJobTo(j, t){
  const b=jobBody(j, t.id); b.id=j.job_id; b.target=j.target;
  try{ await agentPost("/cron/jobs/update", b); }catch(e){ alert(e.message); return; }
  await loadCronSidebar(); await loadAgents(); renderAgentPanel();
}

// 目标:GOAL.md(你定)+ PLAN.md(它拆)+ 每周复盘任务 —— 闭环见 memory/agents.py 头注释
async function renderApGoal(body, a){
  body.append(apEmpty("加载中…"));
  let g;
  try{ g=await (await api("/agents/goal?id="+encodeURIComponent(a.id))).json(); }catch(e){ body.innerHTML=""; body.append(apEmpty("加载失败")); return; }
  if(apStale(a,"goal")) return;
  body.innerHTML="";
  const hasGoal=/^(?!#).*\S/m.test(g.goal||"");
  if(!hasGoal){
    body.append(apEmpty("还没设目标。写清目标、成功标准和不做的事,它会拆成计划、每周复盘、把进展写回来。"));
    const b=el("button","apbtn primary"); b.textContent="设定目标";
    b.onclick=()=>editGoal(body, a, g.goal||g.goal_template);
    body.append(b); return;
  }
  const acts=el("div","apacts");
  const edit=el("button","apbtn"); edit.textContent="编辑目标"; edit.onclick=()=>editGoal(body, a, g.goal);
  const plan=el("button","apbtn"); plan.textContent="拆解计划"; plan.title="让它按目标拆里程碑和任务,写进 PLAN.md";
  plan.onclick=()=>{ openAgentMain(a); send(g.plan_prompt); };
  acts.append(edit, plan);
  if(g.review){
    const rv=el("button","apbtn"); rv.textContent="立即复盘";
    rv.onclick=()=>runCronNow({job_id:g.review.id, conv:"task:"+g.review.id});
    acts.append(rv);
  }
  body.append(acts);
  if(g.review){
    const note=el("div","apnote");
    note.textContent="复盘:"+scheduleText({kind:"cron", expr:g.review.desc}, g.review.desc)+(g.review.enabled?"":"(已停用)")+(g.review.last_run_at?" · 上次 "+fmtTime(g.review.last_run_at):"");
    body.append(note);
  }
  const gd=el("div","apdoc bubble"); gd.innerHTML=docHtml(g.goal); body.append(gd);
  body.append(apSection("计划"));
  if(/^(?!#).*\S/m.test(g.plan||"")){ const pd=el("div","apdoc bubble"); pd.innerHTML=docHtml(g.plan); body.append(pd); }
  else body.append(apEmpty("还没拆。点「拆解计划」让它按目标拆。"));
}
function editGoal(body, a, text){
  body.innerHTML="";
  const ta=el("textarea","aptext"); ta.value=text||""; ta.rows=18;
  const acts=el("div","apacts");
  const cancel=el("button","apbtn"); cancel.textContent="取消"; cancel.onclick=()=>renderAgentPanel();
  const save=el("button","apbtn primary"); save.textContent="保存";
  save.onclick=async()=>{
    try{ await agentPost("/agents/doc",{id:a.id, name:"GOAL.md", text:ta.value}); }catch(e){ alert(e.message); return; }
    await loadCronSidebar();   // 第一次写目标会自动挂上每周复盘任务
    renderAgentPanel();
  };
  acts.append(cancel, save);
  const note=el("div","apnote"); note.textContent="「当前进展」「复盘记录」由每周复盘写回,不用手填。";
  body.append(note, acts, ta);
  ta.focus();
}

// 设定:名称、头像、人格与技能(AGENT.md)
async function renderApSetup(body, a){
  const head=el("div","apsetup");
  const big=el("div","apavbig"); big.innerHTML=avatarSvg(a.avatar);
  const nameIn=el("input","apname"); nameIn.value=a.name; nameIn.maxLength=40;
  if(a.id==="general") nameIn.title="通用 Agent 接住不属于任何项目的会话";
  const saveName=async()=>{
    const v=nameIn.value.trim();
    if(!v || v===a.name){ nameIn.value=a.name; return; }
    try{ await agentPost("/agents/update",{id:a.id, name:v}); }catch(e){ alert(e.message); nameIn.value=a.name; return; }
    await loadAgents();
  };
  nameIn.onblur=saveName; nameIn.onkeydown=e=>{ if(e.key==="Enter") nameIn.blur(); };
  head.append(big, nameIn);
  body.append(head);
  const pick=(title, dict, key)=>{
    body.append(apSection(title));
    const rowEl=el("div","appick");
    for(const [k,v] of Object.entries(dict)){
      const b=el("button","apchip"+(a.avatar[key]===k?" on":"")); b.type="button"; b.title=v.name||v;
      b.innerHTML=avatarSvg({...a.avatar, [key]:k});
      b.onclick=async()=>{
        try{ await agentPost("/agents/update",{id:a.id, avatar:{...a.avatar, [key]:k}}); }catch(e){ alert(e.message); return; }
        await loadAgents(); renderAgentPanel();
      };
      rowEl.append(b);
    }
    body.append(rowEl);
  };
  pick("形状", AV_SHAPES, "shape"); pick("颜色", AV_COLORS, "color"); pick("眼睛", AV_EYES, "eyes");
  body.append(apSection("人格与技能 · AGENT.md"));
  const ta=el("textarea","aptext"); ta.rows=14; ta.placeholder="它是谁、怎么说话、会用哪些技能。每次开工都会读。";
  body.append(ta);
  const acts=el("div","apacts");
  const save=el("button","apbtn primary"); save.textContent="保存"; save.disabled=true;
  ta.oninput=()=>{ save.disabled=false; };
  save.onclick=async()=>{
    try{ await agentPost("/agents/doc",{id:a.id, name:"AGENT.md", text:ta.value}); }catch(e){ alert(e.message); return; }
    save.disabled=true; save.textContent="已保存"; setTimeout(()=>{ save.textContent="保存"; },1200);
  };
  acts.append(save); body.append(acts);
  try{ const d=await (await api("/agents/doc?id="+encodeURIComponent(a.id)+"&name=AGENT.md")).json(); if(!apStale(a,"setup")) ta.value=d.text||""; }catch(e){}
  if(apStale(a,"setup")) return;
  if(a.id==="general"){ body.append(apSection("技能与 MCP"), apEmpty("用设置页的全局配置")); return; }
  let cat;
  try{ cat=await (await api("/settings")).json(); }catch(e){ return; }
  if(apStale(a,"setup")) return;
  const skills=(cat.skills?.items||[]).filter(x=>!x.hidden);
  const mcps=(cat.mcp?.external||[]).filter(x=>x.enabled!==false);
  // 自定义时的初始名单:技能从全局已开启的抄一份(免得一打开就全关),MCP 从空开始
  renderApNames(body, a, "skills", "技能", skills.map(x=>({name:x.name, desc:x.description})),
    ()=>skills.filter(x=>x.enabled).map(x=>x.name));
  renderApNames(body, a, "mcp", "MCP", mcps.map(x=>({name:x.name, desc:x.url||x.command||""})), ()=>[]);
  renderApModelTools(body, a);
  const rm=el("button","apbtn danger"); rm.type="button"; rm.textContent="移除 Agent";
  rm.title="只从列表移除,文件夹和它的文件都留着,再把目录加回来就恢复";
  rm.onclick=async()=>{
    if(!confirm("从列表移除「"+a.name+"」?文件夹和它的文件都会保留。")) return;
    await removeProject(a.project_hash); await loadAgents(); hideAgentPanel(); renderConvs();
  };
  body.append(rm);
}
// 默认模型 + 禁用工具(memory/agents.py 的 model / disallowed_tools,后端硬生效)。
// 同样只重画自己这一块;模型清单异步拉,先占住位置,免得插到「移除 Agent」按钮后面
function renderApModelTools(body, a){
  const box=el("div"); body.append(box);
  box.append(apSection("默认模型(新会话)"));
  const sel=el("select","apselect");
  const o0=el("option"); o0.value=""; o0.textContent="跟随全局默认"; sel.append(o0);
  box.append(sel);
  box.append(apSection("禁用工具"));
  const dis=el("input","apinput"); dis.value=(a.disallowed_tools||[]).join(", ");
  dis.placeholder="如 Bash, WebFetch, mcp__vococo__dispatch_session(逗号分隔)";
  const note=el("div","apnote"); note.textContent="名下会话和定时任务里直接拦掉,子代理也用不了。模型只影响新开的会话。";
  box.append(dis, note);
  const acts=el("div","apacts");
  const save=el("button","apbtn primary"); save.type="button"; save.textContent="保存"; save.disabled=true;
  sel.onchange=dis.oninput=()=>{ save.disabled=false; };
  save.onclick=async()=>{
    const tools=dis.value.split(/[,，\s]+/).map(x=>x.trim()).filter(Boolean);
    let r;
    try{ r=await agentPost("/agents/update",{id:a.id, model:sel.value||null, disallowed_tools:tools.length?tools:null}); }
    catch(e){ alert(e.message); return; }
    a.model=r.agent.model; a.disallowed_tools=r.agent.disallowed_tools; loadAgents();
    dis.value=(a.disallowed_tools||[]).join(", ");
    save.disabled=true; save.textContent="已保存"; setTimeout(()=>{ save.textContent="保存"; },1200);
  };
  acts.append(save); box.append(acts);
  api("/models").then(r=>r.json()).then(d=>{
    for(const [v,label] of (d.choices||[])){ const o=el("option"); o.value=v; o.textContent=label||v; sel.append(o); }
    if(a.model && ![...sel.options].some(o=>o.value===a.model)){ const o=el("option"); o.value=a.model; o.textContent=a.model+"(已不在清单)"; sel.append(o); }
    sel.value=a.model||"";
  }).catch(()=>{ sel.value=a.model||""; });
}
// 技能 / MCP 名单:关 = 跟随全局;开 = 只用勾上的(MCP 勾上的每轮都挂)。
// 改了只重画这一块,别整个面板重画——会冲掉 AGENT.md 里还没保存的输入
function renderApNames(body, a, key, title, items, initial){
  const box=el("div"); body.append(box);
  let order=null, q="";   // 排序只在打开时定一次,勾选时行不跳位置;q = 搜索词
  const draw=()=>{
    box.innerHTML="";
    const own=a[key];   // null = 跟随全局
    const save=async names=>{
      let r;
      try{ r=await agentPost("/agents/update",{id:a.id, [key]:names}); }catch(e){ alert(e.message); draw(); return; }
      a[key]=r.agent[key]; draw(); loadAgents();
    };
    const head=el("div","apsec apsech");
    const t=el("span"); t.textContent=title;
    const sw=el("label","apcustom"); sw.innerHTML='自定义<span class="sw"><input type="checkbox"'+(own?" checked":"")+'><span class="track"></span></span>';
    sw.querySelector("input").onchange=ev=>save(ev.target.checked ? initial() : null);
    head.append(t, sw); box.append(head);
    if(!own){ order=null; box.append(apEmpty("跟随全局设置")); return; }
    if(!items.length){ box.append(apEmpty(key==="mcp"?"还没有外部 MCP,先去设置页添加":"没有可用的技能")); return; }
    const on=new Set(own);
    // 勾上的排前面,一眼看到它在用什么
    order=order||items.slice().sort((x,y)=>(on.has(y.name)?1:0)-(on.has(x.name)?1:0));
    if(items.length>8){   // 技能动辄上百个:给个搜索和「全不选」,从零挑几个比挨个取消快
      const bar=el("div","apnbar");
      const inp=el("input","apnsearch"); inp.placeholder="搜索"; inp.value=q;
      inp.oninput=()=>{ q=inp.value; const pos=inp.selectionStart; draw(); const n=box.querySelector(".apnsearch"); n.focus(); n.setSelectionRange(pos,pos); };
      const none=el("button","apnnone"); none.type="button"; none.textContent="全不选"; none.disabled=!own.length;
      none.onclick=()=>save([]);
      bar.append(inp, none); box.append(bar);
    }
    const ql=q.trim().toLowerCase();
    const shown=ql ? order.filter(it=>(it.name+" "+(it.desc||"")).toLowerCase().includes(ql)) : order;
    const list=el("div","apnames");
    if(!shown.length) list.append(apEmpty("没有匹配的"));
    for(const it of shown){
      const r=el("label","apnrow"); r.title=it.desc||it.name;
      r.innerHTML='<input type="checkbox"'+(on.has(it.name)?" checked":"")+'><span class="aptmain"><span class="aptname"></span><span class="aptsub"></span></span>';
      r.querySelector(".aptname").textContent=it.name;
      r.querySelector(".aptsub").textContent=it.desc||"";
      r.querySelector("input").onchange=ev=>save(ev.target.checked ? [...own, it.name] : own.filter(n=>n!==it.name));
      list.append(r);
    }
    box.append(list);
  };
  draw();
}

// 文件:家目录(vococo 管) + 工作目录第一层 + 关联(默认只读,可写需勾选)
async function renderApFiles(body, a){
  body.append(apEmpty("加载中…"));
  let d;
  try{ d=await (await api("/agents/files?id="+encodeURIComponent(a.id))).json(); }catch(e){ body.innerHTML=""; body.append(apEmpty("加载失败")); return; }
  if(apStale(a,"files")) return;
  body.innerHTML="";
  const fileRow=(name, path, isDir)=>{
    const r=el("div","apfile"); r.innerHTML=ic(isDir?"folder":"doc")+'<span class="apfname"></span>';
    r.querySelector(".apfname").textContent=name;
    if(!isDir){ r.onclick=()=>{ S.agentPanelOn=false; saveAgentPanelPref(); hideAgentPanel(); openDocPreview({kind:"path", target:path, title:name}); syncAgentHeader(); }; }
    else r.classList.add("dir");
    return r;
  };
  body.append(apSection("它自己的 · "+shortPath(d.home)));
  for(const f of d.files) body.append(fileRow(f, d.home+"/"+f, false));
  if(d.workdir){
    body.append(apSection("工作目录 · "+shortPath(d.workdir)));
    if(!d.workdir_top.length) body.append(apEmpty("空"));
    for(const e of d.workdir_top) body.append(fileRow(e.name, d.workdir+"/"+e.name, e.dir));
  }
  body.append(apSection("关联"));
  const links=a.links||[];
  if(!links.length) body.append(apEmpty("没有关联目录。关联后它知道去哪找资料,默认只读。"));
  links.forEach((l, i)=>{
    const r=el("div","aplink");
    const main=el("div","aptmain");
    const p=el("div","aptname"); p.textContent=shortPath(l.path); p.title=l.path;
    const sub=el("div","aptsub"); sub.textContent=l.note||"";
    main.append(p, sub);
    const w=el("label","apwrite"); w.innerHTML='<input type="checkbox"'+(l.writable?" checked":"")+'> 可写';
    w.title="勾上后它往这里写文件不用再批";
    w.querySelector("input").onchange=ev=>saveLinks(a, links.map((x,j)=>j===i?{...x, writable:ev.target.checked}:x));
    const del=el("button","more"); del.textContent="✕"; del.title="取消关联";
    del.onclick=()=>saveLinks(a, links.filter((x,j)=>j!==i));
    r.append(main, w, del);
    body.append(r);
  });
  const add=el("div","apadd"); add.textContent="＋ 关联目录";
  add.onclick=()=>openDirPicker("关联一个目录(默认只读)","✓ 关联", p=>{
    const note=(prompt("备注一下这是什么(可留空)")||"").trim();
    saveLinks(a, [...links, {path:p, note, writable:false}]);
  });
  body.append(add);
}
function shortPath(p){ return String(p||"").replace(/^\/Users\/[^/]+/, "~"); }
async function saveLinks(a, links){
  try{ await agentPost("/agents/update",{id:a.id, links}); }catch(e){ alert(e.message); return; }
  await loadAgents(); renderAgentPanel();
}

// ── 空会话欢迎屏:当前会话属于某个 Agent 时换成它自己的 ─────────────────────────
// 头像 + 名字 + 职责一句话 + 目标一句话,快捷操作按它的状态给(没目标→设目标;没计划→拆计划;有计划→看进展)
function openAgentPanelTab(tab){
  S.agentPanelOn=true; S.agentPanelTab=tab; saveAgentPanelPref();
  if(!$("#docPreview").hidden && typeof closeDocPreview==="function") closeDocPreview();
  $("#agentPanel").dataset.agent=""; syncAgentHeader();
}
function renderAgentEmpty(a){
  const box=$("#agentEmpty"); box.innerHTML="";
  const logo=el("div","elogo"); logo.innerHTML=avatarSvg(a.avatar);
  const h=el("h2"); h.textContent=a.name;
  const p=el("p"); p.textContent=a.summary || "还没写职责,直接在这里告诉它要做什么";
  box.append(logo, h, p);
  if(a.goal){
    const g=el("button","aggoal"); g.type="button"; g.title="看目标和计划";
    const lb=el("span","aggl"); lb.textContent="目标";
    const t=el("span","aggt"); t.textContent=a.goal;
    g.append(lb, t); g.onclick=()=>openAgentPanelTab("goal");
    box.append(g);
  }
  const sugs=el("div","sugs");
  const add=(label, fn)=>{ const b=el("button","sug"); b.type="button"; b.textContent=label; b.onclick=fn; sugs.append(b); };
  if(S.conv!==a.main_conv) add("进入主会话", ()=>openAgentMain(a));
  if(!a.goal) add("设定目标", ()=>openAgentPanelTab("goal"));
  else if(!a.has_plan) add("拆解计划", async()=>{
    let g; try{ g=await (await api("/agents/goal?id="+encodeURIComponent(a.id))).json(); }catch(e){ return; }
    openAgentMain(a); send(g.plan_prompt);
  });
  else add("看看进展", ()=>send("对照目标和计划,说说现在进展到哪、卡在哪、下一步做什么。"));
  if(a.task_count) add("定时任务 · "+a.task_count, ()=>openAgentPanelTab("tasks"));
  else add("新建定时任务", ()=>{ openCronModal(null); S.cronAgentId=a.id; if(a.workdir) $("#cfCwd").value=a.workdir; });
  add("动态", ()=>openAgentPanelTab("feed"));
  box.append(sugs);
}

// ── 与其它模块的衔接 ─────────────────────────────────────────────────────
// 数据刷新后(如定时列表重拉)只重画会受影响的 Tab,别把「设定」里没保存的输入冲掉
function refreshAgentPanelIf(tabs){
  if(!$("#agentPanel").hidden && tabs.includes(S.agentPanelTab)) renderAgentPanel();
}
// 服务端推 agent_run:某 Agent 的主会话刚写进一条定时结果
function onAgentRun(e){
  delete S.histCache[e.conv]; idbDel("hist:"+e.conv);
  if(S.conv===e.conv && typeof reloadHistory==="function") reloadHistory();
  else S.pendingReview[e.conv]=true;
  loadConvs();
  const a=currentAgent();
  if(a && a.main_conv===e.conv && !$("#agentPanel").hidden && S.agentPanelTab==="feed") renderAgentPanel();
}
