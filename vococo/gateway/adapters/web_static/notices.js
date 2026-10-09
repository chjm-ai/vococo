"use strict";
// 2026-09-28 铃铛:所有「等你回答的提问 / 等你批的操作」汇总在标题栏右上角(后端见 memory/notices.py)。
// 2026-10-10 加「拍板」:各 Agent 用 request_decision 放进来的待决事项,点选项 = 发回该 Agent 主会话执行。
// 等待中的选项直接点 = 按时回答;已超时的点了 = 把选择补回原会话让它接着干(gateway/notice_actions.py)。
// 与内联脚本同属全局作用域(无构建步骤),依赖 app-core / markdown / stream 里的 api、esc、mdToHtml、openConv 等。

const NTC = { items:[], seen:new Set(), loading:false };
const NTC_ICON = { "允许一次":"✅", "本次会话都允许":"♾️", "本轮任务都允许":"♾️", "永远允许":"📌", "拒绝":"🛑" };

// 铃铛在侧栏底部「筛选」按钮旁(#noticeBtn),有待处理时显示红底数字。手机上侧栏默认收起,
// 所以同时给 body 挂 has-notices,各视图「打开侧栏」的按钮上出一个小红点(见 styles.css)。
$("#noticeBtn").innerHTML = ic("bell") + '<span class="ntbadge" hidden></span>';

async function loadNotices(){
  if(NTC.loading) return;
  NTC.loading = true;
  try{
    const r = await api("/notices");
    if(!r.ok) return;
    NTC.items = (await r.json()).items || [];
  }catch(e){ return; }
  finally{ NTC.loading = false; }
  renderNoticeBadge();
  if(!$("#noticePop").hidden) renderNoticePop();
  autoPopupNewPending();
}

function renderNoticeBadge(){
  const n = NTC.items.length;
  document.body.classList.toggle("has-notices", n > 0);
  document.querySelectorAll("[data-notice-bell]").forEach(btn=>{
    const b = btn.querySelector(".ntbadge");
    if(b){ b.hidden = !n; b.textContent = n > 99 ? "99+" : String(n); }
    btn.title = n ? `${n} 件事等你处理` : "没有待处理的事";
  });
}

// 后台任务新弹出的审批/提问,当前不在那个会话时自动弹确认窗(每条只弹一次,重连不重复弹)
function autoPopupNewPending(){
  for(const it of NTC.items){
    if(it.status!=="pending" || !it.clarify_id || !it.conv || NTC.seen.has(it.id)) continue;
    NTC.seen.add(it.id);
    if(it.conv===S.conv || S.pendingChoice[it.conv]) continue;  // 当前会话里已内联显示 / 已弹过
    const e = { conv:it.conv, type:"choice", prompt:it.prompt,
      options: it.options.map((lab,i)=>["/clarify "+it.clarify_id+" "+i, (NTC_ICON[lab]?NTC_ICON[lab]+" ":"")+lab]) };
    S.pendingChoice[it.conv] = e;
    openChoiceModal(it.conv, e);
  }
}

function noticeItemHtml(it){
  const state = it.kind==="review" ? '<span class="ntstate late">每月提醒</span>'
    : it.kind==="decision" ? '<span class="ntstate wait">等你拍板 · 选了它接着做</span>'
    : it.status==="pending" ? '<span class="ntstate wait">等你回答</span>' : '<span class="ntstate late">错过了 · 点选项接着做</span>';
  const kind = {approval:"审批", ask:"提问", review:"规则清理", decision:"拍板"}[it.kind] || "通知";
  const opts = it.options.length
    ? it.options.map(lab=>`<button class="btn sm ghost" data-ntact="${esc(it.id)}" data-label="${esc(lab)}">${NTC_ICON[lab]?NTC_ICON[lab]+" ":""}${esc(lab)}</button>`).join("")
    : `<button class="btn sm ghost" data-ntgo="${esc(it.conv||"")}">去会话里回答</button>`;
  return `<div class="ntrow">
    <div class="ntmeta">${kind} · ${state} · ${esc(fmtTime(it.ts))}</div>
    ${it.conv?`<a href="#" class="nttitle" data-ntgo="${esc(it.conv)}">${esc(it.title||"会话")}</a>`:""}
    <div class="ntprompt">${mdToHtml(it.prompt||"")}</div>
    <div class="ntopts">${opts}${it.status==="expired"?`<button class="miniact" data-ntdismiss="${esc(it.id)}">忽略</button>`:""}</div>
  </div>`;
}

function renderNoticePop(){
  const pop = $("#noticePop"), items = NTC.items;
  // 待拍板的事不进「全部忽略」(后端 dismiss_all 同样跳过),只能逐条选或忽略
  const anyExpired = items.some(it=>it.status==="expired" && it.kind!=="decision");
  // 没有任何设备订阅推送 = 超时的事只能来这里看,提醒去开
  const noPush = typeof PUSH!=="undefined" && PUSH.enabled && PUSH.count===0;
  pop.innerHTML = `<div class="nthead">待处理 <span>${items.length}</span>
      ${anyExpired?'<button class="miniact" id="ntDismissAll">全部忽略</button>':""}</div>
    ${noPush?'<div class="ntwarn">手机通知没开(没有设备订阅推送),要批的事不会提醒你。<a href="#" id="ntPushGo">去开启</a></div>':""}
    <div class="ntlist">${items.length ? items.map(noticeItemHtml).join("")
      : '<div class="ntempty">没有待处理的事。提问或审批没来得及点,会留在这里,点选项就能让会话接着做。</div>'}</div>`;
  pop.querySelectorAll("[data-ntgo]").forEach(a=>a.onclick=e=>{
    e.preventDefault(); const c=a.dataset.ntgo; if(!c) return;
    pop.hidden=true; openConv(c);
  });
  pop.querySelectorAll("[data-ntact]").forEach(b=>b.onclick=()=>actNotice(b.dataset.ntact, b.dataset.label, b));
  pop.querySelectorAll("[data-ntdismiss]").forEach(b=>b.onclick=async()=>{
    b.disabled=true;
    await api("/notices/dismiss",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:b.dataset.ntdismiss})}).catch(()=>{});
    loadNotices();
  });
  const go=$("#ntPushGo");
  if(go) go.onclick=e=>{ e.preventDefault(); pop.hidden=true; openSettingsTab("notify"); };
  const all=$("#ntDismissAll");
  if(all) all.onclick=async()=>{
    if(!confirm("忽略所有已超时的待处理项?")) return;
    await api("/notices/dismiss",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({all:true})}).catch(()=>{});
    loadNotices();
  };
}

async function actNotice(id, label, btn){
  const row = btn.closest(".ntrow");
  row.querySelectorAll("button").forEach(x=>x.disabled=true);
  const it = NTC.items.find(x=>x.id===id);
  let d = {};
  try{
    const r = await api("/notices/act",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id,label})});
    d = await r.json().catch(()=>({}));
    if(!r.ok) throw new Error(d.error||("HTTP "+r.status));
  }catch(e){
    alert("没处理成:"+(e&&e.message||e));
    row.querySelectorAll("button").forEach(x=>x.disabled=false);
    return;
  }
  if(it && it.conv) delete S.pendingChoice[it.conv];
  $("#noticePop").hidden = true;
  await loadNotices();
  if(d.open){ openSettingsTab(d.open); return; }  // 规则清理提醒 → 设置页「安全」
  const conv = d.conv || (it && it.conv);
  if(conv && label!=="拒绝") openConv(conv);  // 去看它接着干
}

function openNoticePop(){
  closeHeaderPopovers($("#noticePop"));
  $("#noticePop").hidden = false;
  renderNoticePop();
  loadNotices();
}
document.addEventListener("click", e=>{
  const pop=$("#noticePop");
  if(e.target.closest("[data-notice-bell]")){
    e.stopPropagation();
    if(pop.hidden) openNoticePop(); else pop.hidden = true;
    return;
  }
  if(!pop.hidden && !pop.contains(e.target)) pop.hidden=true;
}, true);
// 早间汇总推送的链接带 ?notices=1:打开页面后直接展开铃铛
if(new URLSearchParams(location.search).get("notices")) setTimeout(()=>{ if(S.token) openNoticePop(); }, 1500);
