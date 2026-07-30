// 工具卡片渲染(diff / todo 清单 / 计划卡 / 命令预览)+ 子代理动态区 + 结果回填。
// 2026-07-23 从 index.html 拆出——被实时流(applyStreamEvent)和历史回放
// (renderEventsFlow)两条路径共用,只依赖页面级工具函数 el/esc/ic/mdToHtml/DOTS
// (定义在 index.html 主脚本里,靠同页多个 <script> 共享同一全局作用域读取,
// 不需要模块系统)。不碰语音通话 IIFE 的闭包状态。

function toolCardShell(title, pathText, bodyEl, collapsed, docPath){
  const card=el("div","toolcard");
  const head=el("div","tc-head");
  const chev=el("span","chev"); if(!collapsed) chev.classList.add("down");
  const titleEl=el("span"); titleEl.innerHTML=title;
  head.append(chev, titleEl);
  const p=el("span","tc-path"); if(pathText) p.textContent=pathText; head.append(p);  // 始终留位,结果预览可后补
  // docPath:这张卡对应一个本地文件(Write/Edit 的 file_path)→ 路径本身变成可点,
  // 右侧分屏预览,不用先展开 diff 再脑内脑补最终内容长什么样(见 index.html 的 openDocPreview)。
  if(docPath){
    p.classList.add("tc-path-doc"); p.title="点击预览文件";
    p.onclick=(e)=>{ e.stopPropagation(); if(typeof openDocPreview==="function") openDocPreview({kind:"path", target:docPath, title:docPath.split("/").pop()||docPath}); };
  }
  const bodyWrap=el("div","tc-body"+(collapsed?" collapsed":""));
  if(bodyEl) bodyWrap.append(bodyEl);
  head.onclick=()=>{ const c=bodyWrap.classList.toggle("collapsed"); chev.classList.toggle("down",!c); };
  card.append(head,bodyWrap);
  card._head=head; card._chev=chev; card._body=bodyWrap; card._path=p;
  return card;
}
function diffEl(oldStr, newStr){
  const d=el("div","diff");
  if(oldStr){ for(const l of oldStr.split("\n")){ const s=el("span","ln del"); s.textContent=l; d.append(s); } }
  if(newStr){ for(const l of newStr.split("\n")){ const s=el("span","ln add"); s.textContent=l; d.append(s); } }
  return d;
}
// ── 子代理(Task)动态:嵌进 Task 卡片的实时工具 chips ──────────────────────
function subUpsert(s, pid, item){
  const st = s.subs[pid] || (s.subs[pid]={list:[]});
  let t = item.id ? st.list.find(x=>x.id===item.id) : null;
  if(!t && item.done) t = [...st.list].reverse().find(x=>x.name===item.name && !x.done);
  if(t){ Object.assign(t, item); }
  else {
    st.list.push(item);
    const p=s.tools.find(x=>x.id===pid); if(p) p.subN=(p.subN||0)+1;  // Task chip 上的步数
  }
  renderSub(s, pid);
}
function renderSub(s, pid){
  const card=s.cards[pid]; if(!card||!card._sub) return;
  const list=(s.subs[pid]||{list:[]}).list;
  const bits=['<span>'+ic("zap")+" "+list.length+' 步</span>'];
  for(const x of list.slice(-8))
    bits.push('<span>'+esc(x.name)+(x.done?(x.ok?" ✓":" ✗"):DOTS)+'</span>');
  card._sub.innerHTML=bits.join("");
}
// ── 工具结果:折进入参卡的展开区(同一张卡,不再单独占一行)──────────────────
// 没有入参卡的工具(出错/有输出)才补一张最小卡
function applyResult(card, e){
  const det=(e.detail||"").trim();
  if(!e.ok) card._head.classList.add("err");                 // 出错:整行标红
  if(e.preview && card._path && !card._path.textContent) card._path.textContent=e.preview;  // 头部无路径时补结果预览
  if(det){
    if(card._body.childNodes.length) card._body.append(el("div","tc-sep"));  // 已有入参 → 加分隔线
    const body=el("pre","tc-res-body"); body.textContent=det; card._body.append(body);
  }
}
function attachResult(s, e){
  const det=(e.detail||"").trim();
  let card=e.tool_id ? s.cards[e.tool_id] : null;
  if(!card){
    if(e.ok && !det) return;              // 成功且无输出:chip 的 ✓ 已表达,不补卡
    card=toolCardShell(ic(e.ok?"wrench":"warn")+" "+esc(e.name), "", null, true);
    if(e.tool_id) s.cards[e.tool_id]=card;
    s.flow.append(card);
  }
  applyResult(card, e);
  scrollDown();
}
function renderToolCard(name, inp){
  try{
    if(name==="Task"||name==="Agent"){   // 子代理:新版工具名 Agent,老版 Task,两者都收
      // 子代理卡片:标题=描述,收起的正文=完整指令;动态区(_sub)实时滚动工具步
      const meta=el("div","tc-code");
      meta.textContent=(inp.subagent_type?("类型:"+inp.subagent_type+"\n\n"):"")+(inp.prompt||"");
      const card=toolCardShell(ic("bot")+" 子代理", inp.description||"", meta, true);
      const sub=el("div","tc-sub"); card.append(sub); card._sub=sub;
      return card;
    }
    if(name==="TodoWrite"){
      const todos=inp.todos||[]; if(!todos.length) return null;
      const ul=el("ul","todo");
      for(const t of todos){
        const st=t.status||"pending";
        const li=el("li", st==="completed"?"done":(st==="in_progress"?"doing":""));
        const mark=st==="completed"?"☑":(st==="in_progress"?"◐":"☐");
        li.innerHTML='<span>'+mark+'</span><span>'+esc(t.content||t.activeForm||"")+'</span>';
        ul.append(li);
      }
      return toolCardShell(ic("clip")+" 待办清单","",ul,false);
    }
    if(name==="ExitPlanMode"){
      const p=el("div","plan"); p.innerHTML=mdToHtml(inp.plan||"(空计划)");
      return toolCardShell(ic("compass")+" 执行计划","",p,false);
    }
    if(name==="Write")     return toolCardShell(ic("doc")+" 写文件", inp.file_path||"", diffEl("", inp.content||""), true, inp.file_path);
    if(name==="Edit")      return toolCardShell(ic("edit")+" 编辑", inp.file_path||"", diffEl(inp.old_string||"", inp.new_string||""), true, inp.file_path);
    if(name==="MultiEdit"){
      const wrap=el("div");
      for(const ed of (inp.edits||[])) wrap.append(diffEl(ed.old_string||"", ed.new_string||""));
      return toolCardShell(ic("edit")+" 多处编辑", inp.file_path||"", wrap, true, inp.file_path);
    }
    if(name==="Bash"){
      const c=el("div","tc-code"); c.textContent=inp.command||"";
      return toolCardShell(ic("command")+" 命令", inp.description||"", c, true);
    }
    // 其余工具:有明显目标参数才给个折叠预览,避免刷屏
    const hint=inp.file_path||inp.path||inp.pattern||inp.query||inp.url||"";
    if(hint){ const c=el("div","tc-code"); c.textContent=hint; return toolCardShell(ic("wrench")+" "+esc(name), "", c, true, inp.file_path); }
    return null;
  }catch(e){ return null; }
}
