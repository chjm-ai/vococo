"use strict";
// 2026-08-14 从 index.html 拆出(前端模块化):设置面板(外观/通知/WebPush/MCP/模型/技能/记忆/人设)+ 会话菜单与滑动操作。
// 与内联脚本同属全局作用域(无构建步骤),加载顺序见 index.html。

// ── 设置(MCP / 技能 / 记忆 / 人设)────────────────────────────────────────
const SET = { data:null, tab:"mcp", skq:"", skScope:"general", mcpFormOpen:false, modelFormOpen:false, providerFormOpen:false };
$("#settingsBtn").innerHTML = ic("gear");

async function openSettings(){
  $("#setModal").hidden=false;
  closeSidebar();                   // 手机上顺手收起侧栏
  $("#setPane").innerHTML = '<div class="setload">加载中…</div>';
  try{
    const r = await api("/settings");
    if(!r.ok) throw new Error("HTTP "+r.status);
    SET.data = await r.json();
  }catch(e){
    $("#setPane").innerHTML = html`<div class="setempty">加载失败:${String(e&&e.message||e)}
      <br><button class="btn ghost sm" style="margin-top:12px" onclick="openSettings()">重试</button></div>`;
    return;
  }
  renderSetTab();
}
function closeSettings(){ $("#setModal").hidden=true; }
$("#settingsBtn").onclick = openSettings;
$("#setClose").onclick = closeSettings;
$("#setModal").onclick = e=>{ if(e.target===$("#setModal")) closeSettings(); };
$("#setTabs").onclick = e=>{
  const b=e.target.closest(".settab"); if(!b) return;
  SET.tab=b.dataset.tab;
  document.querySelectorAll(".settab").forEach(t=>t.classList.toggle("active", t.dataset.tab===SET.tab));
  renderSetTab();
};
function renderSetTab(){
  if(SET.tab==="mcp") renderMcpPane();
  else if(SET.tab==="models") renderModelsPane();
  else if(SET.tab==="skill") renderSkillPane();
  else if(SET.tab==="memory") renderFileList("memory");
  else if(SET.tab==="agents") renderFileList("agents");
  else if(SET.tab==="notify") renderNotifyPane();
  else if(SET.tab==="appearance") renderAppearancePane();
}

// ── 外观分区(主题选择)──────────────────────────────────────────────────
function renderAppearancePane(){
  const mode = curThemeMode();
  const opts = [{v:"auto",label:"跟随系统"},{v:"light",label:"浅色"},{v:"dark",label:"深色"}];
  let h = '<h3>外观</h3><p class="shint">选择界面颜色模式。「跟随系统」会自动跟随操作系统的深色/浅色设置。</p>';
  h += '<div class="theme-seg">';
  opts.forEach(o=>{ h += `<button class="tsopt${mode===o.v?" active":""}" onclick="setTheme('${o.v}')">${o.label}</button>`; });
  h += '</div>';
  $("#setPane").innerHTML = h;
}

// ── 通知分区(Web Push 系统通知)──────────────────────────────────────────
function isIOSNonStandalone(){
  return /iP(hone|ad|od)/.test(navigator.userAgent) && !isStandalone();
}
function renderNotifyPane(){
  let h = '<h3>'+ic("bell")+'系统通知</h3>'+
    '<p class="shint">开启后,回复完成 / 需要审批 / 主动推送(cron)/ 出错 会推送手机系统通知——页面关了、锁屏也能收到。</p>';
  if(!("serviceWorker" in navigator) || !("PushManager" in window)){
    h += '<div class="setempty">当前浏览器不支持 Web Push。</div>';
    $("#setPane").innerHTML=h; return;
  }
  if(!PUSH.enabled){
    h += '<div class="setempty">服务端还没配推送密钥。<br>跑 <code>python -m vococo.gateway.adapters.web_push --gen-keys</code> 生成 VAPID 填进 .env,重启后再来。</div>';
    $("#setPane").innerHTML=h; return;
  }
  h += settingsRow("", "", "本设备通知", '<span id="notifyDesc">检查中…</span>', false,
       '<label class="sw"><input type="checkbox" id="notifyToggle"><span class="track"></span></label>');
  h += settingsRow("", "", "发送测试通知", "开启后点这里,手机应立刻弹一条系统通知——验证链路是否真的通。", false,
       '<button class="btn ghost sm" id="notifyTest" disabled>测试</button>');
  h += '<div class="sechd">通知场景</div>'+
       '<p class="shint">回复完成 · 需要审批确认 · 主动推送(cron)· 出错异常——默认全开,可在 .env 用 <code>PUSH_ON_*=0</code> 单独关某类。</p>';
  h += '<div class="sechd">已订阅设备</div>'+
       '<p class="shint">列出服务端还记着要推送的设备;不用/删掉的旧会话点右边"移除"就不会再收到通知。</p>'+
       '<div id="notifyDevices" class="setempty">加载中…</div>';
  $("#setPane").innerHTML=h;
  bindNotifyPane();
  loadNotifyDevices();
}
function uaLabel(ua){
  if(!ua) return "未知设备";
  if(/iPhone/.test(ua)) return "iPhone";
  if(/iPad/.test(ua)) return "iPad";
  if(/Macintosh/.test(ua)) return "Mac";
  if(/Android/.test(ua)) return "Android";
  return "其他设备";
}
async function loadNotifyDevices(){
  const box=$("#notifyDevices"); if(!box) return;
  let subs;
  try{ subs=(await (await api("/push/subs")).json()).subs||[]; }
  catch(e){ box.innerHTML='<div class="setempty">加载失败:'+esc(e&&e.message||e)+'</div>'; return; }
  if(!subs.length){ box.innerHTML='<div class="setempty">还没有设备订阅。</div>'; return; }
  const mySub = await currentSub().catch(()=>null);
  box.innerHTML = subs.map(s=>{
    const mine = mySub && mySub.endpoint===s.endpoint;
    return settingsRow("", "", esc(uaLabel(s.ua))+(mine?' · 本设备':''), esc(fmtTime(s.subscribedAt))+' 订阅', false,
      '<button class="btn ghost sm" data-ep="'+esc(s.endpoint)+'">移除</button>');
  }).join("");
  box.querySelectorAll("button[data-ep]").forEach(btn=>{
    btn.onclick = async ()=>{
      btn.disabled=true;
      try{
        await api("/push/unsubscribe",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({endpoint:btn.dataset.ep})});
        const mySub = await currentSub().catch(()=>null);
        if(mySub && mySub.endpoint===btn.dataset.ep){ try{ await mySub.unsubscribe(); }catch(e){} }
        await loadNotifyDevices();
        bindNotifyPane();
      }catch(e){ alert("移除失败:"+(e&&e.message||e)); btn.disabled=false; }
    };
  });
}
async function bindNotifyPane(){
  const tgl=$("#notifyToggle"), desc=$("#notifyDesc"); if(!tgl) return;
  const denied = (typeof Notification!=="undefined" && Notification.permission==="denied");
  const sub = denied ? null : await currentSub();
  if(denied){ tgl.checked=false; tgl.disabled=true; desc.textContent="浏览器已拒绝通知,去系统/浏览器设置里放行后再试"; }
  else {
    tgl.checked=!!sub; tgl.disabled=false;
    desc.textContent = sub ? "已开启,本设备会收到通知"
      : (isIOSNonStandalone() ? "iPhone 需先把网页「加到主屏」,从主屏图标打开才能开" : "点右侧开关开启");
  }
  const testBtn=$("#notifyTest");
  if(testBtn){
    testBtn.disabled = !sub;
    testBtn.onclick = async ()=>{
      testBtn.disabled=true; const t=testBtn.textContent; testBtn.textContent="发送中…";
      try{
        const j = await (await api("/push/test",{method:"POST"})).json();
        alert(j.sent>0
          ? "已发往 "+j.sent+" 台设备。看手机系统通知(锁屏/通知中心)。"
          : "服务端订阅列表为空(sent=0)——开关没真正订阅成功,请重开一次并留意报错。");
      }catch(e){ alert("测试失败:"+(e&&e.message||e)); }
      finally{ testBtn.textContent=t; testBtn.disabled=!sub; }
    };
  }
  tgl.onchange = async ()=>{
    tgl.disabled=true;
    try{ tgl.checked ? await enableNotify() : await disableNotify(); }
    catch(e){ alert("操作失败:"+(e&&e.message||e)); }
    finally{ bindNotifyPane(); }
  };
}

// ── Web Push 底座:注册 SW + 订阅 VAPID ────────────────────────────────────
// 装了 Service Worker + 订阅公钥后,页面关了/锁屏也能收到系统通知。
// 服务端未配 VAPID 时 /push/config 返回 enabled:false,通知 tab 显示未配置提示。
const PUSH = { pubKey:"", enabled:false, ready:false };
function b64ToU8(base64){
  const pad="=".repeat((4-base64.length%4)%4);
  const b=(base64+pad).replace(/-/g,"+").replace(/_/g,"/");
  const raw=atob(b), arr=new Uint8Array(raw.length);
  for(let i=0;i<raw.length;i++) arr[i]=raw.charCodeAt(i);
  return arr;
}
function isStandalone(){
  return (window.matchMedia&&window.matchMedia("(display-mode: standalone)").matches) || window.navigator.standalone===true;
}
// 固定设备号:iOS 订阅失效后重新 subscribe() 会拿到全新 endpoint,单靠 endpoint 去重会让
// 同一台设备在服务端越攒越多。存一个不变的 deviceId 让服务端"认设备"而不是"认 endpoint"。
function getDeviceId(){
  try{
    let id=localStorage.getItem("vococo_device_id");
    if(!id){ id=(crypto.randomUUID?crypto.randomUUID():(Date.now()+"-"+Math.random().toString(36).slice(2))); localStorage.setItem("vococo_device_id", id); }
    return id;
  }catch(e){ return ""; }
}
// SW 外壳缓存让页面秒开(可能是旧版);后台发现新版后弹这个提示条,点了才刷新——
// 不自动 reload,免得把正在输入的草稿冲掉
function showShellUpdated(){
  if(document.getElementById("shellUpd")) return;
  const b=document.createElement("div");
  b.id="shellUpd";
  b.textContent="界面已更新 · 点击刷新";
  b.style.cssText="position:fixed;left:50%;transform:translateX(-50%);bottom:76px;z-index:9999;"+
    "padding:8px 14px;border-radius:18px;font-size:13px;cursor:pointer;"+
    "background:var(--accent,#d97757);color:#fff;box-shadow:0 4px 14px rgba(0,0,0,.25)";
  b.onclick=()=>location.reload();
  document.body.appendChild(b);
}
var pushMsgListenerInited = false;  // 防重入:挡重登录时监听器重复叠加
async function initPush(){
  if(!("serviceWorker" in navigator)) return;
  try{ await navigator.serviceWorker.register("/sw.js"); }catch(e){ return; }
  if(!pushMsgListenerInited){
    pushMsgListenerInited = true;
    // 点系统通知 → SW 让页面切到对应会话
    navigator.serviceWorker.addEventListener("message", ev=>{
      const m=ev.data||{};
      if(m.type==="open" && m.conv){ try{ openConv(m.conv); }catch(e){} }
      if(m.type==="shell-updated"){ try{ showShellUpdated(); }catch(e){} }
    });
  }
  try{
    const cfg = await (await api("/push/config")).json();
    PUSH.enabled=!!cfg.enabled; PUSH.pubKey=cfg.vapidPublicKey||"";
  }catch(e){ return; }
  PUSH.ready=true;
}
async function currentSub(){
  if(!("serviceWorker" in navigator) || !("PushManager" in window)) return null;
  const reg=await navigator.serviceWorker.ready;
  return reg.pushManager.getSubscription();
}
async function enableNotify(){
  // 每一步失败都抛带原因的错误,交给 onchange 的 catch 弹出来——别再静默失败让人盲猜。
  if(isIOSNonStandalone())
    throw new Error("现在不是主屏模式。iPhone 需先把本页「添加到主屏幕」,再从主屏图标打开才能开启。");
  if(typeof Notification==="undefined")
    throw new Error("此浏览器不支持通知 API(Notification 未定义)。");
  let perm = Notification.permission;
  if(perm==="default") perm=await Notification.requestPermission();
  if(perm==="denied")
    throw new Error("通知权限被拒。iOS 需删掉主屏图标 + 清掉网站数据后,重新加主屏再试。");
  if(perm!=="granted")
    throw new Error("未获得通知权限(perm="+perm+")。");
  if(!PUSH.pubKey)
    throw new Error("服务端没返回 VAPID 公钥,推送未配置。");
  const reg=await navigator.serviceWorker.ready;
  let sub;
  try{
    sub=await reg.pushManager.subscribe({ userVisibleOnly:true, applicationServerKey:b64ToU8(PUSH.pubKey) });
  }catch(e){
    throw new Error("订阅失败(pushManager.subscribe):"+(e&&e.message||e));
  }
  const payload=Object.assign(sub.toJSON?sub.toJSON():JSON.parse(JSON.stringify(sub)), {ua:navigator.userAgent, deviceId:getDeviceId()});
  const r=await api("/push/subscribe",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
  if(!r.ok)
    throw new Error("订阅已建立但保存到服务端失败(HTTP "+r.status+")。");
}
async function disableNotify(){
  const sub=await currentSub();
  if(sub){
    try{ await api("/push/unsubscribe",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({endpoint:sub.endpoint})}); }catch(e){}
    try{ await sub.unsubscribe(); }catch(e){}
  }
}

// ── MCP 分区 ──────────────────────────────────────────────────────────────
function renderMcpPane(){
  const m = SET.data.mcp;
  let h = '<h3>'+ic("plug")+'MCP 工具服务</h3>'+
    '<p class="shint">MCP(Model Context Protocol)给 vococo 接入额外工具。开关改动下一条消息即生效,无需新建会话、历史不丢。外部工具 schema 很占上下文(如 lemlist 120 个工具 ≈11 万 token/轮),不用时建议关;也可以直接对 Wazir 说「开外贸工具 / 关掉 lemlist」。</p>';
  h += '<div class="sechd">内置</div>';
  h += mcpRow("vococo", "vococo 内置工具", "记忆检索 / 保存、定时任务、发消息、自我重启等——关掉后这些能力会消失,谨慎。", m.vococo_enabled, "vococo");
  h += '<div class="sechd">外部 Server</div>';
  if(!m.external.length){
    h += '<p class="shint" style="margin:2px 0 6px">还没有外部 MCP。可添加如 filesystem、fetch 等社区 server。</p>';
  } else {
    m.external.forEach(x=>{
      const sub = x.type==="stdio" ? esc((x.command||"")+" "+((x.args||[]).join(" "))) : esc(x.url||"");
      h += mcpRow(x.name, esc(x.name)+'<span class="mctag">'+esc(x.type||"stdio")+'</span>', sub, x.enabled!==false, "ext:"+x.name, true);
    });
  }
  h += '<div class="setrowbtns"><button class="btn ghost sm" id="mcpAddBtn">'+ic("plus")+' 添加外部 MCP</button></div>';
  if(SET.mcpFormOpen) h += mcpFormHtml();
  $("#setPane").innerHTML = h;
  bindMcpPane();
}
// 通用「设置行」外壳:.srow > .sinfo(.sname/.sdesc) + .sacts(调用方自己拼具体内容)。
// 2026-07-23:mcpRow 曾是唯一命名了这套外壳的函数,renderSkillPane/renderNotifyPane(×2)/
// loadNotifyDevices 各自手写复刻了一遍,收口成这一处共享骨架——.sacts 里装什么(开关/
// 按钮/两者都有/都没有)每处差异真实存在,不强行统一,交给调用方自己拼 actsHtml。
function settingsRow(rowAttr, rowKey, nameHtml, descHtml, off, actsHtml){
  return html`<div class="srow${html.raw(off?" off":"")}"${html.raw(rowAttr?' '+rowAttr+'="'+esc(rowKey)+'"':'')}>
    <div class="sinfo"><div class="sname">${html.raw(nameHtml)}</div>
    ${html.raw(descHtml?'<div class="sdesc">'+descHtml+'</div>':'')}</div>
    <div class="sacts">${html.raw(actsHtml)}</div></div>`;
}
function mcpRow(id, nameHtml, descHtml, on, key, canDelete){
  const acts=html`${html.raw(canDelete?'<button class="miniact danger" data-mdel="'+esc(id)+'">删除</button>':'')}
    <label class="sw"><input type="checkbox" data-mtgl="${key}"${html.raw(on?" checked":"")}><span class="track"></span></label>`;
  return settingsRow("data-mrow", key, nameHtml, descHtml, !on, acts);
}
function mcpFormHtml(){
  return '<div class="mcpform" id="mcpForm">'+
    '<div class="fld"><label>名称(唯一标识)</label><input id="mfName" placeholder="filesystem"></div>'+
    '<div class="fld"><label>类型</label><select id="mfType"><option value="stdio">stdio(本地命令)</option><option value="sse">sse(远程)</option><option value="http">http(远程)</option></select></div>'+
    '<div id="mfStdio">'+
      '<div class="fld"><label>命令 command</label><input id="mfCmd" placeholder="npx"></div>'+
      '<div class="fld"><label>参数 args(空格分隔)</label><input id="mfArgs" placeholder="-y @modelcontextprotocol/server-filesystem /path/to/dir"></div>'+
      '<div class="fld"><label>环境变量 env(每行 KEY=VALUE,可空)</label><textarea id="mfEnv" placeholder="API_KEY=xxx"></textarea></div>'+
    '</div>'+
    '<div id="mfRemote" hidden>'+
      '<div class="fld"><label>URL</label><input id="mfUrl" placeholder="https://example.com/mcp"></div>'+
      '<div class="fld"><label>请求头 headers(每行 KEY=VALUE,可空)</label><textarea id="mfHeaders" placeholder="Authorization=Bearer xxx"></textarea></div>'+
    '</div>'+
    '<div class="mcperr" id="mfErr"></div>'+
    '<div class="setrowbtns"><button class="btn primary sm" id="mfSave">保存</button><button class="btn ghost sm" id="mfCancel">取消</button></div>'+
  '</div>';
}
function bindMcpPane(){
  $("#setPane").querySelectorAll("[data-mtgl]").forEach(inp=>{
    inp.onchange = async()=>{
      const key=inp.dataset.mtgl, on=inp.checked;
      inp.closest(".srow").classList.toggle("off", !on);
      if(key==="vococo"){ await api("/settings/mcp/vococo",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled:on})}); SET.data.mcp.vococo_enabled=on; }
      else if(key.startsWith("ext:")){ const name=key.slice(4); await api("/settings/mcp/external",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"toggle",name,enabled:on})}); const x=SET.data.mcp.external.find(e=>e.name===name); if(x)x.enabled=on; }
    };
  });
  $("#setPane").querySelectorAll("[data-mdel]").forEach(b=>{
    b.onclick = async()=>{
      const name=b.dataset.mdel;
      if(!confirm("删除外部 MCP「"+name+"」?")) return;
      await api("/settings/mcp/external",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"remove",name})});
      SET.data.mcp.external = SET.data.mcp.external.filter(e=>e.name!==name);
      renderMcpPane();
    };
  });
  const addBtn=$("#mcpAddBtn"); if(addBtn) addBtn.onclick=()=>{ SET.mcpFormOpen=true; renderMcpPane(); };
  const typeSel=$("#mfType");
  if(typeSel){
    const sync=()=>{ const t=typeSel.value; $("#mfStdio").hidden=(t!=="stdio"); $("#mfRemote").hidden=(t==="stdio"); };
    typeSel.onchange=sync; sync();
    $("#mfCancel").onclick=()=>{ SET.mcpFormOpen=false; renderMcpPane(); };
    $("#mfSave").onclick=saveMcpForm;
  }
}
function parseKV(text){ const o={}; (text||"").split("\n").forEach(l=>{ l=l.trim(); if(!l) return; const i=l.indexOf("="); if(i>0) o[l.slice(0,i).trim()]=l.slice(i+1).trim(); }); return o; }
async function saveMcpForm(){
  const err=$("#mfErr"); err.textContent="";
  const name=$("#mfName").value.trim();
  if(!name){ err.textContent="请填名称"; return; }
  const type=$("#mfType").value;
  const body={action:"add", name, type, enabled:true};
  if(type==="stdio"){
    if(!$("#mfCmd").value.trim()){ err.textContent="stdio 需要 command"; return; }
    body.command=$("#mfCmd").value.trim();
    body.args=$("#mfArgs").value.trim().split(/\s+/).filter(Boolean);
    body.env=parseKV($("#mfEnv").value);
  } else {
    if(!$("#mfUrl").value.trim()){ err.textContent=type+" 需要 URL"; return; }
    body.url=$("#mfUrl").value.trim();
    body.headers=parseKV($("#mfHeaders").value);
  }
  const r=await api("/settings/mcp/external",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const d=await r.json();
  if(d.error){ err.textContent=d.error; return; }
  SET.mcpFormOpen=false;
  const rr=await api("/settings"); SET.data=await rr.json();  // 拉回最新列表
  renderMcpPane();
}

// ── 模型分区(内置档位可隐藏/恢复,自定义模型/服务商可增改删,改完下一轮/刷新即生效)──
function renderModelsPane(){
  const md = SET.data.models || {active:"", builtin:[], custom:[], providers:[]};
  const curTag = ' <span class="mctag cur">当前</span>';
  let h = '<h3>'+ic("bot")+'模型 & 服务商</h3>'+
    '<p class="shint">"当前"标的是这个会话正在用的模型。新模型发布但代码还没跟上时,先在下面手动补一档;'+
    '第三方服务商(DeepSeek/Kimi/中转等)也可以直接在这加,保存后刷新模型面板即生效,不用重启。</p>';

  h += '<div class="sechd">内置模型档位</div>';
  (md.builtin||[]).forEach(m=>{
    const cur = m.id===md.active ? curTag : '';
    const acts = '<label class="sw"><input type="checkbox" data-bitgl="'+esc(m.id)+'"'+(m.disabled?'':' checked')+'><span class="track"></span></label>';
    h += settingsRow("data-birow", m.id, esc(m.label)+cur, esc(m.id), !!m.disabled, acts);
  });

  h += '<div class="sechd">自定义模型档位</div>';
  if(!md.custom.length){
    h += '<p class="shint" style="margin:2px 0 6px">还没有手动加的档位。</p>';
  } else {
    md.custom.forEach(m=>{
      const cur = m.id===md.active ? curTag : '';
      const acts = '<button class="miniact" data-mdedit="'+esc(m.id)+'">编辑</button>'+
        '<button class="miniact danger" data-mddel="'+esc(m.id)+'">删除</button>';
      h += settingsRow("data-mdrow", m.id, esc(m.label||m.id)+cur, esc(m.id), false, acts);
    });
  }
  h += '<div class="setrowbtns"><button class="btn ghost sm" id="mdAddBtn">'+ic("plus")+' 添加模型档位</button></div>';
  if(SET.modelFormOpen) h += modelFormHtml();

  h += '<div class="sechd">第三方服务商</div>';
  if(!md.providers.length){
    h += '<p class="shint" style="margin:2px 0 6px">还没有服务商。</p>';
  } else {
    md.providers.forEach(p=>{
      const cur = p.model===md.active ? curTag : '';
      const sub = esc(p.model||"")+' · '+esc(p.base_url||"")+(p.api_key?' · 已设 key':' · 未设 key');
      const acts = '<button class="miniact" data-pvedit="'+esc(p.name)+'">编辑</button>'+
        '<button class="miniact danger" data-pvdel="'+esc(p.name)+'">删除</button>';
      h += settingsRow("data-pvrow", p.name, esc(p.label||p.name)+cur, sub, false, acts);
    });
  }
  h += '<div class="setrowbtns"><button class="btn ghost sm" id="pvAddBtn">'+ic("plus")+' 添加服务商</button></div>';
  if(SET.providerFormOpen) h += providerFormHtml();
  $("#setPane").innerHTML = h;
  bindModelsPane();
}
function modelFormHtml(){
  const editing = SET.modelEditing;
  const cur = editing ? (SET.data.models.custom.find(m=>m.id===editing)||{}) : {};
  return '<div class="mcpform" id="mdForm">'+
    '<div class="fld"><label>模型 id(填 API 认的那个模型名)</label>'+
      '<input id="mdId" placeholder="claude-opus-5" value="'+esc(cur.id||"")+'"'+(editing?' readonly':'')+'></div>'+
    '<div class="fld"><label>显示名(可空,默认用 id)</label><input id="mdLabel" placeholder="Opus 5（订阅）" value="'+esc(cur.label||"")+'"></div>'+
    '<div class="mcperr" id="mdErr"></div>'+
    '<div class="setrowbtns"><button class="btn primary sm" id="mdSave">保存</button><button class="btn ghost sm" id="mdCancel">取消</button></div>'+
  '</div>';
}
function providerFormHtml(){
  const editing = SET.providerEditing;
  const cur = editing ? (SET.data.models.providers.find(p=>p.name===editing)||{}) : {};
  return '<div class="mcpform" id="pvForm">'+
    '<div class="fld"><label>名称(唯一标识)</label><input id="pvName" placeholder="deepseek" value="'+esc(cur.name||"")+'"'+(editing?' readonly':'')+'></div>'+
    '<div class="fld"><label>base_url</label><input id="pvUrl" placeholder="https://api.deepseek.com/anthropic" value="'+esc(cur.base_url||"")+'"></div>'+
    '<div class="fld"><label>model</label><input id="pvModel" placeholder="deepseek-chat" value="'+esc(cur.model||"")+'"></div>'+
    '<div class="fld"><label>api_key</label><input id="pvKey" type="password" placeholder="sk-..." value="'+esc(cur.api_key||"")+'"></div>'+
    '<div class="fld"><label>显示名(可空)</label><input id="pvLabel" placeholder="" value="'+esc(cur.label||"")+'"></div>'+'<div class="fld"><label>mgmt_key(Codex 代理的管理钥匙,填了才有额度查询;可空)</label><input id="pvMgmt" type="password" placeholder="" value="'+esc(cur.mgmt_key||"")+'"></div>'+
    '<div class="fld"><label style="display:flex;align-items:center;gap:6px"><input id="pvVision" type="checkbox"'+(cur.vision==="1"?' checked':'')+'><span>支持视觉直传(Codex 代理/GPT 勾选,图片不转文字)</span></label></div>'+
    '<div class="mcperr" id="pvErr"></div>'+
    '<div class="setrowbtns"><button class="btn primary sm" id="pvSave">保存</button><button class="btn ghost sm" id="pvCancel">取消</button></div>'+
  '</div>';
}
function bindModelsPane(){
  $("#setPane").querySelectorAll("[data-bitgl]").forEach(inp=>{
    inp.onchange = async()=>{
      const id=inp.dataset.bitgl, disabled=!inp.checked;
      inp.closest(".srow").classList.toggle("off", disabled);
      await api("/settings/model",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"toggle_builtin", id, disabled})});
      const m=SET.data.models.builtin.find(x=>x.id===id); if(m) m.disabled=disabled;
    };
  });
  $("#setPane").querySelectorAll("[data-mdedit]").forEach(b=>{
    b.onclick = ()=>{ SET.modelEditing=b.dataset.mdedit; SET.modelFormOpen=true; renderModelsPane(); };
  });
  $("#setPane").querySelectorAll("[data-mddel]").forEach(b=>{
    b.onclick = async()=>{
      const id=b.dataset.mddel;
      if(!confirm("删除模型档位「"+id+"」?")) return;
      await api("/settings/model",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"remove", id})});
      SET.data.models.custom = SET.data.models.custom.filter(m=>m.id!==id);
      renderModelsPane();
    };
  });
  $("#setPane").querySelectorAll("[data-pvedit]").forEach(b=>{
    b.onclick = ()=>{ SET.providerEditing=b.dataset.pvedit; SET.providerFormOpen=true; renderModelsPane(); };
  });
  $("#setPane").querySelectorAll("[data-pvdel]").forEach(b=>{
    b.onclick = async()=>{
      const name=b.dataset.pvdel;
      if(!confirm("删除服务商「"+name+"」?")) return;
      await api("/settings/provider",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"remove", name})});
      SET.data.models.providers = SET.data.models.providers.filter(p=>p.name!==name);
      renderModelsPane();
    };
  });
  const mdAdd=$("#mdAddBtn"); if(mdAdd) mdAdd.onclick=()=>{ SET.modelEditing=null; SET.modelFormOpen=true; renderModelsPane(); };
  const mdCancel=$("#mdCancel"); if(mdCancel) mdCancel.onclick=()=>{ SET.modelFormOpen=false; SET.modelEditing=null; renderModelsPane(); };
  const mdSave=$("#mdSave"); if(mdSave) mdSave.onclick=saveModelForm;
  const pvAdd=$("#pvAddBtn"); if(pvAdd) pvAdd.onclick=()=>{ SET.providerEditing=null; SET.providerFormOpen=true; renderModelsPane(); };
  const pvCancel=$("#pvCancel"); if(pvCancel) pvCancel.onclick=()=>{ SET.providerFormOpen=false; SET.providerEditing=null; renderModelsPane(); };
  const pvSave=$("#pvSave"); if(pvSave) pvSave.onclick=saveProviderForm;
}
async function saveModelForm(){
  const err=$("#mdErr"); err.textContent="";
  const id=$("#mdId").value.trim();
  if(!id){ err.textContent="请填模型 id"; return; }
  const label=$("#mdLabel").value.trim();
  const r=await api("/settings/model",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"add", id, label})});
  const d=await r.json();
  if(d.error){ err.textContent=d.error; return; }
  SET.modelFormOpen=false; SET.modelEditing=null;
  const rr=await api("/settings"); SET.data=await rr.json();
  renderModelsPane();
}
async function saveProviderForm(){
  const err=$("#pvErr"); err.textContent="";
  const name=$("#pvName").value.trim();
  if(!name){ err.textContent="请填名称"; return; }
  const base_url=$("#pvUrl").value.trim();
  const model=$("#pvModel").value.trim();
  if(!base_url||!model){ err.textContent="base_url 和 model 都要填"; return; }
  const body={action:"add", name, base_url, model, api_key:$("#pvKey").value.trim(), label:$("#pvLabel").value.trim(), vision:($("#pvVision")&&$("#pvVision").checked)?"1":"", mgmt_key:$("#pvMgmt")?$("#pvMgmt").value.trim():""};
  const r=await api("/settings/provider",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const d=await r.json();
  if(d.error){ err.textContent=d.error; return; }
  SET.providerFormOpen=false; SET.providerEditing=null;
  const rr=await api("/settings"); SET.data=await rr.json();
  renderModelsPane();
}

// ── 技能分区 ──────────────────────────────────────────────────────────────
function renderSkillPane(){
  const sk=SET.data.skills, scope=SET.skScope;
  const q=SET.skq.toLowerCase();
  let items=sk.items.slice();
  if(q) items=items.filter(x=>x.name.toLowerCase().includes(q)||(x.description||"").toLowerCase().includes(q));
  // 未隐藏在前,隐藏的沉底
  items.sort((a,b)=>(a.hidden?1:0)-(b.hidden?1:0));
  const enabledKey=scope==="coding"?"coding_enabled":"enabled";
  const onCount=sk.items.filter(x=>x[enabledKey]).length;
  const coding=scope==="coding";
  const mode=coding?sk.coding_mode:sk.mode;
  const title=coding?"Git 编程项目":"通用会话";
  const desc=coding
    ? "只在用户明确选择 Git 仓库时加载。与通用会话名单独立，避免无关 Skill 占用上下文。"
    : "用于普通聊天和非 Git 目录。关闭 = 不挂给 AI(省 token、不会被调用);隐藏 = 仅在此列表折叠。";
  let h='<h3>'+ic("zap")+'技能 Skills</h3>'+
    '<div class="skScope"><button class="'+(!coding?"active":"")+'" data-skscope="general">通用会话</button>'+
    '<button class="'+(coding?"active":"")+'" data-skscope="coding">Git 编程项目</button></div>'+
    '<p class="shint">'+desc+' 共 '+sk.items.length+' 个，已开启 '+onCount+' 个。'+
    (mode==="custom"?'<b>(独立名单)</b>':'<b>(继承通用名单)</b>')+'</p>';
  h+='<div class="skbar"><div class="sksearch">'+ic("search")+'<input id="skSearch" placeholder="搜索技能…" value="'+esc(SET.skq)+'"></div>'+
     '<button class="btn ghost sm" id="skReset">'+(coding?"改为继承通用":"恢复默认名单")+'</button></div>';
  if(!items.length){ h+='<div class="setempty">没有匹配的技能</div>'; }
  items.forEach(x=>{
    const enabled=!!x[enabledKey];
    const acts='<button class="miniact'+(x.hidden?" on":"")+'" data-skhide="'+esc(x.name)+'">'+(x.hidden?"显示":"隐藏")+'</button>'+
      '<label class="sw"><input type="checkbox" data-sken="'+esc(x.name)+'"'+(enabled?" checked":"")+'><span class="track"></span></label>';
    h += settingsRow("data-sk", x.name, esc(x.name), x.description?esc(x.description):"", !enabled||x.hidden, acts);
  });
  $("#setPane").innerHTML=h;
  bindSkillPane();
}
function bindSkillPane(){
  const s=$("#skSearch");
  if(s){ s.oninput=()=>{ SET.skq=s.value; const pos=s.selectionStart; renderSkillPane(); const n=$("#skSearch"); if(n){ n.focus(); n.setSelectionRange(pos,pos); } }; }
  $("#setPane").querySelectorAll("[data-skscope]").forEach(b=>{
    b.onclick=()=>{ SET.skScope=b.dataset.skscope; renderSkillPane(); };
  });
  $("#skReset").onclick=async()=>{
    const path=SET.skScope==="coding"?"/settings/skills/coding/reset":"/settings/skills/reset";
    await api(path,{method:"POST"});
    const r=await api("/settings"); SET.data=await r.json(); renderSkillPane();
  };
  $("#setPane").querySelectorAll("[data-sken]").forEach(inp=>{
    inp.onchange=async()=>{
      const name=inp.dataset.sken, on=inp.checked, scope=SET.skScope;
      const r=await api("/settings/skill",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,enabled:on,scope})});
      const d=await r.json();
      SET.data.skills.mode=d.mode||SET.data.skills.mode;
      SET.data.skills.coding_mode=d.coding_mode||SET.data.skills.coding_mode;
      const it=SET.data.skills.items.find(x=>x.name===name); if(it)it[scope==="coding"?"coding_enabled":"enabled"]=on;
      renderSkillPane();
    };
  });
  $("#setPane").querySelectorAll("[data-skhide]").forEach(b=>{
    b.onclick=async()=>{
      const name=b.dataset.skhide;
      const it=SET.data.skills.items.find(x=>x.name===name); const nv=!(it&&it.hidden);
      await api("/settings/skill",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,hidden:nv})});
      if(it)it.hidden=nv; renderSkillPane();
    };
  });
}

// ── 记忆 / 人设 文件 ───────────────────────────────────────────────────────
function renderFileList(group){
  const files=(SET.data.files||[]).filter(f=>f.group===group);
  if(group==="agents"){
    // 人设只有一个文件,直接进编辑器
    const f=files[0]; if(f){ openFileEditor(f.rel, "agents"); return; }
  }
  let h;
  if(group==="memory"){
    h='<h3>'+ic("book")+'长期记忆</h3>'+
      '<p class="shint">这些文件会注入 AI 的系统提示(索引 + 画像),点开可查看/编辑。改完保存,下一轮生效。</p>';
    h+='<div class="setrowbtns" style="margin:0 0 6px"><button class="btn ghost sm" id="memNew">'+ic("plus")+' 新建记忆</button></div>';
  } else {
    h='<h3>'+ic("person")+'人设 AGENTS.md</h3>';
  }
  files.forEach(f=>{
    h+='<div class="frow" data-frel="'+esc(f.rel)+'">'+ic("doc")+
       '<span class="fname">'+esc(f.rel)+'</span>'+
       (f.exists?'':'<span class="fmiss">未创建</span>')+
       '<span class="fchev">›</span></div>';
  });
  $("#setPane").innerHTML=h;
  $("#setPane").querySelectorAll("[data-frel]").forEach(row=>{
    row.onclick=()=>openFileEditor(row.dataset.frel, group);
  });
  const mn=$("#memNew");
  if(mn) mn.onclick=()=>{
    let name=prompt("新记忆文件名(会存到 memory/ 下,自动加 .md):");
    if(!name) return;
    name=name.trim().replace(/\.md$/i,"").replace(/[^\w一-龥-]/g,"-");
    if(!name) return;
    openFileEditor("memory/"+name+".md", "memory", true);
  };
}
async function openFileEditor(rel, group, isNew){
  $("#setPane").innerHTML='<div class="setload">加载中…</div>';
  let content="";
  if(!isNew){
    try{ const r=await api("/file/read?rel="+encodeURIComponent(rel)); content=(await r.json()).content||""; }
    catch(e){ $("#setPane").innerHTML='<div class="setempty">读取失败</div>'; return; }
  }
  const backable = group==="memory";
  $("#setPane").innerHTML=
    '<div class="feditor">'+
      '<div class="fetop">'+
        (backable?'<button class="feback" id="feBack">‹ 返回</button>':'')+
        '<span class="fepath">'+esc(rel)+'</span>'+
      '</div>'+
      '<textarea id="feText" spellcheck="false"></textarea>'+
      '<div class="fesave"><span class="festat" id="feStat"></span>'+
        '<button class="btn primary sm" id="feSave">'+ic("save")+' 保存</button></div>'+
    '</div>';
  $("#feText").value=content;
  const back=$("#feBack"); if(back) back.onclick=()=>renderFileList(group);
  $("#feSave").onclick=async()=>{
    const stat=$("#feStat"); stat.className="festat"; stat.textContent="保存中…";
    const r=await api("/file/save",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({rel, content:$("#feText").value})});
    const d=await r.json();
    if(d.error){ stat.textContent="失败:"+d.error; return; }
    stat.className="festat ok"; stat.textContent="已保存 ✓";
    // 刷新文件列表(新建的文件要标记为已存在)
    try{ const rr=await api("/settings"); SET.data=await rr.json(); }catch(e){}
  };
}
// 会话「更多」浮层菜单(单例):点 ⋯ 弹出,重命名/置顶/归档/复制/删除,点即执行(不再二次确认)
let convMenuEl=null, convMenuBtn=null;
function closeConvMenu(){
  if(convMenuBtn){ convMenuBtn.classList.remove("on"); convMenuBtn=null; }
  if(convMenuEl){ convMenuEl.remove(); convMenuEl=null; }
}
// 触屏左滑会话行露出归档/删除(iOS 邮件同款手势,比长按更符合触屏习惯)
const SWIPE_W=108;   // 需与 CSS .cvact 宽度一致(归档+置顶+删除,3个按钮)
function closeSwipe(row){ row.classList.remove("swiped"); if(S.swipedConv===row.dataset.conv) S.swipedConv=null; }
function closeAllSwipes(except){
  $("#convBody").querySelectorAll(".conv.swiped").forEach(r=>{ if(r!==except) closeSwipe(r); });
}
function bindSwipe(row, body, act){
  let startX=0, startY=0, dx=0, lock=null, dragging=false;
  row.addEventListener("touchstart", ev=>{
    if(ev.touches.length!==1) return;
    startX=ev.touches[0].clientX; startY=ev.touches[0].clientY;
    dx=0; lock=null; dragging=false;
    row.classList.add("swiping");
  }, {passive:true});
  row.addEventListener("touchmove", ev=>{
    if(ev.touches.length!==1) return;
    const x=ev.touches[0].clientX, y=ev.touches[0].clientY, ddx=x-startX, ddy=y-startY;
    if(lock===null) lock = Math.abs(ddx)>Math.abs(ddy)+4 ? "x" : (Math.abs(ddy)>Math.abs(ddx)+4 ? "y" : null);
    if(lock!=="x") return;   // 竖向滑动交给列表滚动,不拦截
    dragging=true;
    const base = row.classList.contains("swiped") ? -SWIPE_W : 0;
    dx = Math.max(-SWIPE_W, Math.min(0, base+ddx));
    body.style.transform="translateX("+dx+"px)"; act.style.transform="translateX("+(SWIPE_W+dx)+"px)";
  }, {passive:true});
  const end=()=>{
    row.classList.remove("swiping");
    body.style.transform=""; act.style.transform="";
    if(dragging){
      row._justSwiped=true;
      if(dx<=-SWIPE_W/2){ closeAllSwipes(row); row.classList.add("swiped"); S.swipedConv=row.dataset.conv; } else closeSwipe(row);
    }
    dragging=false;
  };
  row.addEventListener("touchend", end);
  row.addEventListener("touchcancel", end);
}
$("#convBody").addEventListener("scroll", ()=>closeAllSwipes());
function openConvMenu(btn, conv, slim){
  if(convMenuBtn===btn){ closeConvMenu(); return; }   // 再点一次同一个 → 收起
  closeHeaderPopovers();
  convMenuBtn=btn; btn.classList.add("on");
  const m=el("div","convmenu"); m.style.visibility="hidden";
  // 定时任务会话:菜单换成编辑/启停/删除(归档对定时任务没意义,见 buildCronJobRow)。
  // slim=true(标题栏「⋯」):启停开关和编辑图标已在标题栏直出,菜单里只剩删除。
  // 直接按 conv 精确匹配 S.cronJobs(不再靠前缀字符串猜,理由同 syncCronHeader)。
  const cronJob=(S.cronJobs||[]).find(x=>x.conv===conv);
  if(cronJob){
    const job=cronJob;
    if(!slim){
      const edit=el("button","cmitem"); edit.innerHTML=ic("edit")+" 编辑";
      edit.onclick=ev=>{ ev.stopPropagation(); closeConvMenu(); if(job) openCronModal(job); };
      m.append(edit);
      const enabled=job?!!job.enabled:true;
      const tg=el("button","cmitem"); tg.innerHTML=ic(enabled?"stop":"zap")+" "+(enabled?"停用":"启用");
      tg.onclick=ev=>{ ev.stopPropagation(); closeConvMenu(); if(job) toggleCronJob(job.job_id, !job.enabled); };
      m.append(tg);
    }
    const del=el("button","cmitem danger"); del.innerHTML=ic("trash")+" 删除";
    del.onclick=ev=>{ ev.stopPropagation(); closeConvMenu(); if(job) deleteCronJob(job.job_id, job.title, job.conv); };
    m.append(del);
  }else{
    const convObj=findConv(conv);
    const isMain=conv==="main";
    const isPinned=!!(convObj&&convObj.pinned);
    const ren=el("button","cmitem"); ren.innerHTML=ic("edit")+" 重命名";
    ren.onclick=ev=>{ ev.stopPropagation(); closeConvMenu(); renameConv(conv); };
    m.append(ren);
    if(!isMain){
      const pin=el("button","cmitem"); pin.innerHTML=ic("pin")+(isPinned?" 取消置顶":" 置顶");
      pin.onclick=ev=>{ ev.stopPropagation(); closeConvMenu(); pinConv(conv, !isPinned); };
      m.append(pin);
    }
    const isArch=!!(convObj&&convObj.archived);
    const arch=el("button","cmitem"); arch.innerHTML=ic("folder")+" "+(isArch?"取消归档":"归档");
    arch.onclick=ev=>{ ev.stopPropagation(); closeConvMenu(); toggleArchive(conv); };
    m.append(arch);
    const cpy=el("button","cmitem"); cpy.innerHTML=ic("copy")+" 复制";
    cpy.onclick=ev=>{ ev.stopPropagation(); closeConvMenu(); duplicateConv(conv); };
    m.append(cpy);
    const del=el("button","cmitem danger"); del.innerHTML=ic("trash")+" 删除";
    del.onclick=ev=>{ ev.stopPropagation(); closeConvMenu(); delConv(conv); };
    m.append(del);
  }
  document.body.append(m); convMenuEl=m;
  // 右对齐按钮,默认显示在其下方;贴到视口底部时自动上翻
  const r=btn.getBoundingClientRect();
  let top=r.bottom+4, left=r.right-m.offsetWidth;
  if(top+m.offsetHeight>window.innerHeight) top=r.top-4-m.offsetHeight;
  m.style.top=Math.max(6,top)+"px"; m.style.left=Math.max(6,left)+"px"; m.style.visibility="visible";
}
document.addEventListener("click", closeConvMenu);       // 点空白处关闭
$("#convBody").addEventListener("scroll", closeConvMenu); // 列表滚动时关闭,避免菜单错位
document.addEventListener("click", e=>{ if(!e.target.closest(".conv")) closeAllSwipes(); });  // 点空白处收起滑开的行

// 项目分组「更多」浮层菜单(单例):点 ⋯ 弹出,「删除项目」
let projMenuEl=null, projMenuBtn=null;
function closeProjMenu(){
  if(projMenuBtn){ projMenuBtn.classList.remove("on"); projMenuBtn=null; }
  if(projMenuEl){ projMenuEl.remove(); projMenuEl=null; }
}
function openProjMenu(btn, hash){
  if(projMenuBtn===btn){ closeProjMenu(); return; }   // 再点一次同一个 → 收起
  closeProjMenu();
  projMenuBtn=btn; btn.classList.add("on");
  const m=el("div","convmenu"); m.style.visibility="hidden";
  const del=el("button","cmitem danger"); del.innerHTML=ic("trash")+" 删除项目";
  del.title="移除项目(不删文件夹,可再加回)";
  del.onclick=ev=>{ ev.stopPropagation(); closeProjMenu(); removeProject(hash); };
  m.append(del); document.body.append(m); projMenuEl=m;
  const r=btn.getBoundingClientRect();
  let top=r.bottom+4, left=r.right-m.offsetWidth;
  if(top+m.offsetHeight>window.innerHeight) top=r.top-4-m.offsetHeight;
  m.style.top=Math.max(6,top)+"px"; m.style.left=Math.max(6,left)+"px"; m.style.visibility="visible";
}
document.addEventListener("click", closeProjMenu);
$("#convBody").addEventListener("scroll", closeProjMenu);
$("#messages").addEventListener("scroll", ()=>{
  updateScrollBtn();
  // 滚到顶部自动按最早一轮继续取历史,函数在 index.html 主脚本中声明。
  if(typeof loadEarlierHistory === "function") loadEarlierHistory();
}, {passive:true}); // 流式时用户上翻→显示↓按钮

// 按 conv id 找会话对象:普通会话在 S.convs,后台任务(语音/cron)行分别在 S.voiceSidebar.tasks / S.cronJobs
// (2026-07-29:定时任务漏查这里,导致打开定时任务会话时顶部上下文占用图标读不到数据而隐藏)
function findConv(conv){
  return S.convs.find(x=>x.conv===conv)
    || (S.voiceSidebar.tasks||[]).find(x=>x.conv===conv)
    || (S.cronJobs||[]).find(x=>x.conv===conv)
    || (S.searchConvs||[]).find(x=>x.conv===conv);
}
async function delConv(conv){
  // 语音/定时任务行不在 S.convs 里,从各自所在的列表里乐观移除(定时任务目前走 openConvMenu 的
  // deleteCronJob 专用分支,不会真的调到这里,这里补上纯防御,免得以后加了滑动删除入口却漏改)
  const list = S.convs.some(x=>x.conv===conv) ? S.convs
    : (S.voiceSidebar.tasks||[]).some(x=>x.conv===conv) ? S.voiceSidebar.tasks
    : (S.cronJobs||[]).some(x=>x.conv===conv) ? S.cronJobs
    : (S.searchConvs||[]).some(x=>x.conv===conv) ? S.searchConvs
    : null;
  if(!list) return;
  const idx=list.findIndex(x=>x.conv===conv);
  const removed=list[idx], wasActive=(S.conv===conv);
  // 乐观假删:先从列表移除并立即重绘,不等服务器
  list.splice(idx,1); delete S.histCache[conv]; idbDel("hist:"+conv); delete S.live[conv]; delete S.streamSnap[conv]; markLive(); renderConvs();
  if(wasActive) openConv(S.convs[0]?S.convs[0].conv:"main");   // 删的是当前会话→切到最近一个
  // 纯本地会话(从未发过消息)服务端没有记录,删完即止
  if(String(conv).startsWith("local-")) return;
  try{
    const r=await api("/conv/delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({conv})});
    const j=await r.json().catch(()=>({}));
    if(!r.ok || !j.ok && !j.need_confirm) throw new Error(j.error||"删除失败");
    if(j.need_confirm){
      // 会话 worktree 里有未提交改动/未合并提交:先回滚乐观假删,弹三选——
      // 归档保留(只提醒不丢内容)/ 仍然删除(force,worktree 内容一起丢) / 取消
      list.splice(idx,0,removed); renderConvs();
      const choice=await delConfirmChoice(dirtyBits(j.dirty||{}));
      if(choice==="archive"){ toggleArchive(conv); return; }  // 归档保留:代码留 worktree,只提醒不回收
      if(choice!=="delete") return;                            // 取消:列表已恢复
      const forceResp=await api("/conv/delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({conv,force:true})});
      const forceData=await forceResp.json().catch(()=>({}));
      if(!forceResp.ok || !forceData.ok) throw new Error(forceData.error||"删除失败");
      // force 删除成功后行已被回滚恢复,需再移除一次(否则残留到刷新还看得到)
      const f=list.findIndex(x=>x.conv===conv);
      if(f>=0){
        list.splice(f,1); delete S.histCache[conv]; idbDel("hist:"+conv); delete S.live[conv]; delete S.streamSnap[conv]; markLive(); renderConvs();
      }
    }
  }catch(err){
    // 删除失败:回滚列表并提示(行可能已恢复,先确认不在再插,避免重复行)
    if(!list.some(x=>x.conv===conv)) list.splice(idx,0,removed);
    renderConvs();
    alert((err&&err.message)||"删除失败,已恢复该会话");
  }
}
// 删除会话三选确认(worktree 有未提交改动时):归档保留 / 仍然删除 / 取消。
// 返回 "archive" | "delete" | "cancel"。
function delConfirmChoice(bits){
  return new Promise(res=>{
    const m=$("#delModal");
    $("#delDirty").textContent="该会话的代码还有 "+bits+",直接删除会全部丢失。";
    const done=v=>{ m.hidden=true; m.onclick=null; res(v); };
    $("#delArchive").onclick=()=>done("archive");
    $("#delForce").onclick=()=>done("delete");
    $("#delCancel").onclick=()=>done("cancel");
    m.onclick=e=>{ if(e.target===m) done("cancel"); };  // 点遮罩 = 取消
    m.hidden=false;
  });
}
async function renameConv(conv){
  const c=findConv(conv); if(!c) return;
  const title=(prompt("重命名会话", c.title||"")||"").trim();
  if(!title || title===c.title) return;
  const prev=c.title;
  c.title=title; renderConvs();   // 乐观更新,立即重绘
  // 纯本地会话(从未发过消息)服务端没有记录,改名只停在本地——发出第一条消息后
  // conv 会转正成新 key(见 composer.js sendMsg 的 wasLocal 分支),这里存了也白存
  if(String(conv).startsWith("local-")) return;
  try{
    const r=await api("/conv/rename",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({conv,title})});
    const j=await r.json().catch(()=>({}));
    if(!j.ok) throw new Error();
  }catch(e){
    c.title=prev; renderConvs(); alert("重命名失败");
  }
}
async function duplicateConv(conv){
  try{
    const r=await api("/conv/duplicate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({conv})});
    const j=await r.json().catch(()=>({}));
    if(!j.conv){ alert("复制失败"); return; }
    await loadConvs();  // 列表刷新出新副本(ts 已刷新,顶到最前)
    openConv(j.conv);   // 直接打开副本,标题「原标题副本」立即可见
  }catch(err){ alert("复制失败"); }
}
