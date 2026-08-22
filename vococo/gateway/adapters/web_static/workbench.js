"use strict";
// 工作台：项目/任务全部走 /workbench* 接口读写(memory/workbench.py + state.db)，
// 不再是纯前端 demo。项目支持在界面上新建/重命名/归档，不写死数量。

const WB_DATA = {projects: [], tasks: []};
// 后端不校验 project_id、也没有"未分组"的概念（vococo/memory/workbench.py 里
// project_id 是 NOT NULL 但没有 FK 约束）——这里用空字符串当哨兵值，纯前端合成一个
// 「未分组」伪项目做兜底分组，不需要改后端 schema。
const WB_UNASSIGNED_ID = "";
const WB_UNASSIGNED_PROJECT = {id: WB_UNASSIGNED_ID, name: "未分组"};
const WB = {project:"all", view:"week", anchor:null, editorTaskId:null, editorDocked:false, selected:new Set(), selectAnchor:null, newTask:null, expanded:new Set(), collapsed:new Set(), editSnapshots:new Map(), multiSelectMode:false};
const WB_HISTORY_MAX = 30;
const WB_HISTORY_KEY = "vococo:workbench-history";
const WB_HISTORY = {undo:[], redo:[], busy:false};
let wbClickTimer = null;

// 「显示/隐藏所有备注」「折叠/展开所有子任务」这两个开关每个视图（日/周/月/项目/
// 未排期/日志/废弃）各记各的，不互相影响；存 localStorage，刷新页面也要记得住。
const WB_VIEW_PREFS_KEY = "vococo:workbench-view-prefs";
let WB_VIEW_PREFS = {};
try{ WB_VIEW_PREFS = JSON.parse(localStorage.getItem(WB_VIEW_PREFS_KEY) || "{}"); }catch(e){ WB_VIEW_PREFS = {}; }
function wbViewPref(view){ return WB_VIEW_PREFS[view] || (WB_VIEW_PREFS[view] = {}); }
function wbSaveViewPrefs(){ try{ localStorage.setItem(WB_VIEW_PREFS_KEY, JSON.stringify(WB_VIEW_PREFS)); }catch(e){} }
// 移动端(≤760px)原来在 CSS 里硬 display:none、月视图原来在渲染函数里硬判断
// WB.view!=="month"，都是"常年隐藏备注、点开关也没用"——这两条默认值继续保留，
// 但改成"没手动点过开关"时的初始值，用户点了开关就按开关来，不再被焊死打不开。
function wbIsMobileWidth(){ return typeof window.matchMedia === "function" && window.matchMedia("(max-width:760px)").matches; }
function wbNotesHidden(view){
  const pref = wbViewPref(view);
  return pref.hideNotes === undefined ? (view === "month" || wbIsMobileWidth()) : !!pref.hideNotes;
}
function wbAllCollapsed(view){ return !!wbViewPref(view).collapseAll; }

// 触屏没有 hover，"先选中再点一下打开"这套桌面手势摸不到反馈，容易让人觉得点了没反应。
// 复用 CSS 里已经在用的 (hover: none) 判定，触屏设备改成点一下直接打开详情。
function wbIsTouchLike(){ return typeof window.matchMedia === "function" && window.matchMedia("(hover: none)").matches; }

// iOS 等触屏浏览器只认"用户手势调用栈内同步 focus()"才弹虚拟键盘；requestAnimationFrame
// 已经跳出这个调用栈了，键盘就弹不出来（光标能看见，键盘不出现）——触屏改成立即同步聚焦，
// 桌面维持原来的下一帧再聚焦（展开动画期间同步聚焦容易带来意外滚动）。
function wbFocusSoon(selector, after){
  const run = () => {
    const el = $(selector);
    if(!el) return;
    el.focus({preventScroll:true});
    after?.(el);
    // docked 卡片（新建/触屏详情，见 #wbDock）本来就固定贴在键盘上方，不需要再滚；
    // 只有桌面原地插在列表里的卡片才需要把自己滚到键盘挡不住的地方。
    if(wbIsTouchLike() && !WB.newTask?.docked && !WB.editorDocked) wbScrollAboveKeyboard(el);
  };
  if(wbIsTouchLike()) run(); else requestAnimationFrame(run);
}

// 键盘弹出是异步动画，弹起来才会把可视区域挤小，卡片经常被新键盘盖住一截——用
// visualViewport 的 resize 事件等键盘真的弹完了再把卡片滚到可见区域，比瞎猜延时准；
// 有的机型不发这个事件，兜底再补一次超时。
function wbScrollAboveKeyboard(el){
  // 编辑卡是用户点击任务行原地展开的，已经在可视区域内，不需要滚动——
  // scrollIntoView 反而会让卡片跳动。只有新建卡（不在 .wb-task-card 里）才需要滚。
  if(el.closest(".wb-task-card")) return;
  const card = el.closest(".wb-editor-shell") || el;
  let done = false;
  const scroll = () => { if(done) return; done = true; card.scrollIntoView({block:"nearest", behavior:"smooth"}); };
  if(window.visualViewport){
    const vv = window.visualViewport;
    const onResize = () => { vv.removeEventListener("resize", onResize); scroll(); };
    vv.addEventListener("resize", onResize);
    setTimeout(() => { vv.removeEventListener("resize", onResize); scroll(); }, 400);
  }else{
    setTimeout(scroll, 300);
  }
}

function workbenchIsRealProject(id){ return WB_DATA.projects.some(project => project.id === id); }
function workbenchProject(id){ return id === WB_UNASSIGNED_ID ? WB_UNASSIGNED_PROJECT : WB_DATA.projects.find(project => project.id === id); }
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
function workbenchAllTasks(filter){ return WB_DATA.tasks.filter(task => workbenchProjectMatches(task) && filter(task)); }
function workbenchIsDateView(){ return WB.view === "day" || WB.view === "week" || WB.view === "month" || WB.view === "unscheduled"; }
function workbenchWeekIntersectsMonth(weekKey, monthKey){
  if(!weekKey) return false;
  const start = workbenchDate(weekKey);
  return Array.from({length:7}, (_, index) => {
    const date = new Date(start); date.setDate(date.getDate()+index);
    return workbenchDateKey(date).slice(0, 7) === monthKey;
  }).some(Boolean);
}

function workbenchCurrentFilter(){
  if(WB.view === "unscheduled") return task => !task.date && !task.week && !task.month;
  if(WB.view === "day") return task => task.date === WB.anchor;
  if(WB.view === "week"){ const wk = workbenchWeekKey(); return task => task.week === wk; }
  if(WB.view === "month"){ const mk = workbenchMonthKey(); return task => task.month === mk || (!task.date && workbenchWeekIntersectsMonth(task.week, mk)); }
  return null;
}
function workbenchChildren(parentId){ return WB_DATA.tasks.filter(task => task.parentId === parentId); }
function workbenchChildrenStats(parentId){ const children = workbenchChildren(parentId); return {total: children.length, done: children.filter(c => c.status === "done").length}; }
function workbenchGroupId(project){ return "project:"+WB.view+":"+project.id; }

// ── 数据加载 ────────────────────────────────────────────────────────────
// 工作台没有 SSE 推送(不像侧边栏靠 stream.js 主动 push),数据只在 openWorkbench() 打开
// 那一刻拉一次;之后切项目 chip/切日周月视图都只是本地过滤 WB_DATA,不会跟服务端对一次。
// 加个"点 Tab 时按上次拉取时间兜底刷新"——30s 内来回切不重复请求,超过才真正拉一次。
let wbDataFetchedAt = 0;
const WB_REFRESH_STALE_MS = 30000;
async function loadWorkbenchData(){
  try{
    const r = await api("/workbench");
    const d = await r.json();
    WB_DATA.projects = d.projects || [];
    WB_DATA.tasks = d.tasks || [];
    wbDataFetchedAt = Date.now();   // 失败时不更新,下次点击立刻重试而不是白等 30s
  }catch(e){}
}
function refreshWorkbenchDataIfStale(){
  if(Date.now() - wbDataFetchedAt < WB_REFRESH_STALE_MS) return;
  loadWorkbenchData().then(renderWorkbench);
}

// ── 撤销 / 重做 ───────────────────────────────────────────────────────────
// 历史只保存在当前浏览器会话的 sessionStorage；不改后端 schema，最多保留 30 步。
function workbenchHistoryClone(value){ return JSON.parse(JSON.stringify(value)); }

function workbenchHistorySave(){
  try{ sessionStorage.setItem(WB_HISTORY_KEY, JSON.stringify({undo:WB_HISTORY.undo, redo:WB_HISTORY.redo})); }catch(e){}
}

function workbenchHistoryLoad(){
  try{
    const saved = JSON.parse(sessionStorage.getItem(WB_HISTORY_KEY)||"{}");
    if(Array.isArray(saved.undo)) WB_HISTORY.undo = saved.undo.slice(-WB_HISTORY_MAX);
    if(Array.isArray(saved.redo)) WB_HISTORY.redo = saved.redo.slice(-WB_HISTORY_MAX);
  }catch(e){}
}

function workbenchRemember(change){
  if(WB_HISTORY.busy) return;
  WB_HISTORY.undo.push(change);
  if(WB_HISTORY.undo.length > WB_HISTORY_MAX) WB_HISTORY.undo.shift();
  WB_HISTORY.redo = [];
  workbenchHistorySave();
}

function workbenchRememberTaskPatch(taskId, before, after){
  if(JSON.stringify(before) === JSON.stringify(after)) return;
  workbenchRemember({type:"task-patch", before:[{id:taskId, patch:before}], after:[{id:taskId, patch:after}]});
}

async function workbenchHistoryRequest(path, payload){
  const r = await api(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
  const d = await r.json();
  if(!r.ok || d.error) throw new Error(d.error||"同步失败");
  return d;
}

async function workbenchHistoryApplyTaskPatches(patches){
  const before = patches.map(({id, patch}) => ({id, patch:workbenchHistoryClone(workbenchTask(id)||{})}));
  patches.forEach(({id, patch}) => { const task = workbenchTask(id); if(task) Object.assign(task, patch); });
  renderWorkbench();
  try{
    for(const {id, patch} of patches) await workbenchHistoryRequest("/workbench/tasks/update", Object.assign({id}, patch));
  }catch(e){
    before.forEach(({id, patch}) => { const task = workbenchTask(id); if(task) Object.assign(task, patch); });
    renderWorkbench();
    throw e;
  }
}

async function workbenchHistorySetTaskPresence(target, other){
  const shouldExist = target.length > 0;
  const items = shouldExist ? target : other;
  if(!items.length) return;
  const original = workbenchHistoryClone(WB_DATA.tasks);
  if(shouldExist){
    items.slice().sort((a, b) => a.index - b.index).forEach(({task, index}) => {
      if(!workbenchTask(task.id)) WB_DATA.tasks.splice(Math.min(index, WB_DATA.tasks.length), 0, workbenchHistoryClone(task));
    });
  }else{
    const ids = new Set(items.map(({task}) => task.id));
    WB_DATA.tasks = WB_DATA.tasks.filter(task => !ids.has(task.id));
  }
  wbResetSelection(); WB.editorTaskId = null;
  renderWorkbench();
  try{
    if(shouldExist){
      for(const {task} of items){
        const restored = await workbenchHistoryRequest("/workbench/tasks/restore", {id:task.id});
        const local = workbenchTask(task.id);
        if(local) Object.assign(local, restored.task);
      }
    }else{
      for(const {task} of items) await workbenchHistoryRequest("/workbench/tasks/delete", {id:task.id});
    }
  }catch(e){
    WB_DATA.tasks = original;
    renderWorkbench();
    throw e;
  }
}

async function workbenchApplyHistory(entry, direction){
  const target = entry[direction];
  if(entry.type === "task-patch") return workbenchHistoryApplyTaskPatches(target);
  if(entry.type === "task-presence") return workbenchHistorySetTaskPresence(target, entry[direction === "before" ? "after" : "before"]);
}

async function workbenchMoveHistory(from, to, direction){
  if(WB_HISTORY.busy) return;
  const entry = from.at(-1);
  if(!entry) return;
  WB_HISTORY.busy = true;
  try{
    await workbenchApplyHistory(entry, direction);
    from.pop(); to.push(entry);
    if(to.length > WB_HISTORY_MAX) to.shift();
    workbenchHistorySave();
  }catch(e){ alert((direction === "before" ? "撤销" : "重做")+"失败："+(e.message||"")); }
  finally{ WB_HISTORY.busy = false; }
}

function workbenchUndo(){ return workbenchMoveHistory(WB_HISTORY.undo, WB_HISTORY.redo, "before"); }
function workbenchRedo(){ return workbenchMoveHistory(WB_HISTORY.redo, WB_HISTORY.undo, "after"); }

workbenchHistoryLoad();

// 乐观更新已经改完本地字段并重渲染后调用：失败时按 rollback 把字段改回去再重渲染一次。
async function persistWorkbenchTask(taskId, patch, rollback){
  try{
    const r = await api("/workbench/tasks/update", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(Object.assign({id:taskId}, patch))});
    const d = await r.json();
    if(!r.ok || d.error) throw new Error(d.error||"更新失败");
    return true;
  }catch(e){
    if(rollback){ const task = workbenchTask(taskId); if(task){ Object.assign(task, rollback); if(!workbenchSwapTask(taskId)) renderWorkbench(); } }
    alert("工作台同步失败："+(e.message||""));
    return false;
  }
}

function hydrateWorkbenchImages(){
  document.querySelectorAll("#wbContent .wb-image-list img[data-full]").forEach(im => loadAuthedImg(im, im.dataset.full, true));
}

function workbenchPersistTaskChange(taskId, before, after){
  return persistWorkbenchTask(taskId, after, before).then(ok => {
    if(ok) workbenchRememberTaskPatch(taskId, before, after);
    return ok;
  });
}

function workbenchScheduleLabel(task){
  if(task.date){
    if(task.date === workbenchToday()) return "今天";
    const date = workbenchDate(task.date);
    const now = new Date();
    return (date.getFullYear() !== now.getFullYear() ? date.getFullYear()+"年" : "")+(date.getMonth()+1)+"月"+date.getDate()+"日";
  }
  if(task.week){
    if(task.week === workbenchWeekKey(workbenchDate(workbenchToday()))) return "本周";
    const date = workbenchDate(task.week);
    return (date.getMonth()+1)+"月"+date.getDate()+"日当周";
  }
  if(task.month){
    if(task.month === workbenchToday().slice(0, 7)) return "本月";
    const [year, month] = task.month.split("-");
    return (year !== String(new Date().getFullYear()) ? year+"年" : "")+Number(month)+"月";
  }
  return "";
}

function workbenchScheduleBadge(task){
  const label = workbenchScheduleLabel(task);
  if(!label) return '';
  return '<button type="button" class="wb-schedule" data-open-dp="task:'+esc(task.id)+'" title="调整日期">'+esc(label)+'</button>';
}

function workbenchShouldAutoExpandChildren(parentId){
  const parent = workbenchTask(parentId);
  const dateFilter = workbenchCurrentFilter();
  return !!(parent && dateFilter && dateFilter(parent) && workbenchChildren(parentId).some(dateFilter));
}

function workbenchChildrenExpanded(parentId){
  // 手动展开/折叠过某个具体任务（点它自己的子任务计数徽标）优先级最高，
  // 盖过「折叠/展开所有子任务」这个视图级别的默认值。
  if(WB.expanded.has(parentId)) return true;
  if(WB.collapsed.has(parentId)) return false;
  if(wbAllCollapsed(WB.view)) return false;
  if(WB.view === "project") return true;
  return workbenchShouldAutoExpandChildren(parentId);
}

function workbenchAssigneeBadge(task){
  if(task.assignee === "ai") return '<span class="wb-assignee wb-assignee-ai" title="AI 执行">'+ic("bot")+'</span>';
  return '<span class="wb-assignee wb-assignee-human" title="人工执行">'+ic("person")+'</span>';
}

function workbenchChildToggle(task){
  const children = workbenchChildren(task.id);
  if(!children.length) return '';
  const expanded = workbenchChildrenExpanded(task.id);
  return '<button type="button" class="wb-child-toggle'+(expanded ? ' is-expanded' : '')+'" data-toggle-children="'+esc(task.id)+'" title="'+(expanded ? "收起子任务" : "展开子任务")+'">'+ic(expanded ? "chevronUp" : "chevronDown")+'</button>';
}

function workbenchParentBadge(task, isChild){
  if(!task.parentId || isChild) return '';
  const parent = workbenchTask(task.parentId);
  if(!parent) return '';
  const label = "父任务："+parent.title;
  return '<span class="wb-parent-badge" title="'+esc(label)+'" aria-label="'+esc(label)+'">'+ic("branch")+'<span>'+esc(parent.title)+'</span></span>';
}

function workbenchChildRows(parentId){
  if(!workbenchChildrenExpanded(parentId)) return '';
  const children = workbenchChildren(parentId);
  const newCard = (WB.newTask && WB.newTask.parentId === parentId && !WB.newTask.siblingTaskId) ? workbenchNewChildCard(parentId) : '';
  const rows = children.map(child => workbenchTaskRow(child, true)+(WB.newTask?.siblingTaskId === child.id ? workbenchNewChildCard(parentId) : '')).join('');
  if(!rows && !newCard) return '';
  return '<div class="wb-children" data-parent="'+esc(parentId)+'">'+rows+newCard+'</div>';
}

function workbenchAssigneeSwitch(isAi, taskId){
  const attr = taskId ? ' data-toggle-assignee="'+esc(taskId)+'"' : ' data-new-assignee';
  return '<div class="wb-assignee-switch" role="group" aria-label="执行者">'+
    '<button type="button" class="wb-assignee-opt'+(isAi ? "" : " is-on")+'"'+attr+' data-assignee-set="human" title="人工" aria-label="人工">'+ic("person")+'</button>'+
    '<button type="button" class="wb-assignee-opt'+(isAi ? " is-on" : "")+'"'+attr+' data-assignee-set="ai" title="AI" aria-label="AI">'+ic("bot")+'</button>'+
    '</div>';
}

function workbenchNewChildCardHtml(parentId, standalone){
  if(!WB.newTask || WB.newTask.parentId !== parentId) return '';
  const isAi = WB.newTask.assignee === "ai";
  const assigneeBtn = workbenchAssigneeSwitch(isAi);
  return '<section class="wb-editor-shell wb-new-task'+(standalone ? "" : " wb-new-child")+'" data-new-card><div class="wb-editor-head"><input data-new-title placeholder="新建子任务" value="'+esc(WB.newTask.title)+'" aria-label="子任务标题"></div>'+
    '<textarea data-new-detail placeholder="'+(isAi ? "Prompt" : "备注")+'">'+esc(WB.newTask.detail)+'</textarea>'+
    '<div class="wb-editor-footer">'+assigneeBtn+'<button type="button" class="wb-primary" data-save-new>添加</button></div></section>';
}

// docked 模式（移动端 FAB 拖拽建子任务）不在原地渲染，卡片改在 #wbDock 里出（见 renderWbDock），
// 避免被列表滚动位置和导航栏挡住。
function workbenchNewChildCard(parentId, standalone){
  if(WB.newTask?.docked) return '';
  return workbenchNewChildCardHtml(parentId, standalone);
}

function workbenchSessionLinks(task){
  const ids = task.sessionIds || [];
  if(!ids.length) return '';
  return '<div class="wb-session-links">'+ids.map((sid, i) => '<a class="wb-session-link" data-session="'+esc(sid)+'" title="查看会话">会话'+(i+1)+'</a>').join('')+'</div>';
}

function workbenchTaskRow(task, isChild){
  // docked（触屏）时详情卡不占列表位置，改在 #wbDock 里编辑（见 renderWbDock）——
  // 这里就当没打开编辑器，正常渲染这一行，跟贴键盘的浮层对不上位的问题天然不存在。
  if(WB.editorTaskId === task.id && !WB.editorDocked){
    const editor = renderWorkbenchTaskEditor(task);
    return editor + workbenchChildRows(task.id);
  }
  const action = (task.status === "done" || task.status === "cancelled") ? "恢复" : "完成";
  const selected = WB.selected.has(task.id);
  const detail = (task.detail && !wbNotesHidden(WB.view)) ? '<p class="wb-task-detail">'+esc(task.detail)+'</p>' : "";
  const childClass = isChild ? " wb-task-child" : "";
  const touch = wbIsTouchLike();
  const inner = '<button class="wb-check" type="button" draggable="false" data-complete="'+esc(task.id)+'" aria-label="'+action+'：'+esc(task.title)+'">'+(task.status === "done" ? "✓" : task.status === "cancelled" ? "×" : task.status === "block" ? "!" : "")+'</button>'+
    '<div class="wb-task-copy"><div class="wb-task-title-row"><strong class="wb-task-title">'+esc(task.title)+'</strong>'+workbenchParentBadge(task, isChild)+'</div>'+detail+'</div>'+
    '<div class="wb-task-end">'+workbenchScheduleBadge(task)+(workbenchChildren(task.id).length ? workbenchChildToggle(task) : workbenchAssigneeBadge(task))+'</div>';
  // 多选模式下行尾常驻一个圆形选中指示（仿 Things：空心=未选，实心+勾=已选）；
  // 平时用 CSS 隐藏，不需要因为进/出多选模式而整段重渲染——is-selected 类已经在维护了。
  const selectCircle = touch ? '<div class="wb-select-circle" aria-hidden="true">'+ic("save")+'</div>' : "";
  const row = '<article class="wb-task wb-'+esc(task.status)+(selected ? " is-selected" : "")+(touch ? " wb-swipeable" : "")+childClass+'" data-task="'+esc(task.id)+'" draggable="true">'+
    (touch ? '<div class="wb-swipe-select" aria-hidden="true">'+ic("save")+'</div><div class="wb-swipe-body">'+inner+selectCircle+'</div><div class="wb-swipe-actions"><button type="button" class="wb-swipe-btn wb-swipe-date" data-swipe-dp="'+esc(task.id)+'" aria-label="日期">'+ic("calendar")+'</button><button type="button" class="wb-swipe-btn wb-swipe-move" data-swipe-move="'+esc(task.id)+'" aria-label="移动分组">'+ic("folder")+'</button><button type="button" class="wb-swipe-btn wb-swipe-del" data-swipe-del="'+esc(task.id)+'" aria-label="删除">'+ic("trash")+'</button></div>' : inner)+
    '</article>';
  if(isChild) return row + workbenchChildRows(task.id);
  if(task.parentId) return row + workbenchChildRows(task.id) + (WB.newTask?.siblingTaskId === task.id ? workbenchNewChildCard(task.parentId, true) : '');
  return row + workbenchChildRows(task.id);
}

function renderWorkbenchTaskEditor(task){
  const action = (task.status === "done" || task.status === "cancelled") ? "恢复" : "完成";
  const images = (task.images||[]).map((name, index) => '<figure><img data-full="/image?name='+encodeURIComponent(name)+'" alt="任务附件"><button type="button" data-remove-image="'+index+'" data-image-task="'+esc(task.id)+'" aria-label="移除图片">×</button></figure>').join("");
  const isAi = task.assignee === "ai";
  const assigneeBtn = workbenchAssigneeSwitch(isAi, task.id);
  const dispatchBtn = isAi ? '<button type="button" class="wb-dispatch-btn" data-dispatch="'+esc(task.id)+'" title="让 AI 执行此任务">▶ 执行</button>' : "";
  const addChildBtn = !task.parentId ? '<button type="button" class="wb-add-child" data-add-child="'+esc(task.id)+'">'+ic("plus")+'<span>添加子任务</span></button>' : '';
  const sessionLinks = workbenchSessionLinks(task);
  const parentLink = task.parentId ? (function(){ const p = workbenchTask(task.parentId); const label = p ? "查看父任务："+p.title : "查看父任务"; return '<button type="button" class="wb-parent-link" data-goto-parent="'+esc(task.parentId)+'" title="'+esc(label)+'" aria-label="'+esc(label)+'">'+ic("branch")+(p ? '<span>'+esc(p.title)+'</span>' : '')+'</button>'; })() : '';
  const scheduleLabel = workbenchScheduleLabel(task) || "设定日期";
  return '<article class="wb-task wb-editor-shell wb-task-card wb-'+esc(task.status)+'" data-task="'+esc(task.id)+'">'+
    parentLink+
    '<div class="wb-card-head">'+
      '<button class="wb-check" type="button" data-complete="'+esc(task.id)+'" aria-label="'+action+'：'+esc(task.title)+'">'+(task.status === "done" ? "✓" : task.status === "cancelled" ? "×" : task.status === "block" ? "!" : "")+'</button>'+
      '<input class="wb-card-title" data-edit-title="'+esc(task.id)+'" value="'+esc(task.title)+'" aria-label="任务标题">'+
    '</div>'+
    '<textarea data-edit-detail="'+esc(task.id)+'" placeholder="'+(isAi ? "Prompt（AI 执行时的指令）" : "备注（思路/要点）")+'">'+esc(task.detail||"")+'</textarea>'+
    (images ? '<div class="wb-image-list">'+images+'</div>' : "")+
    sessionLinks+
    '<div class="wb-editor-footer">'+assigneeBtn+'<button type="button" class="wb-dp-trigger'+(workbenchScheduleLabel(task) ? " has-date" : "")+'" data-open-dp="task:'+esc(task.id)+'">'+ic("calendar")+'<span>'+esc(scheduleLabel)+'</span></button>'+addChildBtn+dispatchBtn+'</div>'+'</article>';
}

function workbenchNewTaskCardHtml(project){
  if(!WB.newTask || WB.newTask.project !== project.id) return "";
  const projectLabel = (workbenchProject(WB.newTask.project) || WB_UNASSIGNED_PROJECT).name;
  const isAi = WB.newTask.assignee === "ai";
  const assigneeBtn = workbenchAssigneeSwitch(isAi);
  const scheduleLabel = workbenchScheduleLabel(WB.newTask) || "设定日期";
  return '<section class="wb-editor-shell wb-new-task" data-new-card><div class="wb-editor-head"><input data-new-title placeholder="新建待办事项" value="'+esc(WB.newTask.title)+'" aria-label="任务标题"></div>'+
    '<textarea data-new-detail placeholder="'+(isAi ? "Prompt（AI 执行时的指令）" : "备注")+'">'+esc(WB.newTask.detail)+'</textarea>'+
    '<div class="wb-editor-footer">'+assigneeBtn+'<button type="button" class="wb-dp-trigger" data-open-project-picker aria-label="选择项目">'+ic("folder")+'<span>'+esc(projectLabel)+'</span></button><button type="button" class="wb-dp-trigger'+(workbenchScheduleLabel(WB.newTask) ? " has-date" : "")+'" data-open-dp="new">'+ic("calendar")+'<span>'+esc(scheduleLabel)+'</span></button><button type="button" class="wb-primary" data-save-new>添加</button></div></section>';
}

// docked 模式（移动端 FAB 点顶/拖拽新建）不在原地渲染，卡片改在 #wbDock 里出（见 renderWbDock），
// 避免插入点被滚动位置和底部导航栏挡住——面板固定贴在键盘上方，完成/点空白才真正插入列表。
function workbenchNewTaskCard(project){
  if(WB.newTask?.docked) return "";
  return workbenchNewTaskCardHtml(project);
}

// 「详情」= 在项目 tab 里已经选定某个具体项目（点分组标题跳转过来，或用二级筛选 chip
// 选的）——这时分组标题是多余的（当前就在这个项目里，点了也是原地不动），改成只显示
// 任务列表，底部露一个弱化的「删除分组」入口。「未分组」是伪项目，没有删除的意义。
function workbenchProjectBlock(project, tasks){
  const groupId = workbenchGroupId(project);
  const isDetail = WB.view === "project" && WB.project !== "all";
  const newCard = (!WB.newTask || !WB.newTask.parentId) ? workbenchNewTaskCard(project) : "";
  // 移动端 FAB 新建可以指定插到某个已有任务前面（点顶部/拖拽定位），不然新建卡固定长在列表最底下。
  const beforeId = newCard && WB.newTask.beforeTaskId;
  const rows = (beforeId && tasks.some(t => t.id === beforeId))
    ? tasks.map(t => (t.id === beforeId ? newCard : "")+workbenchTaskRow(t)).join("")
    : tasks.map(t => workbenchTaskRow(t)).join("") + newCard;
  const body = rows ? '<div class="wb-task-list">'+rows+'</div>' : '<p class="wb-empty">暂无任务</p>';
  const header = isDetail ? "" : '<button type="button" class="wb-project-toggle" data-goto-project="'+esc(project.id)+'" title="查看「'+esc(project.name)+'」项目"><i class="wb-project-icon" aria-hidden="true">'+ic("folder")+'</i><span class="wb-project-name"><strong>'+esc(project.name)+'</strong><i class="wb-chevron" aria-hidden="true"></i></span></button>';
  const deleteBtn = (isDetail && project.id !== WB_UNASSIGNED_ID) ? '<button type="button" class="wb-project-delete" data-delete-project="'+esc(project.id)+'">删除分组</button>' : "";
  return '<section class="wb-project-block" data-group="'+esc(groupId)+'">'+header+body+deleteBtn+'</section>';
}

// 已完成的任务只在「已完成」tab 里看（按天分组），unscheduled/day/week/project 这几个
// 视图一律不显示——跟原来"done 任务原地打勾变灰"的行为不一样，切完成状态要连带触发整块
// 重渲染（见 toggleWorkbenchTask/workbenchBatchComplete），不能再用原地 swap 节点那条快路径。
function workbenchHasVisibleAncestor(task, visibleIds){
  const seen = new Set();
  let parentId = task.parentId;
  while(parentId && !seen.has(parentId)){
    if(visibleIds.has(parentId)) return true;
    seen.add(parentId);
    parentId = workbenchTask(parentId)?.parentId;
  }
  return false;
}

function workbenchIsLogged(task){ return task.status === "done" || task.status === "cancelled"; }

function workbenchVisibleTasks(){
  const dateFilter = workbenchCurrentFilter();
  if(!dateFilter) return workbenchTasks(task => !workbenchIsLogged(task));
  const hideDone = WB.view !== "month";
  const combined = hideDone ? (task => dateFilter(task) && !workbenchIsLogged(task)) : dateFilter;
  const all = workbenchAllTasks(combined);
  const visibleIds = new Set(all.map(task => task.id));
  return all.filter(task => !workbenchHasVisibleAncestor(task, visibleIds));
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
    projects = [...WB_DATA.projects, WB_UNASSIGNED_PROJECT];
  }else{
    const one = workbenchProject(WB.project);
    projects = one ? [one] : [];
  }
  if(!projects.length) return '<p class="wb-empty">还没有项目，点右上角「+」新建一个。</p>';
  // 「全部项目」概览下，没任务的分组（含「未分组」）直接不显示（避免一屏全是"暂无任务"的空壳）；
  // 但正在这个项目里写新任务草稿时不能藏——不然卡片会凭空消失。单选某个具体项目时
  // 永远显示该项目自己的区块，哪怕是空的，不然用户点进去会看到一片空白，无处新建。
  const blocks = projects.map(project => {
    const list = project.id === WB_UNASSIGNED_ID ? unassignedTasks : tasks.filter(task => task.project === project.id);
    const hasDraft = WB.newTask && WB.newTask.project === project.id;
    if(WB.project === "all" && !list.length && !hasDraft) return "";
    return workbenchProjectBlock(project, list);
  }).filter(Boolean);
  if(!blocks.length) return '<p class="wb-empty">暂无任务。</p>';
  return '<div class="wb-project-list">'+blocks.join("")+'</div>';
}

// 一级导航共 7 个并列 tab，互斥选中：inbox / 日 / 周 / 月 / 项目 / 已完成 / 回收站。
// 日期切换器只在日/周/月下出现；项目筛选（全部项目/项目A/B/C/+新建）只在「项目」tab 下
// 作为二级区域出现（见 renderWorkbenchBody）；已完成/回收站/inbox 三个不跟时间也不跟
// 项目挂钩，各自按自己的规则显示，没有二级区域。
function workbenchTabHtml(view, label, opts){
  opts = opts || {};
  const cls = [];
  if(opts.icon) cls.push("wb-switch-icon");
  if(WB.view === view) cls.push("on");
  const aria = opts.aria ? ' aria-label="'+opts.aria+'"' : "";
  return '<button class="'+cls.join(" ")+'" type="button" data-view="'+view+'"'+aria+'>'+label+'</button>';
}

// 标题行(工作台标题 + 独立窗口按钮放最右)单独一行；tab 行是两段式分段选择器（参考
// 侧边栏 .sidetabs 的视觉语言）——第一段"日周月"是时间维度，第二段"项目/未排期/
// 已完成/回收站"是跟时间无关的独立列表。两段共用同一个 WB.view 状态，靠
// workbenchTabHtml 每次渲染时用 WB.view === view 现算 .on class，天然互斥，不用
// 额外写"选了这段就清那段"的逻辑。日期切换/项目筛选作为「第二行」跟在 header 后面
// (见 renderWorkbenchSecondRow + renderWorkbenchBody)。
function renderWorkbenchHeader(){
  // 高亮(is-on)代表"内容正显示/展开中"，不是"隐藏/折叠模式已启用"——
  // 不然点开的东西看着反而是灰的、没点开的东西看着是亮的，反直觉。
  const notesOn = !wbNotesHidden(WB.view);
  const collapseOn = !wbAllCollapsed(WB.view);
  return '<header class="wb-toolbar">'+
    '<div class="wb-title-row"><div class="wb-title"><button class="wb-hamb" type="button" data-sidebar aria-label="打开侧边栏">'+ic("panel")+'</button><h1>工作台</h1></div>'+
      '<div class="wb-title-actions">'+
      '<button type="button" class="wb-win-btn'+(notesOn ? " is-on" : "")+'" data-toggle-notes title="'+(notesOn ? "隐藏所有备注" : "显示所有备注")+'" aria-label="显示/隐藏所有备注" aria-pressed="'+notesOn+'">'+ic("doc")+'</button>'+
      '<button type="button" class="wb-win-btn'+(collapseOn ? " is-on" : "")+'" data-toggle-collapse-all title="'+(collapseOn ? "折叠所有子任务" : "展开所有子任务")+'" aria-label="折叠/展开所有子任务" aria-pressed="'+collapseOn+'">'+ic("branch")+'</button>'+
      '<button type="button" class="wb-win-btn" data-workbench-win title="独立窗口" aria-label="独立窗口">'+ic("newwin")+'</button>'+
      '</div></div>'+
    '<div class="wb-switch-row">'+
      '<div class="wb-switch">'+
        workbenchTabHtml("day", "日")+
        workbenchTabHtml("week", "周")+
        workbenchTabHtml("month", "月")+
      '</div>'+
      '<div class="wb-switch">'+
        workbenchTabHtml("project", "项目")+
        workbenchTabHtml("unscheduled", "未排期")+
        workbenchTabHtml("completed", "日志")+
        workbenchTabHtml("trash", "废弃")+
      '</div>'+
    '</div></header>';
}

// tab 行下面的第二行：日/周/月是日期切换器，项目是二级筛选 chip，其余视图没有第二行。
function renderWorkbenchSecondRow(){
  if(WB.view === "day" || WB.view === "week" || WB.view === "month"){
    return '<div class="wb-date-nav"><button type="button" data-nav="-1" aria-label="上一个周期">‹</button><strong>'+workbenchDateLabel()+'</strong><button type="button" data-nav="1" aria-label="下一个周期">›</button><button type="button" data-today>今天</button></div>';
  }
  if(WB.view === "project") return renderWorkbenchProjectFilter();
  return "";
}

const WB_TRASH = {tasks: [], loaded: false, fetchedAt: 0, expandedId: null};

async function loadWorkbenchTrash(){
  WB_TRASH.expandedId = null;
  try{
    const r = await api("/workbench/trash");
    const d = await r.json();
    WB_TRASH.tasks = d.tasks || [];
    WB_TRASH.fetchedAt = Date.now();   // 失败时不更新,下次进回收站立刻重试
  }catch(e){}
  WB_TRASH.loaded = true;
}
// 原来每次点「回收站」都无条件重拉,来回切 Tab 会重复请求同一份数据;跟 refreshWorkbenchDataIfStale
// 一样按 30s 节流,首次进入(fetchedAt=0)必定超过阈值,不影响"第一次进去要拉数据"。
function refreshWorkbenchTrashIfStale(){
  if(Date.now() - WB_TRASH.fetchedAt < WB_REFRESH_STALE_MS) return;
  loadWorkbenchTrash().then(() => { if(WB.view === "trash") renderWorkbench(); });
}

// 点行本身展开/收起详情（标题/备注/图片/来源，只读——回收站里的任务不提供编辑，
// 要改就先恢复）；点"恢复"/"彻底删除"两个按钮走各自的 data-* 处理，不会触发展开。
function workbenchTrashRow(task){
  const actions = '<div class="wb-trash-actions"><button type="button" data-restore-task="'+esc(task.id)+'">恢复</button><button type="button" class="wb-ctx-danger" data-purge-task="'+esc(task.id)+'">彻底删除</button></div>';
  if(WB_TRASH.expandedId !== task.id){
    return '<article class="wb-task wb-trash-row" data-trash-task="'+esc(task.id)+'">'+
      '<div class="wb-task-copy"><strong class="wb-task-title">'+esc(task.title)+'</strong></div>'+actions+
      '</article>';
  }
  const detail = task.detail ? '<p class="wb-trash-detail">'+esc(task.detail)+'</p>' : '<p class="wb-empty">无备注</p>';
  const images = (task.images||[]).map(name => '<figure><img data-full="/image?name='+encodeURIComponent(name)+'" alt="任务附件"></figure>').join("");
  return '<article class="wb-task wb-trash-row wb-trash-row-expanded" data-trash-task="'+esc(task.id)+'">'+
    '<div class="wb-card-head"><strong class="wb-task-title">'+esc(task.title)+'</strong>'+actions+'</div>'+
    detail+
    (images ? '<div class="wb-image-list">'+images+'</div>' : "")+
    '</article>';
}

function renderWorkbenchTrash(){
  if(!WB_TRASH.loaded) return '<p class="wb-empty">加载中…</p>';
  const toolbar = WB_TRASH.tasks.length ? '<div class="wb-trash-toolbar"><button type="button" class="wb-ctx-danger" data-empty-trash>清空废弃</button></div>' : "";
  if(!WB_TRASH.tasks.length) return '<p class="wb-empty">废弃是空的。</p>';
  return toolbar+'<div class="wb-task-list wb-trash-list">'+WB_TRASH.tasks.map(workbenchTrashRow).join("")+'</div>';
}

async function workbenchEmptyTrash(){
  if(!WB_TRASH.tasks.length) return;
  if(!confirm("清空废弃？"+WB_TRASH.tasks.length+" 个任务将被永久删除，不可恢复。")) return;
  const prev = WB_TRASH.tasks;
  WB_TRASH.tasks = [];
  renderWorkbench();
  try{
    const r = await api("/workbench/trash/empty", {method:"POST"});
    if(!r.ok) throw new Error("清空失败");
  }catch(e){ WB_TRASH.tasks = prev; renderWorkbench(); alert("清空废弃失败："+(e.message||"")); }
}

// ── 已完成：跟时间/项目无关，按「完成当天」分组，最近完成的排最前 ──────────────
function workbenchLocalDateKey(date){
  return date.getFullYear()+"-"+String(date.getMonth()+1).padStart(2, "0")+"-"+String(date.getDate()).padStart(2, "0");
}

function workbenchCompletedDayKey(task){
  const ts = task.completedAt || task.updatedAt; // 老数据没有 completedAt，退化用最近一次更新时间
  return ts ? workbenchLocalDateKey(new Date(ts * 1000)) : null;
}

function workbenchCompletedGroupLabel(dateKey){
  if(dateKey === workbenchToday()) return "今天";
  const d = workbenchDate(dateKey);
  return (d.getMonth()+1)+"月"+d.getDate()+"日 · 周"+"日一二三四五六"[d.getDay()];
}

function renderWorkbenchCompleted(){
  const tasks = WB_DATA.tasks.filter(workbenchIsLogged);
  if(!tasks.length) return '<p class="wb-empty">还没有日志。</p>';
  const groups = new Map();
  tasks.forEach(task => {
    const key = workbenchCompletedDayKey(task) || "unknown";
    (groups.get(key) || groups.set(key, []).get(key)).push(task);
  });
  const keys = [...groups.keys()].sort((a, b) => a === "unknown" ? 1 : b === "unknown" ? -1 : (a < b ? 1 : -1));
  return '<div class="wb-completed-list">'+keys.map(key => {
    const label = key === "unknown" ? "未知日期" : workbenchCompletedGroupLabel(key);
    const rows = groups.get(key).map(workbenchTaskRow).join("");
    return '<section class="wb-completed-group"><h3 class="wb-completed-date">'+esc(label)+'</h3><div class="wb-task-list">'+rows+'</div></section>';
  }).join("")+'</div>';
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

function renderWorkbenchBody(){
  const bodyContent = WB.view === "trash" ? renderWorkbenchTrash()
    : WB.view === "completed" ? renderWorkbenchCompleted()
    : renderWorkbenchProjects(); // unscheduled / day / week / month / project：都按项目分组展示
  return renderWorkbenchSecondRow()+bodyContent;
}

// 移动端 FAB 新建走 docked 模式：卡片不插进列表，改在这个固定在键盘上方的浮层里编辑，
// 完成或点空白（走 saveWorkbenchNewTask/workbenchFinishActiveCard）才真正插入对应位置。
function renderWbDock(){
  const dock = document.getElementById("wbDock");
  if(!dock) return;
  if(WB.editorTaskId && WB.editorDocked){
    const task = workbenchTask(WB.editorTaskId);
    dock.innerHTML = task ? renderWorkbenchTaskEditor(task) : "";
    hydrateWorkbenchImages();
    workbenchAutoGrowTextarea(dock.querySelector("textarea[data-edit-detail]"));
    return;
  }
  if(!WB.newTask || !WB.newTask.docked){ dock.innerHTML = ""; return; }
  const html = WB.newTask.parentId
    ? workbenchNewChildCardHtml(WB.newTask.parentId, true)
    : (workbenchProject(WB.newTask.project) ? workbenchNewTaskCardHtml(workbenchProject(WB.newTask.project)) : "");
  dock.innerHTML = html;
  workbenchAutoGrowTextarea(dock.querySelector("textarea[data-new-detail]"));
}

function renderWorkbench(){
  const root = $("#wbContent");
  if(!root) return;
  root.innerHTML = renderWorkbenchHeader()+renderWorkbenchBody();
  hydrateWorkbenchImages();
  workbenchAutoGrowAll();
  wbBindAllSwipes();
  renderWbDock();
  // 已完成/回收站没有项目分组，插不进新建卡，FAB 在这两个视图下藏起来。
  $("#wbFab")?.classList.toggle("wb-fab-off", WB.view === "completed" || WB.view === "trash");
}

// ── 移动端任务行滑动操作（左滑：日期/移动/删除；右滑：进多选）───────────────
const WB_SWIPE_W = 132;
const WB_SWIPE_R = 64; // 右滑进多选的拖拽上限/判定阈值（阈值取一半，32px）

function wbCloseSwipe(row){
  row.classList.remove("wb-swiped");
}

function wbCloseAllSwipes(except){
  document.querySelectorAll(".wb-swipeable.wb-swiped").forEach(r => { if(r !== except) wbCloseSwipe(r); });
}

function wbBindSwipe(row){
  const body = row.querySelector(".wb-swipe-body");
  const act = row.querySelector(".wb-swipe-actions");
  const sel = row.querySelector(".wb-swipe-select");
  if(!body || !act) return;
  let startX = 0, startY = 0, dx = 0, lock = null, dragging = false;
  row.addEventListener("touchstart", ev => {
    if(ev.touches.length !== 1) return;
    startX = ev.touches[0].clientX; startY = ev.touches[0].clientY;
    dx = 0; lock = null; dragging = false;
    row.classList.add("wb-swiping");
  }, {passive: true});
  row.addEventListener("touchmove", ev => {
    if(ev.touches.length !== 1) return;
    // 多选模式下选中/取消全靠点一下，横滑手势整个让路——不然容易跟"点选"的轻触混淆，
    // 也没必要再左滑出日期/移动/删除（批量操作已经在底部工具栏了）。
    if(WB.multiSelectMode) return;
    const x = ev.touches[0].clientX, y = ev.touches[0].clientY;
    const ddx = x - startX, ddy = y - startY;
    if(lock === null) lock = Math.abs(ddx) > Math.abs(ddy) + 4 ? "x" : (Math.abs(ddy) > Math.abs(ddx) + 4 ? "y" : null);
    if(lock !== "x") return;
    dragging = true;
    const base = row.classList.contains("wb-swiped") ? -WB_SWIPE_W : 0;
    dx = Math.max(-WB_SWIPE_W, Math.min(WB_SWIPE_R, base + ddx));
    body.style.transform = "translateX(" + dx + "px)";
    act.style.transform = "translateX(" + (WB_SWIPE_W + dx) + "px)";
    if(sel){
      sel.style.opacity = dx > 0 ? Math.min(1, dx / WB_SWIPE_R) : 0;
      sel.classList.toggle("wb-swipe-select-armed", dx >= WB_SWIPE_R / 2);
    }
  }, {passive: true});
  const end = () => {
    row.classList.remove("wb-swiping");
    body.style.transform = ""; act.style.transform = "";
    if(sel){ sel.style.opacity = ""; sel.classList.remove("wb-swipe-select-armed"); }
    if(dragging){
      row._wbJustSwiped = true;
      if(dx <= -WB_SWIPE_W / 2){ wbCloseAllSwipes(row); row.classList.add("wb-swiped"); }
      else if(dx >= WB_SWIPE_R / 2){ wbCloseSwipe(row); wbEnterMultiSelect(row.dataset.task); }
      else wbCloseSwipe(row);
    }
    dragging = false;
  };
  row.addEventListener("touchend", end);
  row.addEventListener("touchcancel", end);
}

function wbBindAllSwipes(){
  if(!wbIsTouchLike()) return;
  document.querySelectorAll("#wbContent .wb-swipeable").forEach(wbBindSwipe);
  const scrollEl = document.getElementById("workbenchView");
  if(scrollEl && !scrollEl._wbSwipeScrollBound){
    scrollEl._wbSwipeScrollBound = true;
    scrollEl.addEventListener("scroll", () => wbCloseAllSwipes());
  }
}

// ── 移动端多选（右滑进入，仿 Things：底部工具栏 + 右上角"完成"退出）──────────
// 只在触屏生效，跟桌面的 shift+点击/右键多选是两条独立路径，互不干扰。
function wbEnterMultiSelect(taskId){
  wbCloseAllSwipes();
  if(!WB.multiSelectMode){
    WB.multiSelectMode = true;
    document.body.classList.add("wb-multiselect-mode");
  }
  const next = new Set(WB.selected);
  if(next.has(taskId)) next.delete(taskId); else next.add(taskId);
  workbenchApplySelectionClasses(WB.selected, next);
  WB.selected = next;
  WB.selectAnchor = taskId;
  wbRenderMultiSelectBar();
  // 选空了也不自动退出——留在多选态、"完成"按钮还在，退出只认那个按钮。之前"选空自动
  // 退出"这条会在快速双击同一行时开口子：第一下清空选区顺带退出多选，第二下就落到
  // 普通单击路径，误把详情卡打开了（跟"多选模式下禁止开详情"的要求冲突）。
}

function wbExitMultiSelect(){
  WB.multiSelectMode = false;
  document.body.classList.remove("wb-multiselect-mode");
  workbenchApplySelectionClasses(WB.selected, new Set());
  WB.selected = new Set();
  WB.selectAnchor = null;
  wbRenderMultiSelectBar();
}

// 别处（切 tab/项目、批量删除后…）本来就要清空选中态，顺带把多选模式也带出去，
// 不然桌面端切视图后移动端的底部工具栏会孤零零地留在那——选区已经空了，UI 却没退出。
function wbResetSelection(){
  WB.selected = new Set();
  WB.selectAnchor = null;
  if(WB.multiSelectMode){
    WB.multiSelectMode = false;
    document.body.classList.remove("wb-multiselect-mode");
  }
  wbRenderMultiSelectBar();
}

function wbRenderMultiSelectBar(){
  const bar = document.getElementById("wbMultiBar");
  const done = document.getElementById("wbMultiDone");
  if(!bar || !done) return;
  const n = WB.selected.size;
  bar.hidden = !WB.multiSelectMode;
  done.hidden = !WB.multiSelectMode;
  done.textContent = WB.multiSelectMode ? "完成("+n+")" : "完成";
  bar.querySelectorAll("[data-multi]").forEach(btn => { btn.disabled = n === 0; });
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
  if(next.classList.contains("wb-swipeable")) wbBindSwipe(next);
  return true;
}

// 通用的「节点原地换成另一段 HTML，尺寸不同就把高度/宽度过渡一下」动效：
// 行 ↔ 卡片切换、新建卡收起成任务行，都复用这一套，不必每个场景各写一份。
function workbenchAnimateMorph(node, html){
  if(!node) return false;
  const startRect = node.getBoundingClientRect();
  const wrap = document.createElement("div");
  wrap.innerHTML = html;
  const next = wrap.firstElementChild;
  if(!next) return false;
  node.replaceWith(next);
  hydrateWorkbenchImages();
  if(next.classList.contains("wb-swipeable")) wbBindSwipe(next);
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

// 卡片 ↔ 行来回切换时用：行与卡片高度不同，直接换节点会「啪」一下跳变，这里让它长出来/收回去。
function workbenchMorphTask(taskId){
  const task = workbenchTask(taskId);
  if(!task) return false;
  return workbenchAnimateMorph(workbenchNodeForTask(taskId), workbenchTaskRow(task));
}

// 显示顺序即 WB_DATA.tasks 的数组顺序；拖拽结束时再将全量顺序落库。
function workbenchReorderTask(draggedId, targetId, placeBefore){
  if(draggedId === targetId) return false;
  const tasks = WB_DATA.tasks;
  const dragged = workbenchTask(draggedId);
  const target = workbenchTask(targetId);
  if(!dragged || !target) return false;
  const draggedIdx = tasks.indexOf(dragged);
  tasks.splice(draggedIdx, 1);
  let targetIdx = tasks.indexOf(target);
  if(!placeBefore) targetIdx += 1;
  tasks.splice(targetIdx, 0, dragged);
  return true;
}

function workbenchTaskTree(taskId){
  const tree = [], seen = new Set(), pending = [taskId];
  while(pending.length){
    const itemId = pending.pop();
    if(seen.has(itemId)) continue;
    const task = workbenchTask(itemId);
    if(!task) continue;
    seen.add(itemId);
    tree.push(task);
    workbenchChildren(itemId).forEach(child => pending.push(child.id));
  }
  return tree;
}

function workbenchPersistTaskPlacement(task, parentId, projectId, orderBefore){
  const tree = workbenchTaskTree(task.id);
  const before = tree.map(item => ({id:item.id, patch:{parentId:item.parentId, project:item.project}}));
  task.parentId = parentId;
  tree.forEach(item => { item.project = projectId; });
  const after = tree.map(item => ({id:item.id, patch:{parentId:item.parentId, project:item.project}}));
  renderWorkbench();
  api("/workbench/tasks/move", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id:task.id, parentId, project:projectId, order:WB_DATA.tasks.map(item => item.id)})})
    .then(r => r.json().then(data => ({ok:r.ok && !data.error, data})))
    .then(({ok}) => {
      if(ok){
        if(JSON.stringify(before) !== JSON.stringify(after)) workbenchRemember({type:"task-patch", before, after});
        return;
      }
      before.forEach(({id, patch}) => Object.assign(workbenchTask(id)||{}, patch));
      WB_DATA.tasks = orderBefore;
      renderWorkbench();
    })
    .catch(() => {
      before.forEach(({id, patch}) => Object.assign(workbenchTask(id)||{}, patch));
      WB_DATA.tasks = orderBefore;
      renderWorkbench();
    });
}

function workbenchTaskIsAncestor(ancestorId, taskId){
  if(ancestorId === taskId) return true;
  const seen = new Set();
  let parentId = workbenchTask(taskId)?.parentId;
  while(parentId && !seen.has(parentId)){
    if(parentId === ancestorId) return true;
    seen.add(parentId);
    parentId = workbenchTask(parentId)?.parentId;
  }
  return false;
}

function workbenchExpandTaskTree(taskId){
  const children = workbenchChildren(taskId);
  if(!children.length) return;
  WB.expanded.add(taskId); WB.collapsed.delete(taskId);
  children.forEach(child => workbenchExpandTaskTree(child.id));
}

function workbenchNestTask(taskId, parentId, siblingId, placeBefore){
  const task = workbenchTask(taskId);
  const parent = workbenchTask(parentId);
  if(!task || !parent || task.parentId === parentId || workbenchTaskIsAncestor(taskId, parentId)) return;
  const orderBefore = WB_DATA.tasks.slice();
  workbenchReorderTask(taskId, siblingId || parentId, siblingId ? placeBefore : false);
  WB.expanded.add(parentId); WB.collapsed.delete(parentId);
  workbenchExpandTaskTree(parentId);
  workbenchExpandTaskTree(taskId);
  workbenchPersistTaskPlacement(task, parentId, parent.project, orderBefore);
}

function workbenchPromoteTask(taskId, targetId, placeBefore){
  const task = workbenchTask(taskId);
  const target = workbenchTask(targetId);
  if(!task || !target || !task.parentId) return;
  const orderBefore = WB_DATA.tasks.slice();
  workbenchReorderTask(taskId, targetId, placeBefore);
  workbenchPersistTaskPlacement(task, null, target.project, orderBefore);
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
  workbenchSetSelection([taskId]);
  WB.selectAnchor = taskId;
}

function workbenchSelectRange(anchorId, targetId){
  workbenchSetSelection(workbenchRowRange(anchorId, targetId));
}

function openWorkbenchEditor(taskId){
  workbenchFinishActiveCard();
  workbenchSetSelection([]);
  WB.editorTaskId = taskId;
  // 触屏走跟新建卡一样的 docked 浮层（贴键盘上方），不再原地把任务行 morph 成编辑卡——
  // 列表位置、滚动、导航栏都不用管，桌面维持原来的原地展开。
  WB.editorDocked = wbIsTouchLike();
  if(WB.editorDocked || !workbenchMorphTask(taskId)) renderWorkbench();
  wbFocusSoon(WB.editorDocked ? "#wbDock .wb-card-title" : ".wb-card-title", el => {
    const len = el.value.length;
    el.setSelectionRange(len, len); // 只定位光标，不要默认全选标题
  });
}

// 勾选完成后，先原地打勾停留一小段时间（让用户看清「已完成」的反馈），
// 再收起腾出空间；taskId -> setTimeout 句柄，方便在停留期间被取消（比如又点了一次撤销）。
const WB_COMPLETE_HOLD = new Map();
const WB_COMPLETE_HOLD_MS = 1500;

// 已完成的任务在这几个视图里直接隐藏（见 workbenchVisibleTasks）。勾选完成时先走
// workbenchSwapTask 原地换勾选态，停留后再用 workbenchShrinkOut 收起；取消完成（恢复）
// 不需要停留，直接整体重渲染即可。
function toggleWorkbenchTask(taskId){
  const task = workbenchTask(taskId);
  if(!task) return;
  const pendingHold = WB_COMPLETE_HOLD.get(taskId);
  if(pendingHold){ clearTimeout(pendingHold); WB_COMPLETE_HOLD.delete(taskId); }
  const before = {status: task.status};
  const completing = task.status !== "done" && task.status !== "cancelled";
  task.status = completing ? "done" : "todo";
  const after = {status: task.status};
  workbenchPersistTaskChange(taskId, before, after);
  // 月视图本来就会把已完成任务一起展示（见 workbenchVisibleTasks），打勾/恢复都不影响
  // 任务是否可见，原地换勾选态即可，不用收起也不用整体重渲染。
  if(WB.view === "month"){
    if(!workbenchSwapTask(taskId)) renderWorkbench();
    return;
  }
  if(completing && workbenchSwapTask(taskId)){
    WB_COMPLETE_HOLD.set(taskId, setTimeout(() => {
      WB_COMPLETE_HOLD.delete(taskId);
      const node = workbenchNodeForTask(taskId);
      const stillDone = workbenchTask(taskId)?.status === "done";
      if(!node || !stillDone) return;
      if(!workbenchShrinkOut(node)) renderWorkbench();
    }, WB_COMPLETE_HOLD_MS));
    return;
  }
  renderWorkbench();
}

function workbenchDateSchedule(date){
  return {date, month:date.slice(0, 7), week:workbenchWeekKey(workbenchDate(date))};
}

function workbenchWeekSchedule(week){
  return {date:null, month:week.slice(0, 7), week};
}

function workbenchMonthSchedule(month){
  return {date:null, month, week:null};
}

function workbenchUnscheduled(){
  return {date:null, month:null, week:null};
}

function workbenchCurrentViewSchedule(){
  if(WB.view === "day") return workbenchDateSchedule(WB.anchor);
  if(WB.view === "week") return workbenchWeekSchedule(workbenchWeekKey());
  if(WB.view === "month") return workbenchMonthSchedule(workbenchMonthKey());
  return workbenchUnscheduled();
}

function workbenchApplySchedule(task, schedule){
  Object.assign(task, schedule);
}

function scheduleWorkbenchTask(taskId, schedule){
  const task = workbenchTask(taskId);
  if(!task) return;
  const before = {date: task.date, month: task.month, week: task.week};
  workbenchApplySchedule(task, schedule);
  const after = {date: task.date, month: task.month, week: task.week};
  renderWorkbench();
  workbenchPersistTaskChange(taskId, before, after);
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

// 通用「已插入 DOM 的节点从 0 长到自然高度」动效，跟 workbenchAnimateMorph 同一个思路：
// 先量出目标尺寸，再从 0 过渡过去，而不是整页重渲染后让节点凭空「啪」地出现。
function workbenchGrowIn(node){
  if(!node) return false;
  const endRect = node.getBoundingClientRect();
  const endMarginTop = getComputedStyle(node).marginTop;
  node.style.height = "0px";
  node.style.marginTop = "0px";
  node.style.overflow = "hidden";
  void node.offsetHeight;
  node.style.transition = "height .2s cubic-bezier(.22,.61,.36,1), margin-top .2s cubic-bezier(.22,.61,.36,1)";
  requestAnimationFrame(() => {
    node.style.height = endRect.height+"px";
    node.style.marginTop = endMarginTop;
  });
  node.addEventListener("transitionend", function onEnd(event){
    if(event.propertyName !== "height" || event.target !== node) return;
    node.style.height = ""; node.style.marginTop = ""; node.style.overflow = ""; node.style.transition = "";
    node.removeEventListener("transitionend", onEnd);
  });
  return true;
}

// 通用「节点收缩到 0 后从 DOM 移除」动效，跟 workbenchGrowIn 对称。
// 父容器（.wb-task-list）用的是 flex + gap 排间距，不是 margin——gap 不会跟着
// height 一起被压缩，节点移除瞬间会多出一份 gap 的空隙，看起来像"卡一下"。
// 这里额外把 margin-bottom 动画到 -gap，让 gap 也随着收起过程一起吃掉，移除时才是无缝的。
function workbenchShrinkOut(node){
  if(!node) return false;
  const parent = node.parentElement;
  const gap = parent ? (parseFloat(getComputedStyle(parent).rowGap) || 0) : 0;
  node.style.height = node.getBoundingClientRect().height+"px";
  // 样式表里 .wb-task 有 min-height:36px，不清掉的话内联 height 会被它钳住——动画途中
  // 高度其实纹丝不动，直到节点被移除的最后一帧才猛地收拢，这才是卡顿的真正来源。
  node.style.minHeight = "0px";
  node.style.marginTop = getComputedStyle(node).marginTop;
  node.style.marginBottom = "0px";
  node.style.overflow = "hidden";
  void node.offsetHeight;
  node.style.transition = "height .16s ease, margin-top .16s ease, margin-bottom .16s ease, opacity .16s ease";
  requestAnimationFrame(() => {
    node.style.height = "0px";
    node.style.marginTop = "0px";
    node.style.marginBottom = (-gap)+"px";
    node.style.opacity = "0";
  });
  node.addEventListener("transitionend", function onEnd(event){
    if(event.propertyName !== "height" || event.target !== node) return;
    node.remove();
  });
  return true;
}

// 新建卡必须插进 .wb-task-list 里，跟任务行做同一个父容器的兄弟节点——
// 它后续会被 workbenchAnimateMorph 原地换成任务行，容器不对齐会跟着错位。
// 项目原本没有任务时列表容器都不存在（渲染的是「暂无任务」提示），这里顺带把它建出来。
function workbenchInsertNewTaskCard(project){
  const block = document.querySelector('[data-group="'+CSS.escape(workbenchGroupId(project))+'"]')?.closest(".wb-project-block");
  if(!block) return false;
  let list = block.querySelector(".wb-task-list");
  if(!list){
    list = document.createElement("div");
    list.className = "wb-task-list";
    (block.querySelector(".wb-empty") || null)?.replaceWith(list);
    if(!list.isConnected) block.appendChild(list);
  }
  const wrap = document.createElement("div");
  wrap.innerHTML = workbenchNewTaskCard(project);
  const card = wrap.firstElementChild;
  if(!card) return false;
  list.appendChild(card);
  workbenchAutoGrowTextarea(card.querySelector("textarea[data-new-detail]"));
  return workbenchGrowIn(card);
}

// 取消新建时对称地收回去，而不是直接从 DOM 里消失。
function workbenchRemoveNewTaskCard(){
  return workbenchShrinkOut(document.querySelector("[data-new-card]"));
}

function openWorkbenchNewTask(){
  const project = WB.project === "all" ? WB_DATA.projects[0] : workbenchProject(WB.project);
  if(!project){ alert("请先新建一个项目。"); return; }
  clearTimeout(wbClickTimer);
  const prevEditor = WB.editorTaskId;
  WB.editorTaskId = null;
  wbResetSelection();
  WB.newTask = Object.assign({project:project.id, title:"", detail:"", assignee:"human", parentId:null, siblingTaskId:null}, workbenchCurrentViewSchedule());
  let ok = true;
  if(prevEditor) ok = workbenchMorphTask(prevEditor) && ok;
  if(ok) ok = workbenchInsertNewTaskCard(project) && ok;
  if(!ok) renderWorkbench();
  wbFocusSoon("[data-new-title]");
}

async function saveWorkbenchNewTask(){
  const draft = WB.newTask;
  if(!draft) return;
  const title = draft.title.trim();
  if(!title){ $("[data-new-title]")?.focus(); return; }
  const payload = {project:draft.project, title, detail:draft.detail, date:draft.date||null, month:draft.month||null, week:draft.week||null, assignee:draft.assignee||"human", parentId:draft.parentId||null};
  const beforeTaskId = draft.beforeTaskId || null; // 移动端 FAB 定位新建用，见 workbenchOpenNewTaskBefore
  const docked = !!draft.docked; // 卡片长在 #wbDock 里，不在列表里，不能原地 morph 成行
  WB.newTask = null;
  // 乐观本地先造一条临时任务，卡片立刻收起成行——不用等接口回来才有动效，否则回车会
  // 感觉「慢半拍」；等真实 id 回来了原地把临时 id 换掉，保存失败就把这一行撤回。
  const temp = Object.assign({id:"tmp-"+Math.random().toString(36).slice(2), status:"todo", images:[]}, payload);
  const beforeIdx = beforeTaskId ? WB_DATA.tasks.findIndex(t => t.id === beforeTaskId) : -1;
  if(beforeIdx !== -1) WB_DATA.tasks.splice(beforeIdx, 0, temp); else WB_DATA.tasks.push(temp);
  if(docked) renderWorkbench();
  else if(!workbenchAnimateMorph(document.querySelector("[data-new-card]"), workbenchTaskRow(temp))) renderWorkbench();
  try{
    const r = await api("/workbench/tasks/create", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
    const d = await r.json();
    if(!r.ok || d.error) throw new Error(d.error||"创建失败");
    const idx = WB_DATA.tasks.indexOf(temp);
    if(idx !== -1) WB_DATA.tasks[idx] = d.task;
    const node = workbenchNodeForTask(temp.id);
    if(node){
      node.dataset.task = d.task.id;
      node.querySelector("[data-complete]")?.setAttribute("data-complete", d.task.id);
      node.querySelector('[data-open-dp="task:'+temp.id+'"]')?.setAttribute("data-open-dp", "task:"+d.task.id);
    }
    workbenchRemember({type:"task-presence", before:[], after:[{task:workbenchHistoryClone(d.task), index:idx}]});
    // 创建接口只会把新任务追加到末尾，插到中间的位置得靠 move 接口单独持久化一次顺序。
    if(beforeIdx !== -1){
      api("/workbench/tasks/move", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id:d.task.id, parentId:d.task.parentId||null, project:d.task.project, order:WB_DATA.tasks.map(item => item.id)})}).catch(() => {});
    }
  }catch(e){
    WB_DATA.tasks = WB_DATA.tasks.filter(t => t !== temp);
    alert("新建任务失败："+(e.message||""));
    renderWorkbench();
  }
}

// 收起当前展开的卡片——不管是新建卡还是已有任务的编辑卡：新建卡有标题就当完成新建，
// 没标题就当取消；编辑卡直接收回成一行。点击空白处、回车都走这一个函数，别各写一份。
function workbenchFinishActiveCard(){
  if(WB.newTask){
    if(WB.newTask.title.trim()) saveWorkbenchNewTask();
    else { WB.newTask = null; if(!workbenchRemoveNewTaskCard()) renderWorkbench(); }
    return;
  }
  if(WB.editorTaskId){
    const id = WB.editorTaskId;
    const wasDocked = WB.editorDocked;
    WB.editorTaskId = null;
    WB.editorDocked = false;
    // docked 时列表里本来就是正常行（没有卡可 morph），只需要清空 #wbDock；
    // 走一次全量重渲染最简单，也顺带同步了列表行（万一编辑期间标题/备注变了）。
    if(wasDocked || !workbenchMorphTask(id)) renderWorkbench();
  }
}

function workbenchResetChildExpansion(){
  WB.expanded.clear(); WB.collapsed.clear();
}

function wbToggleNotes(){
  const pref = wbViewPref(WB.view);
  pref.hideNotes = !pref.hideNotes;
  wbSaveViewPrefs();
  renderWorkbench();
}

function wbToggleCollapseAll(){
  const pref = wbViewPref(WB.view);
  pref.collapseAll = !pref.collapseAll;
  wbSaveViewPrefs();
  // 清掉手动展开/折叠过的个别任务，不然点了「全部折叠/展开」还有漏网之鱼保持原样。
  workbenchResetChildExpansion();
  renderWorkbench();
}

const WB_CHILDREN_ANIMATING = new Set();

function toggleWorkbenchChildren(parentId){
  if(WB_CHILDREN_ANIMATING.has(parentId)) return;
  if(workbenchChildrenExpanded(parentId) || document.querySelector('[data-parent="'+CSS.escape(parentId)+'"]')){
    workbenchCollapseChildren(parentId);
  }else{
    workbenchExpandChildren(parentId);
  }
}

function workbenchChildrenNode(parentId){
  return document.querySelector('[data-parent="'+CSS.escape(parentId)+'"]');
}

function workbenchChildrenBadge(parentId){
  const node = workbenchNodeForTask(parentId);
  return node ? node.querySelector('[data-toggle-children="'+CSS.escape(parentId)+'"]') : null;
}

function workbenchSetChildToggleIcon(badge, expanded){
  if(!badge) return;
  badge.innerHTML = ic(expanded ? "chevronUp" : "chevronDown");
  badge.title = expanded ? "收起子任务" : "展开子任务";
}

function workbenchExpandChildren(parentId){
  if(WB_CHILDREN_ANIMATING.has(parentId)) return;
  WB_CHILDREN_ANIMATING.add(parentId);
  WB.collapsed.delete(parentId); WB.expanded.add(parentId);
  const parentNode = workbenchNodeForTask(parentId);
  const html = workbenchChildRows(parentId);
  if(!html || !parentNode){
    WB_CHILDREN_ANIMATING.delete(parentId);
    renderWorkbench();
    return;
  }
  const wrap = document.createElement("div");
  wrap.innerHTML = html;
  const node = wrap.firstElementChild;
  parentNode.after(node);
  hydrateWorkbenchImages();
  workbenchAutoGrowAll();
  const badge = workbenchChildrenBadge(parentId);
  if(badge) badge.classList.add("is-expanded");
  workbenchSetChildToggleIcon(badge, true);
  const endHeight = node.scrollHeight;
  node.style.height = "0px";
  node.style.opacity = "0";
  node.style.overflow = "hidden";
  node.style.pointerEvents = "none";
  void node.offsetHeight;
  node.style.transition = "height .2s cubic-bezier(.22,.61,.36,1), opacity .18s ease";
  requestAnimationFrame(() => {
    node.style.height = endHeight+"px";
    node.style.opacity = "1";
  });
  node.addEventListener("transitionend", function onEnd(event){
    if(event.propertyName !== "height" || event.target !== node) return;
    node.style.height = ""; node.style.opacity = ""; node.style.overflow = ""; node.style.pointerEvents = ""; node.style.transition = "";
    node.removeEventListener("transitionend", onEnd);
    WB_CHILDREN_ANIMATING.delete(parentId);
  });
}

function workbenchCollapseChildren(parentId){
  if(WB_CHILDREN_ANIMATING.has(parentId)) return;
  WB_CHILDREN_ANIMATING.add(parentId);
  WB.expanded.delete(parentId); WB.collapsed.add(parentId);
  const node = workbenchChildrenNode(parentId);
  const badge = workbenchChildrenBadge(parentId);
  if(badge) badge.classList.remove("is-expanded");
  workbenchSetChildToggleIcon(badge, false);
  if(!node){
    WB_CHILDREN_ANIMATING.delete(parentId);
    renderWorkbench();
    return;
  }
  const startHeight = node.getBoundingClientRect().height;
  node.style.height = startHeight+"px";
  node.style.overflow = "hidden";
  node.style.pointerEvents = "none";
  void node.offsetHeight;
  node.style.transition = "height .2s cubic-bezier(.22,.61,.36,1), opacity .18s ease";
  requestAnimationFrame(() => {
    node.style.height = "0px";
    node.style.opacity = "0";
  });
  node.addEventListener("transitionend", function onEnd(event){
    if(event.propertyName !== "height" || event.target !== node) return;
    node.remove();
    node.removeEventListener("transitionend", onEnd);
    WB_CHILDREN_ANIMATING.delete(parentId);
  });
}

function gotoWorkbenchParent(parentId){
  const parent = workbenchTask(parentId);
  if(!parent) return;
  WB.view = "project";
  WB.project = parent.project || WB_UNASSIGNED_ID;
  workbenchResetChildExpansion();
  WB.expanded.add(parentId);
  WB.editorTaskId = parentId;
  renderWorkbench();
  requestAnimationFrame(() => {
    const el = document.querySelector('[data-task="'+parentId+'"]');
    if(el) el.scrollIntoView({behavior:'smooth', block:'center'});
  });
}

function toggleWorkbenchAssignee(taskId, target){
  const task = workbenchTask(taskId);
  if(!task) return;
  const prev = task.assignee;
  const next = target || (prev === "ai" ? "human" : "ai");
  if(next === prev) return;
  task.assignee = next;
  if(!workbenchMorphTask(taskId)) renderWorkbench();
  persistWorkbenchTask(taskId, {assignee: task.assignee}, {assignee: prev});
}

// 点"执行"不再直接后台起跑:改成新建一个（按任务分组名匹配到的）项目会话，把
// 任务备注预填进输入框，交给用户自己确认发送——避免 AI 想都没想就跑偏。草稿会话
// 转正(见 composer.js send() 里的 wasLocal 分支)后要把真实 conv 关联回任务卡片
// "会话N"链接,靠 entry.wbTaskId 这个自定义字段带过去。
function dispatchWorkbenchTask(taskId){
  const task = workbenchTask(taskId);
  if(!task) return;
  if(!task.detail?.trim()){ alert("任务没有备注/prompt，无法执行。请先填写备注。"); return; }
  const wbProject = workbenchProject(task.project);
  const projectName = (wbProject && wbProject.name || "").trim().toLowerCase();
  const matched = projectName && (S.projects||[]).find(p => (p.name||"").trim().toLowerCase() === projectName);
  newChatIn(matched ? matched.hash : null);
  const entry = S.convs.find(c => c.conv === S.conv);
  if(entry) entry.wbTaskId = taskId;
  $("#ta").value = task.detail;
  autoGrow();
  saveDraft();
}

// 草稿会话转正瞬间(composer.js)调用:把新出现的真实 conv 关联回任务卡片。
function linkWorkbenchTaskSession(taskId, conv){
  api("/workbench/tasks/link", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id:taskId, conv:conv})}).catch(()=>{});
}

// 选中任意一个任务（顶层或子任务）按回车：总是在它下面新建一个子任务，不管选中项本身是不是子任务。
function workbenchOpenChildOfSelected(){
  if(WB.view === "completed" || WB.view === "trash" || WB.selected.size !== 1) return;
  const task = workbenchTask([...WB.selected][0]);
  if(!task) return;
  openWorkbenchNewChild(task.id, null);
}

// 移动端 FAB 专用：新建卡插到某个已有顶层任务前面（beforeTaskId 为空就跟原来一样落在最底下）。
// docked：卡片不直接插进列表 DOM，改在 #wbDock 里编辑，完成时才落到 beforeTaskId 记的位置——
// 不然插入点滚动到哪、导航栏挡没挡都得现算，键盘一弹更难兼顾。
function workbenchOpenNewTaskBefore(project, beforeTaskId){
  clearTimeout(wbClickTimer);
  WB.editorTaskId = null;
  wbResetSelection();
  WB.newTask = Object.assign({project:project.id, title:"", detail:"", assignee:"human", parentId:null, siblingTaskId:null, beforeTaskId:beforeTaskId||null, docked:true}, workbenchCurrentViewSchedule());
  renderWorkbench();
  wbFocusSoon("[data-new-title]");
}

function openWorkbenchNewChild(parentId, siblingTaskId, opts){
  const parent = workbenchTask(parentId);
  if(!parent) return;
  const project = workbenchProject(parent.project) || WB_DATA.projects[0];
  if(!project) return;
  clearTimeout(wbClickTimer);
  WB.editorTaskId = null;
  wbResetSelection();
  WB.expanded.add(parentId); WB.collapsed.delete(parentId);
  WB.newTask = Object.assign({project:project.id, title:"", detail:"", assignee:"human", parentId, siblingTaskId:siblingTaskId||null, docked:!!opts?.docked}, workbenchCurrentViewSchedule());
  renderWorkbench();
  wbFocusSoon("[data-new-title]");
}

// 删除 = 软删除(移入回收站)，不用再弹确认框——删错了去回收站图标里恢复就行。
async function deleteWorkbenchTask(taskId){
  const task = workbenchTask(taskId);
  if(!task) return;
  const entry = {task:workbenchHistoryClone(task), index:WB_DATA.tasks.indexOf(task)};
  WB_DATA.tasks = WB_DATA.tasks.filter(t => t.id !== taskId);
  if(WB.editorTaskId === taskId) WB.editorTaskId = null;
  WB.selected.delete(taskId);
  renderWorkbench();
  try{
    await workbenchHistoryRequest("/workbench/tasks/delete", {id:taskId});
    workbenchRemember({type:"task-presence", before:[entry], after:[]});
  }catch(e){
    WB_DATA.tasks.splice(entry.index, 0, task);
    renderWorkbench();
    alert("删除失败："+(e.message||""));
  }
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
    const dataIndex = WB_DATA.tasks.length;
    WB_DATA.tasks.push(d.task);
    workbenchRemember({type:"task-presence", before:[], after:[{task:workbenchHistoryClone(d.task), index:dataIndex}]});
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
  const before = [], after = [];
  ids.forEach(id => {
    const task = workbenchTask(id);
    if(!task || task.status === "done") return;
    before.push({id, patch:{status:task.status}});
    task.status = "done";
    after.push({id, patch:{status:task.status}});
  });
  if(!before.length) return;
  // 已完成任务会被当前视图隐藏，必须整体重渲染。
  renderWorkbench();
  Promise.all(after.map(({id, patch}, index) => persistWorkbenchTask(id, patch, before[index].patch))).then(results => {
    if(results.every(Boolean)) workbenchRemember({type:"task-patch", before, after});
  });
}

// 已取消跟已完成走同一套「隐藏于常规视图、在日志 tab 可见」的规则，直接照抄
// workbenchBatchComplete 的结构。
function workbenchBatchCancel(ids){
  const before = [], after = [];
  ids.forEach(id => {
    const task = workbenchTask(id);
    if(!task || task.status === "cancelled") return;
    before.push({id, patch:{status:task.status}});
    task.status = "cancelled";
    after.push({id, patch:{status:task.status}});
  });
  if(!before.length) return;
  renderWorkbench();
  Promise.all(after.map(({id, patch}, index) => persistWorkbenchTask(id, patch, before[index].patch))).then(results => {
    if(results.every(Boolean)) workbenchRemember({type:"task-patch", before, after});
  });
}

function workbenchBatchSchedule(ids, schedule){
  const before = [], after = [];
  ids.forEach(id => {
    const task = workbenchTask(id);
    if(!task) return;
    before.push({id, patch:{date:task.date, month:task.month, week:task.week}});
    workbenchApplySchedule(task, schedule);
    after.push({id, patch:{date:task.date, month:task.month, week:task.week}});
  });
  if(!before.length) return;
  renderWorkbench();
  Promise.all(after.map(({id, patch}, index) => persistWorkbenchTask(id, patch, before[index].patch))).then(results => {
    if(results.every(Boolean)) workbenchRemember({type:"task-patch", before, after});
  });
}

function workbenchBatchMove(ids, projectId){
  const before = [], after = [];
  ids.forEach(id => {
    const task = workbenchTask(id);
    if(!task) return;
    before.push({id, patch:{project:task.project}});
    task.project = projectId;
    after.push({id, patch:{project:task.project}});
  });
  if(!before.length) return;
  renderWorkbench();
  Promise.all(after.map(({id, patch}, index) => persistWorkbenchTask(id, patch, before[index].patch))).then(results => {
    if(results.every(Boolean)) workbenchRemember({type:"task-patch", before, after});
  });
}

async function workbenchBatchDelete(ids){
  if(!ids.length) return;
  const idSet = new Set(ids);
  const entries = WB_DATA.tasks.map((task, index) => ({task, index})).filter(({task}) => idSet.has(task.id)).map(({task, index}) => ({task:workbenchHistoryClone(task), index}));
  if(!entries.length) return;
  WB_DATA.tasks = WB_DATA.tasks.filter(t => !idSet.has(t.id));
  if(idSet.has(WB.editorTaskId)) WB.editorTaskId = null;
  wbResetSelection();
  renderWorkbench();
  try{
    for(const {task} of entries) await workbenchHistoryRequest("/workbench/tasks/delete", {id:task.id});
    workbenchRemember({type:"task-presence", before:entries, after:[]});
  }catch(e){
    entries.slice().sort((a, b) => a.index - b.index).forEach(({task, index}) => WB_DATA.tasks.splice(index, 0, task));
    renderWorkbench();
    alert("删除失败："+(e.message||""));
  }
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
  WB.anchor = workbenchDateKey(date); workbenchResetChildExpansion(); renderWorkbench();
}

// ── 日期选择弹层（仿 Things）─────────────────────────────────────────────
// 三处入口共用同一个弹层实例：任务编辑卡单个日期、新建任务卡日期、右键菜单批量日期。
// target: {kind:"task", id} | {kind:"new"} | {kind:"ctx", ids}
const WB_DP = {open:false, target:null, expanded:false, baseWeek:null, rangeBefore:4, rangeAfter:12, results:[], _reposition:null};
const WB_DP_STEP = 8; // 展开后触底/触顶时一次追加的周数

function workbenchDpCurrentSchedule(){
  const target = WB_DP.target;
  if(!target) return null;
  if(target.kind === "task") return workbenchTask(target.id) || null;
  if(target.kind === "new") return WB.newTask || null;
  return null; // ctx：批量操作没有单一排期
}

function workbenchDpPresetSchedule(kind){
  const today = workbenchToday();
  if(kind === "today") return workbenchDateSchedule(today);
  if(kind === "week") return workbenchWeekSchedule(workbenchWeekKey(workbenchDate(today)));
  if(kind === "month") return workbenchMonthSchedule(today.slice(0, 7));
  return workbenchUnscheduled();
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
  return workbenchDpCurrentSchedule()?.date || null;
}

function workbenchDpHasSchedule(){
  const target = WB_DP.target;
  if(target?.kind === "ctx") return target.ids.some(id => {
    const task = workbenchTask(id);
    return !!(task?.date || task?.week || task?.month);
  });
  const schedule = workbenchDpCurrentSchedule();
  return !!(schedule?.date || schedule?.week || schedule?.month);
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

function workbenchDpApply(schedule){
  const target = WB_DP.target;
  if(!target) return;
  if(target.kind === "task") scheduleWorkbenchTask(target.id, schedule);
  else if(target.kind === "new"){ workbenchApplySchedule(WB.newTask, schedule); renderWorkbench(); }
  else if(target.kind === "ctx"){ workbenchBatchSchedule(target.ids, schedule); if(WB.multiSelectMode) wbExitMultiSelect(); }
  workbenchDpClose();
}

function workbenchDpApplyDate(date){
  workbenchDpApply(workbenchDateSchedule(date));
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
    '<div class="wb-sheet-handle" aria-hidden="true"></div>'+
    '<div class="wb-dp-search"><input type="text" data-dp-search placeholder="搜索日期，如 9/12 或 2026年9月12日" aria-label="搜索日期"><button type="button" class="wb-dp-search-clear" data-dp-search-clear hidden aria-label="清空搜索">'+ic("close")+'</button></div>'+
    '<div data-dp-body></div>';
}

// 日历（默认按天选）、周选择器、月选择器共用同一套 actions 行 + 清除按钮外壳，
// 中间的选择区随 WB_DP.mode 切换——"本周"/"本月" 变成模式开关（再点一下切回日历），
// 不再是"立刻套用当前周/月"的一次性预设。
function workbenchDpDayGridHtml(){
  const current = workbenchDpCurrentDate();
  const today = workbenchToday();
  const weeks = WB_DP.expanded
    ? workbenchDpWeekStarts(workbenchDpSundayKey(today), WB_DP.rangeBefore, WB_DP.rangeAfter)
    : workbenchDpWeekStarts(WB_DP.baseWeek, 0, 3);
  const rows = weeks.map(weekKey => workbenchDpWeekRow(weekKey, current, today)).join("");
  return '<div class="wb-dp-weekdays">'+["周日","周一","周二","周三","周四","周五","周六"].map(w => '<span>'+w+'</span>').join("")+'</div>'+
    '<div class="wb-dp-grid'+(WB_DP.expanded ? " is-expanded" : "")+'" data-dp-grid>'+rows+'</div>'+
    (WB_DP.expanded ? "" : '<button type="button" class="wb-dp-expand" data-dp-expand aria-label="展开更多日期">'+ic("chevronDown")+'</button>');
}

function workbenchDpWeekPickerHtml(){
  const selectedWeek = workbenchDpCurrentSchedule()?.week || null;
  const todayWeek = workbenchWeekKey(workbenchDate(workbenchToday()));
  const base = WB_DP.weekBase || todayWeek;
  const rows = Array.from({length:12}, (_, i) => {
    const d = workbenchDate(base); d.setDate(d.getDate() + (i-2)*7);
    const weekKey = workbenchWeekKey(d);
    const start = workbenchDate(weekKey);
    const end = new Date(start); end.setDate(start.getDate()+6);
    const label = "Week"+workbenchWeekNumber(start)+" · "+(start.getMonth()+1)+"/"+start.getDate()+"–"+(end.getMonth()+1)+"/"+end.getDate();
    const cls = ["wb-dp-week-row"];
    if(weekKey === todayWeek) cls.push("is-today");
    if(weekKey === selectedWeek) cls.push("is-selected");
    return '<button type="button" class="'+cls.join(" ")+'" data-dp-pick-week="'+weekKey+'">'+label+(weekKey === todayWeek ? '<span class="wb-dp-tag">本周</span>' : '')+'</button>';
  }).join("");
  return '<div class="wb-dp-week-pager"><button type="button" data-dp-week-page="-1" aria-label="更早的周">‹ 更早</button><button type="button" data-dp-week-page="1" aria-label="更晚的周">更晚 ›</button></div>'+
    '<div class="wb-dp-week-list" data-dp-week-list>'+rows+'</div>';
}

function workbenchDpMonthPickerHtml(){
  const selectedMonth = workbenchDpCurrentSchedule()?.month || null;
  const todayMonth = workbenchToday().slice(0, 7);
  const year = WB_DP.monthYear || workbenchDate(workbenchToday()).getFullYear();
  const cells = Array.from({length:12}, (_, i) => {
    const mk = year+"-"+String(i+1).padStart(2, "0");
    const cls = ["wb-dp-month"];
    if(mk === todayMonth) cls.push("is-today");
    if(mk === selectedMonth) cls.push("is-selected");
    return '<button type="button" class="'+cls.join(" ")+'" data-dp-pick-month="'+mk+'">'+(i+1)+'月</button>';
  }).join("");
  return '<div class="wb-dp-month-nav"><button type="button" data-dp-year="-1" aria-label="上一年">‹</button><strong>'+year+'年</strong><button type="button" data-dp-year="1" aria-label="下一年">›</button></div>'+
    '<div class="wb-dp-month-grid">'+cells+'</div>';
}

function workbenchDpRenderBody(){
  const body = document.querySelector("#wbDatePicker [data-dp-body]");
  if(!body) return;
  if(WB_DP.results.length){
    body.innerHTML = '<div class="wb-dp-results" data-dp-results>'+WB_DP.results.map(workbenchDpResultRow).join("")+'</div>';
  } else {
    const picker = WB_DP.mode === "week" ? workbenchDpWeekPickerHtml()
      : WB_DP.mode === "month" ? workbenchDpMonthPickerHtml()
      : workbenchDpDayGridHtml();
    // 没被选中的 tab 显示"日/周/月"，选中的那个才显示"今天/本周/本月"；不再用星标区分。
    body.innerHTML =
      '<div class="wb-dp-actions">'+
      '<button type="button" class="'+(WB_DP.mode === "day" ? "is-on" : "")+'" data-dp-preset="today"><span>'+(WB_DP.mode === "day" ? "今天" : "日")+'</span></button>'+
      '<button type="button" class="'+(WB_DP.mode === "week" ? "is-on" : "")+'" data-dp-preset="week"><span>'+(WB_DP.mode === "week" ? "本周" : "周")+'</span></button>'+
      '<button type="button" class="'+(WB_DP.mode === "month" ? "is-on" : "")+'" data-dp-preset="month"><span>'+(WB_DP.mode === "month" ? "本月" : "月")+'</span></button>'+
      '</div>'+
      // 固定高度容器：三个 tab（日/周/月）内容高矮不一，套一层固定高度，切换时面板不跳动。
      '<div class="wb-dp-picker">'+picker+'</div>'+
      '<button type="button" class="wb-dp-clear" data-dp-clear'+(workbenchDpHasSchedule() ? "" : " disabled")+' title="移除排期" aria-label="移除排期">'+ic("eraser")+'</button>';
  }
  if(WB_DP._reposition) WB_DP._reposition(); // 主体换了搜索结果/日历，高度跟着变，重新贴一次锚点
}

function workbenchDpEnsureEl(){
  let el = document.getElementById("wbDatePicker");
  if(el) return el;
  if(!document.getElementById("wbDpBackdrop")){
    const backdrop = document.createElement("div");
    backdrop.id = "wbDpBackdrop";
    // 桌面端透明不挡点击，靠 document mousedown 判断点在外面就关；移动端 CSS 把它变成
    // 真正的遮罩层，同时兼作点击关闭（底部弹层是模态的，点空白处也该能关）。
    backdrop.addEventListener("click", workbenchDpClose);
    document.body.appendChild(backdrop);
  }
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
    if(top) workbenchDpApplyDate(top.dateKey);
  });
  return el;
}

function workbenchDpHandleClick(event){
  const pick = event.target.closest("[data-dp-pick]");
  if(pick){ workbenchDpApplyDate(pick.dataset.dpPick); return; }
  const pickWeek = event.target.closest("[data-dp-pick-week]");
  if(pickWeek){ workbenchDpApply(workbenchWeekSchedule(pickWeek.dataset.dpPickWeek)); return; }
  const pickMonth = event.target.closest("[data-dp-pick-month]");
  if(pickMonth){ workbenchDpApply(workbenchMonthSchedule(pickMonth.dataset.dpPickMonth)); return; }
  const yearNav = event.target.closest("[data-dp-year]");
  if(yearNav){
    WB_DP.monthYear = (WB_DP.monthYear || workbenchDate(workbenchToday()).getFullYear()) + Number(yearNav.dataset.dpYear);
    workbenchDpRenderBody();
    return;
  }
  const weekPage = event.target.closest("[data-dp-week-page]");
  if(weekPage){
    const base = workbenchDate(WB_DP.weekBase || workbenchWeekKey(workbenchDate(workbenchToday())));
    base.setDate(base.getDate() + Number(weekPage.dataset.dpWeekPage) * 8 * 7);
    WB_DP.weekBase = workbenchWeekKey(base);
    workbenchDpRenderBody();
    return;
  }
  const preset = event.target.closest("[data-dp-preset]");
  if(preset){
    const kind = preset.dataset.dpPreset; // "today" | "week" | "month"
    const targetMode = kind === "today" ? "day" : kind;
    // 第一下只切视图（不套用、不关面板）；已经在那个视图上再点一下，才真正套用「今天/本周/本月」并关闭。
    if(WB_DP.mode !== targetMode){ WB_DP.mode = targetMode; workbenchDpRenderBody(); return; }
    workbenchDpApply(workbenchDpPresetSchedule(kind));
    return;
  }
  if(event.target.closest("[data-dp-clear]")){ workbenchDpApply(workbenchUnscheduled()); return; }
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
  const current = workbenchDpCurrentSchedule();
  WB_DP.baseWeek = workbenchDpSundayKey(current?.date || current?.week || (current?.month ? current.month+"-01" : workbenchToday()));
  // 有已有排期就直接落在对应模式（周排期打开就是周选择器，月排期打开就是月选择器），没有才落回日历。
  WB_DP.mode = current?.month ? "month" : current?.week ? "week" : "day";
  WB_DP.weekBase = current?.week || workbenchWeekKey(workbenchDate(workbenchToday()));
  WB_DP.monthYear = current?.month ? +current.month.slice(0, 4) : workbenchDate(workbenchToday()).getFullYear();
  const el = workbenchDpEnsureEl();
  el.style.display = "block";
  document.getElementById("wbDpBackdrop")?.classList.add("show");
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
  document.getElementById("wbDpBackdrop")?.classList.remove("show");
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
  if(!event.target.closest(".wb-swipe-actions")) wbCloseAllSwipes();
  // 多选模式下，点任务行内任何东西（完成勾选/日期徽标/父任务链接/展开子任务…）都先让位
  // 给"选中/取消"——不然手指点歪一点就跑去标完成或跳转，跟"进多选就是为了批量选"的
  // 意图冲突。真要单独编辑，先退出多选（右上角"完成"）。
  if(WB.multiSelectMode && wbIsTouchLike()){
    const row = event.target.closest("[data-task]");
    if(row){ wbEnterMultiSelect(row.dataset.task); return; }
  }
  if(event.target.closest("[data-workbench-win]")){ openWorkbenchStandalone(); return; }
  if(event.target.closest("[data-sidebar]")){ expandSidebarResponsive(); return; }
  if(event.target.closest("[data-toggle-notes]")){ wbToggleNotes(); return; }
  if(event.target.closest("[data-toggle-collapse-all]")){ wbToggleCollapseAll(); return; }
  const complete = event.target.closest("[data-complete]");
  if(complete){ toggleWorkbenchTask(complete.dataset.complete); return; }
  const toggleChildren = event.target.closest("[data-toggle-children]");
  if(toggleChildren){ toggleWorkbenchChildren(toggleChildren.dataset.toggleChildren); return; }
  const toggleAssignee = event.target.closest("[data-toggle-assignee]");
  if(toggleAssignee){ toggleWorkbenchAssignee(toggleAssignee.dataset.toggleAssignee, toggleAssignee.dataset.assigneeSet); return; }
  const dispatch = event.target.closest("[data-dispatch]");
  if(dispatch){ dispatchWorkbenchTask(dispatch.dataset.dispatch); return; }
  const addChild = event.target.closest("[data-add-child]");
  if(addChild){ openWorkbenchNewChild(addChild.dataset.addChild); return; }
  const gotoParent = event.target.closest("[data-goto-parent]");
  if(gotoParent){ gotoWorkbenchParent(gotoParent.dataset.gotoParent); return; }
  const sessionLink = event.target.closest("[data-session]");
  if(sessionLink){ const sid = sessionLink.dataset.session; if(typeof openConv === "function") openConv(sid); return; }
  const newAssignee = event.target.closest("[data-new-assignee]");
  if(newAssignee && WB.newTask){
    const next = newAssignee.dataset.assigneeSet || (WB.newTask.assignee === "ai" ? "human" : "ai");
    if(next !== WB.newTask.assignee){ WB.newTask.assignee = next; renderWorkbench(); wbFocusSoon("[data-new-title]"); }
    return;
  }
  const openDp = event.target.closest("[data-open-dp]");
  if(openDp){
    const [kind, id] = openDp.dataset.openDp.split(":");
    workbenchDpOpen(kind === "task" ? {kind:"task", id} : {kind:"new"}, openDp);
    return;
  }
  const openProjectPicker = event.target.closest("[data-open-project-picker]");
  if(openProjectPicker){ workbenchProjectPickerOpen(openProjectPicker); return; }
  const swipeDp = event.target.closest("[data-swipe-dp]");
  if(swipeDp){
    const tid = swipeDp.dataset.swipeDp;
    wbCloseAllSwipes();
    workbenchDpOpen({kind:"task", id:tid}, swipeDp);
    return;
  }
  const swipeMove = event.target.closest("[data-swipe-move]");
  if(swipeMove){
    const tid = swipeMove.dataset.swipeMove;
    const rect = swipeMove.getBoundingClientRect();
    wbCloseAllSwipes();
    workbenchOpenCtxMenu(rect.right, rect.top, [tid], "move");
    return;
  }
  const swipeDel = event.target.closest("[data-swipe-del]");
  if(swipeDel){
    const tid = swipeDel.dataset.swipeDel;
    wbCloseAllSwipes();
    deleteWorkbenchTask(tid);
    return;
  }
  const gotoProject = event.target.closest("[data-goto-project]");
  if(gotoProject){
    clearTimeout(wbClickTimer);
    WB.view = "project"; WB.project = gotoProject.dataset.gotoProject;
    workbenchResetChildExpansion();
    WB.newTask=null; WB.editorTaskId=null; wbResetSelection();
    renderWorkbench(); refreshWorkbenchDataIfStale();
    return;
  }
  const deleteProject = event.target.closest("[data-delete-project]");
  if(deleteProject){
    const id = deleteProject.dataset.deleteProject;
    const project = workbenchProject(id);
    if(project && confirm('删除「'+project.name+'」这个分组？名下任务不会被删除，只是这个分组不再显示。')) archiveWorkbenchProject(id);
    return;
  }
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
  // 新建卡片内部（标题输入框、备注、日期、项目下拉）保留原生交互，不走下面的
  // 「点击空白处收起」逻辑——那些点击都已经在上面按具体 data-* 处理过或本就该正常触发。
  if(event.target.closest("[data-new-card]")) return;
  const taskRow = event.target.closest("[data-task]");
  if(taskRow){
    // 刚滑动过的行松手后会触发一次 click，吞掉它避免打开详情。
    if(taskRow._wbJustSwiped){ taskRow._wbJustSwiped = false; return; }
    const taskId = taskRow.dataset.task;
    if(taskId === WB.editorTaskId) return;
    // 移动端：详情卡（或新建卡）开着的时候点别的任务，只收起当前卡，不直接跳到点的
    // 那条——不然手滑很容易连续切换详情。想看别的任务，先收起再点一次。
    if(wbIsTouchLike() && (WB.editorTaskId || WB.newTask)){
      clearTimeout(wbClickTimer); wbClickTimer = null;
      workbenchFinishActiveCard();
      return;
    }
    // 任务切换的选中状态还要保留 250ms 的双击判定，但收起当前卡片不能等它：
    // 否则点击别的任务后，卡片会明显晚半拍才开始收起。
    workbenchFinishActiveCard();
    if(event.shiftKey && WB.selectAnchor && WB.selectAnchor !== taskId){
      clearTimeout(wbClickTimer); wbClickTimer = null;
      workbenchSelectRange(WB.selectAnchor, taskId);
      return;
    }
    if(wbIsTouchLike()){
      clearTimeout(wbClickTimer); wbClickTimer = null;
      openWorkbenchEditor(taskId);
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
  if(project){ clearTimeout(wbClickTimer); WB.project = project.dataset.project; workbenchResetChildExpansion(); WB.newTask=null; WB.editorTaskId=null; wbResetSelection(); renderWorkbench(); refreshWorkbenchDataIfStale(); return; }
  const view = event.target.closest("[data-view]");
  if(view){
    clearTimeout(wbClickTimer);
    WB.view = view.dataset.view;
    if(WB.view !== "project") WB.project = "all"; // 项目筛选只在「项目」tab 下有意义，切走就复位
    WB.newTask=null; WB.editorTaskId=null; wbResetSelection(); workbenchResetChildExpansion();
    renderWorkbench();
    if(WB.view === "trash") refreshWorkbenchTrashIfStale();
    else refreshWorkbenchDataIfStale();
    return;
  }
  const restoreBtn = event.target.closest("[data-restore-task]");
  if(restoreBtn){ workbenchRestoreTask(restoreBtn.dataset.restoreTask); return; }
  const purgeBtn = event.target.closest("[data-purge-task]");
  if(purgeBtn){ workbenchPurgeTask(purgeBtn.dataset.purgeTask); return; }
  if(event.target.closest("[data-empty-trash]")){ workbenchEmptyTrash(); return; }
  const trashRow = event.target.closest("[data-trash-task]");
  if(trashRow){
    const id = trashRow.dataset.trashTask;
    WB_TRASH.expandedId = WB_TRASH.expandedId === id ? null : id;
    renderWorkbench();
    return;
  }
  const nav = event.target.closest("[data-nav]");
  if(nav){ shiftWorkbenchDate(Number(nav.dataset.nav)); return; }
  if(event.target.closest("[data-today]")){ WB.anchor = workbenchToday(); workbenchResetChildExpansion(); renderWorkbench(); return; }
  // 点击空白处收起当前展开的卡片（新建卡或编辑卡）。
  workbenchFinishActiveCard();
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
  if(WB.multiSelectMode) return; // 多选模式下双击也不该跳去开详情，交互目的就是选中/取消
  if(event.target.closest("[data-complete],[data-toggle-children]")) return;
  const taskRow = event.target.closest("[data-task]");
  if(!taskRow) return;
  if(wbClickTimer){ clearTimeout(wbClickTimer); wbClickTimer = null; }
  const taskId = taskRow.dataset.task;
  if(taskId === WB.editorTaskId) return;
  openWorkbenchEditor(taskId);
});

let wbDragTaskId = null;

function workbenchClearDropIndicators(){
  document.querySelectorAll(".wb-task-drop-before,.wb-task-drop-after,.wb-task-drop-nest").forEach(el => el.classList.remove("wb-task-drop-before", "wb-task-drop-after", "wb-task-drop-nest"));
}

function workbenchDropMode(dragged, target, clientY, rect){
  if(target.parentId){
    if(workbenchTaskIsAncestor(dragged.id, target.parentId)) return "noop";
    return dragged.parentId === target.parentId ? "reorder" : "nest-child";
  }
  const center = clientY >= rect.top + rect.height * .25 && clientY <= rect.bottom - rect.height * .25;
  if(center) return dragged.parentId === target.id ? "noop" : "nest";
  if(dragged.parentId) return "promote";
  return "reorder";
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
  if(!dragged || !target) return;
  const rect = row.getBoundingClientRect();
  const mode = workbenchDropMode(dragged, target, event.clientY, rect);
  workbenchClearDropIndicators();
  if(mode === "noop") return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "move";
  if(mode === "nest") row.classList.add("wb-task-drop-nest");
  else row.classList.toggle("wb-task-drop-before", event.clientY < rect.top + rect.height / 2);
  if(mode !== "nest") row.classList.toggle("wb-task-drop-after", event.clientY >= rect.top + rect.height / 2);
});

$("#workbenchView").addEventListener("drop", event => {
  if(!wbDragTaskId) return;
  const row = event.target.closest("[data-task]");
  workbenchClearDropIndicators();
  if(!row || row.dataset.task === wbDragTaskId){ wbDragTaskId = null; return; }
  const taskId = wbDragTaskId; wbDragTaskId = null;
  const dragged = workbenchTask(taskId);
  const target = workbenchTask(row.dataset.task);
  if(!dragged || !target) return;
  event.preventDefault();
  const rect = row.getBoundingClientRect();
  const mode = workbenchDropMode(dragged, target, event.clientY, rect);
  if(mode === "noop") return;
  if(mode === "nest"){ workbenchNestTask(taskId, target.id); return; }
  const before = event.clientY < rect.top + rect.height / 2;
  if(mode === "nest-child"){ workbenchNestTask(taskId, target.parentId, target.id, before); return; }
  if(mode === "promote"){ workbenchPromoteTask(taskId, target.id, before); return; }
  const orderBefore = WB_DATA.tasks.slice();
  if(workbenchReorderTask(taskId, target.id, before)) workbenchPersistTaskPlacement(dragged, dragged.parentId, target.project, orderBefore);
});

$("#workbenchView").addEventListener("dragend", event => {
  event.target.closest("[data-task]")?.classList.remove("wb-task-dragging");
  workbenchClearDropIndicators();
  wbDragTaskId = null;
});

// ── 移动端新建任务 FAB ───────────────────────────────────────────────────
// 触屏没有原生 HTML5 拖拽（上面那一整套 dragstart/dragover/drop 靠鼠标），FAB 自己
// 用 Pointer Events 实现一套最小的拖拽：按住不动一段距离内算「拖拽」，松手时看落在
// 哪条任务行的哪个区——中间一段落成子任务，上/下沿插到那条前面/后面；没有明显位移
// 就当一次普通点击，插到当前视图最上方。只在触屏设备上出现（见 styles.css 的 hover:none）。
const WB_FAB = {pointerId:null, dragging:false, startX:0, startY:0};

function wbFabClearDrop(){
  document.querySelectorAll(".wb-task-drop-before,.wb-task-drop-after,.wb-task-drop-nest").forEach(el => el.classList.remove("wb-task-drop-before", "wb-task-drop-after", "wb-task-drop-nest"));
}

// FAB 跟手移动时自己正好挡在指尖下面，直接 elementFromPoint 只会摸到自己——量之前先让它对点击透明。
function wbFabElementUnder(clientX, clientY, dragEl){
  dragEl.style.pointerEvents = "none";
  const el = document.elementFromPoint(clientX, clientY);
  dragEl.style.pointerEvents = "";
  return el;
}

// 行的中间一半判定成「落到这条任务上」（新建子任务），跟桌面原生拖拽的 nest 判定
// （workbenchDropMode）用同一套 25%/75% 分界，手感一致。
function wbFabUpdateTarget(clientX, clientY, dragEl){
  wbFabClearDrop();
  const el = wbFabElementUnder(clientX, clientY, dragEl);
  // 匹配顶层任务和子任务（子任务在 .wb-children 下，不是 .wb-task-list 的直接子元素）
  const row = el?.closest?.("#workbenchView [data-task]:not(.wb-task-card)");
  if(!row) return null;
  const rect = row.getBoundingClientRect();
  const relY = (clientY - rect.top) / rect.height;
  const task = workbenchTask(row.dataset.task);
  const isChild = !!(task && task.parentId);
  const mode = relY < .25 ? "before" : relY > .75 ? "after" : "nest";
  row.classList.toggle("wb-task-drop-before", mode === "before");
  row.classList.toggle("wb-task-drop-after", mode === "after");
  row.classList.toggle("wb-task-drop-nest", mode === "nest");
  return {row, mode, isChild};
}

// 顶层任务行之间偶尔夹着自己的子任务块（.wb-children），跳过去才是下一个真正的兄弟任务。
function wbFabNextTopLevelId(row){
  let el = row.nextElementSibling;
  if(el && el.classList.contains("wb-children")) el = el.nextElementSibling;
  return (el && el.matches("[data-task]")) ? el.dataset.task : null;
}

function wbFabRowProject(row){
  const task = workbenchTask(row.dataset.task);
  if(!task) return null;
  return workbenchIsRealProject(task.project) ? workbenchProject(task.project) : WB_UNASSIGNED_PROJECT;
}

function workbenchFabAddAtTop(){
  if(WB.view === "completed" || WB.view === "trash") return;
  const project = WB.project === "all" ? WB_DATA.projects[0] : workbenchProject(WB.project);
  if(!project){ alert("请先新建一个项目。"); return; }
  const block = document.querySelector('[data-group="'+CSS.escape(workbenchGroupId(project))+'"]');
  const firstRow = block?.querySelector(".wb-task-list > [data-task]");
  workbenchOpenNewTaskBefore(project, firstRow?.dataset.task || null);
}

function workbenchFabDropAt(clientX, clientY, dragEl){
  const hit = wbFabUpdateTarget(clientX, clientY, dragEl);
  wbFabClearDrop();
  if(!hit) return;
  if(hit.mode === "nest"){ openWorkbenchNewChild(hit.row.dataset.task, null, {docked:true}); return; }
  // 落在子任务之间：在同一父任务下插入新子任务
  if(hit.isChild){
    const task = workbenchTask(hit.row.dataset.task);
    if(!task || !task.parentId) return;
    let siblingId = null;
    if(hit.mode === "after"){
      siblingId = task.id;
    }else{
      // before：找到前一个兄弟，插在它后面；没有前兄弟就插到子任务列表顶部（siblingId=null）
      const siblings = workbenchChildren(task.parentId);
      const idx = siblings.findIndex(s => s.id === task.id);
      if(idx > 0) siblingId = siblings[idx - 1].id;
    }
    openWorkbenchNewChild(task.parentId, siblingId, {docked:true});
    return;
  }
  const project = wbFabRowProject(hit.row);
  if(!project) return;
  const beforeTaskId = hit.mode === "before" ? hit.row.dataset.task : wbFabNextTopLevelId(hit.row);
  workbenchOpenNewTaskBefore(project, beforeTaskId);
}

(function(){
  const fab = $("#wbFab");
  if(!fab) return;
  // 自己拦下 click：松手那一刻已经在 pointerup 里处理完了，浏览器紧接着补发的原生
  // click 冒泡到 #workbenchView 会被最后那句「点空白处收起当前卡片」当场把新建卡关掉。
  fab.addEventListener("click", event => event.stopPropagation());
  fab.addEventListener("pointerdown", event => {
    if(event.button !== 0 && event.pointerType === "mouse") return;
    WB_FAB.pointerId = event.pointerId;
    WB_FAB.dragging = false;
    WB_FAB.startX = event.clientX; WB_FAB.startY = event.clientY;
    fab.setPointerCapture(event.pointerId);
  });
  fab.addEventListener("pointermove", event => {
    if(event.pointerId !== WB_FAB.pointerId) return;
    const dx = event.clientX - WB_FAB.startX, dy = event.clientY - WB_FAB.startY;
    if(!WB_FAB.dragging && Math.hypot(dx, dy) > 10){
      WB_FAB.dragging = true;
      fab.classList.add("wb-fab-dragging");
    }
    if(WB_FAB.dragging){
      fab.style.transform = "translate("+dx+"px,"+(dy-36)+"px)";
      wbFabUpdateTarget(event.clientX, event.clientY, fab);
    }
  });
  fab.addEventListener("pointerup", event => {
    if(event.pointerId !== WB_FAB.pointerId) return;
    WB_FAB.pointerId = null;
    fab.classList.remove("wb-fab-dragging");
    fab.style.transform = "";
    if(WB_FAB.dragging){ WB_FAB.dragging = false; workbenchFabDropAt(event.clientX, event.clientY, fab); }
    else workbenchFabAddAtTop();
    wbFabClearDrop();
  });
  fab.addEventListener("pointercancel", () => {
    WB_FAB.pointerId = null; WB_FAB.dragging = false;
    fab.classList.remove("wb-fab-dragging");
    fab.style.transform = "";
    wbFabClearDrop();
  });
})();

// 移动端多选：底部工具栏 + 右上角"完成"，右滑进入见 wbEnterMultiSelect。两个元素是
// 固定在 #workbenchView 外层的静态 DOM（跟 #wbFab/#wbDock 同一套模式），不会被
// renderWorkbench() 的重绘冲掉，只靠 wbRenderMultiSelectBar() 切 hidden/文案。
(function(){
  const done = document.getElementById("wbMultiDone");
  const bar = document.getElementById("wbMultiBar");
  if(!done || !bar) return;
  bar.innerHTML = '<button type="button" data-multi="date">'+ic("calendar")+'<span>日期</span></button>'+
    '<button type="button" data-multi="move">'+ic("folder")+'<span>移动</span></button>'+
    '<button type="button" data-multi="more">'+ic("more")+'<span>更多</span></button>'+
    '<button type="button" class="wb-multi-danger" data-multi="delete">'+ic("trash")+'<span>删除</span></button>';
  done.addEventListener("click", () => wbExitMultiSelect());
  bar.addEventListener("click", event => {
    const btn = event.target.closest("[data-multi]");
    if(!btn || btn.disabled) return;
    const ids = [...WB.selected];
    if(!ids.length) return;
    const rect = btn.getBoundingClientRect();
    const action = btn.dataset.multi;
    if(action === "date") workbenchDpOpenAt({kind:"ctx", ids}, rect.left, rect.top);
    else if(action === "move") workbenchOpenCtxMenu(rect.left, rect.top, ids, "move");
    else if(action === "more") workbenchOpenCtxMenu(rect.left, rect.top, ids);
    else if(action === "delete") workbenchBatchDelete(ids);
  });
})();

// docked 新建卡（#wbDock）跟着键盘走：键盘弹起来时 visualViewport 会变矮，两者的差
// 就是键盘（+浏览器地址栏之类）占掉的高度，把面板贴在这段之上。空的时候 CSS :empty
// 直接不占地方，这里更新 bottom 不会有任何可见影响。
// 键盘被收起（不管是键盘自己的收起键、切到别的 app 再回来，还是别的手势，不只是点
// 空白）也当一次「完成」：不然浮层没了键盘撑着，孤零零贴在屏幕最下面，像卡死了。
(function(){
  const dock = document.getElementById("wbDock");
  if(!dock || !window.visualViewport) return;
  const vv = window.visualViewport;
  let wasKeyboardOpen = false;
  const update = () => {
    const gap = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
    dock.style.bottom = (gap + 12) + "px";
    const keyboardOpen = gap > 80;
    if(wasKeyboardOpen && !keyboardOpen && (WB.newTask?.docked || WB.editorDocked)) workbenchFinishActiveCard();
    wasKeyboardOpen = keyboardOpen;
  };
  vv.addEventListener("resize", update);
  vv.addEventListener("scroll", update);
  update();
})();

$("#workbenchView").addEventListener("focusin", event => {
  const taskId = event.target.dataset.editTitle || event.target.dataset.editDetail;
  const field = event.target.dataset.editTitle ? "title" : event.target.dataset.editDetail ? "detail" : null;
  const task = workbenchTask(taskId);
  if(task && field) WB.editSnapshots.set(taskId+":"+field, task[field]);
  // 编辑卡内聚焦时锁住滚动位置：浏览器会跨多帧持续调整滚动（键盘动画期间），
  // 单次 rAF 来不及，改为用 scroll 事件在 500ms 内持续把位置钉回去。
  if(event.target.closest(".wb-task-card")){
    const sv = document.getElementById("workbenchView");
    if(sv){
      const x = sv.scrollLeft, y = sv.scrollTop;
      const lock = () => { sv.scrollLeft = x; sv.scrollTop = y; };
      sv.addEventListener("scroll", lock);
      setTimeout(() => sv.removeEventListener("scroll", lock), 500);
      lock();
    }
  }
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

// 标题/备注失焦时才落库，避免每敲一个字都打一次 API；一次编辑字段记作一条撤销记录。
$("#workbenchView").addEventListener("focusout", event => {
  const titleId = event.target.dataset.editTitle;
  const detailId = event.target.dataset.editDetail;
  const taskId = titleId || detailId;
  const field = titleId ? "title" : detailId ? "detail" : null;
  if(!taskId || !field) return;
  const task = workbenchTask(taskId);
  const snapshotKey = taskId+":"+field;
  const previous = WB.editSnapshots.get(snapshotKey);
  WB.editSnapshots.delete(snapshotKey);
  if(!task || previous === undefined || previous === task[field]) return;
  const before = {[field]:previous};
  const after = {[field]:task[field]};
  workbenchPersistTaskChange(taskId, before, after);
});

$("#workbenchView").addEventListener("paste", event => {
  const taskId = event.target.dataset.editDetail;
  const files = [...(event.clipboardData?.items||[])].map(item => item.getAsFile()).filter(Boolean);
  if(!taskId || !files.some(file => file.type.startsWith("image/"))) return;
  event.preventDefault(); addWorkbenchImages(taskId, files);
});

document.addEventListener("keydown", event => {
  if($("#workbenchView").hidden) return;
  if(event.metaKey && !event.ctrlKey && !event.altKey && event.key.toLowerCase() === "z"){
    // 文本框保留浏览器原生的逐字撤销；其他工作台操作才走任务历史。
    if(event.target.closest("input,textarea,select,[contenteditable='true']")) return;
    event.preventDefault();
    if(event.shiftKey) workbenchRedo(); else workbenchUndo();
    return;
  }
  // 中文等输入法组词时 Enter 只用于确认候选词，不能当作「完成任务」。
  // 部分浏览器在 compositionend 前会把该键报成 229，两个条件都要排除。
  if(event.key === "Enter" && !event.isComposing && event.keyCode !== 229 && event.target.matches("[data-new-title],[data-edit-title]")){
    event.preventDefault(); workbenchFinishActiveCard(); return;
  }
  // 选中任务卡（非编辑态）时按回车：不管选中的是顶层任务还是子任务，都在它下面新建子任务。
  if(event.key === "Enter" && !event.isComposing && event.keyCode !== 229 && !event.metaKey && !event.ctrlKey && !event.altKey && !event.shiftKey && !WB.newTask && !WB.editorTaskId){
    if(event.target.closest("input,textarea,select,button,[contenteditable='true']")) return;
    if(WB.selected.size !== 1) return;
    event.preventDefault(); workbenchOpenChildOfSelected(); return;
  }
  if(event.key === "Escape"){
    if(WB_DP.open){ workbenchDpClose(); return; }
    if(workbenchCtxMenuOpen()){ workbenchCloseCtxMenu(); return; }
    if(workbenchProjectPickerIsOpen()){ workbenchProjectPickerClose(); return; }
    if(WB.newTask){ WB.newTask=null; if(!workbenchRemoveNewTaskCard()) renderWorkbench(); }
    else if(WB.editorTaskId){
      const id = WB.editorTaskId;
      const wasDocked = WB.editorDocked;
      WB.editorTaskId = null;
      WB.editorDocked = false;
      if(wasDocked || !workbenchMorphTask(id)) renderWorkbench();
    }
    return;
  }
  if((event.key === "Delete" || event.key === "Backspace") && !event.metaKey && !event.ctrlKey && !event.altKey){
    if(event.target.closest("input,textarea,select,button,[contenteditable='true']")) return;
    if(WB.selected.size){ event.preventDefault(); workbenchBatchDelete([...WB.selected]); return; }
    if(WB.editorTaskId){ event.preventDefault(); deleteWorkbenchTask(WB.editorTaskId); return; }
  }
  if(event.code !== "Space" || event.ctrlKey || event.metaKey || event.altKey) return;
  if(event.target.closest("input,textarea,select,button,[contenteditable='true']")) return;
  if(event.shiftKey){
    if(WB.view === "completed" || WB.view === "trash" || WB.selected.size !== 1) return;
    const task = workbenchTask([...WB.selected][0]);
    if(!task) return;
    event.preventDefault();
    openWorkbenchNewChild(task.parentId || task.id, task.parentId ? task.id : null);
    return;
  }
  if(WB.view === "completed" || WB.view === "trash") return; // 这两个视图没有项目分组，插不进新建卡
  event.preventDefault(); openWorkbenchNewTask();
});

// ── 通用"选择浮层"背景层：桌面端锚定悬浮，移动端底部弹出。ctx 菜单和项目选择器
// 共用同一个 backdrop——桌面端透明不挡点击，移动端点它关闭当前打开的那一个。
function wbSheetBackdrop(){
  let el = document.getElementById("wbSheetBackdrop");
  if(!el){
    el = document.createElement("div");
    el.id = "wbSheetBackdrop";
    document.body.appendChild(el);
    el.addEventListener("click", () => {
      if(workbenchCtxMenuOpen()) workbenchCloseCtxMenu();
      workbenchProjectPickerClose();
    });
  }
  return el;
}

// ── 底部弹层的下滑关闭手势：只在移动端底部弹出态生效（桌面端面板是锚定悬浮，不装
// 这套）。抓手（.wb-sheet-handle）是唯一的拖拽触发区，跟手指走；松手过阈值就关，
// 没过就弹回原位——面板本体（日历/周/月三种 mode、ctx 菜单、项目选择器）不用各自
// 实现一遍，靠"关掉当前这块 id 对应的浮层"这一个统一出口分发。
function wbSheetCloseByEl(el){
  if(el.id === "wbDatePicker") workbenchDpClose();
  else if(el.id === "wbCtxMenu") workbenchCloseCtxMenu();
  else if(el.id === "wbProjectPicker") workbenchProjectPickerClose();
}

(function(){
  let dragEl = null, startY = 0, pointerId = null;
  function isMobileSheet(){ return window.matchMedia("(max-width:760px)").matches; }
  document.addEventListener("pointerdown", event => {
    const handle = event.target.closest(".wb-sheet-handle");
    if(!handle || !isMobileSheet()) return;
    dragEl = handle.closest(".wb-dp, .wb-ctx-menu");
    if(!dragEl) return;
    startY = event.clientY;
    pointerId = event.pointerId;
    dragEl.style.transition = "none";
    handle.setPointerCapture?.(pointerId);
  });
  document.addEventListener("pointermove", event => {
    if(!dragEl || event.pointerId !== pointerId) return;
    const dy = Math.max(0, event.clientY - startY);
    dragEl.style.transform = "translateY("+dy+"px)";
  });
  function endDrag(event){
    if(!dragEl || event.pointerId !== pointerId) return;
    const dy = Math.max(0, event.clientY - startY);
    const el = dragEl;
    el.style.transition = ""; el.style.transform = "";
    if(dy > 70) wbSheetCloseByEl(el);
    dragEl = null; pointerId = null;
  }
  document.addEventListener("pointerup", endDrag);
  document.addEventListener("pointercancel", endDrag);
})();

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
    '<button type="button" data-ctx="cancel">标记为已取消</button>'+
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
  el.innerHTML = '<div class="wb-sheet-handle" aria-hidden="true"></div>'+workbenchCtxMenuHtml();
  return el;
}

function workbenchOpenCtxMenu(x, y, ids, mode){
  wbCtxIds = ids;
  wbCtxMode = mode || "root";
  const el = workbenchRenderCtxMenu();
  el.style.display = "block";
  el.style.left = x+"px"; el.style.top = y+"px";
  wbSheetBackdrop().classList.add("show");
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
  if(!workbenchProjectPickerIsOpen()) wbSheetBackdrop().classList.remove("show");
}

function workbenchHandleCtxClick(event){
  // 移动端从底部多选工具栏弹出这个菜单时，选区其实已经"用掉了"——菜单里的操作
  // 一执行完就该退出多选，不然工具栏和高亮会孤零零地留在已经处理完的任务上。
  const exitMultiIfNeeded = () => { if(WB.multiSelectMode) wbExitMultiSelect(); };
  const moveTo = event.target.closest("[data-ctx-move]");
  if(moveTo){
    workbenchBatchMove(wbCtxIds, moveTo.dataset.ctxMove);
    workbenchCloseCtxMenu();
    exitMultiIfNeeded();
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
  if(action === "done"){ workbenchBatchComplete(wbCtxIds); workbenchCloseCtxMenu(); exitMultiIfNeeded(); return; }
  if(action === "cancel"){ workbenchBatchCancel(wbCtxIds); workbenchCloseCtxMenu(); exitMultiIfNeeded(); return; }
  if(action === "set-ai" || action === "set-human"){
    const assignee = action === "set-ai" ? "ai" : "human";
    wbCtxIds.forEach(id => { const t = workbenchTask(id); if(t){ const prev = t.assignee; t.assignee = assignee; persistWorkbenchTask(id, {assignee}, {assignee: prev}); }});
    workbenchCloseCtxMenu(); renderWorkbench(); exitMultiIfNeeded(); return;
  }
  if(action === "delete"){ workbenchBatchDelete(wbCtxIds); workbenchCloseCtxMenu(); exitMultiIfNeeded(); return; }
}

// 用 mousedown 而不是 click 来判定「点了菜单外面」：右键弹出菜单本身就是由一次
// mousedown+contextmenu 触发的，如果监听 click 来关闭，某些浏览器会在 contextmenu
// 后紧跟着补一个 click，把刚弹出的菜单立刻关掉——mousedown 没有这个时序问题。
document.addEventListener("mousedown", event => {
  if(!workbenchCtxMenuOpen()) return;
  if(event.target.closest("#wbCtxMenu")) return;
  workbenchCloseCtxMenu();
});

// ── 新建任务的项目选择：原生 <select> 换成跟右键菜单同款的浮层——桌面端锚定在
// 触发按钮旁，移动端走底部弹出。复用 .wb-ctx-menu 的样式和交互，只是列表内容换成项目名。
function workbenchProjectPickerIsOpen(){
  const el = document.getElementById("wbProjectPicker");
  return !!el && el.style.display !== "none";
}

function workbenchProjectPickerHtml(){
  return [...WB_DATA.projects, WB_UNASSIGNED_PROJECT].map(p =>
    '<button type="button" data-pp="'+esc(p.id)+'">'+esc(p.name)+'</button>'
  ).join("");
}

function workbenchProjectPickerOpen(anchorEl){
  if(!WB.newTask) return;
  let el = document.getElementById("wbProjectPicker");
  if(!el){
    el = document.createElement("div");
    el.id = "wbProjectPicker";
    el.className = "wb-ctx-menu";
    el.style.display = "none";
    document.body.appendChild(el);
    el.addEventListener("click", event => {
      const btn = event.target.closest("[data-pp]");
      if(!btn || !WB.newTask) return;
      WB.newTask.project = btn.dataset.pp;
      workbenchProjectPickerClose();
      renderWorkbench();
    });
  }
  el.innerHTML = '<div class="wb-sheet-handle" aria-hidden="true"></div>'+workbenchProjectPickerHtml();
  el.style.display = "block";
  wbSheetBackdrop().classList.add("show");
  const rect = anchorEl.getBoundingClientRect();
  el.style.left = rect.left+"px"; el.style.top = (rect.bottom+6)+"px";
  requestAnimationFrame(() => {
    const w = el.getBoundingClientRect();
    let left = rect.left, top = rect.bottom+6;
    if(left + w.width > window.innerWidth - 4) left = window.innerWidth - w.width - 4;
    if(top + w.height > window.innerHeight - 4) top = rect.top - w.height - 6;
    el.style.left = Math.max(4, left)+"px"; el.style.top = Math.max(4, top)+"px";
  });
}

function workbenchProjectPickerClose(){
  const el = document.getElementById("wbProjectPicker");
  if(el) el.style.display = "none";
  if(!workbenchCtxMenuOpen()) wbSheetBackdrop().classList.remove("show");
}

document.addEventListener("mousedown", event => {
  if(!workbenchProjectPickerIsOpen()) return;
  if(event.target.closest("#wbProjectPicker") || event.target.closest("[data-open-project-picker]")) return;
  workbenchProjectPickerClose();
});

window.openWorkbench = openWorkbench;
window.closeWorkbench = closeWorkbench;
