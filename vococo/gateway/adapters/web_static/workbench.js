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
  tasks: [
    {id:"talk-script", project:"consulting", title:"约人话术准备", status:"done", month:"2026-08", week:"2026-08-17", date:"2026-08-18", sourceIds:["consulting-now"]},
    {id:"meet-network", project:"consulting", title:"约见第一梯队：胜源、喆铭", status:"focus", month:"2026-08", week:"2026-08-17", date:"2026-08-20", sourceIds:["august-plan", "consulting-now"]},
    {id:"case-page", project:"consulting", title:"案例单页初稿（vococo + VocoTrade）", status:"todo", month:"2026-08", week:"2026-08-17", date:null, sourceIds:["august-plan"]},
    {id:"agent-wrap", project:"vocotrade", title:"邮件获客 agent 收口", status:"focus", month:"2026-08", week:"2026-08-17", date:"2026-08-20", sourceIds:["august-plan", "vocotrade-now"]},
    {id:"video-path", project:"vocotrade", title:"研究 AI 剪辑宣传视频链路", status:"todo", month:"2026-08", week:"2026-08-17", date:null, sourceIds:["august-plan"]},
    {id:"material-direction", project:"vocotrade", title:"确定宣传素材的第一版方向", status:"todo", month:"2026-08", week:null, date:null, sourceIds:["vocotrade-now"]},
    {id:"crawler-plan", project:"fabric", title:"确认 B12–B16 爬虫排产的下一步", status:"block", month:"2026-08", week:"2026-08-17", date:null, sourceIds:["fabric-now"]},
    {id:"difs", project:"fabric", title:"DIFS 展会预热：核对 B 批补发", status:"todo", month:"2026-08", week:"2026-08-17", date:"2026-08-21", sourceIds:["fabric-now"]},
    {id:"lemlist", project:"fabric", title:"跟踪 BD-Knit-01 回复与退信", status:"todo", month:"2026-08", week:"2026-08-17", date:null, sourceIds:["fabric-now"]},
    {id:"contract", project:"transition", title:"确认竞业协议条款原文", status:"focus", month:"2026-08", week:"2026-08-17", date:"2026-08-20", sourceIds:["august-plan"]},
    {id:"family-talk", project:"transition", title:"跟小雯同步创业计划与对外说法", status:"todo", month:"2026-08", week:"2026-08-17", date:null, sourceIds:["august-plan"]},
    {id:"mortgage", project:"transition", title:"房贷延期材料咨询", status:"todo", month:"2026-08", week:"2026-08-17", date:null, sourceIds:["august-plan"]},
  ],
};

const WB = {project:"all", view:"week", anchor:"2026-08-20", editorTaskId:null, newTask:null, collapsed:new Set()};

function workbenchProject(id){ return WORKBENCH_DEMO.projects.find(project => project.id === id); }
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
function workbenchTaskHighlight(task){ return task.highlight || task.title.split(/[：（(]/)[0]; }
function workbenchGroupId(project){ return "project:"+WB.view+":"+project.id; }

function workbenchSourceLink(task, compact){
  const sources = (task.sourceIds||[]).map(workbenchSource).filter(Boolean);
  if(!sources.length) return '<span class="wb-no-source">无来源</span>';
  const source = sources[0];
  const suffix = sources.length > 1 ? " +"+(sources.length-1) : "";
  return '<button type="button" class="wb-source-link'+(compact ? " wb-task-source" : "")+'" data-source="'+esc(source.id)+'" data-highlight="'+esc(workbenchTaskHighlight(task))+'" title="'+esc(source.label)+'">'+ic("doc")+'<span>'+esc(source.label)+suffix+'</span></button>';
}

function workbenchTaskRow(task){
  const action = task.status === "done" ? "恢复" : "完成";
  const expanded = WB.editorTaskId === task.id;
  return '<article class="wb-task wb-'+esc(task.status)+(expanded ? " is-open" : "")+'" data-task="'+esc(task.id)+'">'+
    '<button class="wb-check" type="button" data-complete="'+esc(task.id)+'" aria-label="'+action+'：'+esc(task.title)+'">'+(task.status === "done" ? "✓" : task.status === "block" ? "!" : "")+'</button>'+
    '<strong class="wb-task-title">'+esc(task.title)+'</strong>'+workbenchSourceLink(task, true)+'</article>'+
    (expanded ? renderWorkbenchTaskEditor(task) : "");
}

function renderWorkbenchTaskEditor(task){
  const sources = (task.sourceIds||[]).map(id => {
    const source = workbenchSource(id);
    return source ? '<button type="button" class="wb-source-link" data-source="'+esc(id)+'" data-highlight="'+esc(workbenchTaskHighlight(task))+'">'+ic("doc")+'<span>'+esc(source.label)+'</span></button>' : "";
  }).join("");
  const images = (task.images||[]).map((image, index) => '<figure><img src="'+esc(image)+'" alt="任务附件"><button type="button" data-remove-image="'+index+'" data-image-task="'+esc(task.id)+'" aria-label="移除图片">×</button></figure>').join("");
  return '<section class="wb-task-editor" data-editor="'+esc(task.id)+'">'+
    '<div class="wb-editor-head"><input data-edit-title="'+esc(task.id)+'" value="'+esc(task.title)+'" aria-label="任务标题"><button type="button" data-close-editor aria-label="收起任务">×</button></div>'+
    '<textarea data-edit-detail="'+esc(task.id)+'" placeholder="备注">'+esc(task.detail||"")+'</textarea>'+
    '<div class="wb-editor-footer"><button type="button" data-schedule-today="'+esc(task.id)+'">今天</button><input type="date" data-schedule-date="'+esc(task.id)+'" value="'+esc(task.date||"")+'" aria-label="安排日期"><div class="wb-editor-sources">'+sources+'</div></div>'+
    (images ? '<div class="wb-image-list">'+images+'</div>' : "")+'</section>';
}

function workbenchNewTaskCard(project){
  if(!WB.newTask || WB.newTask.project !== project.id) return "";
  const sourceOptions = WORKBENCH_DEMO.sources.map(source => '<option value="'+esc(source.id)+'" '+(WB.newTask.sourceId === source.id ? "selected" : "")+'>'+esc(source.label)+'</option>').join("");
  const projectOptions = WORKBENCH_DEMO.projects.map(item => '<option value="'+esc(item.id)+'" '+(WB.newTask.project === item.id ? "selected" : "")+'>'+esc(item.name)+'</option>').join("");
  return '<section class="wb-task-editor wb-new-task" data-new-card><div class="wb-editor-head"><input data-new-title placeholder="新建待办事项" value="'+esc(WB.newTask.title)+'" aria-label="任务标题"><button type="button" data-cancel-new aria-label="取消新建">×</button></div>'+
    '<textarea data-new-detail placeholder="备注">'+esc(WB.newTask.detail)+'</textarea>'+
    '<div class="wb-editor-footer"><select data-new-project aria-label="项目">'+projectOptions+'</select><select data-new-source aria-label="来源文档"><option value="">来源文档</option>'+sourceOptions+'</select><input type="date" data-new-date value="'+esc(WB.newTask.date||"")+'" aria-label="安排日期"><button type="button" class="wb-primary" data-save-new>添加</button></div></section>';
}

function workbenchProjectBlock(project, tasks){
  const groupId = workbenchGroupId(project);
  const collapsed = WB.collapsed.has(groupId);
  const body = tasks.length ? '<div class="wb-task-list">'+tasks.map(workbenchTaskRow).join("")+'</div>' : '<p class="wb-empty">暂无任务</p>';
  return '<section class="wb-project-block"><button type="button" class="wb-project-toggle" data-group="'+esc(groupId)+'" aria-expanded="'+(!collapsed)+'"><span class="wb-project-name"><i class="wb-chevron" aria-hidden="true"></i><strong>'+esc(project.name)+'</strong><em>'+esc(project.state)+'</em></span><span>'+tasks.length+' 项</span></button>'+
    (collapsed ? "" : body+workbenchNewTaskCard(project))+'</section>';
}

function workbenchVisibleTasks(){
  if(WB.view === "day") return workbenchTasks(task => task.date === WB.anchor);
  if(WB.view === "week") return workbenchTasks(task => task.week === workbenchWeekKey());
  return workbenchTasks(task => task.month === workbenchMonthKey());
}

function renderWorkbenchProjects(){
  const tasks = workbenchVisibleTasks();
  const projects = WORKBENCH_DEMO.projects.filter(workbenchProjectMatches);
  return '<div class="wb-project-list">'+projects.map(project => workbenchProjectBlock(project, tasks.filter(task => task.project === project.id))).join("")+'</div>';
}

function openWorkbenchSource(sourceId, highlight){
  const source = workbenchSource(sourceId);
  if(!source || typeof openDocPreview !== "function") return;
  openDocPreview({kind:"path", target:source.path, title:source.label, highlight});
}

function renderWorkbenchHeader(){
  return '<header class="wb-toolbar"><div class="wb-title"><button class="wb-hamb" type="button" data-sidebar aria-label="打开侧边栏">'+ic("panel")+'</button><h1>工作台</h1></div>'+
    '<div class="wb-controls"><button type="button" class="wb-add-task" data-new-task>+ 新建任务</button><div class="wb-switch">'+["day","week","month"].map(view => '<button class="'+(WB.view === view ? "on" : "")+'" type="button" data-view="'+view+'">'+({day:"日",week:"周",month:"月"}[view])+'</button>').join("")+'</div>'+
    '<div class="wb-date-nav"><button type="button" data-nav="-1" aria-label="上一个周期">‹</button><strong>'+workbenchDateLabel()+'</strong><button type="button" data-nav="1" aria-label="下一个周期">›</button><button type="button" data-today>今天</button></div></div></header>';
}

function renderWorkbench(){
  const root = $("#wbContent");
  if(!root) return;
  root.innerHTML = renderWorkbenchHeader()+'<div class="wb-project-filter"><button class="'+(WB.project === "all" ? "on" : "")+'" type="button" data-project="all">全部项目</button>'+WORKBENCH_DEMO.projects.map(project => '<button class="'+(WB.project === project.id ? "on" : "")+'" type="button" data-project="'+esc(project.id)+'">'+esc(project.name)+'</button>').join("")+'</div>'+renderWorkbenchProjects();
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

function openWorkbenchNewTask(){
  const project = WB.project === "all" ? WORKBENCH_DEMO.projects[0] : workbenchProject(WB.project);
  if(!project) return;
  WB.editorTaskId = null;
  WB.newTask = {project:project.id, title:"", detail:"", sourceId:"", date:WB.view === "day" ? WB.anchor : ""};
  renderWorkbench();
  requestAnimationFrame(() => $("[data-new-title]")?.focus());
}

function saveWorkbenchNewTask(){
  const draft = WB.newTask;
  if(!draft) return;
  const title = draft.title.trim();
  if(!title){ $("[data-new-title]")?.focus(); return; }
  const task = {id:"task-"+Date.now(), project:draft.project, title, detail:draft.detail, status:"todo", month:workbenchMonthKey(), week:WB.view === "month" ? null : workbenchWeekKey(), date:draft.date||null, sourceIds:draft.sourceId ? [draft.sourceId] : []};
  if(task.date){ task.month=task.date.slice(0, 7); task.week=workbenchWeekKey(workbenchDate(task.date)); }
  WORKBENCH_DEMO.tasks.push(task);
  WB.newTask = null;
  WB.editorTaskId = task.id;
  renderWorkbench();
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
  const complete = event.target.closest("[data-complete]");
  if(complete){ toggleWorkbenchTask(complete.dataset.complete); return; }
  const source = event.target.closest("[data-source]");
  if(source){ openWorkbenchSource(source.dataset.source, source.dataset.highlight); return; }
  const group = event.target.closest("[data-group]");
  if(group){ WB.collapsed.has(group.dataset.group) ? WB.collapsed.delete(group.dataset.group) : WB.collapsed.add(group.dataset.group); renderWorkbench(); return; }
  if(event.target.closest("[data-new-task]")){ openWorkbenchNewTask(); return; }
  if(event.target.closest("[data-save-new]")){ saveWorkbenchNewTask(); return; }
  if(event.target.closest("[data-cancel-new]")){ WB.newTask=null; renderWorkbench(); return; }
  if(event.target.closest("[data-close-editor]")){ WB.editorTaskId=null; renderWorkbench(); return; }
  const today = event.target.closest("[data-schedule-today]");
  if(today){ scheduleWorkbenchTask(today.dataset.scheduleToday, WORKBENCH_DEMO.today); return; }
  const removeImage = event.target.closest("[data-remove-image]");
  if(removeImage){ const task=workbenchTask(removeImage.dataset.imageTask); task?.images?.splice(Number(removeImage.dataset.removeImage), 1); renderWorkbench(); return; }
  const taskRow = event.target.closest("[data-task]");
  if(taskRow){ WB.newTask=null; WB.editorTaskId = WB.editorTaskId === taskRow.dataset.task ? null : taskRow.dataset.task; renderWorkbench(); return; }
  const project = event.target.closest("[data-project]");
  if(project){ WB.project = project.dataset.project; WB.newTask=null; WB.editorTaskId=null; renderWorkbench(); return; }
  const view = event.target.closest("[data-view]");
  if(view){ WB.view = view.dataset.view; WB.newTask=null; WB.editorTaskId=null; renderWorkbench(); return; }
  const nav = event.target.closest("[data-nav]");
  if(nav){ shiftWorkbenchDate(Number(nav.dataset.nav)); return; }
  if(event.target.closest("[data-today]")){ WB.anchor = WORKBENCH_DEMO.today; renderWorkbench(); }
});

$("#workbenchView").addEventListener("input", event => {
  const task = workbenchTask(event.target.dataset.editTitle || event.target.dataset.editDetail);
  if(task){
    if(event.target.dataset.editTitle) task.title = event.target.value;
    if(event.target.dataset.editDetail) task.detail = event.target.value;
    return;
  }
  if(!WB.newTask) return;
  if("newTitle" in event.target.dataset) WB.newTask.title = event.target.value;
  if("newDetail" in event.target.dataset) WB.newTask.detail = event.target.value;
});

$("#workbenchView").addEventListener("change", event => {
  if(event.target.dataset.scheduleDate){ scheduleWorkbenchTask(event.target.dataset.scheduleDate, event.target.value); return; }
  if(!WB.newTask) return;
  if("newProject" in event.target.dataset){ WB.newTask.project=event.target.value; renderWorkbench(); }
  if("newSource" in event.target.dataset) WB.newTask.sourceId=event.target.value;
  if("newDate" in event.target.dataset) WB.newTask.date=event.target.value;
});

$("#workbenchView").addEventListener("paste", event => {
  const taskId = event.target.dataset.editDetail;
  const files = [...(event.clipboardData?.items||[])].map(item => item.getAsFile()).filter(Boolean);
  if(!taskId || !files.some(file => file.type.startsWith("image/"))) return;
  event.preventDefault(); addWorkbenchImages(taskId, files);
});

document.addEventListener("keydown", event => {
  if($("#workbenchView").hidden || event.code !== "Space" || event.ctrlKey || event.metaKey || event.altKey) return;
  if(event.target.closest("input,textarea,select,button,[contenteditable='true']")) return;
  event.preventDefault(); openWorkbenchNewTask();
});

window.openWorkbench = openWorkbench;
window.closeWorkbench = closeWorkbench;
