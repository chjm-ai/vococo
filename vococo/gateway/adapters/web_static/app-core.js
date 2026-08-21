"use strict";
// 2026-08-14 从 index.html 拆出(前端模块化):全局状态 S / 图标 / $,el,esc 工具 + 主题 / IndexedDB / api() / 登录。
// 与内联脚本同属全局作用域(无构建步骤),加载顺序见 index.html。

// ── 状态 ────────────────────────────────────────────────────────────────
const S = {
  token: localStorage.getItem("vococo_token") || "",
  // 工作台独立窗口(见 workbench.js 的 wb-win-btn),登录后直接落地工作台、跳过通话视图。
  // 走独立路径 /workbench 而不是 "/?view=workbench"——sw.js 的离线缓存只按 pathname
  // 白名单 "/",query 串不影响命中,会被当成 "/" 走网络优先超时退回缓存那条特殊逻辑,
  // 全新 URL 缓存未命中时直接 503,慢网络下独立窗口会打开一片空白(见 web.py 路由注释)。
  standaloneWorkbench: location.pathname === "/workbench/window",
  surface: "chat",  // chat | call | workbench：当前主视图，仅供前端切换与侧栏高亮
  conv: "main",
  callReturnConv: "main", // 进入主会话前的普通会话;挂断后恢复,避免刷新错读语音会话历史
  convs: [],            // [{conv,title,turns,last_ts,pinned}]
  searchConvs: [],      // 搜索打开但不在当前筛选侧栏内的会话,供标题栏和操作菜单继续引用
  projects: [],         // [{hash,name,path,last_used}]
  project: null,        // 「活跃」项目 hash(顶部 ＋新对话 建在此;随打开的会话联动);null=默认项目
  expanded: new Set(),  // 已展开的分组:默认项目用 "__default__",项目用其 hash
  moreShown: new Set(), // 点过「展开更多」的分组(键同 expanded):折叠该组或刷新页面即清空,回到默认 5 条
  sideTab: localStorage.getItem("vococo_sidetab") || "projects",  // 侧栏「项目/置顶/最近」Tab,记住上次选择
  tabShown: {pinned: 20, recent: 20},  // 「置顶」「最近」两个 Tab 各自已展示的条数,点「更多」每次 +20
  tabLastFetch: {},     // 侧栏 Tab 各自上次发起刷新请求的时间戳(ms),点击 Tab 时按此节流(见 sidebar.js refreshSideTabIfStale)
  browseDir: "",        // 目录浏览器当前所在目录
  images: [],           // {data,media_type,url}
  audios: [],           // {id,filename,text,mediaType,url,status}:status = uploading|done|error
  files: [],            // {id,filename,mediaType,status}:通用文件，类型不设前端白名单
  composerAttachments: {}, // conv → {images,audios,files}:未发送附件仅在当前页面内按会话隔离
  docPreview: {},       // conv → {kind,target,title,highlight}:文档预览面板按会话独立记忆,
                         // 切会话时据此显示/隐藏并重新渲染(见 openConv、markdown.js 的 openDocPreview/closeDocPreview)
  stream: null,         // 当前正在流式的 assistant 气泡 DOM 引用
  localSent: false,     // 本客户端刚发了消息,收到 "user" 事件时跳过(避免重复气泡)
  es: null,
  lastId: 0,            // 已处理的最大事件编号,用于断线重连后去重
  bootId: null,         // 服务端进程启动标识,变了说明重启过,断线补发这条路救不回来,得整体核对
  autoResumed: {},      // conv → bool:重启中断的回复已自动触发过继续生成(每会话一次,防重复)
  models: {default:"", choices:[], efforts:{}},  // /models 拉到的可选模型及各模型思考深度
  model: "",            // 当前会话使用的模型(空=用默认)
  commands: [],          // /commands 拉到的系统斜杠命令清单 [{name,desc}]
  skills: [],            // /commands 拉到的已启用 skill 清单 [{name,desc}]
  cmd: {open:false, items:[], active:0},  // 斜杠命令菜单当前状态
  git: null,            // 当前项目会话的 git 状态(/conv/git 返回);非项目会话为 null
  histCache: {},        // conv → turns[]:切会话时先渲缓存,消除空屏闪烁
  turnEventsCache: {},  // turn_id → 完整 events[]:工具卡片懒加载,点开某轮任意卡片后整轮缓存复用
  pendingChoice: {},    // conv → choice 事件:审批发给非当前会话时暂存,切回该会话再渲染
  live: {},             // conv → 进行中回合的事件缓冲:任意会话(含后台)都记录,切回时重放重建流式气泡
  histLoading: null,    // openConv 正在拉 /history 的会话名:该窗口内 SSE 流式事件只缓冲不建气泡,
                         // 防止和 /history 里的草稿气泡各建一个、切会话瞬间闪出重复内容(见 openConv/handleEvent)
  streamSnap: {},       // conv → {state,asOf}:离开时摘下的流式气泡 DOM + 已处理到的事件下标,
                         // 切回来同一进度直接接上,不用把 live 缓冲从头重放一遍(见 maybeReplayStream)
  pendingReview: {},    // conv → bool:本地缓存会话是否有未读完成内容(避免灰点闪烁)
  pending: {},          // conv → 待发送队列 [{id,text,images}]:上一个任务没结束时点发送,暂存,任务完成自动发
  pendId: 0,            // 待发送项自增 id,供删除定位
  voiceRec: {},         // conv → {bubble}:语音转写中的占位气泡,按录音发起时所属会话记录,切走再切回能补出来
  convFilter: "all",
  rateStatus: {level:"ok", msg:"", lastErr:"", ts:0},  // ok|warn|err, 更新侧栏底部状态点
  serviceState: "syncing", // syncing|online|cached|offline：缓存画面不能伪装成在线数据
  dragSrc: null,        // 项目拖拽排序:正在被拖起的项目 hash
  dragMoved: false,     // 刚发生过一次拖拽落地,下一次 click 吞掉(不触发分组展开/收起)
  voiceSidebar: {main:null, tasks:[]},  // /voice/sidebar 拉到的"语音任务"固定分组数据
  voiceSidebarLoaded: false, // /voice/sidebar 是否已拉过(未拉时侧边栏「语音通话」行显示骨架占位,见 buildVoiceMainRow)
  cronJobs: [],         // /cron/sidebar 拉到的定时任务列表 [{conv,job_id,title,schedule_desc,enabled,last_status,...}]
  cronEditId: null,     // 定时任务表单当前在编辑哪个 job_id;null=新建模式
  cronGroups: {         // 「定时」Tab 两类任务分组的折叠状态,默认展开 VOCOCO、收起本机任务
    managed: localStorage.getItem("vococo_cron_group_managed") !== "0",
    system: localStorage.getItem("vococo_cron_group_system") === "1"
  },
  systemTasks: [],      // /system/tasks 拉到的本机系统任务列表(launchd/crontab,只读,见 cron/system_tasks.py)
  systemHostname: "",   // /system/tasks 返回的当前机器名,标在「本机系统任务」区块标题上
  swipedConv: null,     // 当前左滑展开的会话 id;renderConvs 重建 DOM 时靠它补回 swiped 状态,
                         // 否则任意会话(含后台)一完成回合就会触发 loadConvs→renderConvs 把菜单冲掉
  omniEnabled: false,   // /voice/config 的 omni_enabled(免提 Omni 管线开关),登录后预取好,
                         // 避免每次进通话视图都要等一趟网络往返才知道走哪条路径
  vadThreshold: 0.7,    // /voice/config 的 vad_threshold/vad_silence_ms,兜底默认跟
  vadSilenceMs: 2500,   // config.py 的 VOICE_OMNI_VAD_SILENCE_MS 默认值一致;2026-07-22 800 触发对话
                         // 全面错位(正常语流停顿就判停,腰斩链路高频触发),回调到 2500 折中
  omniSafetyMs: 180000, // /voice/config 的 safety_ms,兜底默认跟 config.py 的
                         // VOICE_OMNI_SAFETY_MS 一致(180s 长口述安全网,2026-08-10 起)
  omniVoice: "Serena",  // /voice/config 的 omni_voice,Omni 出声模式 session.update 的音色
                         // (注意跟 Qwen-TTS 音色表不通用,Cherry 会 400,见 config.py)
};
// 顶部任务条与侧边栏的停止/隐藏逻辑共用；不能放进通话 IIFE，否则侧边栏重绘会
// 因找不到 barTasks 直接中断，连带项目列表无法渲染。
const barTasks = new Map();  // id -> 任务行,最近若干条(不限状态)
const $ = s => document.querySelector(s);
const el = (t,c) => { const e=document.createElement(t); if(c)e.className=c; return e; };
const esc = s => s.replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

// 标题栏弹层互斥：打开任一项时，收起其余项（含「⋯」菜单），避免在窄屏上彼此遮挡。
function closeHeaderPopovers(except){
  for(const id of ["projPop","gitPop","ctxPop","convDocsPop"]){
    const pop=$("#"+id);
    if(pop && pop!==except) pop.hidden=true;
  }
  if(typeof closeConvMenu === "function") closeConvMenu();
}

// ── 线框图标(替代实体 emoji,统一 currentColor 描边风格)──────────────────
const ICONS = {
  mic:'<path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3z"/><path d="M19 11a7 7 0 0 1-14 0"/><path d="M12 19v3"/><path d="M8 22h8"/>',
  folder:'<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/>',
  trash:'<path d="M4 7h16"/><path d="M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/><path d="M6 7l1 13a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-13"/><path d="M10 11v6"/><path d="M14 11v6"/>',
  bot:'<rect x="5" y="9" width="14" height="10" rx="2"/><path d="M12 5v4"/><circle cx="12" cy="4" r="1.1"/><circle cx="9" cy="14" r="1"/><circle cx="15" cy="14" r="1"/><path d="M3 13h2"/><path d="M19 13h2"/>',
  wrench:'<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94z"/>',
  warn:'<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  lock:'<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  clip:'<path d="M9 2h6a1 1 0 0 1 1 1v2H8V3a1 1 0 0 1 1-1z"/><rect x="5" y="4" width="14" height="18" rx="2"/><path d="M9 12h6"/><path d="M9 16h6"/>',
  compass:'<circle cx="12" cy="12" r="9"/><polygon points="16 8 13.5 13.5 8 16 10.5 10.5 16 8"/>',
  doc:'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M8 13h8"/><path d="M8 17h8"/>',
  edit:'<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>',
  zap:'<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
  bell:'<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9z"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>',
  gear:'<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
  plug:'<path d="M9 2v6"/><path d="M15 2v6"/><path d="M7 8h10v3a5 5 0 0 1-10 0V8z"/><path d="M12 16v6"/>',
  book:'<path d="M4 4a2 2 0 0 1 2-2h13a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H6a2 2 0 0 1-2-2V4z"/><path d="M8 2v18"/>',
  person:'<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  search:'<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
  save:'<path d="M20 6L9 17l-5-5"/>',
  plus:'<path d="M12 5v14"/><path d="M5 12h14"/>',
  close:'<path d="M18 6L6 18"/><path d="M6 6l12 12"/>',
  filter:'<path d="M22 3H2l8 9.46V19l4 2V12.46L22 3z"/>',
  copy:'<rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  command:'<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 11l3 3-3 3"/><path d="M12 17h5"/>',
  panel:'<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M9 3v18"/>',
  voice:'<path d="M3 10v4"/><path d="M7 7v10"/><path d="M11 4v16"/><path d="M15 7v10"/><path d="M19 10v4"/>',
  mute:'<path d="M1 1l22 22"/><path d="M9 9v3a3 3 0 0 0 5.12 2.12"/><path d="M15 9.34V6a3 3 0 0 0-5.94-.6"/><path d="M17 16.95A7 7 0 0 1 5 12v-2"/><path d="M19 10v2a7 7 0 0 1-.11 1.23"/><path d="M12 19v3"/><path d="M8 22h8"/>',
  stop:'<rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" stroke="none"/>',
  tasks:'<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>',
  branch:'<circle cx="6" cy="3" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="6" cy="21" r="2"/><path d="M6 5v14"/><path d="M18 8a9 9 0 0 1-9 9"/>',
  grid:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  eraser:'<path d="m7 21-4.3-4.3c-1-1-1-2.5 0-3.4l9.6-9.6c1-1 2.5-1 3.4 0l5.6 5.6c1 1 1 2.5 0 3.4L13 21"/><path d="M22 21H7"/><path d="m5 11 9 9"/>',
  phone:'<path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"/>',
  star:'<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>',
  pin:'<line x1="12" y1="17" x2="12" y2="22"/><path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z"/>',
  external:'<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><path d="M15 3h6v6"/><path d="M10 14L21 3"/>',
  download:'<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/>',
  refresh:'<path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10"/><path d="M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
  newwin:'<rect x="3" y="3" width="13" height="13" rx="2"/><path d="M21 8v11a2 2 0 0 1-2 2H8"/>',
  inbox:'<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
  calendar:'<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4"/><path d="M8 2v4"/><path d="M3 10h18"/>',
  chevronDown:'<path d="M6 9l6 6 6-6"/>',
};
function ic(name){ return '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'+(ICONS[name]||"")+'</svg>'; }
function setIcon(el, name){ el.innerHTML = ic(name); }
// ── 主题(深/浅/跟随系统)───────────────────────────────────────────────
// curThemeMode(): 返回存储的偏好 "auto"|"light"|"dark"
function curThemeMode(){
  return document.documentElement.getAttribute("data-theme") || "auto";
}
function syncTheme(){
  const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg2").trim();
  const m = document.querySelector('meta[name="theme-color"]'); if(m && bg) m.setAttribute("content", bg);
}
function setTheme(mode){
  if(mode==="auto"){
    document.documentElement.removeAttribute("data-theme");
    try{ localStorage.removeItem("vococo_theme"); }catch(e){}
  } else {
    document.documentElement.setAttribute("data-theme", mode);
    try{ localStorage.setItem("vococo_theme", mode); }catch(e){}
  }
  syncTheme();
  api("/prefs",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({theme: mode==="auto" ? null : mode})}).catch(()=>{});
  if(typeof SET!=="undefined" && SET.tab==="appearance") renderAppearancePane();
}
if(window.matchMedia){
  window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", ()=>{
    if(!document.documentElement.getAttribute("data-theme")) syncTheme();  // 仅在"跟随系统"时联动
  });
}

// ── 磁盘缓存(IndexedDB)──────────────────────────────────────────────────
// 内存缓存(S.histCache/S.convs)刷新页面就没了,这层把它落盘,下次打开秒出内容。
// 只是内存缓存的下一层:该发的网络请求(/history、/conversations)照发不误,回来数据
// 不一致才重绘——磁盘不改变"网络为准"的核对逻辑,只是让"网络回来之前"那段空窗有内容可看。
const IDB_NAME="vococo-cache", IDB_STORE="kv", HIST_CACHE_CAP=40;
let _idbPromise=null;
function idbOpen(){
  if(_idbPromise) return _idbPromise;
  _idbPromise=new Promise((resolve,reject)=>{
    if(!window.indexedDB){ reject(new Error("no idb")); return; }
    const req=indexedDB.open(IDB_NAME,1);
    req.onupgradeneeded=()=>{ req.result.createObjectStore(IDB_STORE); };
    req.onsuccess=()=>resolve(req.result);
    req.onerror=()=>reject(req.error);
  });
  return _idbPromise;
}
async function idbGet(key){
  try{
    const db=await idbOpen();
    return await new Promise((resolve,reject)=>{
      const req=db.transaction(IDB_STORE,"readonly").objectStore(IDB_STORE).get(key);
      req.onsuccess=()=>resolve(req.result); req.onerror=()=>reject(req.error);
    });
  }catch(e){ return undefined; }  // 隐私模式/不支持 IndexedDB → 静默降级,不影响正常功能
}
async function idbSet(key,val){
  try{
    const db=await idbOpen();
    await new Promise((resolve,reject)=>{
      const req=db.transaction(IDB_STORE,"readwrite").objectStore(IDB_STORE).put(val,key);
      req.onsuccess=()=>resolve(); req.onerror=()=>reject(req.error);
    });
  }catch(e){}
}
async function idbDel(key){
  try{
    const db=await idbOpen();
    await new Promise((resolve,reject)=>{
      const req=db.transaction(IDB_STORE,"readwrite").objectStore(IDB_STORE).delete(key);
      req.onsuccess=()=>resolve(); req.onerror=()=>reject(req.error);
    });
  }catch(e){}
}
// 会话历史落盘 + 简单 LRU 上限,避免长期使用后磁盘缓存无限增长
async function idbSetHist(conv, turns){
  idbSet("hist:"+conv, turns);
  try{
    const order=(await idbGet("hist:__order__"))||[];
    const next=[conv, ...order.filter(c=>c!==conv)];
    if(next.length>HIST_CACHE_CAP){
      for(const c of next.splice(HIST_CACHE_CAP)) idbDel("hist:"+c);
    }
    idbSet("hist:__order__", next);
  }catch(e){}
}

// ── API ─────────────────────────────────────────────────────────────────
async function api(path, opts={}){
  opts.headers = Object.assign({"X-Auth-Token": S.token}, opts.headers||{});
  let r;
  try{ r = await fetch(path, opts); }
  catch(e){ setServiceState("offline"); throw e; }
  if(r.status===401){ showLogin(); throw new Error("401"); }
  setServiceState(r.status>=500 ? "offline" : "online");
  return r;
}

function setServiceState(state){
  S.serviceState=state;
  const el=$("#syncState"); if(!el) return;
  const view={
    syncing:["同步中", "正在向服务同步最新数据"],
    online:["已同步", "数据已从服务确认"],
    cached:["缓存数据", "正在同步；当前侧边栏来自本地缓存"],
    offline:["服务不可达", "正在显示本地缓存；刷新后仍无法连接服务"],
  }[state] || ["同步中", "正在向服务同步最新数据"];
  el.className="syncstate "+state; el.textContent=view[0]; el.title=view[1];
}
// 语音通话入口只保留侧边栏根目录置顶的「语音通话」行(见 buildVoiceMainRow),
// 不再在聊天输入框上方浮一个圆球——那个入口跟侧栏那行重复,已按用户要求去掉。

// ── 登录 ────────────────────────────────────────────────────────────────
function showLogin(){
  $("#login").style.display="flex"; $("#app").hidden=true;
  const bs=$("#bootSkip"); if(bs) bs.remove();  // 摘掉早期"跳过闪烁"那段强制样式,否则 #app 会被 !important 钉在显示状态
  if(S.es){S.es.close();S.es=null;}
}
async function tryEnter(){
  // 有缓存口令时,首帧就已经靠 #bootSkip 样式把口令页藏起来、#app 显示出来了(见 </head> 后那段内联脚本)。
  // 这里立刻把内容区铺成骨架屏,不要空等——避免"口令消失→空白/欢迎屏→数据陆续到位"的中间闪烁态。
  $("#wrap").innerHTML=""; $("#empty").style.display="none"; $("#convLoading").classList.add("on");
  // 服务端偏好(/prefs)要等到下面的 secondary 批次才拉回来,首屏这次沿用
  // localStorage 缓存的上次 filter(跟主题缓存同一个套路)——命中"active"/
  // "archived" 时直接让后端只回那一半,省掉归档堆积后的越境传输量。
  try{ const cf = localStorage.getItem("vococo_conv_filter"); if(cf) S.convFilter = cf; }catch(e){}
  const cachedConvs = await idbGet("convs");
  if(cachedConvs){ applyConvs(cachedConvs); setServiceState("cached"); }  // 缓存先画,但明确不是已同步数据
  try{
    const qs = (S.convFilter==="active"||S.convFilter==="archived") ? ("?filter="+S.convFilter) : "";
    const r = await fetch("/conversations"+qs, {headers:{"X-Auth-Token":S.token}});
    if(r.status===401){ $("#loginErr").textContent="口令错误"; showLogin(); return; }
    if(!r.ok) throw new Error("HTTP "+r.status);
    const d = await r.json();
    setServiceState("online");
    idbSet("convs", d);   // 落盘,下次刷新页面秒出侧栏
    localStorage.setItem("vococo_token", S.token);
    $("#login").style.display="none"; $("#app").hidden=false;
    connect(); initPush();
    connectTaskStreamOnce();  // 启动任务 SSE,让聊天输入框顶部状态条也能实时更新
    const hasSavedExpanded = localStorage.getItem("vococo_expanded") !== null;
    loadExpanded();
    if(!hasSavedExpanded){ S.expanded.add("__default__"); }  // 从没手动折叠过 → 默认项目首次展开
    applyConvs(d);   // 这次 /conversations 顺便验证了口令,数据直接拿来用,不用再多打一次
    prefetchHistories();   // 后台预热最近会话历史(有 2.5s 延迟,不抢首屏带宽)
    // 独立窗口打开的工作台(?view=workbench,见 workbench.js 的 wb-win-btn):跳过通话视图,
    // 直接落地工作台内容区。sb-collapsed 把桌面侧边栏收起到宽度 0(跟手动收起是同一套
    // CSS/状态),但保留 wb-hamb 展开图标——收起不等于砍掉入口,想展开还是能点开。
    // wb-standalone 只用来藏"独立窗口"按钮本身,不做二次开窗。
    if(S.standaloneWorkbench){ document.body.classList.add("wb-standalone", "sb-collapsed"); openWorkbench(); }
    else { openCallView(); }  // 默认进入统一对话视图:文本和语音共用一个输入区,主会话入口不再单独占一行。
    // 侧边栏用得到的次要数据(主题偏好/项目列表/模型/斜杠命令)并行去拉,不再堵在落地页前面。
    const secondary = Promise.all([loadPrefs(), loadProjects(), loadModels(), loadCommands(), loadVoiceOmniConfig()])
      .then(updateFilterBtn).catch(()=>{});
    loadVoiceSidebar();  // 不阻塞主流程,拉到即刷新侧边栏
    loadCronSidebar();   // 同上,定时任务分组
    loadSystemTasks();   // 同上,「定时」Tab 里的本机系统任务(launchd/crontab)区块
    // 登录这一波已经把各 Tab 的数据都拉了一遍,记下时间戳,避免落地后立刻点 Tab 又空转一次请求
    const _loginTs=Date.now();
    S.tabLastFetch.projects=_loginTs; S.tabLastFetch.pinned=_loginTs; S.tabLastFetch.recent=_loginTs; S.tabLastFetch.cron=_loginTs;
    await secondary;
    initVoiceSelect();
    renderConvs();  // 项目分组数据(loadProjects)到位后再补画一次侧栏分组
  }catch(e){
    setServiceState("offline");
    if(!cachedConvs) $("#loginErr").textContent="服务不可达，请稍后重试";
  }
}
$("#loginBtn").onclick = ()=>{ S.token = $("#pw").value.trim(); tryEnter(); };
$("#pw").onkeydown = e=>{ if(e.key==="Enter"){ S.token=$("#pw").value.trim(); tryEnter(); } };
