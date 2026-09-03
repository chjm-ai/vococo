"use strict";
// 设置 → 数据:运行数据面板。总览(工作强度日历 / 模型花费 / 每日趋势 / 运行健康)
// + 会话明细(逐个会话的 token、花费、缓存命中、工具失败)。
// 数据全部来自 GET /stats(聚合在 gateway/stats.py),这里只负责画。

const ST = { range:"30d", view:"overview", data:null, q:"", sel:null, loading:false };

// 模型配色:借用既有分类标签色板,作用是互相区分,不表达状态语义
const ST_PALETTE = ["var(--accent)","var(--tag-copper)","var(--tag-leaf)","var(--tag-ink)",
  "var(--tag-olive)","var(--tag-gold)","var(--tag-sun)","var(--dim2)"];

function stNum(n){
  n = Number(n)||0;
  if(n>=1e9) return (n/1e9).toFixed(1)+"B";
  if(n>=1e6) return (n/1e6).toFixed(1)+"M";
  if(n>=1e3) return (n/1e3).toFixed(1)+"k";
  return String(Math.round(n));
}
function stMoney(n){
  n = Number(n)||0;
  if(n>=1000) return "$"+(n/1000).toFixed(2)+"k";
  if(n>=1) return "$"+n.toFixed(2);
  return n>0 ? "$"+n.toFixed(3) : "—";
}
function stTime(ts){
  return new Date(ts*1000).toLocaleString("zh-CN",
    {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"});
}
function stDur(sec){
  if(sec<3600) return Math.max(1,Math.round(sec/60))+" 分钟";
  if(sec<86400) return (sec/3600).toFixed(1)+" 小时";
  return (sec/86400).toFixed(1)+" 天";
}
// 强度 → 颜色:开方压一下,否则个别爆量的一天会把其余全压成同一个浅色
function stShade(v,max){
  if(!v||!max) return "";
  return `background:color-mix(in srgb, var(--accent) ${Math.round(16+84*Math.sqrt(v/max))}%, var(--panel2))`;
}

async function renderStatsPane(){
  if(!ST.data && !ST.loading){ loadStats(); }
  drawStats();
}
async function loadStats(){
  ST.loading = true; drawStats();
  try{
    const r = await api("/stats?range="+encodeURIComponent(ST.range));
    if(!r.ok) throw new Error("HTTP "+r.status);
    ST.data = await r.json();
    if(ST.data.error) throw new Error(ST.data.error);
  }catch(e){
    ST.data = null; ST.err = String(e&&e.message||e);
  }
  ST.loading = false;
  if(SET.tab==="stats") drawStats();
}

function stHeader(){
  const tabs = [["overview","总览"],["sessions","会话明细"]]
    .map(([v,l])=>`<button class="${ST.view===v?"active":""}" data-stv="${v}">${l}</button>`).join("");
  const ranges = [["7d","7 天"],["30d","30 天"],["all","全部"]]
    .map(([v,l])=>`<button class="${ST.range===v?"active":""}" data-str="${v}">${l}</button>`).join("");
  return '<h3>'+ic("gear")+'数据面板</h3>'+
    '<p class="shint">vococo 的运行统计:工作强度、模型用量与花费、运行健康,以及每个会话的明细。</p>'+
    `<div class="stbar"><div class="stx">${tabs}</div><div class="stx" style="margin-left:auto">${ranges}</div></div>`;
}

function drawStats(){
  const pane = $("#setPane");
  let body;
  if(ST.loading && !ST.data) body = '<div class="setload">统计中…首次打开要扫一遍历史日志,大约 20 秒</div>';
  else if(!ST.data) body = html`<div class="setempty">加载失败:${ST.err||"未知错误"}
    <br><button class="btn ghost sm" style="margin-top:12px" onclick="loadStats()">重试</button></div>`;
  else if(ST.sel!=null) body = stDetail();
  else body = ST.view==="overview" ? stOverview() : stSessions();
  pane.innerHTML = stHeader() + body;
  bindStatsPane();
}

function bindStatsPane(){
  const pane = $("#setPane");
  pane.querySelectorAll("[data-stv]").forEach(b=>b.onclick=()=>{
    ST.view=b.dataset.stv; ST.sel=null; drawStats();
  });
  pane.querySelectorAll("[data-str]").forEach(b=>b.onclick=()=>{
    if(ST.range===b.dataset.str) return;
    ST.range=b.dataset.str; ST.sel=null; ST.data=null; loadStats();
  });
  const back = pane.querySelector("#stBack");
  if(back) back.onclick = ()=>{ ST.sel=null; drawStats(); };
  pane.querySelectorAll(".strow").forEach(r=>r.onclick=()=>{
    ST.sel = r.dataset.key; drawStats(); pane.scrollTop = 0;
  });
  const q = pane.querySelector("#stQ");
  if(q){
    q.oninput = ()=>{
      const pos = q.selectionStart;
      ST.q = q.value.trim().toLowerCase();
      drawStats();
      const n = $("#setPane").querySelector("#stQ");
      if(n){ n.focus(); n.setSelectionRange(pos,pos); }
    };
  }
}

// ── 总览 ────────────────────────────────────────────────────────────────
function stOverview(){
  const d = ST.data, o = d.overview;
  const models = d.models||[];
  const totalCost = models.reduce((s,m)=>s+m.cost,0);
  const vocoCost = models.filter(m=>m.scope==="vococo").reduce((s,m)=>s+m.cost,0);
  const hitBase = (o.cache_read||0)+(o.input_fresh||0);
  const hit = hitBase ? (o.cache_read/hitBase*100) : 0;
  const tools = d.tools||[];
  const toolRate = o.tool_calls ? (o.tool_ok/o.tool_calls*100).toFixed(1) : "—";

  const kpis = [
    ["活跃天数", o.days, o.first_day ? "首次对话 "+o.first_day : ""],
    ["会话数", o.sessions, "已归档 "+o.archived],
    ["对话轮次", stNum(o.turns), o.days ? "日均 "+Math.round(o.turns/o.days)+" 轮" : ""],
    ["工具调用", stNum(o.tool_calls), "成功率 "+toolRate+"%"],
    ["等值花费", stMoney(totalCost), "vococo "+stMoney(vocoCost)],
    ["缓存命中", hit.toFixed(1)+"%", stNum(o.cache_read)+" 复用"],
  ].map(k=>`<div class="k"><div class="n">${k[1]}</div><div class="l">${k[0]}</div><div class="d">${k[2]}</div></div>`).join("");

  // 工作强度日历:一格一天,从首次对话排到今天(最多一年),满宽自动换行。
  // 刻意不跟随上面的时间范围——日历的价值就是看长期节奏,切到「7 天」也该看到全部历史。
  const days = Object.keys(d.daily||{}).sort();
  let cal = '<div class="setempty">还没有对话数据</div>', maxTurns = 0, calLegend = "";
  if(days.length){
    maxTurns = Math.max(...days.map(k=>d.daily[k].turns||0));
    const cur = new Date(days[0]+"T00:00:00");
    const end = new Date(); end.setHours(0,0,0,0);
    const key = dd=>dd.getFullYear()+"-"+String(dd.getMonth()+1).padStart(2,"0")+"-"+String(dd.getDate()).padStart(2,"0");
    let cells = "";
    while(cur<=end){
      const k = key(cur);
      const e = d.daily[k];
      const v = e ? (e.turns||0) : 0;
      const wk = "日一二三四五六"[cur.getDay()];
      cells += `<div class="cel${cur.getDate()===1?" mstart":""}" style="${stShade(v,maxTurns)}"
        title="${k} 周${wk} · ${v} 轮${e&&e.cost?" · "+stMoney(e.cost):""}"></div>`;
      cur.setDate(cur.getDate()+1);
    }
    cal = `<div class="stcal">${cells}</div>`;
    calLegend = `<div class="stlg">少 <span class="cel"></span>
      <span class="cel" style="${stShade(maxTurns*0.2,maxTurns)}"></span>
      <span class="cel" style="${stShade(maxTurns*0.5,maxTurns)}"></span>
      <span class="cel" style="${stShade(maxTurns,maxTurns)}"></span> 多
      <span style="margin-left:auto">${days[0]} → 今天 · 最忙一天 ${maxTurns} 轮</span></div>`;
  }

  // 模型:同一个模型的 vococo / 终端两条并成一行,再标 vococo 占比
  const by = {};
  models.forEach(m=>{
    const t = by[m.model] || (by[m.model] = {calls:0,cost:0,in:0,out:0,cr:0,voco:0});
    t.calls+=m.calls; t.cost+=m.cost; t.in+=m.in_tok+m.cache_w; t.out+=m.out_tok; t.cr+=m.cache_r;
    if(m.scope==="vococo") t.voco+=m.cost;
  });
  const list = Object.entries(by).sort((a,b)=>b[1].cost-a[1].cost);
  let modelSec = '<div class="setempty">这段时间没有模型调用记录</div>';
  if(list.length){
    const stack = list.map(([n,t],i)=>
      `<div style="width:${totalCost?t.cost/totalCost*100:0}%;background:${ST_PALETTE[i%8]}" title="${esc(n)} ${stMoney(t.cost)}"></div>`).join("");
    const rows = list.map(([n,t],i)=>`<tr>
      <td><span class="stdot" style="background:${ST_PALETTE[i%8]}"></span>${esc(n)}</td>
      <td class="r">${stNum(t.calls)}</td><td class="r">${stNum(t.in)}</td><td class="r">${stNum(t.out)}</td>
      <td class="r">${stNum(t.cr)}</td><td class="r">${stMoney(t.cost)}</td>
      <td class="r">${t.cost?Math.round(t.voco/t.cost*100):0}%</td></tr>`).join("");
    modelSec = `<div class="ststack">${stack}</div>
      <table class="sttab"><tr><th>模型</th><th class="r">调用</th><th class="r">输入</th><th class="r">输出</th>
      <th class="r">缓存读</th><th class="r">等值花费</th>
      <th class="r" title="这个模型的花费里,有多少是 vococo 会话产生的;其余来自你在终端直接跑的 Claude Code">这里花的</th></tr>${rows}</table>`;
  }

  // 每日花费趋势:日历是全年,这张图跟随上面选的时间范围
  const keep = ST.range==="7d" ? 7 : ST.range==="30d" ? 30 : days.length;
  const costDays = days.slice(-keep).filter(k=>d.daily[k].cost);
  let trend = "";
  if(costDays.length){
    const vals = costDays.map(k=>d.daily[k].cost);
    const mx = Math.max(...vals);
    const bars = costDays.map((k,i)=>`<i style="height:${Math.max(2,vals[i]/mx*100)}%" title="${k} ${stMoney(vals[i])}"></i>`).join("");
    const avg = vals.reduce((s,v)=>s+v,0)/vals.length;
    trend = `<div class="stsec"><div class="sechd">每日花费趋势</div>
      <div class="stbars">${bars}</div>
      <div class="stlg"><span>${costDays[0]}</span><span style="flex:1"></span>
        <span>峰值 ${stMoney(mx)} · 日均 ${stMoney(avg)}</span><span style="flex:1"></span>
        <span>${costDays[costDays.length-1]}</span></div></div>`;
  }

  const toolBars = tools.slice(0,7).map(t=>`<div style="margin-bottom:var(--sp-3)">
    <div style="display:flex;justify-content:space-between;font-size:var(--fs-sm)">
      <span>${esc(t.name)}</span><span style="color:var(--dim2)">${stNum(t.calls)} · ${Math.round(t.ok/t.calls*100)}%</span></div>
    <div class="stmini"><i style="width:${tools[0].calls?t.calls/tools[0].calls*100:0}%"></i></div></div>`).join("");

  return `<div class="stkpi">${kpis}</div>
  <div class="stsec"><div class="sechd">工作强度日历</div>${cal}${calLegend}</div>
  <div class="stsec"><div class="sechd">模型使用与花费</div>${modelSec}
    <p class="shint" style="margin-top:var(--sp-3)">花费是按官方单价折算的<b>等值成本</b>:Claude 官方走订阅、实际不额外扣钱,
      这个数用来看干了多少活;DeepSeek / Kimi / 中转这些按量计费的才是真实支出。
      单价表可在 <code>data/model_prices.json</code> 覆盖。<br>
      最后一列<b>「这里花的」</b>= 该模型的花费里 vococo 会话占多少,剩下的是你在终端直接跑 Claude Code 用掉的
      (两边共用同一份 <code>~/.claude</code> 日志,所以能分开算)。</p></div>
  ${trend}
  <div class="stsec"><div class="sechd">运行健康</div><div class="stgrid2">
    <div><svg width="104" height="104" viewBox="0 0 36 36">
      <circle cx="18" cy="18" r="15.9" style="fill:none;stroke:var(--panel2);stroke-width:3.2"/>
      <circle cx="18" cy="18" r="15.9" stroke-dasharray="${hit.toFixed(1)} 100" transform="rotate(-90 18 18)"
        style="fill:none;stroke:var(--accent);stroke-width:3.2;stroke-linecap:round"/>
      <text x="18" y="18.5" text-anchor="middle" style="fill:var(--text);font-size:6.5px;font-weight:600">${hit.toFixed(1)}%</text>
      <text x="18" y="23" text-anchor="middle" style="fill:var(--dim);font-size:3px">缓存命中</text></svg>
      <div class="stchips"><span class="stchip">累计吞吐 ${stNum(o.total_tokens)}</span>
        <span class="stchip">复用 ${stNum(o.cache_read)}</span>
        <span class="stchip ${o.errors>20?"bad":"good"}">异常会话 ${o.errors}</span></div></div>
    <div>${toolBars||'<div class="setempty">没有工具调用</div>'}</div>
  </div></div>`;
}

// ── 会话明细 ────────────────────────────────────────────────────────────
function stSessions(){
  const all = ST.data.sessions||[];
  const list = ST.q
    ? all.filter(s=>(s.title+" "+s.model).toLowerCase().includes(ST.q))
    : all;
  const rows = list.map(s=>{
    const base = s.cache_read+s.input_fresh;
    const tags = [s.model, stTime(s.end), s.turns+" 轮"];
    if(s.worktree) tags.push("worktree");
    if(s.archived) tags.push("已归档");
    return `<div class="strow" data-key="${esc(s.key)}">
      <div class="t"><div class="nm">${s.error?'<span style="color:var(--err)">⚠ </span>':""}${esc(s.title)}</div>
        <div class="mt">${esc(tags.join(" · "))}</div></div>
      <div class="num"><b>${stNum(s.tokens)}</b><span>tokens</span></div>
      <div class="num"><b>${s.linked?stMoney(s.cost):"—"}</b><span>${s.linked?"等值":"无日志"}</span></div>
      <div class="num"><b>${base?Math.round(s.cache_read/base*100)+"%":"—"}</b><span>命中</span></div></div>`;
  }).join("");
  return `<div class="stsrch">${ic("search")}<input id="stQ" placeholder="搜索会话标题或模型…" value="${esc(ST.q)}"></div>
    <p class="shint">共 ${list.length} 个会话(按最近活跃排序,最多 200 个)· 点开看单会话明细。
      花费按 SDK 会话号关联日志算出;早期会话没记这个号,会显示「无日志」,中途换过号的会偏低。</p>
    ${rows||'<div class="setempty">没有匹配的会话</div>'}`;
}

function stDetail(){
  const s = (ST.data.sessions||[]).find(x=>x.key===ST.sel);
  if(!s){ ST.sel=null; return stSessions(); }
  const base = s.cache_read+s.input_fresh;
  const hit = base ? (s.cache_read/base*100).toFixed(1) : "—";
  const ctxPct = s.ctx_window ? Math.min(100, s.ctx/s.ctx_window*100) : 0;
  const models = s.models.length
    ? s.models.map((m,i)=>`<span class="stchip"><span class="stdot" style="background:${ST_PALETTE[i%8]}"></span>${esc(m.model)} ×${m.calls}</span>`).join("")
    : '<span class="stchip">没关联到日志(老会话没记 SDK 会话号)</span>';
  const tops = s.top_tools.length
    ? s.top_tools.map(t=>`<div style="margin-bottom:var(--sp-2)">
        <div style="display:flex;justify-content:space-between;font-size:var(--fs-sm)">
          <span>${esc(t.name)}</span><span style="color:var(--dim2)">${t.calls}</span></div>
        <div class="stmini"><i style="width:${t.calls/s.top_tools[0].calls*100}%"></i></div></div>`).join("")
    : '<div class="setempty">这个会话没有工具调用</div>';
  const flags = [s.model, stTime(s.start)+" → "+stTime(s.end), "跨度 "+stDur(Math.max(60,s.end-s.start))];
  if(s.worktree) flags.push("独立 worktree");
  if(s.archived) flags.push("已归档");

  return `<button class="btn ghost sm" id="stBack" style="margin-bottom:var(--sp-3)">← 返回会话列表</button>
  <div class="stdet">
    <h4>${esc(s.title)}</h4>
    <div style="font-size:var(--fs-sm);color:var(--dim2)">${esc(flags.join(" · "))}
      ${s.error?' · <span style="color:var(--err)">有异常</span>':""}</div>
    <div class="kv">
      <div><span>对话轮次</span><b>${s.turns}</b></div>
      <div><span>累计 tokens</span><b>${stNum(s.tokens)}</b></div>
      <div><span>等值花费</span><b>${s.linked?stMoney(s.cost):"—"}</b></div>
      <div><span>缓存命中</span><b>${hit}${hit==="—"?"":"%"}</b></div>
      <div><span>模型调用</span><b>${s.calls||"—"}</b></div>
      <div><span>工具调用</span><b>${s.tools}</b></div>
      <div><span>工具失败</span><b style="${s.tool_fail?"color:var(--err)":""}">${s.tool_fail}</b></div>
      <div><span>单轮均价</span><b>${s.linked&&s.turns?stMoney(s.cost/s.turns):"—"}</b></div>
    </div>
    <div class="sechd" style="margin-top:0">上下文占用</div>
    <div class="stmini"><i style="width:${ctxPct}%"></i></div>
    <div style="font-size:var(--fs-sm);color:var(--dim2);margin-top:var(--sp-1)">
      ${stNum(s.ctx)} / ${s.ctx_window?stNum(s.ctx_window):"—"} tokens${ctxPct>80?" · 接近上限,建议开新会话":""}</div>
    <div class="stgrid2" style="margin-top:var(--sp-5)">
      <div><div class="sechd" style="margin-top:0">实际使用的模型</div>
        <div class="stchips" style="margin-top:0">${models}</div>
        <div class="sechd">缓存明细</div>
        <div style="font-size:var(--fs-sm);color:var(--dim)">复用 ${stNum(s.cache_read)} · 新读 ${stNum(s.input_fresh)}</div></div>
      <div><div class="sechd" style="margin-top:0">工具使用 TOP5</div>${tops}</div>
    </div>
  </div>`;
}
