"use strict";
// 工作台 Demo：数据定格在 2026-08-20，只验证月 / 周 / 日的信息结构与本地交互。
// 不请求、不写入 Obsidian / Things / SQLite；刷新页面会还原全部任务状态。

const WORKBENCH_DEMO = {
  projects: [
    {id:"consulting", name:"AI 咨询", state:"推进中", sourceIds:["consulting-now", "august-plan"]},
    {id:"vocotrade", name:"VocoTrade", state:"收口中", sourceIds:["vocotrade-now", "august-plan"]},
    {id:"fabric", name:"面料外贸", state:"待校准", sourceIds:["fabric-now"]},
    {id:"transition", name:"离职过渡", state:"本周关键", sourceIds:["august-plan"]},
  ],
  sources: [
    {id:"august-plan", label:"月计划 / 2026年8月", path:"5.规划/2026月计划/2026年8月.md", heading:"Week3 · 8/17–8/23", excerpt:"AI咨询：约见第一梯队人脉、案例单页初稿；VocoTrade：邮件获客 agent 收口、AI 剪辑宣传视频链路。"},
    {id:"consulting-now", label:"AI咨询 / NOW", path:"2.重点项目/AI咨询/NOW.md", heading:"当前执行", excerpt:"先见人、听反馈、验证需求；案例素材和展示页属于后续动作。"},
    {id:"vocotrade-now", label:"VocoTrade / NOW", path:"2.重点项目/VocoTrade/NOW.md", heading:"当前执行", excerpt:"遗留收口后转向产品宣传与获客，不再继续扩张新功能。"},
    {id:"fabric-now", label:"面料外贸 / NOW", path:"2.重点项目/面料外贸/NOW.md", heading:"W21（已结束）", excerpt:"文档仍停在 8/10–8/16，本周三件事尚未重新设置。"},
  ],
  goals: [
    {id:"network", project:"consulting", month:"2026-08", title:"激活第一梯队人脉，听到真实需求", sourceIds:["august-plan", "consulting-now"]},
    {id:"materials", project:"consulting", month:"2026-08", title:"完成可对外展示的案例素材", sourceIds:["august-plan"]},
    {id:"acquisition", project:"vocotrade", month:"2026-08", title:"停新功能，把产品转成获客材料", sourceIds:["august-plan", "vocotrade-now"]},
    {id:"pipeline", project:"fabric", month:"2026-08", title:"维持主动开发产线，不额外扩张", sourceIds:["fabric-now"]},
    {id:"protection", project:"transition", month:"2026-08", title:"锁定离职谈判与家庭现金流缓冲", sourceIds:["august-plan"]},
  ],
  tasks: [
    {id:"talk-script", project:"consulting", goal:"network", title:"约人话术准备", status:"done", month:"2026-08", week:"2026-08-17", date:"2026-08-18", sourceIds:["consulting-now"]},
    {id:"meet-network", project:"consulting", goal:"network", title:"约见第一梯队：胜源、喆铭", status:"focus", month:"2026-08", week:"2026-08-17", date:"2026-08-20", sourceIds:["august-plan", "consulting-now"]},
    {id:"case-page", project:"consulting", goal:"materials", title:"案例单页初稿（vococo + VocoTrade）", status:"todo", month:"2026-08", week:"2026-08-17", date:null, sourceIds:["august-plan"]},
    {id:"agent-wrap", project:"vocotrade", goal:"acquisition", title:"邮件获客 agent 收口", status:"focus", month:"2026-08", week:"2026-08-17", date:"2026-08-20", sourceIds:["august-plan", "vocotrade-now"]},
    {id:"video-path", project:"vocotrade", goal:"acquisition", title:"研究 AI 剪辑宣传视频链路", status:"todo", month:"2026-08", week:"2026-08-17", date:null, sourceIds:["august-plan"]},
    {id:"material-direction", project:"vocotrade", goal:"acquisition", title:"确定宣传素材的第一版方向", status:"todo", month:"2026-08", week:null, date:null, sourceIds:["vocotrade-now"]},
    {id:"crawler-plan", project:"fabric", goal:"pipeline", title:"确认 B12–B16 爬虫排产的下一步", status:"block", month:"2026-08", week:"2026-08-17", date:null, sourceIds:["fabric-now"]},
    {id:"difs", project:"fabric", goal:"pipeline", title:"DIFS 展会预热：核对 B 批补发", status:"todo", month:"2026-08", week:"2026-08-17", date:"2026-08-21", sourceIds:["fabric-now"]},
    {id:"lemlist", project:"fabric", goal:null, title:"跟踪 BD-Knit-01 回复与退信", status:"todo", month:"2026-08", week:"2026-08-17", date:null, sourceIds:["fabric-now"]},
    {id:"contract", project:"transition", goal:"protection", title:"确认竞业协议条款原文", status:"focus", month:"2026-08", week:"2026-08-17", date:"2026-08-20", sourceIds:["august-plan"]},
    {id:"family-talk", project:"transition", goal:"protection", title:"跟小雯同步创业计划与对外说法", status:"todo", month:"2026-08", week:"2026-08-17", date:null, sourceIds:["august-plan"]},
    {id:"mortgage", project:"transition", goal:"protection", title:"房贷延期材料咨询", status:"todo", month:"2026-08", week:"2026-08-17", date:null, sourceIds:["august-plan"]},
  ],
};

const WB = {project:"all", view:"week", anchor:"2026-08-20", sourceId:null};

function workbenchProject(id){
  return WORKBENCH_DEMO.projects.find(project => project.id === id);
}

function workbenchGoal(id){
  return WORKBENCH_DEMO.goals.find(goal => goal.id === id);
}

function workbenchSource(id){
  return WORKBENCH_DEMO.sources.find(source => source.id === id);
}

function workbenchDate(value){
  return new Date(value+"T12:00:00");
}

function workbenchDateKey(date){
  return date.toISOString().slice(0, 10);
}

function workbenchMonthKey(date = workbenchDate(WB.anchor)){
  return workbenchDateKey(date).slice(0, 7);
}

function workbenchWeekKey(date = workbenchDate(WB.anchor)){
  const weekday = date.getDay() || 7;
  date.setDate(date.getDate() - weekday + 1);
  return workbenchDateKey(date);
}

function workbenchDateLabel(){
  const date = workbenchDate(WB.anchor);
  if(WB.view === "month") return date.getFullYear()+"年"+(date.getMonth()+1)+"月";
  if(WB.view === "week") return "Week"+workbenchWeekNumber(date)+" · "+workbenchWeekRange(date);
  return date.getFullYear()+"年"+(date.getMonth()+1)+"月"+date.getDate()+"日 · 周"+"日一二三四五六"[date.getDay()];
}

function workbenchWeekNumber(date){
  const firstDay = new Date(date.getFullYear(), 0, 1);
  return Math.ceil((((date - firstDay) / 86400000) + firstDay.getDay() + 1) / 7);
}

function workbenchWeekRange(date){
  const start = workbenchDate(workbenchWeekKey(date));
  const end = new Date(start); end.setDate(start.getDate()+6);
  return (start.getMonth()+1)+"/"+start.getDate()+"–"+(end.getMonth()+1)+"/"+end.getDate();
}

function workbenchProjectMatches(item){
  return WB.project === "all" || item.project === WB.project;
}

function workbenchTasks(filter){
  return WORKBENCH_DEMO.tasks.filter(task => workbenchProjectMatches(task) && filter(task));
}

function workbenchTaskStatus(task){
  return {done:"已完成", focus:"现在推进", todo:"待办", block:"待决定"}[task.status] || "待办";
}

function workbenchTaskSchedule(task){
  if(task.date) return task.date.slice(5).replace("-", "/");
  if(task.week) return "本周待排日";
  return "本月待排周";
}

function workbenchSourceButtons(sourceIds){
  if(!sourceIds?.length) return '<span class="wb-manual">手动维护</span>';
  return sourceIds.map(id => {
    const source = workbenchSource(id);
    return source ? '<button type="button" class="wb-source-link" data-source="'+esc(id)+'">'+esc(source.label)+' ↗</button>' : "";
  }).join("");
}

function workbenchTaskRow(task){
  const project = workbenchProject(task.project);
  const isDone = task.status === "done";
  const action = isDone ? "恢复" : "完成";
  return '<article class="wb-task wb-'+esc(task.status)+'">'+
    '<button class="wb-check" type="button" data-complete="'+esc(task.id)+'" aria-label="'+action+'：'+esc(task.title)+'">'+(isDone ? "✓" : task.status === "block" ? "!" : "")+'</button>'+
    '<div class="wb-task-copy"><div class="wb-task-line"><strong>'+esc(task.title)+'</strong><span class="wb-project-tag">'+esc(project.name)+'</span></div>'+
    '<div class="wb-task-meta"><span>'+esc(workbenchTaskSchedule(task))+'</span><span>·</span><span>'+esc(workbenchTaskStatus(task))+'</span></div>'+
    '<div class="wb-task-sources">'+workbenchSourceButtons(task.sourceIds)+'</div></div></article>';
}

function workbenchGoalRow(goal){
  const tasks = workbenchTasks(task => task.goal === goal.id && task.month === workbenchMonthKey());
  const scheduled = tasks.filter(task => task.week).length;
  const source = goal.sourceIds?.[0];
  return '<div class="wb-goal-row"><div><strong>'+esc(goal.title)+'</strong><span>'+scheduled+' / '+tasks.length+' 项已排周</span></div>'+
    '<button type="button" class="wb-source-link" data-source="'+esc(source)+'">来源 ↗</button></div>';
}

function workbenchProjectBlock(project){
  const month = workbenchMonthKey();
  const goals = WORKBENCH_DEMO.goals.filter(goal => goal.project === project.id && goal.month === month);
  const tasks = workbenchTasks(task => task.project === project.id && task.month === month);
  const unaligned = tasks.filter(task => !task.goal);
  return '<section class="wb-project-block"><header><div><h2>'+esc(project.name)+'</h2><span>'+esc(project.state)+'</span></div><div class="wb-project-sources">'+workbenchSourceButtons(project.sourceIds)+'</div></header>'+
    '<div class="wb-goals">'+goals.map(workbenchGoalRow).join("")+'</div>'+
    '<div class="wb-project-tasks">'+tasks.map(workbenchTaskRow).join("")+'</div>'+
    (unaligned.length ? '<p class="wb-unaligned">⚠ '+unaligned.length+' 项未关联月目标</p>' : "")+'</section>';
}

function renderWorkbenchDay(){
  const todayTasks = workbenchTasks(task => task.date === WB.anchor);
  const waitingTasks = workbenchTasks(task => task.week === workbenchWeekKey() && !task.date);
  return workbenchTaskSection("今天推进", todayTasks, "今天没有已安排的核心任务。")+
    workbenchTaskSection("本周待安排", waitingTasks, "本周没有待安排任务。");
}

function renderWorkbenchWeek(){
  const weekTasks = workbenchTasks(task => task.week === workbenchWeekKey() && task.date);
  const waitingTasks = workbenchTasks(task => task.week === workbenchWeekKey() && !task.date);
  return workbenchTaskSection("本周已安排", weekTasks, "本周暂无已安排任务。")+
    workbenchTaskSection("本周待安排", waitingTasks, "本周没有待安排任务。");
}

function renderWorkbenchMonth(){
  const projects = WORKBENCH_DEMO.projects.filter(workbenchProjectMatches);
  const waitingTasks = workbenchTasks(task => task.month === workbenchMonthKey() && !task.week);
  return '<section class="wb-section"><div class="wb-section-head"><h2>项目与月目标</h2><span>目标下无任务，或任务未关联目标时会标出</span></div>'+projects.map(workbenchProjectBlock).join("")+'</section>'+
    workbenchTaskSection("本月待分配", waitingTasks, "本月没有未分配到周的任务。");
}

function workbenchTaskSection(title, tasks, emptyText){
  return '<section class="wb-section"><div class="wb-section-head"><h2>'+esc(title)+'</h2><span>'+tasks.length+' 项</span></div>'+
    (tasks.length ? '<div class="wb-task-list">'+tasks.map(workbenchTaskRow).join("")+'</div>' : '<p class="wb-empty">'+esc(emptyText)+'</p>')+'</section>';
}

function renderWorkbenchSource(){
  const source = workbenchSource(WB.sourceId);
  if(!source) return "";
  return '<aside class="wb-source-panel"><div><span>来源文档</span><strong>'+esc(source.label)+'</strong></div><button type="button" data-close-source aria-label="关闭来源">×</button>'+
    '<dl><dt>文件</dt><dd>'+esc(source.path)+'</dd><dt>定位</dt><dd>'+esc(source.heading)+'</dd><dt>摘录</dt><dd>'+esc(source.excerpt)+'</dd></dl></aside>';
}

function renderWorkbench(){
  const root = $("#wbContent");
  if(!root) return;
  const body = {day:renderWorkbenchDay, week:renderWorkbenchWeek, month:renderWorkbenchMonth}[WB.view]();
  root.innerHTML = '<header class="wb-toolbar"><div class="wb-title"><button class="wb-hamb" type="button" data-sidebar aria-label="打开侧边栏">'+ic("panel")+'</button><h1>工作台</h1><span>静态 Demo</span></div>'+
    '<div class="wb-controls"><div class="wb-switch">'+["day","week","month"].map(view => '<button class="'+(WB.view === view ? "on" : "")+'" type="button" data-view="'+view+'">'+({day:"日",week:"周",month:"月"}[view])+'</button>').join("")+'</div>'+
    '<div class="wb-date-nav"><button type="button" data-nav="-1" aria-label="上一个周期">‹</button><strong>'+workbenchDateLabel()+'</strong><button type="button" data-nav="1" aria-label="下一个周期">›</button><button type="button" data-today>今天</button></div></div></header>'+
    '<div class="wb-project-filter"><button class="'+(WB.project === "all" ? "on" : "")+'" type="button" data-project="all">全部项目</button>'+WORKBENCH_DEMO.projects.map(project => '<button class="'+(WB.project === project.id ? "on" : "")+'" type="button" data-project="'+esc(project.id)+'">'+esc(project.name)+'</button>').join("")+'</div>'+body+
    '<p class="wb-demo-note">静态演示数据 · 勾选只在当前页面有效 · 不会写入 Things、Obsidian 或任务库</p>'+renderWorkbenchSource();
}

function toggleWorkbenchTask(taskId){
  const task = WORKBENCH_DEMO.tasks.find(item => item.id === taskId);
  if(!task) return;
  task.status = task.status === "done" ? "todo" : "done";
  renderWorkbench();
}

function shiftWorkbenchDate(direction){
  const date = workbenchDate(WB.anchor);
  const offset = WB.view === "month" ? 30 : WB.view === "week" ? 7 : 1;
  date.setDate(date.getDate() + direction * offset);
  WB.anchor = workbenchDateKey(date);
  renderWorkbench();
}

function openWorkbench(){
  closeCallView();
  S.surface = "workbench";
  $("#chatMain").hidden = true;
  $("#workbenchView").hidden = false;
  closeSidebar();
  renderConvs();
  renderWorkbench();
}

function closeWorkbench(){
  const view = $("#workbenchView");
  if(view) view.hidden = true;
}

$("#workbenchView").addEventListener("click", event => {
  if(event.target.closest("[data-sidebar]")){ expandSidebarResponsive(); return; }
  const taskButton = event.target.closest("[data-complete]");
  if(taskButton){ toggleWorkbenchTask(taskButton.dataset.complete); return; }
  const sourceButton = event.target.closest("[data-source]");
  if(sourceButton){ WB.sourceId = sourceButton.dataset.source; renderWorkbench(); return; }
  if(event.target.closest("[data-close-source]")){ WB.sourceId = null; renderWorkbench(); return; }
  const projectButton = event.target.closest("[data-project]");
  if(projectButton){ WB.project = projectButton.dataset.project; renderWorkbench(); return; }
  const viewButton = event.target.closest("[data-view]");
  if(viewButton){ WB.view = viewButton.dataset.view; renderWorkbench(); return; }
  const navButton = event.target.closest("[data-nav]");
  if(navButton){ shiftWorkbenchDate(Number(navButton.dataset.nav)); return; }
  if(event.target.closest("[data-today]")){ WB.anchor = "2026-08-20"; renderWorkbench(); }
});

window.openWorkbench = openWorkbench;
window.closeWorkbench = closeWorkbench;
