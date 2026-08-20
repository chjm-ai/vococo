"use strict";
// 工作台：项目/来源文档/任务全部走 /workbench* 接口读写(memory/workbench.py + state.db)，
// 不再是纯前端 demo。项目支持在界面上新建/重命名/归档，不写死数量。

const WB_DATA = {projects: [], sources: [], tasks: []};
const WB = {project:"all", view:"week", anchor:null, editorTaskId:null, selectedTaskId:null, newTask:null, collapsed:new Set()};
let wbClickTimer = null;

function workbenchProject(id){ return WB_DATA.projects.find(project => project.id === id); }
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

function workbenchProjectMatches(item){ return WB.project === "all" || item.project === WB.project; }
function workbenchTasks(filter){ return WB_DATA.tasks.filter(task => workbenchProjectMatches(task) && filter(task)); }
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

function workbenchTaskRow(task){
  if(WB.editorTaskId === task.id) return renderWorkbenchTaskEditor(task);
  const action = task.status === "done" ? "恢复" : "完成";
  const selected = WB.selectedTaskId === task.id;
  const detail = task.detail ? '<p class="wb-task-detail">'+esc(task.detail)+'</p>' : "";
  return '<article class="wb-task wb-'+esc(task.status)+(selected ? " is-selected" : "")+'" data-task="'+esc(task.id)+'" draggable="true">'+
    '<button class="wb-check" type="button" draggable="false" data-complete="'+esc(task.id)+'" aria-label="'+action+'：'+esc(task.title)+'">'+(task.status === "done" ? "✓" : task.status === "block" ? "!" : "")+'</button>'+
    '<div class="wb-task-copy"><strong class="wb-task-title">'+esc(task.title)+'</strong>'+detail+'</div>'+workbenchSourceLink(task, true)+'</article>';
}

function renderWorkbenchTaskEditor(task){
  const action = task.status === "done" ? "恢复" : "完成";
  const sources = (task.sourceIds||[]).map(id => {
    const source = workbenchSource(id);
    return source ? '<button type="button" class="wb-source-link" data-source="'+esc(id)+'" data-highlight="'+esc(workbenchTaskHighlight(task))+'">'+ic("doc")+'<span>'+esc(source.label)+'</span></button>' : "";
  }).join("");
  const images = (task.images||[]).map((name, index) => '<figure><img data-full="/image?name='+encodeURIComponent(name)+'" alt="任务附件"><button type="button" data-remove-image="'+index+'" data-image-task="'+esc(task.id)+'" aria-label="移除图片">×</button></figure>').join("");
  return '<article class="wb-task wb-editor-shell wb-task-card wb-'+esc(task.status)+'" data-task="'+esc(task.id)+'">'+
    '<div class="wb-card-head">'+
      '<button class="wb-check" type="button" data-complete="'+esc(task.id)+'" aria-label="'+action+'：'+esc(task.title)+'">'+(task.status === "done" ? "✓" : task.status === "block" ? "!" : "")+'</button>'+
      '<input class="wb-card-title" data-edit-title="'+esc(task.id)+'" value="'+esc(task.title)+'" aria-label="任务标题">'+
      '<button type="button" class="wb-card-delete" data-delete-task="'+esc(task.id)+'" aria-label="删除任务">'+ic("trash")+'</button>'+
    '</div>'+
    '<textarea data-edit-detail="'+esc(task.id)+'" placeholder="备注">'+esc(task.detail||"")+'</textarea>'+
    '<div class="wb-editor-footer"><button type="button" data-schedule-today="'+esc(task.id)+'">今天</button><input type="date" data-schedule-date="'+esc(task.id)+'" value="'+esc(task.date||"")+'" aria-label="安排日期"><div class="wb-editor-sources">'+sources+'</div></div>'+
    (images ? '<div class="wb-image-list">'+images+'</div>' : "")+'</article>';
}

function workbenchNewTaskCard(project){
  if(!WB.newTask || WB.newTask.project !== project.id) return "";
  const sourceOptions = WB_DATA.sources.map(source => '<option value="'+esc(source.id)+'" '+(WB.newTask.sourceId === source.id ? "selected" : "")+'>'+esc(source.label)+'</option>').join("");
  const projectOptions = WB_DATA.projects.map(item => '<option value="'+esc(item.id)+'" '+(WB.newTask.project === item.id ? "selected" : "")+'>'+esc(item.name)+'</option>').join("");
  return '<section class="wb-editor-shell wb-new-task" data-new-card><div class="wb-editor-head"><input data-new-title placeholder="新建待办事项" value="'+esc(WB.newTask.title)+'" aria-label="任务标题"></div>'+
    '<textarea data-new-detail placeholder="备注">'+esc(WB.newTask.detail)+'</textarea>'+
    '<div class="wb-editor-footer"><select data-new-project aria-label="项目">'+projectOptions+'</select><select data-new-source aria-label="来源文档"><option value="">来源文档</option>'+sourceOptions+'</select><input type="date" data-new-date value="'+esc(WB.newTask.date||"")+'" aria-label="安排日期"><button type="button" class="wb-primary" data-save-new>添加</button></div></section>';
}

function workbenchProjectBlock(project, tasks){
  const groupId = workbenchGroupId(project);
  const collapsed = WB.collapsed.has(groupId);
  const body = tasks.length ? '<div class="wb-task-list">'+tasks.map(workbenchTaskRow).join("")+'</div>' : '<p class="wb-empty">暂无任务</p>';
  return '<section class="wb-project-block"><button type="button" class="wb-project-toggle" data-group="'+esc(groupId)+'" aria-expanded="'+(!collapsed)+'"><span class="wb-project-name"><strong>'+esc(project.name)+'</strong><i class="wb-chevron" aria-hidden="true"></i></span></button>'+
    (collapsed ? "" : body+workbenchNewTaskCard(project))+'</section>';
}

function workbenchVisibleTasks(){
  if(WB.view === "unscheduled") return workbenchTasks(task => !task.date);
  if(WB.view === "day") return workbenchTasks(task => task.date === WB.anchor);
  if(WB.view === "week") return workbenchTasks(task => task.week === workbenchWeekKey());
  return workbenchTasks(task => task.month === workbenchMonthKey());
}

function renderWorkbenchProjects(){
  const tasks = workbenchVisibleTasks();
  const projects = WB_DATA.projects.filter(workbenchProjectMatches);
  if(!projects.length) return '<p class="wb-empty">还没有项目，点右上角「+」新建一个。</p>';
  return '<div class="wb-project-list">'+projects.map(project => workbenchProjectBlock(project, tasks.filter(task => task.project === project.id))).join("")+'</div>';
}

function openWorkbenchSource(sourceId, highlight){
  const source = workbenchSource(sourceId);
  if(!source || typeof openDocPreview !== "function") return;
  openDocPreview({kind:"path", target:source.path, title:source.label, highlight});
}

function renderWorkbenchHeader(){
  const dateNav = WB.view === "unscheduled" ? "" :
    '<div class="wb-date-nav"><button type="button" data-nav="-1" aria-label="上一个周期">‹</button><strong>'+workbenchDateLabel()+'</strong><button type="button" data-nav="1" aria-label="下一个周期">›</button><button type="button" data-today>今天</button></div>';
  return '<header class="wb-toolbar"><div class="wb-title"><button class="wb-hamb" type="button" data-sidebar aria-label="打开侧边栏">'+ic("panel")+'</button><h1>工作台</h1></div>'+
    '<div class="wb-controls"><button type="button" class="wb-add-task" data-new-task>+ 新建任务</button><div class="wb-switch">'+
      ["day","week","month"].map(view => '<button class="'+(WB.view === view ? "on" : "")+'" type="button" data-view="'+view+'">'+({day:"日",week:"周",month:"月"}[view])+'</button>').join("")+
      '<button class="wb-switch-icon'+(WB.view === "unscheduled" ? " on" : "")+'" type="button" data-view="unscheduled" aria-label="未排期">'+ic("inbox")+'</button>'+
    '</div>'+dateNav+'<button type="button" class="wb-win-btn" data-workbench-win title="独立窗口" aria-label="独立窗口">'+ic("newwin")+'</button></div></header>';
}

function renderWorkbenchProjectFilter(){
  const chips = WB_DATA.projects.map(project => '<button class="'+(WB.project === project.id ? "on" : "")+'" type="button" data-project="'+esc(project.id)+'" title="右键：重命名/归档">'+esc(project.name)+'</button>').join("");
  return '<div class="wb-project-filter"><button class="'+(WB.project === "all" ? "on" : "")+'" type="button" data-project="all">全部项目</button>'+chips+
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
  root.innerHTML = renderWorkbenchHeader()+renderWorkbenchProjectFilter()+renderWorkbenchProjects();
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

function selectWorkbenchTask(taskId){
  const prevEditor = WB.editorTaskId;
  const prevSelected = WB.selectedTaskId;
  const hadNewTask = !!WB.newTask;
  WB.newTask = null;
  WB.editorTaskId = null;
  WB.selectedTaskId = taskId;
  if(hadNewTask){ renderWorkbench(); return; }
  let ok = true;
  if(prevEditor && prevEditor !== taskId) ok = workbenchMorphTask(prevEditor) && ok;
  if(prevSelected && prevSelected !== taskId && prevSelected !== prevEditor) ok = workbenchSwapTask(prevSelected) && ok;
  ok = workbenchSwapTask(taskId) && ok;
  if(!ok) renderWorkbench();
}

function openWorkbenchEditor(taskId){
  const prevEditor = WB.editorTaskId;
  const prevSelected = WB.selectedTaskId;
  const hadNewTask = !!WB.newTask;
  WB.newTask = null;
  WB.selectedTaskId = null;
  WB.editorTaskId = taskId;
  let ok = !hadNewTask;
  if(ok){
    if(prevEditor && prevEditor !== taskId) ok = workbenchMorphTask(prevEditor) && ok;
    if(prevSelected && prevSelected !== taskId && prevSelected !== prevEditor) ok = workbenchSwapTask(prevSelected) && ok;
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
  WB.selectedTaskId = null;
  WB.newTask = {project:project.id, title:"", detail:"", sourceId:"", date:WB.view === "day" ? WB.anchor : ""};
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
  const payload = {project:draft.project, title, detail:draft.detail, date, month, week, sourceIds:draft.sourceId ? [draft.sourceId] : []};
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

async function deleteWorkbenchTask(taskId){
  const task = workbenchTask(taskId);
  if(!task || !confirm('删除任务「'+task.title+'」？不可恢复。')) return;
  WB_DATA.tasks = WB_DATA.tasks.filter(t => t.id !== taskId);
  if(WB.editorTaskId === taskId) WB.editorTaskId = null;
  if(WB.selectedTaskId === taskId) WB.selectedTaskId = null;
  renderWorkbench();
  try{
    const r = await api("/workbench/tasks/delete", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id:taskId})});
    if(!r.ok) throw new Error("删除失败");
  }catch(e){ alert("删除失败，请刷新页面重试"); }
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

async function openWorkbench(){
  closeCallView(); S.surface = "workbench";
  $("#chatMain").hidden = true; $("#workbenchView").hidden = false;
  closeSidebar(); renderConvs();
  if(!WB.anchor) WB.anchor = workbenchToday();
  await loadWorkbenchData();
  renderWorkbench();
}

function closeWorkbench(){ const view = $("#workbenchView"); if(view) view.hidden = true; }

// 独立窗口:走真实页面(带侧栏的完整 SPA)加 ?view=workbench,登录后直接落地工作台、
// 不显示侧边导航(见 app-core.js tryEnter 的 standaloneWorkbench 分支 + wb-standalone 样式)。
function openWorkbenchStandalone(){
  window.open("/?view=workbench", "_blank",
    "noopener,width=1180,height=860,menubar=no,toolbar=no,location=no,status=no");
}

$("#workbenchView").addEventListener("click", event => {
  if(event.target.closest("[data-workbench-win]")){ openWorkbenchStandalone(); return; }
  if(event.target.closest("[data-sidebar]")){ expandSidebarResponsive(); return; }
  const complete = event.target.closest("[data-complete]");
  if(complete){ toggleWorkbenchTask(complete.dataset.complete); return; }
  const del = event.target.closest("[data-delete-task]");
  if(del){ deleteWorkbenchTask(del.dataset.deleteTask); return; }
  const source = event.target.closest("[data-source]");
  if(source){ openWorkbenchSource(source.dataset.source, source.dataset.highlight); return; }
  const group = event.target.closest("[data-group]");
  if(group){ WB.collapsed.has(group.dataset.group) ? WB.collapsed.delete(group.dataset.group) : WB.collapsed.add(group.dataset.group); renderWorkbench(); return; }
  if(event.target.closest("[data-new-task]")){ openWorkbenchNewTask(); return; }
  if(event.target.closest("[data-add-project]")){ addWorkbenchProject(); return; }
  if(event.target.closest("[data-save-new]")){ saveWorkbenchNewTask(); return; }
  const today = event.target.closest("[data-schedule-today]");
  if(today){ scheduleWorkbenchTask(today.dataset.scheduleToday, workbenchToday()); return; }
  const removeImage = event.target.closest("[data-remove-image]");
  if(removeImage){
    const taskId = removeImage.dataset.imageTask;
    const task = workbenchTask(taskId);
    const idx = Number(removeImage.dataset.removeImage);
    const name = task?.images?.[idx];
    if(task && name){ task.images.splice(idx, 1); if(!workbenchMorphTask(taskId)) renderWorkbench(); removeWorkbenchImage(taskId, name); }
    return;
  }
  // 新建卡片内部（标题输入框、备注、日期、项目/来源下拉）保留原生交互，不走下面的
  // 「点击空白处收起」逻辑——那些点击都已经在上面按具体 data-* 处理过或本就该正常触发。
  if(event.target.closest("[data-new-card]")) return;
  const taskRow = event.target.closest("[data-task]");
  if(taskRow){
    const taskId = taskRow.dataset.task;
    if(taskId === WB.editorTaskId) return;
    if(taskId === WB.selectedTaskId){
      // 已经是选中状态：这一下直接打开，不用再等去分辨是不是双击
      clearTimeout(wbClickTimer); wbClickTimer = null;
      openWorkbenchEditor(taskId);
      return;
    }
    // 单击延迟触发选中，留出窗口给浏览器原生 dblclick 检测（提前重渲染会替换节点，破坏双击识别）
    // 双击的两次 click 都会走到这里：必须先清掉上一个待触发的计时器，否则会遗留一个孤儿计时器，
    // 在 dblclick 打开编辑卡之后延迟把它关掉。
    clearTimeout(wbClickTimer);
    wbClickTimer = setTimeout(() => { wbClickTimer = null; selectWorkbenchTask(taskId); }, 250);
    return;
  }
  const project = event.target.closest("[data-project]");
  if(project){ clearTimeout(wbClickTimer); WB.project = project.dataset.project; WB.newTask=null; WB.editorTaskId=null; WB.selectedTaskId=null; renderWorkbench(); return; }
  const view = event.target.closest("[data-view]");
  if(view){ clearTimeout(wbClickTimer); WB.view = view.dataset.view; WB.newTask=null; WB.editorTaskId=null; WB.selectedTaskId=null; renderWorkbench(); return; }
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
  const project = event.target.closest("[data-project]");
  if(!project || project.dataset.project === "all") return;
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
  if($("#workbenchView").hidden) return;
  if(event.key === "Escape"){
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

window.openWorkbench = openWorkbench;
window.closeWorkbench = closeWorkbench;
