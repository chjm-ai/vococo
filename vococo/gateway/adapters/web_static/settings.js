"use strict";
// 2026-08-14 从 index.html 拆出(前端模块化):设置面板(外观/通知/WebPush/MCP/模型/技能/记忆/人设)+ 会话菜单与滑动操作。
// 与内联脚本同属全局作用域(无构建步骤),加载顺序见 index.html。

// ── 设置(MCP / 技能 / 记忆 / 人设)────────────────────────────────────────
const SET = { data:null, tab:"stats", skq:"", skScope:"general", mcpFormOpen:false, modelFormOpen:false, providerFormOpen:false };

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
$("#setClose").onclick = closeSettings;
$("#setModal").onclick = e=>{ if(e.target===$("#setModal")) closeSettings(); };
$("#setTabs").onclick = e=>{
  const b=e.target.closest(".settab"); if(!b) return;
  SET.tab=b.dataset.tab;
  document.querySelectorAll(".settab").forEach(t=>t.classList.toggle("active", t.dataset.tab===SET.tab));
  renderSetTab();
};
function renderSetTab(){
  if(SET.tab==="stats") renderStatsPane();   // 见 stats.js
  else if(SET.tab==="mcp") renderMcpPane();
  else if(SET.tab==="models") renderModelsPane();
  else if(SET.tab==="skill") renderSkillPane();
  else if(SET.tab==="memory") renderFileList("memory");
  else if(SET.tab==="agents") renderFileList("agents");
  else if(SET.tab==="notify") renderNotifyPane();
  else if(SET.tab==="security") renderSecurityPane();
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

// ── 安全分区(永久审批规则 / 审批记录;待处理的审批在铃铛里,见 notices.js)────────
// 数据来自 GET /security(memory/approvals.py)。审批记录可按结果筛选。
const SEC = { filter:"" };
const SEC_KIND = { write_dir:"写入目录", git_push:"git push", mcp_tool:"外部工具" };
const SEC_FILTERS = [["","全部"],["denied","你拒绝了"],["timeout","超时"],["noninteractive_deny","无人值守拒绝"],["blocked","直接拦截"],["rule","命中规则"]];
async function renderSecurityPane(){
  let h = '<h3>'+ic("lock")+'安全</h3>'+
    '<p class="shint">危险操作的审批规则和记录。点「永远允许」存下的规则列在这里,不需要了随时删掉。</p>';
  h += '<div id="secBody" class="setempty">加载中…</div>';
  $("#setPane").innerHTML = h;
  await loadSecurity();
}
function secConvLink(key){
  if(!key) return '<span class="dim">—</span>';
  return '<a href="#" data-secconv="'+esc(key)+'">'+esc(key)+'</a>';
}
async function loadSecurity(){
  const box=$("#secBody"); if(!box) return;
  let d;
  try{ d=await (await api("/security?decision="+encodeURIComponent(SEC.filter))).json(); }
  catch(e){ box.innerHTML='加载失败:'+esc(e&&e.message||e); return; }
  const dec=d.decisions||{};
  let h='';
  h+='<p class="shint">等审批超时、或夜里免打扰没推送的操作,都在右上角铃铛里,点选项就能补批并让任务接着跑。</p>';
  h+='<div class="sechd">永久规则</div>';
  if(!(d.rules||[]).length) h+='<div class="setempty">还没有规则。审批弹窗里点「永远允许」会存到这里。</div>';
  // 30 天没命中过的规则标出来(和后端每月的清理提醒同一口径,见 notice_actions.stale_rules)
  const staleCut=Date.now()/1000-30*86400;
  if((d.rules||[]).length) h+=d.rules.map(r=>settingsRow("", "", esc(SEC_KIND[r.kind]||r.kind)+' · '+esc(r.scope)+
      ((r.last_used_at||r.created_at)<staleCut?'<span class="mctag">30 天没用过</span>':''),
      esc(r.label)+'<br>'+esc(fmtTime(r.created_at))+' 创建 · 命中 '+r.hits+' 次'+
      (r.last_used_at?' · 最近 '+esc(fmtTime(r.last_used_at)):' · 从没用过'), false,
      '<button class="miniact danger" data-secdel="'+esc(r.id)+'">删除</button>')).join("");
  h+='<div class="sechd">审批记录</div><p class="shint">需要审批或被拦下的操作,保留 90 天。</p>';
  h+='<div class="secfilters">'+SEC_FILTERS.map(([v,l])=>
      '<button class="btn sm '+(SEC.filter===v?'primary':'ghost')+'" data-secf="'+v+'">'+l+'</button>').join("")+'</div>';
  if(!(d.audit||[]).length) h+='<div class="setempty">没有记录。</div>';
  else h+=d.audit.map(a=>settingsRow("", "", esc(dec[a.decision]||a.decision)+' · '+esc(a.reason||a.tier),
      esc(a.detail)+'<br>'+esc(fmtTime(a.ts))+' · '+esc(a.tool)+' · '+secConvLink(a.session_key), false, '')).join("");
  box.className=""; box.innerHTML=h;
  box.querySelectorAll("[data-secf]").forEach(b=>b.onclick=()=>{ SEC.filter=b.dataset.secf; loadSecurity(); });
  box.querySelectorAll("[data-secconv]").forEach(a=>a.onclick=e=>{ e.preventDefault(); closeSettings(); openConv(a.dataset.secconv); });
  box.querySelectorAll("[data-secdel]").forEach(b=>b.onclick=async()=>{
    if(!confirm("删除这条规则?之后同类操作会重新询问。")) return;
    b.disabled=true;
    const r=await api("/security/rule/delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:b.dataset.secdel})});
    if(!r.ok){ alert("删除失败"); b.disabled=false; return; }
    loadSecurity();
  });
}

// ── 通知分区(Web Push 系统通知)──────────────────────────────────────────
function isIOS(){
  return /iP(hone|ad|od)/.test(navigator.userAgent);
}
function isIOSNonStandalone(){
  return isIOS() && !isStandalone();
}
function permissionDeniedMessage(){
  return isIOS()
    ? "通知权限被拒。iOS 需删掉主屏图标 + 清掉网站数据后,重新加主屏再试。"
    : "通知权限被拒。请在浏览器或系统设置中允许 vococo 发送通知后再试。";
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
  if(denied){ tgl.checked=false; tgl.disabled=false; desc.textContent="通知权限已被浏览器拒绝。点击右侧开关查看解除步骤"; }
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
function openSettingsTab(tab){
  SET.tab=tab;
  document.querySelectorAll(".settab").forEach(t=>t.classList.toggle("active", t.dataset.tab===tab));
  openSettings();
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
    throw new Error(permissionDeniedMessage());
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
    '<p class="shint">MCP(Model Context Protocol)给 vococo 接入额外工具。开关改动下一条消息即生效,无需新建会话、历史不丢。外部工具 schema 很占上下文(如 lemlist 120 个工具 ≈11 万 token/轮),不用时建议关;也可以直接对 vococo 说「开外贸工具 / 关掉 lemlist」。</p>';
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

// ── 模型分区(模型面板同源显示/隐藏,保存配置不切换当前会话模型)───────────────
function modelConfig(id){
  const md=SET.data.models, choices=md.choices||md.builtin||[];
  const choice=choices.find(m=>m.id===id)||{};
  const configId=choice.config_id||id;
  const custom=(md.custom||[]).find(m=>m.id===configId)||{};
  // 第三方 Claude 的面板 ID 带供应商前缀，编辑时仍须更新原始配置记录。
  return {...custom,...choice,id:configId,label:custom.label??choice.label,
    context_window:Object.hasOwn(custom,"context_window")?custom.context_window:choice.context_window,
    effort_levels:Object.hasOwn(custom,"effort_levels")?custom.effort_levels:choice.effort_levels};
}
async function modelSettingsRequest(url, body){
  const response=await api(url, body ? {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)} : undefined);
  const result=await response.json().catch(()=>{ throw new Error("服务器返回无效数据(HTTP "+response.status+")"); });
  if(!response.ok || result.error) throw new Error(result.error||"请求失败(HTTP "+response.status+")");
  return result;
}
async function refreshChatModelList(){
  const previous=S.models;
  await loadModels();
  // loadModels 自己吞掉网络异常，在这里补回设置面板的失败提示。
  if(S.models===previous || !Array.isArray(S.models&&S.models.choices))
    throw new Error("配置已保存，但聊天页模型列表刷新失败，请重试");
}
async function refreshModelSettings(){
  await refreshChatModelList();
  SET.data=await modelSettingsRequest("/settings");
}
function verbositySettingsHtml(md){
  return modelOutputSettingsHtml(md,"verbosities","verbosity","输出详细程度",
    "按 GPT 模型分别保存，下一条消息生效；低更简洁，中兼顾细节，高更详细。思考强度独立设置。");
}
function summarySettingsHtml(md){
  return modelOutputSettingsHtml(md,"summaries","summary","思考摘要",
    "只控制展示的思考摘要，不改变最终回答详略或思考强度。需要模型和代理支持；简单问题可能不生成摘要。");
}
function modelOutputSettingsHtml(md,key,field,title,hint){
  const states=md[key]||{}, choices=md.choices||md.builtin||[];
  let rows="";
  choices.forEach(model=>{
    const state=states[model.id]; if(!state) return;
    const options=state.levels.map(([value,label])=>'<option value="'+esc(value)+'"'+
      (state.value===value?' selected':'')+'>'+esc(label)+'</option>').join("");
    rows+='<div class="fld"><label>'+esc(model.label||model.id)+'</label>'+
      '<select data-'+field+'="'+esc(model.id)+'" aria-label="'+esc(model.label||model.id)+' '+title+'">'+options+'</select></div>';
  });
  if(!rows) return "";
  return '<div class="sechd">'+title+'</div><p class="shint">'+hint+'</p>'+
    '<div class="mcpform">'+rows+'<div class="mcperr" data-'+field+'-error role="status"></div></div>';
}
function bindVerbositySettings(){ bindOutputSettings("verbosities","verbosity","/verbosity"); }
function bindSummarySettings(){ bindOutputSettings("summaries","summary","/reasoning-summary"); }
function bindOutputSettings(key,field,url){
  const pane=$("#setPane"), error=pane.querySelector("[data-"+field+"-error]");
  pane.querySelectorAll("[data-"+field+"]").forEach(select=>{
    select.onchange=async()=>{
      const model=select.dataset[field], state=SET.data.models[key][model], old=state.value;
      select.disabled=true; error.textContent="";
      try{
        const response=await api(url,{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({model,[field]:select.value})});
        const result=await response.json();
        if(!response.ok) throw new Error(result.error||"保存失败");
        state.value=result[field];
      }catch(e){ select.value=old; error.textContent=e.message||"保存失败"; }
      finally{ select.disabled=false; }
    };
  });
}
function renderModelsPane(){
  const md = SET.data.models || {active:"", choices:[], builtin:[], custom:[], providers:[]};
  const curTag = ' <span class="mctag cur">当前</span>';
  let h = '<h3>'+ic("bot")+'模型 & 服务商</h3>'+
    '<p class="shint">"当前"标的是这个会话正在用的模型。新模型发布但代码还没跟上时,先在下面手动补一档;'+
    '第三方服务商(DeepSeek/Kimi/中转等)也可以直接在这加。名称、排序和显示开关保存后立即更新列表，不切换当前会话模型。</p>'+
    '<div class="mcperr" id="modelsErr" role="status"></div>';

  h += verbositySettingsHtml(md);
  h += summarySettingsHtml(md);
  h += '<div class="sechd">模型面板显示</div>'+
    '<p class="shint" style="margin:2px 0 6px">这里和聊天页模型面板完全一致；关闭后从面板隐藏，随时可恢复。</p>';
  (md.choices||md.builtin||[]).forEach(m=>{
    const cur = m.id===md.active ? curTag : '';
    const acts = '<button class="miniact" data-mdedit="'+esc(m.id)+'">编辑</button>'+
      '<label class="sw"><input type="checkbox" data-modeltgl="'+esc(m.id)+'"'+(m.disabled?'':' checked')+'><span class="track"></span></label>';
    h += settingsRow("data-modelrow", m.id, esc(m.label)+cur, esc(m.id), !!m.disabled, acts);
  });

  h += '<div class="sechd">自定义模型档位配置</div>';
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
  const cur = editing ? modelConfig(editing) : {};
  const providers=SET.data.models.providers||[];
  let options='<option value="">官方订阅</option>';
  providers.forEach(p=>{ options+='<option value="'+esc(p.name)+'"'+(cur.provider===p.name?' selected':'')+'>'+esc(p.label||p.name)+'</option>'; });
  if(cur.provider && !providers.some(p=>p.name===cur.provider))
    options+='<option value="'+esc(cur.provider)+'" selected>'+esc(cur.provider)+'（未配置）</option>';
  return '<div class="mcpform" id="mdForm">'+
    '<div class="fld"><label>模型 id(填 API 认的那个模型名)</label>'+
      '<input id="mdId" placeholder="claude-opus-5-5" value="'+esc(cur.id||"")+'"'+(editing?' readonly':'')+'></div>'+
    '<div class="fld"><label>显示名(可空,默认用 id)</label><input id="mdLabel" placeholder="Opus 5.5（订阅）" value="'+esc(cur.label||"")+'"></div>'+
    '<div class="fld"><label>供应商'+(cur.builtin?'（内置模型路由不可修改）':'')+'</label><select id="mdProvider"'+(cur.builtin?' disabled':'')+'>'+options+'</select></div>'+
    '<div class="fld"><label>分组内排序（-1000000 到 1000000，越小越靠前）</label><input id="mdOrder" type="number" step="1" value="'+esc(String(cur.order??1000))+'"></div>'+
    '<div class="fld"><label for="mdContext">上下文窗口（留空使用默认）</label><input id="mdContext" type="text" inputmode="numeric" value="'+esc(String(cur.context_window??""))+'"></div>'+
    '<div class="fld"><label for="mdEfforts">思考档位（逗号分隔，留空使用默认）</label><input id="mdEfforts" placeholder="low,medium,high,xhigh,ultra,max,off" value="'+esc(Array.isArray(cur.effort_levels)?cur.effort_levels.join(","):"")+'"></div>'+
    '<div class="mcperr" id="mdErr" role="status"></div>'+
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
  bindVerbositySettings();
  bindSummarySettings();
  $("#setPane").querySelectorAll("[data-modeltgl]").forEach(inp=>{
    inp.onchange = async()=>{
      const id=inp.dataset.modeltgl, disabled=!inp.checked;
      const err=$("#modelsErr"); err.textContent=""; inp.disabled=true;
      let saved=false;
      try{
        await modelSettingsRequest("/settings/model",{action:"toggle", id, disabled});
        saved=true;
        const choices=SET.data.models.choices||SET.data.models.builtin||[];
        const m=choices.find(x=>x.id===id); if(m) m.disabled=disabled;
        inp.closest(".srow").classList.toggle("off", disabled);
        await refreshChatModelList();
      }catch(e){ if(!saved) inp.checked=disabled; err.textContent=e.message||"保存失败"; }
      finally{ inp.disabled=false; }
    };
  });
  $("#setPane").querySelectorAll("[data-mdedit]").forEach(b=>{
    b.onclick = ()=>{ SET.modelEditing=b.dataset.mdedit; SET.modelFormOpen=true; renderModelsPane(); };
  });
  $("#setPane").querySelectorAll("[data-mddel]").forEach(b=>{
    b.onclick = async()=>{
      const id=b.dataset.mddel;
      if(!confirm("删除模型档位配置「"+id+"」?内置或供应商已有模型只会恢复默认显示。")) return;
      const err=$("#modelsErr"); err.textContent=""; b.disabled=true;
      try{
        await modelSettingsRequest("/settings/model",{action:"remove", id});
        await refreshModelSettings();
        if(SET.modelEditing===id){ SET.modelFormOpen=false; SET.modelEditing=null; }
        renderModelsPane();
      }catch(e){ err.textContent=e.message||"删除失败"; }
      finally{ b.disabled=false; }
    };
  });
  $("#setPane").querySelectorAll("[data-pvedit]").forEach(b=>{
    b.onclick = ()=>{ SET.providerEditing=b.dataset.pvedit; SET.providerFormOpen=true; renderModelsPane(); };
  });
  $("#setPane").querySelectorAll("[data-pvdel]").forEach(b=>{
    b.onclick = async()=>{
      const name=b.dataset.pvdel;
      if(!confirm("删除服务商「"+name+"」?")) return;
      const err=$("#modelsErr"); err.textContent=""; b.disabled=true;
      try{
        await modelSettingsRequest("/settings/provider",{action:"remove", name});
        await refreshModelSettings();
        if(SET.providerEditing===name){ SET.providerFormOpen=false; SET.providerEditing=null; }
        renderModelsPane();
      }catch(e){ err.textContent=e.message||"删除失败"; }
      finally{ b.disabled=false; }
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
  const orderText=$("#mdOrder").value.trim(), order=orderText===""?1000:Number(orderText);
  if($("#mdOrder").validity.badInput || !Number.isSafeInteger(order)){ err.textContent="排序必须是整数"; return; }
  const contextText=$("#mdContext").value.trim(), context_window=contextText?Number(contextText):null;
  if(contextText && (!/^\d+$/.test(contextText) || !Number.isSafeInteger(context_window) || context_window<=0)){
    err.textContent="上下文窗口必须是正整数，留空使用默认"; return;
  }
  const effortText=$("#mdEfforts").value.trim();
  const effort_levels=effortText?effortText.split(",").map(level=>level.trim()):null;
  const allowed=["low","medium","high","xhigh","ultra","max","off"];
  if(effort_levels && (effort_levels.some(level=>!allowed.includes(level)) || new Set(effort_levels).size!==effort_levels.length)){
    err.textContent="思考档位须用英文逗号分隔，且不能重复；支持 low, medium, high, xhigh, ultra, max, off"; return;
  }
  const body={action:"add", id, label:$("#mdLabel").value.trim(), order, context_window, effort_levels};
  // 新增表单也可能填入已有内置 ID，不能借此改掉它的路由。
  if(!modelConfig(id).builtin) body.provider=$("#mdProvider").value;
  const btn=$("#mdSave"); btn.disabled=true;
  try{
    await modelSettingsRequest("/settings/model",body);
    await refreshModelSettings();
    SET.modelFormOpen=false; SET.modelEditing=null;
    renderModelsPane();
  }catch(e){ err.textContent=e.message||"保存失败"; }
  finally{ btn.disabled=false; }
}
async function saveProviderForm(){
  const err=$("#pvErr"); err.textContent="";
  const name=$("#pvName").value.trim();
  if(!name){ err.textContent="请填名称"; return; }
  const base_url=$("#pvUrl").value.trim();
  const model=$("#pvModel").value.trim();
  if(!base_url||!model){ err.textContent="base_url 和 model 都要填"; return; }
  const body={action:"add", name, base_url, model, api_key:$("#pvKey").value.trim(), label:$("#pvLabel").value.trim(), vision:($("#pvVision")&&$("#pvVision").checked)?"1":"", mgmt_key:$("#pvMgmt")?$("#pvMgmt").value.trim():""};
  const btn=$("#pvSave"); btn.disabled=true;
  try{
    await modelSettingsRequest("/settings/provider",body);
    await refreshModelSettings();
    SET.providerFormOpen=false; SET.providerEditing=null;
    renderModelsPane();
  }catch(e){ err.textContent=e.message||"保存失败"; }
  finally{ btn.disabled=false; }
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

vococoLazy.ready("settings");
