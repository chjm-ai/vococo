"use strict";
// 工作台：项目/来源文档/任务全部走 /workbench* 接口读写(memory/workbench.py + state.db)，
// 不再是纯前端 demo。项目支持在界面上新建/重命名/归档，不写死数量。

const WB_DATA = {projects: [], sources: [], tasks: []};
// 后端不校验 project_id、也没有"未分组"的概念（vococo/memory/workbench.py 里
// project_id 是 NOT NULL 但没有 FK 约束）——这里用空字符串当哨兵值，纯前端合成一个
// 「未分组」伪项目做兜底分组，不需要改后端 schema。
const WB_UNASSIGNED_ID = "";
const WB_UNASSIGNED_PROJECT = {id: WB_UNASSIGNED_ID, name: "未分组"};
const WB = {project:"all", view:"week", anchor:null, editorTaskId:null, selected:new Set(), selectAnchor:null, newTask:null, collapsed:new Set(), expanded:new Set()};
let wbClickTimer = null;

function workbenchIsRealProject(id){ return WB_DATA.projects.some(project => project.id === id); }
function workbenchProject(id){ return id === WB_UNASSIGNED_ID ? WB_UNASSIGNED_PROJECT : WB_DATA.projects.find(project => project.id === id); }
function workbenchSource(id){ return WB_DATA.sources.find(source => source.id === id); }
function workbenchTask(id){ return WB_DATA.tasks.find(task => task.id === id); }
function workbenchDate(value){ return new Date(value+"T12:00:00"); }
function workbenchDateKey(date){ return date.toISOString().slice(0, 10); }
function workbenchToday(){ return workbenchDateKey(new Date()); }
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

// 只用于任务（task.project 是字符串 id）；筛到「未分组」时，任何指向不存在项目的
// 游离 project_id（不只是空字符串）都算未分组——后端不校验 project_id 是否真实存在。
function workbenchProjectMatches(item){
  if(WB.project === "all") return true;
  if(WB.project === WB_UNASSIGNED_ID) return !workbenchIsRealProject(item.project);
  return item.project === WB.project;
}
function workbenchTasks(filter){ return WB_DATA.tasks.filter(task => !task.parentId && workbenchProjectMatches(task) && filter(task)); }
function workbenchChildren(parentId){ return WB_DATA.tasks.filter(task => task.parentId === parentId); }
function workbenchChildrenStats(parentId){ const children = workbenchChildren(parentId); return {total: children.length, done: children.filter(c => c.status === "done").length}; }
function workbenchTaskHighlight(task){ return task.highlight || task.title.split(/[：（(]/)[0]; }
function workbenchGroupId(project){ return "project:"+WB.view+":"+project.id; }

// ── 数据加载 ────────────────────────────────────────────────────────────
async function loadWorkbenchData(){
  try{
    const r = await api("/workbench");
    const d = await r.json();
    WB_DATA.projects = d.projects || [];
    WB_DATA.sources = d.sources || [];
    WB_DATA.tasks = d.tasks || [];
  }catch(e){}
}

// 乐观更新已经改完本地字段并重渲染后调用：失败时按 rollback 把字段改回去再重渲染一次。
async function persistWorkbenchTask(taskId, patch, rollback){
  try{
    const r = await api("/workbench/tasks/update", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(Object.assign({id:taskId}, patch))});
    const d = await r.json();
    if(!r.ok || d.error) throw new Error(d.error||"更新失败");
  }catch(e){
    if(rollback){ const task = workbenchTask(taskId); if(task){ Object.assign(task, rollback); if(!workbenchSwapTask(taskId)) renderWorkbench(); } }
    alert("工作台同步失败："+(e.message||""));
  }
}

function hydrateWorkbenchImages(){
  document.querySelectorAll("#wbContent .wb-image-list img[data-full]").forEach(im => loadAuthedImg(im, im.dataset.full, true));
}

function workbenchSourceLink(task, compact){
  const sources = (task.sourceIds||[]).map(workbenchSource).filter(Boolean);
  if(!sources.length) return '<span class="wb-no-source">无来源</span>';
  const source = sources[0];
  const suffix = sources.length > 1 ? " +"+(sources.length-1) : "";
  return '<button type="button" draggable="false" class="wb-source-link'+(compact ? " wb-task-source" : "")+'" data-source="'+esc(source.id)+'" data-highlight="'+esc(workbenchTaskHighlight(task))+'" title="'+esc(source.label)+'">'+ic("doc")+'<span>'+esc(source.label)+suffix+'</span></button>';
}

function workbenchAssigneeBadge(task){
  if(task.assignee === "ai") return '<span class="wb-assignee wb-assignee-ai" title="AI 执行">⚡</span>';
  return '';
}

function workbenchChildCount(task){
  if(task.parentId) return '';
  const stats = workbenchChildrenStats(task.id);
  if(!stats.total) return '';
  return '<span class="wb-child-count" data-toggle-children="'+esc(task.id)+'" title="子任务">'+stats.done+'/'+stats.total+'</span>';
}

function workbenchChildRows(parentId){
  if(!WB.expanded.has(parentId)) return '';
  const children = workbenchChildren(parentId);
  const newCard = (WB.newTask && WB.newTask.parentId === parentId) ? workbenchNewChildCard(parentId) : '';
  if(!children.length && !newCard) return '<div class="wb-children" data-parent="'+esc(parentId)+'"><button type="button" class="wb-add-child" data-add-child="'+esc(parentId)+'">+ 添加子任务</button></div>';
  return '<div class="wb-children" data-parent="'+esc(parentId)+'">'+children.map(child => workbenchTaskRow(child, true)).join('')+
    newCard+
    '<button type="button" class="wb-add-child" data-add-child="'+esc(parentId)+'">+ 添加子任务</button></div>';
}

function workbenchNewChildCard(parentId){
  if(!WB.newTask || WB.newTask.parentId !== parentId) return '';
  const isAi = WB.newTask.assignee === "ai";
  const assigneeBtn = '<button type="button" class="wb-assignee-toggle'+(isAi ? " is-ai" : "")+'" data-new-assignee title="切换执行者">'+(isAi ? "⚡ AI" : "👤 人工")+'</button>';
  return '<section class="wb-editor-shell wb-new-task wb-new-child" data-new-card><div class="wb-editor-head"><input data-new-title placeholder="新建子任务" value="'+esc(WB.newTask.title)+'" aria-label="子任务标题"></div>'+
    '<textarea data-new-detail placeholder="'+(isAi ? "Prompt" : "备注")+'">'+esc(WB.newTask.detail)+'</textarea>'+
    '<div class="wb-editor-footer">'+assigneeBtn+'<button type="button" class="wb-primary" data-save-new>添加</button></div></section>';
}

function workbenchSessionLinks(task){
  const ids = task.sessionIds || [];
  if(!ids.length) return '';
  return '<div class="wb-session-links">'+ids.map((sid, i) => '<a class="wb-session-link" data-session="'+esc(sid)+'" title="查看会话">会话'+(i+1)+'</a>').join('')+'</div>';
}

function workbenchTaskRow(task, isChild){
  if(WB.editorTaskId === task.id) return renderWorkbenchTaskEditor(task);
  const action = task.status === "done" ? "恢复" : "完成";
  const selected = WB.selected.has(task.id);
  const detail = task.detail ? '<p class="wb-task-detail">'+esc(task.detail)+'</p>' : "";
  const childClass = isChild ? " wb-task-child" : "";
  const row = '<article class="wb-task wb-'+esc(task.status)+(selected ? " is-selected" : "")+childClass+'" data-task="'+esc(task.id)+'" draggable="true">'+
    '<button class="wb-check" type="button" draggable="false" data-complete="'+esc(task.id)+'" aria-label="'+action+'：'+esc(task.title)+'">'+(task.status === "done" ? "✓" : task.status === "block" ? "!" : "")+'</button>'+
    '<div class="wb-task-copy">'+workbenchAssigneeBadge(task)+'<strong class="wb-task-title">'+esc(task.title)+'</strong>'+detail+'</div>'+
    '<div class="wb-task-end">'+workbenchChildCount(task)+workbenchSourceLink(task, true)+'</div>'+
    '</article>';
  if(isChild || task.parentId) return row;
  return row + workbenchChildRows(task.id);
}

function renderWorkbenchTaskEditor(task){
  const action = task.status === "done" ? "恢复" : "完成";
  const sources = (task.sourceIds||[]).map(id => {
    const source = workbenchSource(id);
    return source ? '<button type="button" class="wb-source-link" data-source="'+esc(id)+'" data-highlight="'+esc(workbenchTaskHighlight(task))+'">'+ic("doc")+'<span>'+esc(source.label)+'</span></button>' : "";
  }).join("");
  const images = (task.images||[]).map((name, index) => '<figure><img data-full="/image?name='+encodeURIComponent(name)+'" alt="任务附件"><button type="button" data-remove-image="'+index+'" data-image-task="'+esc(task.id)+'" aria-label="移除图片">×</button></figure>').join("");
  const isAi = task.assignee === "ai";
  const assigneeBtn = '<button type="button" class="wb-assignee-toggle'+(isAi ? " is-ai" : "")+'" data-toggle-assignee="'+esc(task.id)+'" title="切换执行者">'+(isAi ? "⚡ AI" : "👤 人工")+'</button>';
  const dispatchBtn = isAi ? '<button type="button" class="wb-dispatch-btn" data-dispatch="'+esc(task.id)+'" title="让 AI 执行此任务">▶ 执行</button>' : "";
  const sessionLinks = workbenchSessionLinks(task);
  return '<article class="wb-task wb-editor-shell wb-task-card wb-'+esc(task.status)+'" data-task="'+esc(task.id)+'">'+
    '<div class="wb-card-head">'+
      '<button class="wb-check" type="button" data-complete="'+esc(task.id)+'" aria-label="'+action+'：'+esc(task.title)+'">'+(task.status === "done" ? "✓" : task.status === "block" ? "!" : "")+'</button>'+
      '<input class="wb-card-title" data-edit-title="'+esc(task.id)+'" value="'+esc(task.title)+'" aria-label="任务标题">'+
      '<button type="button" class="wb-card-delete" data-delete-task="'+esc(task.id)+'" aria-label="删除任务">'+ic("trash")+'</button>'+
    '</div>'+
    '<textarea data-edit-detail="'+esc(task.id)+'" placeholder="'+(isAi ? "Prompt（AI 执行时的指令）" : "备注（思路/要点）")+'">'+esc(task.detail||"")+'</textarea>'+
    (images ? '<div class="wb-image-list">'+images+'</div>' : "")+
    sessionLinks+
    '<div class="wb-editor-footer">'+assigneeBtn+dispatchBtn+'<button type="button" class="wb-dp-trigger'+(task.date ? " has-date" : "")+'" data-open-dp="task:'+esc(task.id)+'">'+ic("calendar")+'<span>'+esc(workbenchDpLabel(task.date))+'</span></button><div class="wb-editor-sources">'+sources+'</div></div>'+'</article>';
}

function workbenchNewTaskCard(project){
  if(!WB.newTask || WB.newTask.project !== project.id) return "";
  const sourceOptions = WB_DATA.sources.map(source => '<option value="'+esc(source.id)+'" '+(WB.newTask.sourceId === source.id ? "selected" : "")+'>'+esc(source.label)+'</option>').join("");
  const projectOptions = [...WB_DATA.projects, WB_UNASSIGNED_PROJECT].map(item => '<option value="'+esc(item.id)+'" '+(WB.newTask.project === item.id ? "selected" : "")+'>'+esc(item.name)+'</option>').join("");
  const isAi = WB.newTask.assignee === "ai";
  const assigneeBtn = '<button type="button" class="wb-assignee-toggle'+(isAi ? " is-ai" : "")+'" data-new-assignee title="切换执行者">'+(isAi ? "⚡ AI" : "👤 人工")+'</button>';
  return '<section class="wb-editor-shell wb-new-task" data-new-card><div class="wb-editor-head"><input data-new-title placeholder="新建待办事项" value="'+esc(WB.newTask.title)+'" aria-label="任务标题"></div>'+
    '<textarea data-new-detail placeholder="'+(isAi ? "Prompt（AI 执行时的指令）" : "备注")+'">'+esc(WB.newTask.detail)+'</textarea>'+
    '<div class="wb-editor-footer">'+assigneeBtn+'<select data-new-project aria-label="项目">'+projectOptions+'</select><select data-new-source aria-label="来源文档"><option value="">来源文档</option>'+sourceOptions+'</select><button type="button" class="wb-dp-trigger'+(WB.newTask.date ? " has-date" : "")+'" data-open-dp="new">'+ic("calendar")+'<span>'+esc(workbenchDpLabel(WB.newTask.date))+'</span></button><button type="button" class="wb-primary" data-save-new>添加</button></div></section>';
}

function workbenchProjectBlock(project, tasks){
  const groupId = workbenchGroupId(project);
  const collapsed = WB.collapsed.has(groupId);
  const body = tasks.length ? '<div class="wb-task-list">'+tasks.map(t => workbenchTaskRow(t)).join("")+'</div>' : '<p class="wb-empty">暂无任务</p>';
  const showNewCard = (!WB.newTask || !WB.newTask.parentId) ? workbenchNewTaskCard(project) : "";
  return '<section class="wb-project-block"><button type="button" class="wb-project-toggle" data-group="'+esc(groupId)+'" aria-expanded="'+(!collapsed)+'"><span class="wb-project-name"><strong>'+esc(project.name)+'</strong><i class="wb-chevron" aria-hidden="true"></i></span></button>'+
    (collapsed ? "" : body+showNewCard)+'</section>';
}

function workbenchVisibleTasks(){
  if(WB.view === "unscheduled") return workbenchTasks(task => !task.date);
  if(WB.view === "day") return workbenchTasks(task => task.date === WB.anchor);
  if(WB.view === "week") return workbenchTasks(task => task.week === workbenchWeekKey());
  return workbenchTasks(task => task.month === workbenchMonthKey());
}

function renderWorkbenchProjects(){
  const tasks = workbenchVisibleTasks();
  const unassignedTasks = tasks.filter(task => !workbenchIsRealProject(task.project));
  // workbenchProjectMatches 是给「任务」用的（判断 task.project），项目对象本身没有
  // .project 字段，不能拿来筛项目列表——这里按当前筛选独立决定要显示哪些项目分组。
  let projects;
  if(WB.project === WB_UNASSIGNED_ID){
    projects = [WB_UNASSIGNED_PROJECT];
  }else if(WB.project === "all"){
    // 「未分组」只在确实有游离任务时才出现，避免空占位。
    projects = unassignedTasks.length ? [...WB_DATA.projects, WB_UNASSIGNED_PROJECT] : WB_DATA.projects;
  }else{
    const one = workbenchProject(WB.project);
    projects = one ? [one] : [];
  }
  if(!projects.length) return '<p class="wb-empty">还没有项目，点右上角「+」新建一个。</p>';
  return '<div class="wb-project-list">'+projects.map(project => {
    const list = project.id === WB_UNASSIGNED_ID ? unassignedTasks : tasks.filter(task => task.project === project.id);
    return workbenchProjectBlock(project, list);
  }).join("")+'</div>';
}

function openWorkbenchSource(sourceId, highlight){
  const source = workbenchSource(sourceId);
  if(!source || typeof openDocPreview !== "function") return;
  openDocPreview({kind:"path", target:source.path, title:source.label, highlight});
}

function renderWorkbenchHeader(){
  const dateFreeView = WB.view === "unscheduled" || WB.view === "trash";
  const dateNav = dateFreeView ? "" :
    '<div class="wb-date-nav"><button type="button" data-nav="-1" aria-label="上一个周期">‹</button><strong>'+workbenchDateLabel()+'</strong><button type="button" data-nav="1" aria-label="下一个周期">›</button><button type="button" data-today>今天</button></div>';
  return '<header class="wb-toolbar"><div class="wb-title"><button class="wb-hamb" type="button" data-sidebar aria-label="打开侧边栏">'+ic("panel")+'</button><h1>工作台</h1>'+
      '<button type="button" class="wb-win-btn" data-workbench-win title="独立窗口" aria-label="独立窗口">'+ic("newwin")+'</button></div>'+
    '<div class="wb-switch">'+
      '<button class="wb-switch-icon'+(WB.view === "unscheduled" ? " on" : "")+'" type="button" data-view="unscheduled" aria-label="未排期">'+ic("inbox")+'</button>'+
      '<button class="wb-switch-icon'+(WB.view === "trash" ? " on" : "")+'" type="button" data-view="trash" aria-label="回收站">'+ic("trash")+'</button>'+
      ["day","week","month"].map(view => '<button class="'+(WB.view === view ? "on" : "")+'" type="button" data-view="'+view+'">'+({day:"日",week:"周",month:"月"}[view])+'</button>').join("")+
    '</div>'+dateNav+'</header>';
}

const WB_TRASH = {tasks: [], loaded: false};

async function loadWorkbenchTrash(){
  try{
    const r = await api("/workbench/trash");
    const d = await r.json();
    WB_TRASH.tasks = d.tasks || [];
  }catch(e){}
  WB_TRASH.loaded = true;
}

function workbenchTrashRow(task){
  return '<article class="wb-task wb-trash-row" data-trash-task="'+esc(task.id)+'">'+
    '<div class="wb-task-copy"><strong class="wb-task-title">'+esc(task.title)+'</strong></div>'+
    '<div class="wb-trash-actions"><button type="button" data-restore-task="'+esc(task.id)+'">恢复</button><button type="button" class="wb-ctx-danger" data-purge-task="'+esc(task.id)+'">彻底删除</button></div>'+
    '</article>';
}

function renderWorkbenchTrash(){
  if(!WB_TRASH.loaded) return '<p class="wb-empty">加载中…</p>';
  if(!WB_TRASH.tasks.length) return '<p class="wb-empty">回收站是空的。</p>';
  return '<div class="wb-task-list wb-trash-list">'+WB_TRASH.tasks.map(workbenchTrashRow).join("")+'</div>';
}

function renderWorkbenchProjectFilter(){
  const chips = WB_DATA.projects.map(project => '<button class="'+(WB.project === project.id ? "on" : "")+'" type="button" data-project="'+esc(project.id)+'" title="右键：重命名/归档">'+esc(project.name)+'</button>').join("");
  const unassignedChip = '<button class="'+(WB.project === WB_UNASSIGNED_ID ? "on" : "")+'" type="button" data-project="'+WB_UNASSIGNED_ID+'">未分组</button>';
  return '<div class="wb-project-filter"><button class="'+(WB.project === "all" ? "on" : "")+'" type="button" data-project="all">全部项目</button>'+chips+unassignedChip+
    '<button class="wb-project-add" type="button" data-add-project aria-label="新建项目" title="新建项目">'+ic("plus")+'</button></div>';
}

// 备注框跟着内容长高，但夹在 [最小, 最大] 之间——超过最大值就交给自己的滚动条，
// 不然一段很长的备注能把整张卡片撑到没边。
const WB_DETAIL_MIN_H = 76, WB_DETAIL_MAX_H = 240;
function workbenchAutoGrowTextarea(el){
  if(!el) return;
  el.style.height = "auto";
  el.style.height = Math.min(Math.max(el.scrollHeight, WB_DETAIL_MIN_H), WB_DETAIL_MAX_H)+"px";
}
function workbenchAutoGrowAll(){
  document.querySelectorAll("#workbenchView textarea[data-edit-detail], #workbenchView textarea[data-new-detail]").forEach(workbenchAutoGrowTextarea);
}

function renderWorkbench(){
  const root = $("#wbContent");
  if(!root) return;
  const body = WB.view === "trash" ? renderWorkbenchTrash() : renderWorkbenchProjectFilter()+renderWorkbenchProjects();
  root.innerHTML = renderWorkbenchHeader()+body;
  hydrateWorkbenchImages();
  workbenchAutoGrowAll();
}

function workbenchNodeForTask(taskId){
  return document.querySelector('[data-task="'+CSS.escape(taskId)+'"]');
}

// 只替换单个任务节点，不重建整棵列表；否则每次点击都会让浏览器把全表回流一遍，动效必卡。
function workbenchSwapTask(taskId){
  const node = workbenchNodeForTask(taskId);
  const task = workbenchTask(taskId);
  if(!node || !task) return false;
  const wrap = document.createElement("div");
  wrap.innerHTML = workbenchTaskRow(task);
  const next = wrap.firstElementChild;
  if(!next) return false;
  node.replaceWith(next);
  hydrateWorkbenchImages();
  return true;
}

// 同上，额外做一个高度过渡：行与卡片高度不同，直接换节点会「啪」一下跳变，这里让它长出来/收回去。
function workbenchMorphTask(taskId){
  const node = workbenchNodeForTask(taskId);
  const task = workbenchTask(taskId);
  if(!node || !task) return false;
  const startRect = node.getBoundingClientRect();
  const wrap = document.createElement("div");
  wrap.innerHTML = workbenchTaskRow(task);
  const next = wrap.firstElementChild;
  if(!next) return false;
  node.replaceWith(next);
  hydrateWorkbenchImages();
  workbenchAutoGrowTextarea(next.querySelector("textarea[data-edit-detail]"));
  const endRect = next.getBoundingClientRect();
  const endStyle = getComputedStyle(next);
  const endMarginLeft = endStyle.marginLeft;
  const endMarginRight = endStyle.marginRight;
  const sameSize = Math.abs(endRect.height - startRect.height) < 1 && Math.abs(endRect.width - startRect.width) < 1;
  if(sameSize) return true;
  // 编辑卡比列表行宽（左右各多出 16px），行→卡切换时宽度也要跟高度一起过渡，
  // 否则宽度是一瞬间跳变的，看起来就是「抖一下」。
  next.style.height = startRect.height+"px";
  next.style.width = startRect.width+"px";
  next.style.marginLeft = "0px";
  next.style.marginRight = "0px";
  next.style.overflow = "hidden";
  void next.offsetHeight;
  next.style.transition = "height .2s cubic-bezier(.22,.61,.36,1), width .2s cubic-bezier(.22,.61,.36,1), margin-left .2s cubic-bezier(.22,.61,.36,1), margin-right .2s cubic-bezier(.22,.61,.36,1)";
  requestAnimationFrame(() => {
    next.style.height = endRect.height+"px";
    next.style.width = endRect.width+"px";
    next.style.marginLeft = endMarginLeft;
    next.style.marginRight = endMarginRight;
  });
  next.addEventListener("transitionend", function onEnd(event){
    if(event.propertyName !== "height" || event.target !== next) return;
    next.style.height = ""; next.style.width = ""; next.style.marginLeft = ""; next.style.marginRight = "";
    next.style.overflow = ""; next.style.transition = "";
    next.removeEventListener("transitionend", onEnd);
  });
  return true;
}

// 只在同一个项目分组内调整任务的相对顺序；显示顺序即 WB_DATA.tasks 的数组顺序（不持久化，刷新后按后端 sort_order 恢复）。
function workbenchReorderTask(draggedId, targetId, placeBefore){
  if(draggedId === targetId) return false;
  const tasks = WB_DATA.tasks;
  const dragged = workbenchTask(draggedId);
  const target = workbenchTask(targetId);
  if(!dragged || !target || dragged.project !== target.project) return false;
  const draggedIdx = tasks.indexOf(dragged);
  tasks.splice(draggedIdx, 1);
  let targetIdx = tasks.indexOf(target);
  if(!placeBefore) targetIdx += 1;
  tasks.splice(targetIdx, 0, dragged);
  return true;
}

function workbenchRefreshProjectBlock(projectId){
  const project = workbenchProject(projectId);
  if(!project) return false;
  const node = document.querySelector('[data-group="'+CSS.escape(workbenchGroupId(project))+'"]')?.closest(".wb-project-block");
  if(!node) return false;
  const tasks = workbenchVisibleTasks().filter(task => task.project === projectId);
  const wrap = document.createElement("div");
  wrap.innerHTML = workbenchProjectBlock(project, tasks);
  const next = wrap.firstElementChild;
  if(!next) return false;
  node.replaceWith(next);
  return true;
}

// 只做 class 增删，不重建任何节点——纯选中态变化没必要触发那么重的操作。
function workbenchApplySelectionClasses(prevSet, nextSet){
  prevSet.forEach(id => { if(!nextSet.has(id)) workbenchNodeForTask(id)?.classList.remove("is-selected"); });
  nextSet.forEach(id => workbenchNodeForTask(id)?.classList.add("is-selected"));
}

function workbenchSetSelection(ids){
  const next = new Set(ids);
  workbenchApplySelectionClasses(WB.selected, next);
  WB.selected = next;
}

// 取 anchor→target 之间「同一个项目分组、当前 DOM 顺序」上的所有任务 id；
// 两端不在同一个 .wb-task-list 里（比如跨了项目分组）就退化成只选 target。
function workbenchRowRange(anchorId, targetId){
  const anchorNode = workbenchNodeForTask(anchorId);
  const targetNode = workbenchNodeForTask(targetId);
  if(!anchorNode || !targetNode) return [targetId];
  const list = anchorNode.closest(".wb-task-list");
  if(!list || list !== targetNode.closest(".wb-task-list")) return [targetId];
  const rows = [...list.querySelectorAll(":scope > [data-task]")];
  const i = rows.indexOf(anchorNode), j = rows.indexOf(targetNode);
  if(i === -1 || j === -1) return [targetId];
  const [lo, hi] = i < j ? [i, j] : [j, i];
  return rows.slice(lo, hi+1).map(el => el.dataset.task);
}

function workbenchSelectSingle(taskId){
  const prevEditor = WB.editorTaskId;
  const hadNewTask = !!WB.newTask;
  WB.newTask = null;
  WB.editorTaskId = null;
  workbenchSetSelection([taskId]);
  WB.selectAnchor = taskId;
  if(hadNewTask){ renderWorkbench(); return; }
  if(prevEditor && prevEditor !== taskId && !workbenchMorphTask(prevEditor)) renderWorkbench();
}

function workbenchSelectRange(anchorId, targetId){
  const prevEditor = WB.editorTaskId;
  const hadNewTask = !!WB.newTask;
  WB.newTask = null;
  WB.editorTaskId = null;
  workbenchSetSelection(workbenchRowRange(anchorId, targetId));
  if(hadNewTask){ renderWorkbench(); return; }
  if(prevEditor && !workbenchMorphTask(prevEditor)) renderWorkbench();
}

function openWorkbenchEditor(taskId){
  const prevEditor = WB.editorTaskId;
  const hadNewTask = !!WB.newTask;
  WB.newTask = null;
  workbenchSetSelection([]);
  WB.editorTaskId = taskId;
  let ok = !hadNewTask;
  if(ok){
    if(prevEditor && prevEditor !== taskId) ok = workbenchMorphTask(prevEditor) && ok;
    ok = workbenchMorphTask(taskId) && ok;
  }
  if(!ok) renderWorkbench();
  requestAnimationFrame(() => {
    const el = $(".wb-card-title");
    if(!el) return;
    el.focus({preventScroll:true});
    const len = el.value.length;
    el.setSelectionRange(len, len); // 只定位光标，不要默认全选标题
  });
}

function toggleWorkbenchTask(taskId){
  const task = workbenchTask(taskId);
  if(!task) return;
  const prevStatus = task.status;
  task.status = task.status === "done" ? "todo" : "done";
  if(!workbenchSwapTask(taskId)) renderWorkbench();
  persistWorkbenchTask(taskId, {status: task.status}, {status: prevStatus});
}

function scheduleWorkbenchTask(taskId, date){
  const task = workbenchTask(taskId);
  if(!task) return;
  const prev = {date: task.date, month: task.month, week: task.week};
  task.date = date || null;
  if(date){ task.month = date.slice(0, 7); task.week = workbenchWeekKey(workbenchDate(date)); }
  renderWorkbench();
  persistWorkbenchTask(taskId, {date: task.date, month: task.month, week: task.week}, prev);
}

async function uploadWorkbenchImage(taskId, file){
  const dataUrl = await new Promise(resolve => {
    const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.readAsDataURL(file);
  });
  const comma = dataUrl.indexOf(",");
  const data = comma >= 0 ? dataUrl.slice(comma+1) : dataUrl;
  try{
    const r = await api("/workbench/tasks/image/add", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id:taskId, data, mediaType:file.type})});
    const d = await r.json();
    if(!r.ok || d.error) throw new Error(d.error||"上传失败");
    const task = workbenchTask(taskId);
    if(!task) return;
    task.images = [...(task.images||[]), d.name];
    if(!workbenchMorphTask(taskId)) renderWorkbench();
  }catch(e){ alert("图片上传失败："+(e.message||"")); }
}

function addWorkbenchImages(taskId, files){
  const images = [...files].filter(file => file.type.startsWith("image/"));
  images.forEach(file => uploadWorkbenchImage(taskId, file));
}

async function removeWorkbenchImage(taskId, name){
  try{ await api("/workbench/tasks/image/remove", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id:taskId, name})}); }
  catch(e){}
}

// 跟 workbenchMorphTask 是同一套手法：先量出「长成之后」的高度，再从 0 长过去，
// 而不是整页重渲染后让新卡片凭空「啪」地出现。
function workbenchInsertNewTaskCard(project){
  const block = document.querySelector('[data-group="'+CSS.escape(workbenchGroupId(project))+'"]')?.closest(".wb-project-block");
  if(!block) return false;
  const wrap = document.createElement("div");
  wrap.innerHTML = workbenchNewTaskCard(project);
  const card = wrap.firstElementChild;
  if(!card) return false;
  block.appendChild(card);
  workbenchAutoGrowTextarea(card.querySelector("textarea[data-new-detail]"));
  const endRect = card.getBoundingClientRect();
  const endMarginTop = getComputedStyle(card).marginTop;
  card.style.height = "0px";
  card.style.marginTop = "0px";
  card.style.overflow = "hidden";
  void card.offsetHeight;
  card.style.transition = "height .2s cubic-bezier(.22,.61,.36,1), margin-top .2s cubic-bezier(.22,.61,.36,1)";
  requestAnimationFrame(() => {
    card.style.height = endRect.height+"px";
    card.style.marginTop = endMarginTop;
  });
  card.addEventListener("transitionend", function onEnd(event){
    if(event.propertyName !== "height" || event.target !== card) return;
    card.style.height = ""; card.style.marginTop = ""; card.style.overflow = ""; card.style.transition = "";
    card.removeEventListener("transitionend", onEnd);
  });
  return true;
}

// 取消新建时对称地收回去，而不是直接从 DOM 里消失。
function workbenchRemoveNewTaskCard(){
  const card = document.querySelector("[data-new-card]");
  if(!card) return false;
  card.style.height = card.getBoundingClientRect().height+"px";
  card.style.marginTop = getComputedStyle(card).marginTop;
  card.style.overflow = "hidden";
  void card.offsetHeight;
  card.style.transition = "height .16s ease, margin-top .16s ease, opacity .16s ease";
  requestAnimationFrame(() => {
    card.style.height = "0px";
    card.style.marginTop = "0px";
    card.style.opacity = "0";
  });
  card.addEventListener("transitionend", function onEnd(event){
    if(event.propertyName !== "height" || event.target !== card) return;
    card.remove();
  });
  return true;
}

function openWorkbenchNewTask(){
  const project = WB.project === "all" ? WB_DATA.projects[0] : workbenchProject(WB.project);
  if(!project){ alert("请先新建一个项目。"); return; }
  clearTimeout(wbClickTimer);
  const prevEditor = WB.editorTaskId;
  WB.editorTaskId = null;
  WB.selected = new Set(); WB.selectAnchor = null;
  WB.newTask = {project:project.id, title:"", detail:"", sourceId:"", date:WB.view === "day" ? WB.anchor : "", assignee:"human", parentId:null};
  const groupId = workbenchGroupId(project);
  const wasCollapsed = WB.collapsed.has(groupId);
  if(wasCollapsed) WB.collapsed.delete(groupId);
  let ok = !wasCollapsed;
  if(ok && prevEditor) ok = workbenchMorphTask(prevEditor) && ok;
  if(ok) ok = workbenchInsertNewTaskCard(project) && ok;
  if(!ok) renderWorkbench();
  requestAnimationFrame(() => $("[data-new-title]")?.focus());
}

async function saveWorkbenchNewTask(){
  const draft = WB.newTask;
  if(!draft) return;
  const title = draft.title.trim();
  if(!title){ $("[data-new-title]")?.focus(); return; }
  const date = draft.date || null;
  const month = date ? date.slice(0, 7) : workbenchMonthKey();
  const week = date ? workbenchWeekKey(workbenchDate(date)) : (WB.view === "month" ? null : workbenchWeekKey());
  const payload = {project:draft.project, title, detail:draft.detail, date, month, week, sourceIds:draft.sourceId ? [draft.sourceId] : [], assignee:draft.assignee||"human", parentId:draft.parentId||null};
  WB.newTask = null;
  try{
    const r = await api("/workbench/tasks/create", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
    const d = await r.json();
    if(!r.ok || d.error) throw new Error(d.error||"创建失败");
    WB_DATA.tasks.push(d.task);
    WB.editorTaskId = d.task.id;
  }catch(e){ alert("新建任务失败："+(e.message||"")); }
  renderWorkbench();
}

function toggleWorkbenchChildren(parentId){
  if(WB.expanded.has(parentId)) WB.expanded.delete(parentId);
  else WB.expanded.add(parentId);
  renderWorkbench();
}

function toggleWorkbenchAssignee(taskId){
  const task = workbenchTask(taskId);
  if(!task) return;
  const prev = task.assignee;
  task.assignee = prev === "ai" ? "human" : "ai";
  if(!workbenchMorphTask(taskId)) renderWorkbench();
  persistWorkbenchTask(taskId, {assignee: task.assignee}, {assignee: prev});
}

async function dispatchWorkbenchTask(taskId){
  const task = workbenchTask(taskId);
  if(!task) return;
  if(!task.detail?.trim()){ alert("任务没有备注/prompt，无法执行。请先填写备注。"); return; }
  try{
    const r = await api("/workbench/tasks/dispatch", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id:taskId})});
    const d = await r.json();
    if(!r.ok || d.error) throw new Error(d.error||"执行失败");
    const t = workbenchTask(taskId);
    if(t && d.task) Object.assign(t, d.task);
    if(!workbenchMorphTask(taskId)) renderWorkbench();
  }catch(e){ alert("派发执行失败："+(e.message||"")); }
}

function openWorkbenchNewChild(parentId){
  const parent = workbenchTask(parentId);
  if(!parent) return;
  const project = workbenchProject(parent.project) || WB_DATA.projects[0];
  if(!project) return;
  clearTimeout(wbClickTimer);
  const prevEditor = WB.editorTaskId;
  WB.editorTaskId = null;
  WB.selected = new Set(); WB.selectAnchor = null;
  WB.expanded.add(parentId);
  WB.newTask = {project:project.id, title:"", detail:"", sourceId:"", date:WB.view === "day" ? WB.anchor : "", assignee:"human", parentId:parentId};
  renderWorkbench();
  requestAnimationFrame(() => $("[data-new-title]")?.focus());
}

// 删除 = 软删除(移入回收站)，不用再弹确认框——删错了去回收站图标里恢复就行。
async function deleteWorkbenchTask(taskId){
  const task = workbenchTask(taskId);
  if(!task) return;
  WB_DATA.tasks = WB_DATA.tasks.filter(t => t.id !== taskId);
  if(WB.editorTaskId === taskId) WB.editorTaskId = null;
  WB.selected.delete(taskId);
  renderWorkbench();
  try{
    const r = await api("/workbench/tasks/delete", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id:taskId})});
    if(!r.ok) throw new Error("删除失败");
  }catch(e){ alert("删除失败，请刷新页面重试"); }
}

async function workbenchRestoreTask(taskId){
  const idx = WB_TRASH.tasks.findIndex(t => t.id === taskId);
  if(idx === -1) return;
  const [task] = WB_TRASH.tasks.splice(idx, 1);
  renderWorkbench();
  try{
    const r = await api("/workbench/tasks/restore", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id:taskId})});
    const d = await r.json();
    if(!r.ok || d.error) throw new Error(d.error||"恢复失败");
    WB_DATA.tasks.push(d.task);
  }catch(e){ WB_TRASH.tasks.splice(idx, 0, task); renderWorkbench(); alert("恢复失败："+(e.message||"")); }
}

async function workbenchPurgeTask(taskId){
  if(!confirm("彻底删除该任务？不可恢复。")) return;
  const idx = WB_TRASH.tasks.findIndex(t => t.id === taskId);
  if(idx === -1) return;
  const [task] = WB_TRASH.tasks.splice(idx, 1);
  renderWorkbench();
  try{
    const r = await api("/workbench/tasks/purge", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id:taskId})});
    if(!r.ok) throw new Error("彻底删除失败");
  }catch(e){ WB_TRASH.tasks.splice(idx, 0, task); renderWorkbench(); alert("彻底删除失败："+(e.message||"")); }
}

// ── 多选批量操作（右键菜单）─────────────────────────────────────────────
function workbenchBatchComplete(ids){
  let ok = true;
  const rollbacks = [];
  ids.forEach(id => {
    const task = workbenchTask(id);
    if(!task || task.status === "done") return;
    rollbacks.push({id, status: task.status});
    task.status = "done";
    ok = workbenchSwapTask(id) && ok;
  });
  if(!ok) renderWorkbench();
  rollbacks.forEach(({id, status}) => persistWorkbenchTask(id, {status:"done"}, {status}));
}

function workbenchBatchSchedule(ids, date){
  const rollbacks = [];
  ids.forEach(id => {
    const task = workbenchTask(id);
    if(!task) return;
    rollbacks.push({id, date: task.date, month: task.month, week: task.week});
    task.date = date || null;
    if(date){ task.month = date.slice(0, 7); task.week = workbenchWeekKey(workbenchDate(date)); }
  });
  renderWorkbench();
  rollbacks.forEach(({id, date: prevDate, month: prevMonth, week: prevWeek}) => {
    const task = workbenchTask(id);
    if(!task) return;
    persistWorkbenchTask(id, {date: task.date, month: task.month, week: task.week}, {date: prevDate, month: prevMonth, week: prevWeek});
  });
}

function workbenchBatchMove(ids, projectId){
  const rollbacks = [];
  ids.forEach(id => {
    const task = workbenchTask(id);
    if(!task) return;
    rollbacks.push({id, project: task.project});
    task.project = projectId;
  });
  renderWorkbench();
  rollbacks.forEach(({id, project: prevProject}) => persistWorkbenchTask(id, {project: projectId}, {project: prevProject}));
}

function workbenchBatchDelete(ids){
  if(!ids.length) return;
  const idSet = new Set(ids);
  WB_DATA.tasks = WB_DATA.tasks.filter(t => !idSet.has(t.id));
  if(idSet.has(WB.editorTaskId)) WB.editorTaskId = null;
  WB.selected = new Set(); WB.selectAnchor = null;
  renderWorkbench();
  ids.forEach(id => {
    api("/workbench/tasks/delete", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id})}).catch(()=>{});
  });
}

async function addWorkbenchProject(){
  const name = (prompt("新建项目名称：")||"").trim();
  if(!name) return;
  try{
    const r = await api("/workbench/projects/create", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({name})});
    const d = await r.json();
    if(!r.ok || d.error) throw new Error(d.error||"创建失败");
    WB_DATA.projects.push(d.project);
    WB.project = d.project.id;
    renderWorkbench();
  }catch(e){ alert("新建项目失败："+(e.message||"")); }
}

async function renameWorkbenchProject(projectId, name){
  const project = workbenchProject(projectId);
  const prevName = project ? project.name : "";
  if(project) project.name = name;
  renderWorkbench();
  try{
    const r = await api("/workbench/projects/rename", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id:projectId, name})});
    const d = await r.json();
    if(!r.ok || d.error) throw new Error(d.error||"重命名失败");
  }catch(e){
    if(project){ project.name = prevName; renderWorkbench(); }
    alert("重命名失败："+(e.message||""));
  }
}

async function archiveWorkbenchProject(projectId){
  WB_DATA.projects = WB_DATA.projects.filter(p => p.id !== projectId);
  if(WB.project === projectId) WB.project = "all";
  renderWorkbench();
  try{
    const r = await api("/workbench/projects/archive", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id:projectId})});
    if(!r.ok) throw new Error("归档失败");
  }catch(e){ alert("归档失败，请刷新页面重试"); }
}

function workbenchProjectMenu(projectId){
  const project = workbenchProject(projectId);
  if(!project) return;
  const name = prompt("重命名项目(清空后确定 = 归档该项目，名下任务会保留)：", project.name);
  if(name === null) return;
  const trimmed = name.trim();
  if(!trimmed){
    if(confirm('归档项目「'+project.name+'」？名下任务仍会保留，只是不再显示。')) archiveWorkbenchProject(projectId);
    return;
  }
  if(trimmed !== project.name) renameWorkbenchProject(projectId, trimmed);
}

function shiftWorkbenchDate(direction){
  const date = workbenchDate(WB.anchor);
  date.setDate(date.getDate() + direction * (WB.view === "month" ? 30 : WB.view === "week" ? 7 : 1));
  WB.anchor = workbenchDateKey(date); renderWorkbench();
}

// ── 日期选择弹层（仿 Things）─────────────────────────────────────────────
// 三处入口共用同一个弹层实例：任务编辑卡单个日期、新建任务卡日期、右键菜单批量日期。
// target: {kind:"task", id} | {kind:"new"} | {kind:"ctx", ids}
const WB_DP = {open:false, target:null, expanded:false, baseWeek:null, rangeBefore:4, rangeAfter:12, results:[], _reposition:null};
const WB_DP_STEP = 8; // 展开后触底/触顶时一次追加的周数

function workbenchDpLabel(dateStr){
  if(!dateStr) return "设定日期";
  if(dateStr === workbenchToday()) return "今天";
  const d = workbenchDate(dateStr);
  const now = new Date();
  return (d.getFullYear() !== now.getFullYear() ? d.getFullYear()+"年" : "")+(d.getMonth()+1)+"月"+d.getDate()+"日";
}

// 面板按周日起始排列，只影响这个弹层的视觉网格，跟 workbenchWeekKey（周一起始，后端分组用）互不相关。
function workbenchDpSundayKey(dateStr){
  const d = workbenchDate(dateStr);
  d.setDate(d.getDate() - d.getDay());
  return workbenchDateKey(d);
}

function workbenchDpWeekStarts(baseWeekKey, before, after){
  const base = workbenchDate(baseWeekKey);
  const keys = [];
  for(let i = -before; i <= after; i++){
    const d = new Date(base); d.setDate(d.getDate() + i*7);
    keys.push(workbenchDateKey(d));
  }
  return keys;
}

function workbenchDpDaysOfWeek(weekStartKey){
  const start = workbenchDate(weekStartKey);
  const days = [];
  for(let i = 0; i < 7; i++){
    const d = new Date(start); d.setDate(d.getDate()+i);
    days.push(workbenchDateKey(d));
  }
  return days;
}

function workbenchDpCurrentDate(){
  const target = WB_DP.target;
  if(!target) return null;
  if(target.kind === "task") return workbenchTask(target.id)?.date || null;
  if(target.kind === "new") return WB.newTask ? WB.newTask.date : null;
  return null; // ctx：批量操作没有单一「当前日期」
}

// 支持 9/12、9-12、2026-9-12、2026/9/12、9月12日、9月（缺省 1 号）几种写法。
// 带年份 → 唯一结果；不带年份 → 年份有歧义，列出「今天起最近的一次出现」+ 后两年，共最多 3 条候选
// （仿 Things 的搜索结果下拉，不用猜用户想要哪一年）。
function workbenchDpParseQuery(text){
  const s = text.trim();
  if(!s) return null;
  let m = s.match(/^(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?$/);
  if(m) return {year:+m[1], month:+m[2], day:+m[3]};
  m = s.match(/^(\d{1,2})[-/月](\d{1,2})日?$/);
  if(m) return {year:null, month:+m[1], day:+m[2]};
  m = s.match(/^(\d{1,2})月$/);
  if(m) return {year:null, month:+m[1], day:1};
  return null;
}

function workbenchDpResultItem(dateKey){
  const d = workbenchDate(dateKey);
  const now = new Date();
  const label = (d.getFullYear() !== now.getFullYear() ? d.getFullYear()+"年" : "")+(d.getMonth()+1)+"月"+d.getDate()+"日";
  const weekday = "周"+"日一二三四五六"[d.getDay()];
  return {dateKey, label, weekday};
}

function workbenchDpSearchResults(text){
  const parsed = workbenchDpParseQuery(text);
  if(!parsed || parsed.month < 1 || parsed.month > 12 || parsed.day < 1 || parsed.day > 31) return [];
  if(parsed.year != null){
    const key = workbenchDpNormalize(parsed.year, parsed.month, parsed.day);
    return key ? [workbenchDpResultItem(key)] : [];
  }
  const today = workbenchToday();
  let y = new Date().getFullYear();
  const firstTry = workbenchDpNormalize(y, parsed.month, parsed.day);
  if(!firstTry || firstTry < today) y += 1;
  const results = [];
  for(let tries = 0; results.length < 3 && tries < 6; tries++, y++){
    const key = workbenchDpNormalize(y, parsed.month, parsed.day);
    if(key) results.push(workbenchDpResultItem(key));
  }
  return results;
}

function workbenchDpNormalize(year, month, day){
  if(month < 1 || month > 12 || day < 1 || day > 31) return null;
  const d = new Date(year, month-1, day, 12);
  if(d.getMonth() !== month-1) return null; // 拒绝 2/30 这类溢出日期
  return workbenchDateKey(d);
}

function workbenchDpApply(dateStr){
  const target = WB_DP.target;
  if(!target) return;
  if(target.kind === "task") scheduleWorkbenchTask(target.id, dateStr);
  else if(target.kind === "new"){ WB.newTask.date = dateStr; renderWorkbench(); }
  else if(target.kind === "ctx") workbenchBatchSchedule(target.ids, dateStr);
  workbenchDpClose();
}

function workbenchDpWeekRow(weekKey, selected, today){
  const cells = workbenchDpDaysOfWeek(weekKey).map(dayKey => {
    const d = workbenchDate(dayKey);
    const isMonthStart = d.getDate() === 1;
    const label = isMonthStart ? (d.getMonth()+1)+"月<br>1" : String(d.getDate());
    const cls = ["wb-dp-day"];
    if(isMonthStart) cls.push("is-month-start");
    if(dayKey === today) cls.push("is-today");
    if(dayKey === selected) cls.push("is-selected");
    return '<button type="button" class="'+cls.join(" ")+'" data-dp-pick="'+dayKey+'">'+label+'</button>';
  }).join("");
  return '<div class="wb-dp-week">'+cells+'</div>';
}

function workbenchDpResultRow(item, index){
  return '<button type="button" class="wb-dp-result'+(index === 0 ? " is-active" : "")+'" data-dp-pick="'+item.dateKey+'">'+
    '<span class="wb-dp-result-icon">'+ic("calendar")+'</span>'+
    '<span class="wb-dp-result-label">'+esc(item.label)+'</span>'+
    '<span class="wb-dp-result-weekday">'+esc(item.weekday)+'</span></button>';
}

// 弹层分「外壳」（搜索框，一次性建好，不随打字重建）和「主体」（日历 / 搜索结果，随状态换）。
// 打字每敲一下都要重渲染主体，要是连搜索框一起 innerHTML 掉，输入框会丢焦点/丢光标位置。
function workbenchDpRenderShell(){
  const el = document.getElementById("wbDatePicker");
  if(!el) return;
  el.innerHTML =
    '<div class="wb-dp-search"><input type="text" data-dp-search placeholder="搜索日期，如 9/12 或 2026年9月12日" aria-label="搜索日期"><button type="button" class="wb-dp-search-clear" data-dp-search-clear hidden aria-label="清空搜索">'+ic("close")+'</button></div>'+
    '<div data-dp-body></div>';
}

function workbenchDpRenderBody(){
  const body = document.querySelector("#wbDatePicker [data-dp-body]");
  if(!body) return;
  if(WB_DP.results.length){
    body.innerHTML = '<div class="wb-dp-results" data-dp-results>'+WB_DP.results.map(workbenchDpResultRow).join("")+'</div>';
  } else {
    const current = workbenchDpCurrentDate();
    const today = workbenchToday();
    const weeks = WB_DP.expanded
      ? workbenchDpWeekStarts(workbenchDpSundayKey(today), WB_DP.rangeBefore, WB_DP.rangeAfter)
      : workbenchDpWeekStarts(WB_DP.baseWeek, 0, 3);
    const rows = weeks.map(weekKey => workbenchDpWeekRow(weekKey, current, today)).join("");
    body.innerHTML =
      '<div class="wb-dp-actions"><button type="button" data-dp-today>'+ic("star")+'<span>今天</span></button><button type="button" data-dp-clear'+(current ? "" : " disabled")+'><span>移除</span></button></div>'+
      '<div class="wb-dp-weekdays">'+["周日","周一","周二","周三","周四","周五","周六"].map(w => '<span>'+w+'</span>').join("")+'</div>'+
      '<div class="wb-dp-grid'+(WB_DP.expanded ? " is-expanded" : "")+'" data-dp-grid>'+rows+'</div>'+
      (WB_DP.expanded ? "" : '<button type="button" class="wb-dp-expand" data-dp-expand aria-label="展开更多日期">'+ic("chevronDown")+'</button>');
  }
  if(WB_DP._reposition) WB_DP._reposition(); // 主体换了搜索结果/日历，高度跟着变，重新贴一次锚点
}

function workbenchDpEnsureEl(){
  let el = document.getElementById("wbDatePicker");
  if(el) return el;
  el = document.createElement("div");
  el.id = "wbDatePicker";
  el.className = "wb-dp";
  el.style.display = "none";
  document.body.appendChild(el);
  el.addEventListener("click", workbenchDpHandleClick);
  el.addEventListener("contextmenu", event => event.preventDefault());
  el.addEventListener("scroll", workbenchDpHandleScroll, true); // capture：滚动事件不冒泡，得在祖先上抓 capture 阶段
  el.addEventListener("input", event => {
    if(event.target.matches("[data-dp-search]")) workbenchDpHandleSearchInput(event.target);
  });
  el.addEventListener("keydown", event => {
    if(event.key !== "Enter" || !event.target.matches("[data-dp-search]")) return;
    const top = WB_DP.results[0];
    if(top) workbenchDpApply(top.dateKey);
  });
  return el;
}

function workbenchDpHandleClick(event){
  const pick = event.target.closest("[data-dp-pick]");
  if(pick){ workbenchDpApply(pick.dataset.dpPick); return; }
  if(event.target.closest("[data-dp-today]")){ workbenchDpApply(workbenchToday()); return; }
  if(event.target.closest("[data-dp-clear]")){ workbenchDpApply(null); return; }
  if(event.target.closest("[data-dp-search-clear]")){
    WB_DP.results = [];
    const input = document.querySelector("#wbDatePicker [data-dp-search]");
    if(input){ input.value = ""; input.focus(); }
    document.querySelector("#wbDatePicker [data-dp-search-clear]")?.setAttribute("hidden", "");
    workbenchDpRenderBody();
    return;
  }
  if(event.target.closest("[data-dp-expand]")){
    WB_DP.expanded = true;
    workbenchDpRenderBody();
    const grid = document.querySelector("#wbDatePicker [data-dp-grid]");
    const target = grid?.querySelector(".is-selected") || grid?.querySelector(".is-today");
    if(grid && target) grid.scrollTop = target.closest(".wb-dp-week").offsetTop - grid.clientHeight/2;
    return;
  }
}

// 边打字边出结果列表（仿 Things 搜索下拉），不用等回车；输入清空就退回日历视图。
function workbenchDpHandleSearchInput(input){
  const clearBtn = document.querySelector("#wbDatePicker [data-dp-search-clear]");
  if(clearBtn) clearBtn.toggleAttribute("hidden", !input.value);
  WB_DP.results = workbenchDpSearchResults(input.value);
  workbenchDpRenderBody();
}

// 展开态触底/触顶各自往那个方向再拉一批周；往上拉是往同一个滚动容器的顶部插入内容，
// 必须用 scrollHeight 差值把 scrollTop 顶回去，不然刚才看的那一段会往下窜。
function workbenchDpHandleScroll(event){
  const grid = event.target.closest?.("[data-dp-grid]");
  if(!grid || !WB_DP.expanded) return;
  const prevScrollTop = grid.scrollTop;
  const prevHeight = grid.scrollHeight;
  if(grid.scrollTop < 100){
    WB_DP.rangeBefore += WB_DP_STEP;
    workbenchDpRenderBody();
    const newGrid = document.querySelector("#wbDatePicker [data-dp-grid]");
    if(newGrid) newGrid.scrollTop = prevScrollTop + (newGrid.scrollHeight - prevHeight);
  }else if(grid.scrollHeight - grid.scrollTop - grid.clientHeight < 100){
    WB_DP.rangeAfter += WB_DP_STEP;
    workbenchDpRenderBody();
    const newGrid = document.querySelector("#wbDatePicker [data-dp-grid]");
    if(newGrid) newGrid.scrollTop = prevScrollTop;
  }
}

// 主体在「日历」和「搜索结果」之间切换时高度不一样，每次重渲染主体都要贴一次锚点——
// 这两个函数被存进 WB_DP._reposition，由 workbenchDpRenderBody 在每次换内容后调用。
function workbenchDpClampToAnchor(rect){
  const el = document.getElementById("wbDatePicker");
  if(!el) return;
  el.style.left = rect.left+"px"; el.style.top = (rect.bottom+6)+"px";
  requestAnimationFrame(() => {
    const w = el.getBoundingClientRect();
    let left = rect.left, top = rect.bottom+6;
    if(left + w.width > window.innerWidth - 4) left = window.innerWidth - w.width - 4;
    if(top + w.height > window.innerHeight - 4) top = rect.top - w.height - 6;
    el.style.left = Math.max(4, left)+"px"; el.style.top = Math.max(4, top)+"px";
  });
}

function workbenchDpClampToPoint(x, y){
  const el = document.getElementById("wbDatePicker");
  if(!el) return;
  requestAnimationFrame(() => {
    const rect = el.getBoundingClientRect();
    el.style.left = Math.max(4, Math.min(x, window.innerWidth - rect.width - 4))+"px";
    el.style.top = Math.max(4, Math.min(y, window.innerHeight - rect.height - 4))+"px";
  });
}

function workbenchDpOpenCommon(target){
  WB_DP.open = true; WB_DP.target = target; WB_DP.expanded = false; WB_DP.results = [];
  WB_DP.rangeBefore = 4; WB_DP.rangeAfter = 12;
  const current = workbenchDpCurrentDate();
  WB_DP.baseWeek = workbenchDpSundayKey(current || workbenchToday());
  const el = workbenchDpEnsureEl();
  el.style.display = "block";
  workbenchDpRenderShell();
  workbenchDpRenderBody();
  return el;
}

function workbenchDpOpen(target, anchorEl){
  workbenchDpOpenCommon(target);
  WB_DP._reposition = () => workbenchDpClampToAnchor(anchorEl.getBoundingClientRect());
  WB_DP._reposition();
}

function workbenchDpOpenAt(target, x, y){
  const el = workbenchDpOpenCommon(target);
  el.style.left = x+"px"; el.style.top = y+"px";
  WB_DP._reposition = () => workbenchDpClampToPoint(x, y);
  WB_DP._reposition();
}

function workbenchDpClose(){
  WB_DP.open = false; WB_DP.target = null; WB_DP._reposition = null;
  const el = document.getElementById("wbDatePicker");
  if(el) el.style.display = "none";
}

document.addEventListener("mousedown", event => {
  if(!WB_DP.open) return;
  if(event.target.closest("#wbDatePicker") || event.target.closest("[data-open-dp]")) return;
  workbenchDpClose();
});

async function openWorkbench(){
  closeCallView(); S.surface = "workbench";
  $("#chatMain").hidden = true; $("#workbenchView").hidden = false;
  closeSidebar(); renderConvs();
  if(!WB.anchor) WB.anchor = workbenchToday();
  await loadWorkbenchData();
  renderWorkbench();
}

function closeWorkbench(){ const view = $("#workbenchView"); if(view) view.hidden = true; }

// 独立窗口:走真实页面(带侧栏的完整 SPA)/workbench/window,登录后直接落地工作台内容区
// (见 app-core.js tryEnter 的 standaloneWorkbench 分支 + wb-standalone 样式)。
// 不用 "/?view=workbench":sw.js 离线缓存按 pathname 白名单 "/",这个全新 query'd URL
// 会被当成 "/" 走网络优先超时退回缓存那条特殊逻辑,缓存未命中时给 503,慢网络下独立
// 窗口会打开一片空白(见 web.py 路由注释)。/workbench/window 不在白名单里,请求原样透传。
// 不能叫 "/workbench"——那是数据接口(loadWorkbenchData 用),撞了会导致数据取不到。
function openWorkbenchStandalone(){
  window.open("/workbench/window", "_blank",
    "noopener,width=1180,height=860,menubar=no,toolbar=no,location=no,status=no");
}

$("#workbenchView").addEventListener("click", event => {
  if(event.target.closest("[data-workbench-win]")){ openWorkbenchStandalone(); return; }
  if(event.target.closest("[data-sidebar]")){ expandSidebarResponsive(); return; }
  const complete = event.target.closest("[data-complete]");
  if(complete){ toggleWorkbenchTask(complete.dataset.complete); return; }
  const toggleChildren = event.target.closest("[data-toggle-children]");
  if(toggleChildren){ toggleWorkbenchChildren(toggleChildren.dataset.toggleChildren); return; }
  const toggleAssignee = event.target.closest("[data-toggle-assignee]");
  if(toggleAssignee){ toggleWorkbenchAssignee(toggleAssignee.dataset.toggleAssignee); return; }
  const dispatch = event.target.closest("[data-dispatch]");
  if(dispatch){ dispatchWorkbenchTask(dispatch.dataset.dispatch); return; }
  const addChild = event.target.closest("[data-add-child]");
  if(addChild){ openWorkbenchNewChild(addChild.dataset.addChild); return; }
  const sessionLink = event.target.closest("[data-session]");
  if(sessionLink){ const sid = sessionLink.dataset.session; if(typeof openConv === "function") openConv("task:"+sid); return; }
  const newAssignee = event.target.closest("[data-new-assignee]");
  if(newAssignee && WB.newTask){ WB.newTask.assignee = WB.newTask.assignee === "ai" ? "human" : "ai"; renderWorkbench(); requestAnimationFrame(() => $("[data-new-title]")?.focus()); return; }
  const del = event.target.closest("[data-delete-task]");
  if(del){ deleteWorkbenchTask(del.dataset.deleteTask); return; }
  const openDp = event.target.closest("[data-open-dp]");
  if(openDp){
    const [kind, id] = openDp.dataset.openDp.split(":");
    workbenchDpOpen(kind === "task" ? {kind:"task", id} : {kind:"new"}, openDp);
    return;
  }
  const source = event.target.closest("[data-source]");
  if(source){ openWorkbenchSource(source.dataset.source, source.dataset.highlight); return; }
  const group = event.target.closest("[data-group]");
  if(group){ WB.collapsed.has(group.dataset.group) ? WB.collapsed.delete(group.dataset.group) : WB.collapsed.add(group.dataset.group); renderWorkbench(); return; }
  if(event.target.closest("[data-new-task]")){ openWorkbenchNewTask(); return; }
  if(event.target.closest("[data-add-project]")){ addWorkbenchProject(); return; }
  if(event.target.closest("[data-save-new]")){ saveWorkbenchNewTask(); return; }
  const removeImage = event.target.closest("[data-remove-image]");
  if(removeImage){
    const taskId = removeImage.dataset.imageTask;
    const task = workbenchTask(taskId);
    const idx = Number(removeImage.dataset.removeImage);
    const name = task?.images?.[idx];
    if(task && name){ task.images.splice(idx, 1); if(!workbenchMorphTask(taskId)) renderWorkbench(); removeWorkbenchImage(taskId, name); }
    return;
  }
  const img = event.target.closest(".wb-image-list img");
  if(img){ openImgViewer(img, [...img.closest(".wb-image-list").querySelectorAll("img")]); return; }
  // 新建卡片内部（标题输入框、备注、日期、项目/来源下拉）保留原生交互，不走下面的
  // 「点击空白处收起」逻辑——那些点击都已经在上面按具体 data-* 处理过或本就该正常触发。
  if(event.target.closest("[data-new-card]")) return;
  const taskRow = event.target.closest("[data-task]");
  if(taskRow){
    const taskId = taskRow.dataset.task;
    if(taskId === WB.editorTaskId) return;
    if(event.shiftKey && WB.selectAnchor && WB.selectAnchor !== taskId){
      clearTimeout(wbClickTimer); wbClickTimer = null;
      workbenchSelectRange(WB.selectAnchor, taskId);
      return;
    }
    if(WB.selected.size === 1 && WB.selected.has(taskId)){
      // 已经是唯一选中项：这一下直接打开，不用再等去分辨是不是双击
      clearTimeout(wbClickTimer); wbClickTimer = null;
      openWorkbenchEditor(taskId);
      return;
    }
    // 高亮先走，跟手；真正的选中状态（anchor/关编辑卡等）延迟到 250ms 后确认不是双击
    // 再落地——提前重渲染会替换节点，破坏双击识别。纯 class 增删不碰节点，可以先做。
    workbenchApplySelectionClasses(WB.selected, new Set([taskId]));
    // 双击的两次 click 都会走到这里：必须先清掉上一个待触发的计时器，否则会遗留一个孤儿计时器，
    // 在 dblclick 打开编辑卡之后延迟把它关掉。
    clearTimeout(wbClickTimer);
    wbClickTimer = setTimeout(() => { wbClickTimer = null; workbenchSelectSingle(taskId); }, 250);
    return;
  }
  const project = event.target.closest("[data-project]");
  if(project){ clearTimeout(wbClickTimer); WB.project = project.dataset.project; WB.newTask=null; WB.editorTaskId=null; WB.selected=new Set(); WB.selectAnchor=null; renderWorkbench(); return; }
  const view = event.target.closest("[data-view]");
  if(view){
    clearTimeout(wbClickTimer);
    WB.view = view.dataset.view;
    WB.newTask=null; WB.editorTaskId=null; WB.selected=new Set(); WB.selectAnchor=null;
    renderWorkbench();
    if(WB.view === "trash") loadWorkbenchTrash().then(() => { if(WB.view === "trash") renderWorkbench(); });
    return;
  }
  const restoreBtn = event.target.closest("[data-restore-task]");
  if(restoreBtn){ workbenchRestoreTask(restoreBtn.dataset.restoreTask); return; }
  const purgeBtn = event.target.closest("[data-purge-task]");
  if(purgeBtn){ workbenchPurgeTask(purgeBtn.dataset.purgeTask); return; }
  const nav = event.target.closest("[data-nav]");
  if(nav){ shiftWorkbenchDate(Number(nav.dataset.nav)); return; }
  if(event.target.closest("[data-today]")){ WB.anchor = workbenchToday(); renderWorkbench(); return; }
  if(WB.editorTaskId){
    const id = WB.editorTaskId;
    WB.editorTaskId = null;
    if(!workbenchMorphTask(id)) renderWorkbench();
    return;
  }
  // 点击空白处收起新建卡片：标题写了内容就当完成新建，没写就当取消——不再需要专门的叉。
  if(WB.newTask){
    if(WB.newTask.title.trim()) saveWorkbenchNewTask();
    else { WB.newTask = null; if(!workbenchRemoveNewTaskCard()) renderWorkbench(); }
  }
});

$("#workbenchView").addEventListener("contextmenu", event => {
  const taskRow = event.target.closest("[data-task]");
  if(taskRow && !taskRow.classList.contains("wb-task-card")){
    event.preventDefault();
    clearTimeout(wbClickTimer); wbClickTimer = null;
    const taskId = taskRow.dataset.task;
    const ids = WB.selected.has(taskId) && WB.selected.size > 1 ? [...WB.selected] : [taskId];
    if(ids.length === 1){ workbenchSetSelection(ids); WB.selectAnchor = taskId; }
    workbenchOpenCtxMenu(event.clientX, event.clientY, ids);
    return;
  }
  const project = event.target.closest("[data-project]");
  if(!project || project.dataset.project === "all" || project.dataset.project === WB_UNASSIGNED_ID) return;
  event.preventDefault();
  workbenchProjectMenu(project.dataset.project);
});

$("#workbenchView").addEventListener("dblclick", event => {
  if(event.target.closest("[data-complete],[data-source]")) return;
  const taskRow = event.target.closest("[data-task]");
  if(!taskRow) return;
  if(wbClickTimer){ clearTimeout(wbClickTimer); wbClickTimer = null; }
  const taskId = taskRow.dataset.task;
  if(taskId === WB.editorTaskId) return;
  openWorkbenchEditor(taskId);
});

let wbDragTaskId = null;

function workbenchClearDropIndicators(){
  document.querySelectorAll(".wb-task-drop-before,.wb-task-drop-after").forEach(el => el.classList.remove("wb-task-drop-before", "wb-task-drop-after"));
}

$("#workbenchView").addEventListener("dragstart", event => {
  const row = event.target.closest("[data-task]");
  if(!row || row.classList.contains("wb-task-card") || row.dataset.task === WB.editorTaskId){ event.preventDefault(); return; }
  wbDragTaskId = row.dataset.task;
  clearTimeout(wbClickTimer); wbClickTimer = null;
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", wbDragTaskId);
  row.classList.add("wb-task-dragging");
});

$("#workbenchView").addEventListener("dragover", event => {
  if(!wbDragTaskId) return;
  const row = event.target.closest("[data-task]");
  if(!row || row.dataset.task === wbDragTaskId) return;
  const dragged = workbenchTask(wbDragTaskId);
  const target = workbenchTask(row.dataset.task);
  if(!dragged || !target || dragged.project !== target.project) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "move";
  const before = event.clientY < row.getBoundingClientRect().top + row.getBoundingClientRect().height / 2;
  workbenchClearDropIndicators();
  row.classList.toggle("wb-task-drop-before", before);
  row.classList.toggle("wb-task-drop-after", !before);
});

$("#workbenchView").addEventListener("drop", event => {
  if(!wbDragTaskId) return;
  const row = event.target.closest("[data-task]");
  workbenchClearDropIndicators();
  if(!row || row.dataset.task === wbDragTaskId){ wbDragTaskId = null; return; }
  const taskId = wbDragTaskId; wbDragTaskId = null;
  const dragged = workbenchTask(taskId);
  const target = workbenchTask(row.dataset.task);
  if(!dragged || !target || dragged.project !== target.project) return;
  event.preventDefault();
  const before = row.classList.contains("wb-task-drop-before");
  if(workbenchReorderTask(taskId, row.dataset.task, before) && !workbenchRefreshProjectBlock(dragged.project)) renderWorkbench();
});

$("#workbenchView").addEventListener("dragend", event => {
  event.target.closest("[data-task]")?.classList.remove("wb-task-dragging");
  workbenchClearDropIndicators();
  wbDragTaskId = null;
});

$("#workbenchView").addEventListener("input", event => {
  const task = workbenchTask(event.target.dataset.editTitle || event.target.dataset.editDetail);
  if(task){
    if(event.target.dataset.editTitle) task.title = event.target.value;
    if(event.target.dataset.editDetail){ task.detail = event.target.value; workbenchAutoGrowTextarea(event.target); }
    return;
  }
  if(!WB.newTask) return;
  if("newTitle" in event.target.dataset) WB.newTask.title = event.target.value;
  if("newDetail" in event.target.dataset){ WB.newTask.detail = event.target.value; workbenchAutoGrowTextarea(event.target); }
});

// 标题/备注失焦时才落库，避免每敲一个字都打一次 API。
$("#workbenchView").addEventListener("focusout", event => {
  const titleId = event.target.dataset.editTitle;
  const detailId = event.target.dataset.editDetail;
  const taskId = titleId || detailId;
  if(!taskId) return;
  const task = workbenchTask(taskId);
  if(!task) return;
  persistWorkbenchTask(taskId, titleId ? {title:task.title} : {detail:task.detail}, null);
});

$("#workbenchView").addEventListener("change", event => {
  if(!WB.newTask) return;
  if("newProject" in event.target.dataset){ WB.newTask.project=event.target.value; renderWorkbench(); }
  if("newSource" in event.target.dataset) WB.newTask.sourceId=event.target.value;
});

$("#workbenchView").addEventListener("paste", event => {
  const taskId = event.target.dataset.editDetail;
  const files = [...(event.clipboardData?.items||[])].map(item => item.getAsFile()).filter(Boolean);
  if(!taskId || !files.some(file => file.type.startsWith("image/"))) return;
  event.preventDefault(); addWorkbenchImages(taskId, files);
});

document.addEventListener("keydown", event => {
  if($("#workbenchView").hidden) return;
  if(event.key === "Escape"){
    if(WB_DP.open){ workbenchDpClose(); return; }
    if(workbenchCtxMenuOpen()){ workbenchCloseCtxMenu(); return; }
    if(WB.newTask){ WB.newTask=null; if(!workbenchRemoveNewTaskCard()) renderWorkbench(); }
    else if(WB.editorTaskId){
      const id = WB.editorTaskId;
      WB.editorTaskId = null;
      if(!workbenchMorphTask(id)) renderWorkbench();
    }
    return;
  }
  if(event.code !== "Space" || event.ctrlKey || event.metaKey || event.altKey) return;
  if(event.target.closest("input,textarea,select,button,[contenteditable='true']")) return;
  event.preventDefault(); openWorkbenchNewTask();
});

// ── 多选右键菜单：时间 / 完成 / 移动到 / 删除 ────────────────────────────
let wbCtxIds = [];
let wbCtxMode = "root";

function workbenchCtxMenuOpen(){
  const el = document.getElementById("wbCtxMenu");
  return !!el && el.style.display !== "none";
}

function workbenchCtxMenuHtml(){
  if(wbCtxMode === "move"){
    const opts = [...WB_DATA.projects, WB_UNASSIGNED_PROJECT].map(p => '<button type="button" data-ctx-move="'+esc(p.id)+'">'+esc(p.name)+'</button>').join("");
    return opts+'<div class="wb-ctx-sep"></div><button type="button" data-ctx="back">‹ 返回</button>';
  }
  const n = wbCtxIds.length;
  return '<button type="button" data-ctx="date">时间...</button>'+
    '<button type="button" data-ctx="done">标记完成</button>'+
    '<button type="button" data-ctx="move">移动到...</button>'+
    '<div class="wb-ctx-sep"></div>'+
    '<button type="button" data-ctx="set-ai">设为 AI 任务</button>'+
    '<button type="button" data-ctx="set-human">设为人工任务</button>'+
    '<div class="wb-ctx-sep"></div>'+
    '<button type="button" class="wb-ctx-danger" data-ctx="delete">删除 '+n+' 个任务</button>';
}

function workbenchRenderCtxMenu(){
  let el = document.getElementById("wbCtxMenu");
  if(!el){
    el = document.createElement("div");
    el.id = "wbCtxMenu";
    el.className = "wb-ctx-menu";
    el.style.display = "none";
    document.body.appendChild(el);
    el.addEventListener("click", workbenchHandleCtxClick);
    el.addEventListener("contextmenu", event => event.preventDefault());
  }
  el.innerHTML = workbenchCtxMenuHtml();
  return el;
}

function workbenchOpenCtxMenu(x, y, ids){
  wbCtxIds = ids;
  wbCtxMode = "root";
  const el = workbenchRenderCtxMenu();
  el.style.display = "block";
  el.style.left = x+"px"; el.style.top = y+"px";
  requestAnimationFrame(() => {
    // 菜单靠近视口边缘时夹回来，不然会被裁掉一截。
    const rect = el.getBoundingClientRect();
    el.style.left = Math.max(4, Math.min(x, window.innerWidth - rect.width - 4))+"px";
    el.style.top = Math.max(4, Math.min(y, window.innerHeight - rect.height - 4))+"px";
  });
}

function workbenchCloseCtxMenu(){
  const el = document.getElementById("wbCtxMenu");
  if(el) el.style.display = "none";
  wbCtxIds = [];
}

function workbenchHandleCtxClick(event){
  const moveTo = event.target.closest("[data-ctx-move]");
  if(moveTo){
    workbenchBatchMove(wbCtxIds, moveTo.dataset.ctxMove);
    workbenchCloseCtxMenu();
    return;
  }
  const action = event.target.closest("[data-ctx]")?.dataset.ctx;
  if(!action) return;
  if(action === "back"){ wbCtxMode = "root"; workbenchRenderCtxMenu(); return; }
  if(action === "date"){
    const rect = document.getElementById("wbCtxMenu").getBoundingClientRect();
    const ids = [...wbCtxIds];
    workbenchCloseCtxMenu();
    workbenchDpOpenAt({kind:"ctx", ids}, rect.left, rect.top);
    return;
  }
  if(action === "move"){ wbCtxMode = "move"; workbenchRenderCtxMenu(); return; }
  if(action === "done"){ workbenchBatchComplete(wbCtxIds); workbenchCloseCtxMenu(); return; }
  if(action === "set-ai" || action === "set-human"){
    const assignee = action === "set-ai" ? "ai" : "human";
    wbCtxIds.forEach(id => { const t = workbenchTask(id); if(t){ const prev = t.assignee; t.assignee = assignee; persistWorkbenchTask(id, {assignee}, {assignee: prev}); }});
    workbenchCloseCtxMenu(); renderWorkbench(); return;
  }
  if(action === "delete"){ workbenchBatchDelete(wbCtxIds); workbenchCloseCtxMenu(); return; }
}

// 用 mousedown 而不是 click 来判定「点了菜单外面」：右键弹出菜单本身就是由一次
// mousedown+contextmenu 触发的，如果监听 click 来关闭，某些浏览器会在 contextmenu
// 后紧跟着补一个 click，把刚弹出的菜单立刻关掉——mousedown 没有这个时序问题。
document.addEventListener("mousedown", event => {
  if(!workbenchCtxMenuOpen()) return;
  if(event.target.closest("#wbCtxMenu")) return;
  workbenchCloseCtxMenu();
});

window.openWorkbench = openWorkbench;
window.closeWorkbench = closeWorkbench;
