"use strict";
// 工作台 Demo：数据定格在 2026-08-20，仅验证界面与浏览器内存交互。
// 不请求、不写入 Obsidian / Things / SQLite；刷新页面会还原任务编辑与图片。

const WORKBENCH_DEMO = {
  today: "2026-08-20",
  projects: [
    {id:"consulting", name:"AI 咨询", state:"推进中"},
    {id:"vocotrade", name:"VocoTrade", state:"收口中"},
    {id:"fabric", name:"面料外贸", state:"待校准"},
    {id:"transition", name:"离职过渡", state:"本周关键"},
  ],
  sources: [
    {id:"august-plan", label:"月计划 / 2026年8月", path:"/Users/wesley/Library/Mobile Documents/iCloud~md~obsidian/Documents/Wesley notes/5.规划/2026月计划/2026年8月.md"},
    {id:"consulting-now", label:"AI咨询 / NOW", path:"/Users/wesley/Library/Mobile Documents/iCloud~md~obsidian/Documents/Wesley notes/2.重点项目/AI咨询/NOW.md"},
    {id:"vocotrade-now", label:"VocoTrade / NOW", path:"/Users/wesley/Library/Mobile Documents/iCloud~md~obsidian/Documents/Wesley notes/2.重点项目/VocoTrade/NOW.md"},
    {id:"fabric-now", label:"面料外贸 / NOW", path:"/Users/wesley/Library/Mobile Documents/iCloud~md~obsidian/Documents/Wesley notes/2.重点项目/面料外贸/NOW.md"},
  ],
  goals: [
    {id:"network", project:"consulting", month:"2026-08", title:"激活第一梯队人脉，听到真实需求"},
    {id:"materials", project:"consulting", month:"2026-08", title:"完成可对外展示的案例素材"},
    {id:"acquisition", project:"vocotrade", month:"2026-08", title:"停新功能，把产品转成获客材料"},
    {id:"pipeline", project:"fabric", month:"2026-08", title:"维持主动开发产线，不额外扩张"},
    {id:"protection", project:"transition", month:"2026-08", title:"锁定离职谈判与家庭现金流缓冲"},
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

const WB = {project:"all", view:"week", anchor:"2026-08-20", detailTaskId:null, collapsed:new Set(["week-waiting", "month-waiting"])};

function workbenchProject(id){ return WORKBENCH_DEMO.projects.find(project => project.id === id); }
function workbenchGoal(id){ return WORKBENCH_DEMO.goals.find(goal => goal.id === id); }
function workbenchSource(id){ return WORKBENCH_DEMO.sources.find(source => source.id === id); }
function workbenchTask(id){ return WORKBENCH_DEMO.tasks.find(task => task.id === id); }
function workbenchDate(value){ return new Date(value+"T12:00:00"); }
function workbenchDateKey(date){ return date.toISOString().slice(0, 10); }
function workbenchMonthKey(date = workbenchDate(WB.anchor)){ return workbenchDateKey(date).slice(0, 7); }

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

function workbenchProjectMatches(item){ return WB.project === "all" || item.project === WB.project; }
function workbenchTasks(filter){ return WORKBENCH_DEMO.tasks.filter(task => workbenchProjectMatches(task) && filter(task)); }
function workbenchTaskStatus(task){ return {done:"已完成", focus:"现在推进", todo:"待办", block:"待决定"}[task.status] || "待办"; }
function workbenchTaskHighlight(task){ return task.highlight || task.title.split(/[：（(]/)[0]; }

function workbenchTaskRow(task){
  const project = workbenchProject(task.project);
  const action = task.status === "done" ? "恢复" : "完成";
  return '<article class="wb-task wb-'+esc(task.status)+'" data-task="'+esc(task.id)+'">'+
    '<button class="wb-check" type="button" data-complete="'+esc(task.id)+'" aria-label="'+action+'：'+esc(task.title)+'">'+(task.status === "done" ? "✓" : task.status === "block" ? "!" : "")+'</button>'+
    '<strong class="wb-task-title">'+esc(task.title)+'</strong><span class="wb-project-tag">'+esc(project.name)+'</span></article>';
}

function workbenchGoalRow(goal){
  const tasks = workbenchTasks(task => task.goal === goal.id && task.month === workbenchMonthKey());
  const scheduled = tasks.filter(task => task.week).length;
  return '<div class="wb-goal-row"><strong>'+esc(goal.title)+'</strong><span>'+scheduled+' / '+tasks.length+' 项已排周</span></div>';
}

function workbenchProjectBlock(project){
  const month = workbenchMonthKey();
  const goals = WORKBENCH_DEMO.goals.filter(goal => goal.project === project.id && goal.month === month);
  const tasks = workbenchTasks(task => task.project === project.id && task.month === month);
  const unaligned = tasks.filter(task => !task.goal);
  return '<section class="wb-project-block"><header><div><h2>'+esc(project.name)+'</h2><span>'+esc(project.state)+'</span></div></header>'+
    '<div class="wb-goals">'+goals.map(workbenchGoalRow).join("")+'</div><div class="wb-project-tasks">'+tasks.map(workbenchTaskRow).join("")+'</div>'+
    (unaligned.length ? '<p class="wb-unaligned">⚠ '+unaligned.length+' 项未关联月目标</p>' : "")+'</section>';
}

function workbenchTaskSection(id, title, tasks, emptyText){
  const collapsed = WB.collapsed.has(id);
  const button = '<button type="button" class="wb-section-toggle" data-group="'+esc(id)+'" aria-expanded="'+(!collapsed)+'"><h2>'+esc(title)+'</h2><span>'+tasks.length+' 项 '+(collapsed ? "⌄" : "⌃")+'</span></button>';
  const body = tasks.length ? '<div class="wb-task-list">'+tasks.map(workbenchTaskRow).join("")+'</div>' : '<p class="wb-empty">'+esc(emptyText)+'</p>';
  return '<section class="wb-section">'+button+(collapsed ? "" : body)+'</section>';
}

function renderWorkbenchDay(){
  const todayTasks = workbenchTasks(task => task.date === WB.anchor);
  const waitingTasks = workbenchTasks(task => task.week === workbenchWeekKey() && !task.date);
  return workbenchTaskSection("day-focus", "今天推进", todayTasks, "今天没有已安排的核心任务。")+
    workbenchTaskSection("week-waiting", "本周待安排", waitingTasks, "本周没有待安排任务。");
}

function renderWorkbenchWeek(){
  const weekTasks = workbenchTasks(task => task.week === workbenchWeekKey() && task.date);
  const waitingTasks = workbenchTasks(task => task.week === workbenchWeekKey() && !task.date);
  return workbenchTaskSection("week-scheduled", "本周已安排", weekTasks, "本周暂无已安排任务。")+
    workbenchTaskSection("week-waiting", "本周待安排", waitingTasks, "本周没有待安排任务。");
}

function renderWorkbenchMonth(){
  const projects = WORKBENCH_DEMO.projects.filter(workbenchProjectMatches);
  const waitingTasks = workbenchTasks(task => task.month === workbenchMonthKey() && !task.week);
  return '<section class="wb-section"><div class="wb-section-head"><h2>项目与月目标</h2><span>目标下无任务，或任务未关联目标时会标出</span></div>'+projects.map(workbenchProjectBlock).join("")+'</section>'+
    workbenchTaskSection("month-waiting", "本月待分配", waitingTasks, "本月没有未分配到周的任务。");
}

function openWorkbenchSource(sourceId, highlight){
  const source = workbenchSource(sourceId);
  if(!source || typeof openDocPreview !== "function") return;
  openDocPreview({kind:"path", target:source.path, title:source.label, highlight});
}

function renderWorkbenchHeader(){
  return '<header class="wb-toolbar"><div class="wb-title"><button class="wb-hamb" type="button" data-sidebar aria-label="打开侧边栏">'+ic("panel")+'</button><h1>工作台</h1><span>静态 Demo</span></div>'+
    '<div class="wb-controls"><div class="wb-switch">'+["day","week","month"].map(view => '<button class="'+(WB.view === view ? "on" : "")+'" type="button" data-view="'+view+'">'+({day:"日",week:"周",month:"月"}[view])+'</button>').join("")+'</div>'+
    '<div class="wb-date-nav"><button type="button" data-nav="-1" aria-label="上一个周期">‹</button><strong>'+workbenchDateLabel()+'</strong><button type="button" data-nav="1" aria-label="下一个周期">›</button><button type="button" data-today>今天</button></div></div></header>';
}

function renderWorkbenchDetail(task){
  const project = workbenchProject(task.project);
  const goal = workbenchGoal(task.goal);
  const sources = (task.sourceIds||[]).map(id => {
    const source = workbenchSource(id);
    return source ? '<button type="button" class="wb-source-link" data-source="'+esc(id)+'" data-highlight="'+esc(workbenchTaskHighlight(task))+'">'+esc(source.label)+' ↗</button>' : "";
  }).join("");
  const images = (task.images||[]).map((image, index) => '<figure><img src="'+esc(image)+'" alt="任务附件"><button type="button" data-remove-image="'+index+'" aria-label="移除图片">×</button></figure>').join("");
  return '<header class="wb-detail-bar"><button type="button" data-back-workbench>‹ 返回工作台</button><span>'+esc(project.name)+' · '+esc(workbenchTaskStatus(task))+'</span></header>'+
    '<section class="wb-detail"><input class="wb-detail-title" data-edit-title="'+esc(task.id)+'" value="'+esc(task.title)+'" aria-label="任务标题">'+
    '<label class="wb-detail-label">任务说明<textarea data-edit-detail="'+esc(task.id)+'" placeholder="补充执行说明；可直接粘贴图片">'+esc(task.detail||"")+'</textarea></label>'+
    '<div class="wb-detail-schedule"><div><span>安排</span><strong>'+esc(task.date || "未安排日期")+'</strong></div><button type="button" data-schedule-today="'+esc(task.id)+'">添加到今天</button><input type="date" data-schedule-date="'+esc(task.id)+'" value="'+esc(task.date||"")+'" aria-label="安排日期"></div>'+
    '<div class="wb-detail-context"><span>月目标</span><strong>'+esc(goal?.title || "未关联月目标")+'</strong></div>'+
    '<div class="wb-detail-sources"><span>来源文档</span><div>'+sources+'</div><small>点击在右侧预览；预览窗口右上可独立打开。</small></div>'+
    '<div class="wb-detail-images"><div><span>图片</span><button type="button" data-add-image>粘贴或选择图片</button></div><input type="file" data-image-input="'+esc(task.id)+'" accept="image/*" hidden><div class="wb-image-list">'+images+'</div></div>'+
    '<p class="wb-demo-note">此页编辑、排期和图片仅保存在当前浏览器，刷新后还原。</p></section>';
}

function renderWorkbench(){
  const root = $("#wbContent");
  if(!root) return;
  const detailTask = workbenchTask(WB.detailTaskId);
  if(detailTask){ root.innerHTML = renderWorkbenchDetail(detailTask); return; }
  const body = {day:renderWorkbenchDay, week:renderWorkbenchWeek, month:renderWorkbenchMonth}[WB.view]();
  root.innerHTML = renderWorkbenchHeader()+'<div class="wb-project-filter"><button class="'+(WB.project === "all" ? "on" : "")+'" type="button" data-project="all">全部项目</button>'+WORKBENCH_DEMO.projects.map(project => '<button class="'+(WB.project === project.id ? "on" : "")+'" type="button" data-project="'+esc(project.id)+'">'+esc(project.name)+'</button>').join("")+'</div>'+body+
    '<p class="wb-demo-note">静态演示数据 · 勾选只在当前页面有效 · 不会写入 Things、Obsidian 或任务库</p>';
}

function toggleWorkbenchTask(taskId){
  const task = workbenchTask(taskId);
  if(!task) return;
  task.status = task.status === "done" ? "todo" : "done";
  renderWorkbench();
}

function scheduleWorkbenchTask(taskId, date){
  const task = workbenchTask(taskId);
  if(!task) return;
  task.date = date || null;
  if(date){ task.month = date.slice(0, 7); task.week = workbenchWeekKey(workbenchDate(date)); }
  renderWorkbench();
}

function addWorkbenchImages(taskId, files){
  const images = [...files].filter(file => file.type.startsWith("image/"));
  if(!images.length) return;
  Promise.all(images.map(file => new Promise(resolve => {
    const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.readAsDataURL(file);
  }))).then(values => {
    const task = workbenchTask(taskId); if(!task) return;
    task.images = [...(task.images||[]), ...values]; renderWorkbench();
  });
}

function shiftWorkbenchDate(direction){
  const date = workbenchDate(WB.anchor);
  date.setDate(date.getDate() + direction * (WB.view === "month" ? 30 : WB.view === "week" ? 7 : 1));
  WB.anchor = workbenchDateKey(date); renderWorkbench();
}

function openWorkbench(){
  closeCallView(); S.surface = "workbench";
  $("#chatMain").hidden = true; $("#workbenchView").hidden = false;
  closeSidebar(); renderConvs(); renderWorkbench();
}

function closeWorkbench(){ const view = $("#workbenchView"); if(view) view.hidden = true; }

$("#workbenchView").addEventListener("click", event => {
  if(event.target.closest("[data-sidebar]")){ expandSidebarResponsive(); return; }
  if(event.target.closest("[data-back-workbench]")){ WB.detailTaskId = null; renderWorkbench(); return; }
  const complete = event.target.closest("[data-complete]");
  if(complete){ toggleWorkbenchTask(complete.dataset.complete); return; }
  const source = event.target.closest("[data-source]");
  if(source){ openWorkbenchSource(source.dataset.source, source.dataset.highlight); return; }
  const group = event.target.closest("[data-group]");
  if(group){ WB.collapsed.has(group.dataset.group) ? WB.collapsed.delete(group.dataset.group) : WB.collapsed.add(group.dataset.group); renderWorkbench(); return; }
  const today = event.target.closest("[data-schedule-today]");
  if(today){ scheduleWorkbenchTask(today.dataset.scheduleToday, WORKBENCH_DEMO.today); return; }
  const addImage = event.target.closest("[data-add-image]");
  if(addImage){ $("#workbenchView").querySelector("[data-image-input]")?.click(); return; }
  const removeImage = event.target.closest("[data-remove-image]");
  if(removeImage){ const task = workbenchTask(WB.detailTaskId); task?.images?.splice(Number(removeImage.dataset.removeImage), 1); renderWorkbench(); return; }
  const taskRow = event.target.closest("[data-task]");
  if(taskRow){ WB.detailTaskId = taskRow.dataset.task; renderWorkbench(); return; }
  const project = event.target.closest("[data-project]");
  if(project){ WB.project = project.dataset.project; renderWorkbench(); return; }
  const view = event.target.closest("[data-view]");
  if(view){ WB.view = view.dataset.view; renderWorkbench(); return; }
  const nav = event.target.closest("[data-nav]");
  if(nav){ shiftWorkbenchDate(Number(nav.dataset.nav)); return; }
  if(event.target.closest("[data-today]")){ WB.anchor = WORKBENCH_DEMO.today; renderWorkbench(); }
});

$("#workbenchView").addEventListener("input", event => {
  const task = workbenchTask(event.target.dataset.editTitle || event.target.dataset.editDetail);
  if(!task) return;
  if(event.target.dataset.editTitle) task.title = event.target.value;
  if(event.target.dataset.editDetail) task.detail = event.target.value;
});

$("#workbenchView").addEventListener("change", event => {
  if(event.target.dataset.scheduleDate) scheduleWorkbenchTask(event.target.dataset.scheduleDate, event.target.value);
  if(event.target.dataset.imageInput) addWorkbenchImages(event.target.dataset.imageInput, event.target.files);
});

$("#workbenchView").addEventListener("paste", event => {
  const taskId = event.target.dataset.editDetail;
  const files = [...(event.clipboardData?.items||[])].map(item => item.getAsFile()).filter(Boolean);
  if(!taskId || !files.some(file => file.type.startsWith("image/"))) return;
  event.preventDefault(); addWorkbenchImages(taskId, files);
});

window.openWorkbench = openWorkbench;
window.closeWorkbench = closeWorkbench;
