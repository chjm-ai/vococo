"use strict";
// 2026-08-14 从 index.html 拆出(前端模块化):Markdown 渲染(mdToHtml)+ 文档预览分屏(openDocPreview)。
// 与内联脚本同属全局作用域(无构建步骤),加载顺序见 index.html。

// ── Markdown 渲染(内置,不依赖 CDN)────────────────────────────────────────
// 文档预览:识别"看起来是文档"的链接/本地路径,点击不跳新标签页,而是右侧分屏预览
// (见 openDocPreview)。http(s) 链接后缀命中或落在 /pub/ 发布目录下才算文档;
// 本地路径(无协议头,以 / . ~ 开头)只要后缀命中就算,不要求存在——服务端会校验。
const DOC_EXT_RE = /\.(md|markdown|txt|html?|pdf|csv|json|log|ya?ml|py|jsx?|tsx?|sh|css|xml)(\?[^\s)]*)?$/i;
function isDocUrl(url){
  if(/^https?:\/\//i.test(url)) return DOC_EXT_RE.test(url) || /\/pub\//.test(url);
  return DOC_EXT_RE.test(url);
}
function docLinkHtml(txt, target){
  const isHttp = /^https?:\/\//i.test(target);
  const attr = isHttp ? 'data-doc-url="'+target+'"' : 'data-doc-path="'+target+'"';
  // href 留着当兜底:点击处理器(见 initDocPreview)正常都会 preventDefault 走站内预览,
  // 万一 JS 没跑起来,http 链接至少还能退化成新标签页打开;本地路径不写 href——
  // 写 href="#" 会被 iOS 长按菜单"拷贝链接"拿到(结果是域名/#,没用),不写 href
  // 的 <a> 不会触发 iOS 链接长按菜单,用户长按变成文字选中,可以正常拷贝路径文本。
  return '<a'+(isHttp?' href="'+target+'"':'')+' class="doclink" '+attr+(isHttp?' target="_blank" rel="noopener"':"")+'>'+ic("doc")+" "+txt+"</a>";
}
function inlineMd(t){
  t = esc(t);
  // 占位机制:markdown 链接、反引号代码 两类都先抠出来存进 parts、原地留一个不会被后面
  // 的正则误伤的占位符,等其余处理(粗体/斜体/裸链接/裸路径)跑完再统一还原。反引号也要
  // 抠出来是因为:用户习惯把路径包一层反引号提高可读性(``/Users/xx/report.md``),但仍然
  // 期待它可点——不占位的话反引号会先转成 <code>...</code>,后面裸路径正则要求的"前面是
  // 空白/行首"边界会被 <code> 的 '>' 挡住,导致包了反引号的路径反而点不了(2026-07-30 踩过)。
  // 占位符数字外面包一层 \u0000 转义序列(NUL),不能像原来"L"+数字那样直接拼数字——
  // 中文行文里到处是"共 3 个文件"这种夹数字写法,朴素占位符会跟真实文本撞车。注意这里
  // 必须写 \u0000 转义序列,不能敲字面 NUL 字节——HTML5 解析器会把源码里的字面 NUL
  // 替换成 U+FFFD,写字面字节等于占位符在浏览器里直接失效(改这段踩过一次,直接编辑
  // 工具似乎会把 \u0000 文本解释成真实 NUL 字节,这行改动是用脚本写的,不是用 Edit)。
  const parts=[];
  const hold = html => { parts.push(html); return "\u0000"+(parts.length-1)+"\u0000"; };
  t = t.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,(m,txt,url)=>{
    let html;
    if(/^https?:\/\//i.test(url)) html = isDocUrl(url) ? docLinkHtml(txt,url) : '<a href="'+url+'" target="_blank" rel="noopener">'+txt+'</a>';
    else if(DOC_EXT_RE.test(url)) html = docLinkHtml(txt,url);   // 本地路径 + 文档后缀 → 预览
    else html = txt;   // 本地路径但不是文档、也不是外链 → 没法点,退化成纯文字,别留死链接
    return hold(html);
  });
  t = t.replace(/`([^`]+)`/g,(m,c)=>{
    // 反引号里的内容本身就是个文档链接/路径 → 直接当文档链接处理,不再展示成灰底代码块
    if(/^https?:\/\//i.test(c)) return hold(isDocUrl(c) ? docLinkHtml(c,c) : "<code>"+c+"</code>");
    if(DOC_EXT_RE.test(c) && c.includes("/")) return hold(docLinkHtml(c,c));
    return hold("<code>"+c+"</code>");
  });
  t = t.replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>");
  t = t.replace(/\*([^*\n]+)\*/g,"<em>$1</em>");
  t = t.replace(/(https?:\/\/[^\s<]+[^\s<.,)])/g,(m,url)=>isDocUrl(url)?docLinkHtml(url,url):'<a href="'+url+'" target="_blank" rel="noopener">'+url+'</a>');
  // 裸本地路径:除了 /Users/xx/report.md 这种绝对路径,更常见的是 AI 直接写相对路径,
  // 比如 "00-inbox/2026-07-30_xxx.md"(没有 ./ 前缀)。分两支:①有 / ~/ ./ ../ 显式前缀的,
  // 后面随便接什么字符(文件名本身常带中文,比如用户这个案例);②没有显式前缀的裸相对路径,
  // 目录段限定 ASCII(字母/数字/-_.)——限制住是为了不把"路径是~/x.md"这种中文紧贴着路径、
  // 中间没空格的情况,把前面的中文字也一起吞进链接里(踩过一次,见此前 commit)。
  // 两支共同要求：前面必须是行首/空白/左括号,且路径里至少有一段目录(不然"看下 config.py"
  // 这种孤零零的文件名也会被当成链接,误伤太多)。
  t = t.replace(/(^|[\s(])((?:~\/|\.{1,2}\/|\/)[^\s<>()`"'&]+\.(?:md|markdown|txt|html?|pdf|csv|json|log|ya?ml|py|jsx?|tsx?|sh|css|xml)|[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)*\/[^\s<>()`"'&]+\.(?:md|markdown|txt|html?|pdf|csv|json|log|ya?ml|py|jsx?|tsx?|sh|css|xml))/g,
    (m,pre,p)=>pre+docLinkHtml(p,p));
  t = t.replace(/\u0000(\d+)\u0000/g,(m,i)=>parts[+i]);   // 还原 markdown 链接 / 反引号代码
  return t;
}
function mdToHtml(src){
  src = (src||"").replace(/\r\n/g,"\n");
  const codes=[];
  src = src.replace(/```(\w*)\n?([\s\S]*?)```/g,(m,lang,code)=>{
    codes.push('<div class="codeblock"><button class="codecopy" onclick="copyCodeBlock(this)" title="复制代码">'+ic("copy")+
      '</button><pre class="code"><code>'+esc(code.replace(/\n$/,""))+"</code></pre></div>");
    return " C"+(codes.length-1)+" ";
  });
  const L = src.split("\n"); let out=""; let i=0; let para=[];
  const flush=()=>{ if(para.length){ out+="<p>"+para.map(inlineMd).join("<br>")+"</p>"; para=[]; } };
  const sepRe=/^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$/;
  while(i<L.length){
    let ln=L[i];
    const cm=ln.match(/^ C(\d+) $/);
    if(cm){ flush(); out+=codes[+cm[1]]; i++; continue; }
    if(!ln.trim()){ flush(); i++; continue; }
    let m;
    if(m=ln.match(/^(#{1,6})\s+(.*)$/)){ flush(); const n=m[1].length; out+="<h"+n+">"+inlineMd(m[2])+"</h"+n+">"; i++; continue; }
    if(/^\s*(---|\*\*\*|___)\s*$/.test(ln)){ flush(); out+="<hr>"; i++; continue; }
    // 表格
    if(ln.includes("|") && i+1<L.length && sepRe.test(L[i+1])){
      flush();
      const cells=r=>r.replace(/^\s*\|/,"").replace(/\|\s*$/,"").split("|").map(c=>c.trim());
      const head=cells(ln); i+=2; const rows=[];
      while(i<L.length && L[i].includes("|") && L[i].trim()){ rows.push(cells(L[i])); i++; }
      let t="<table><thead><tr>"+head.map(h=>"<th>"+inlineMd(h)+"</th>").join("")+"</tr></thead><tbody>";
      for(const r of rows){ t+="<tr>"+head.map((_,k)=>"<td>"+inlineMd(r[k]||"")+"</td>").join("")+"</tr>"; }
      out+=t+"</tbody></table>"; continue;
    }
    if(m=ln.match(/^\s*>\s?(.*)$/)){ flush(); const q=[]; while(i<L.length&&(m=L[i].match(/^\s*>\s?(.*)$/))){ q.push(m[1]); i++; } out+="<blockquote>"+q.map(inlineMd).join("<br>")+"</blockquote>"; continue; }
    if(/^\s*[-*+]\s+/.test(ln)){ flush(); let h="<ul>"; while(i<L.length&&(m=L[i].match(/^\s*[-*+]\s+(.*)$/))){ h+="<li>"+inlineMd(m[1])+"</li>"; i++; } out+=h+"</ul>"; continue; }
    if(/^\s*\d+\.\s+/.test(ln)){ flush(); let h="<ol>"; while(i<L.length&&(m=L[i].match(/^\s*\d+\.\s+(.*)$/))){ h+="<li>"+inlineMd(m[1])+"</li>"; i++; } out+=h+"</ol>"; continue; }
    para.push(ln); i++;
  }
  flush();
  return out;
}

// ── 文档预览分屏(右侧滑出)────────────────────────────────────────────────
// 触发源有两处:1) 聊天正文里的 .doclink(见 inlineMd/docLinkHtml,事件委托在 #wrap 上)
// 2) 工具卡片里可点的文件路径(见 tool-card.js 的 toolCardShell 第 5 个参数)。
// 两种链接分两条取数路径:http(s) 链接直接拿 URL 打(同源 /pub/ 页面也走真实 URL,
// 保证页内相对资源能正常加载);本地文件路径没有可直接访问的 URL(会挡在鉴权后面),
// 走 /doc/preview 接口带 X-Auth-Token 头取 blob。
const DP = {objUrl:null};
function extOf(u){ const m=(u||"").match(/\.([a-z0-9]+)(?:\?.*)?$/i); return m ? m[1].toLowerCase() : ""; }
function dpRevoke(){ if(DP.objUrl){ URL.revokeObjectURL(DP.objUrl); DP.objUrl=null; } }
function closeDocPreview(){
  $("#docPreview").hidden = true;
  dpRevoke();
  $("#dpBody").innerHTML = "";
}
async function openDocPreview({kind, target, title}){
  dpRevoke();
  $("#docPreview").hidden = false;
  $("#dpTitle").textContent = title || target;
  const body = $("#dpBody");
  body.innerHTML = '<div class="dp-loading">'+DOTS+'</div>';
  $("#dpOpenBtn").hidden = kind!=="url";
  if(kind==="url") $("#dpOpenBtn").onclick = ()=>window.open(target, "_blank", "noopener");
  $("#dpDlBtn").hidden = true;
  if(kind==="url"){ renderDocUrl(target); return; }
  try{
    const resp = await api("/doc/preview?conv="+encodeURIComponent(S.conv||"")+"&path="+encodeURIComponent(target));
    if(!resp.ok){
      let msg = "预览失败("+resp.status+")";
      try{ const j=await resp.json(); if(j.error) msg=j.error; }catch(e){}
      body.innerHTML = '<div class="dp-err">'+esc(msg)+'</div>';
      return;
    }
    renderDocBlob(await resp.blob(), target);
  }catch(e){
    body.innerHTML = '<div class="dp-err">加载失败,检查一下网络</div>';
  }
}
// 外部 URL 的 iframe 是真实跨域内容(不像本地文件那样先读成 blob 隔离),给个宽松沙箱——
// 挡"跳出 iframe 劫持整个页面"(不给 allow-top-navigation),别的照常放行,不影响正常浏览。
const DP_URL_SANDBOX = 'sandbox="allow-scripts allow-same-origin allow-popups allow-forms"';
function renderDocUrl(target){
  const body=$("#dpBody"), ext=extOf(target);
  const sameOrigin = target.startsWith(location.origin+"/");
  if(ext==="md"||ext==="markdown"||ext==="txt"){
    // 跨域 fetch 会被本站 CSP 的 connect-src 'self' 直接挡在浏览器这一层(压根发不出网络
    // 请求),不是"对方不让读"——干等 fetch 失败再退化没意义,直接走"尝试内嵌+给出说明"。
    if(!sameOrigin){ dpExternalFallback(target, "跨域文档:浏览器安全策略不允许本站直接读取外部内容,下面尝试直接显示原页面。"); return; }
    fetch(target).then(r=>r.text()).then(txt=>{
      if(ext==="txt"){ body.innerHTML=""; const pre=el("pre","dp-text"); pre.textContent=txt; body.append(pre); }
      else body.innerHTML='<div class="dp-md bubble">'+mdToHtml(txt)+'</div>';
    }).catch(()=>{ dpExternalFallback(target, "读取失败,下面尝试直接显示原页面。"); });
    return;
  }
  if(/^(png|jpe?g|gif|webp|svg)$/.test(ext)){ body.innerHTML='<img class="dp-img" src="'+esc(target)+'">'; return; }
  if(!sameOrigin){ dpExternalFallback(target, "这是外部链接,能不能内嵌预览取决于对方网站自己的安全策略(比如 GitHub 就明确禁止被其他网站嵌入)。"); return; }
  body.innerHTML='<iframe class="dp-frame" '+DP_URL_SANDBOX+' src="'+esc(target)+'"></iframe>';
}
// 跨域内容常常连"能不能被嵌进 iframe"都由对方说了算(X-Frame-Options/CSP frame-ancestors),
// 浏览器不给 JS 任何"被拒绝"的可探测信号——没法判断失败后自动切换,只能提前把话说清楚,
// 同时仍然尝试 iframe(万一对方允许,直接就能看)。
function dpExternalFallback(target, note){
  const body=$("#dpBody");
  body.innerHTML =
    '<div class="dp-err">'+esc(note)+' 如果下面还是空白,点右上角"在新标签页打开"直接看原页面。</div>'+
    '<iframe class="dp-frame" '+DP_URL_SANDBOX+' src="'+esc(target)+'"></iframe>';
}
function renderDocBlob(blob, target){
  const body=$("#dpBody"), ctype=(blob.type||"").split(";")[0].trim(), ext=extOf(target);
  $("#dpDlBtn").hidden=false;
  $("#dpDlBtn").onclick=()=>{
    const u=URL.createObjectURL(blob);
    const a=document.createElement("a"); a.href=u; a.download=target.split("/").pop()||"download";
    document.body.append(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(u), 4000);
  };
  const isMd = ext==="md"||ext==="markdown"||ctype==="text/markdown";
  const textExts=["txt","json","log","py","js","jsx","ts","tsx","sh","yml","yaml","css","xml","csv","ini"];
  if(isMd){
    blob.text().then(txt=>{ body.innerHTML='<div class="dp-md bubble">'+mdToHtml(txt)+'</div>'; });
  } else if(ctype.startsWith("text/")||textExts.includes(ext)){
    blob.text().then(txt=>{ body.innerHTML=""; const pre=el("pre","dp-text"); pre.textContent=txt; body.append(pre); });
  } else if(ctype.startsWith("image/")||/^(png|jpe?g|gif|webp|svg)$/.test(ext)){
    DP.objUrl=URL.createObjectURL(blob); body.innerHTML='<img class="dp-img" src="'+DP.objUrl+'">';
  } else if(ctype==="application/pdf"||ext==="pdf"){
    DP.objUrl=URL.createObjectURL(blob); body.innerHTML='<iframe class="dp-frame" src="'+DP.objUrl+'"></iframe>';
  } else if(ctype==="text/html"||ext==="html"||ext==="htm"){
    // 沙箱只放行弹窗,不给 allow-scripts+allow-same-origin(会让 blob 里的脚本拿到跟主站同源的能力,
    // 等于把鉴权 token 所在的页面上下文让给了一份 AI 写的、未经审查的 HTML)——预览允许有损,不允许有洞。
    DP.objUrl=URL.createObjectURL(blob); body.innerHTML='<iframe class="dp-frame" sandbox="allow-popups" src="'+DP.objUrl+'"></iframe>';
  } else {
    body.innerHTML='<div class="dp-err">这种文件类型暂不支持预览,点右上角下载看</div>';
  }
}
function initDocPreview(){
  $("#dpOpenBtn").innerHTML=ic("external");
  $("#dpDlBtn").innerHTML=ic("download");
  $("#dpClose").innerHTML=ic("close");
  $("#dpClose").onclick=closeDocPreview;
  document.addEventListener("keydown", e=>{ if(e.key==="Escape" && !$("#docPreview").hidden) closeDocPreview(); });
  $("#wrap").addEventListener("click", e=>{
    const a=e.target.closest("a.doclink");
    if(!a) return;
    e.preventDefault();
    const url=a.dataset.docUrl, path=a.dataset.docPath, title=a.textContent.trim();
    if(url) openDocPreview({kind:"url", target:url, title});
    else if(path) openDocPreview({kind:"path", target:path, title});
  });
}
initDocPreview();
