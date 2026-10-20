// 仅展示公开操作和已提交产出，独立于普通回复的流式状态。
const researchProgress = (()=>{
  const records=new Map(), fetching=new Map();
  const stages={plan:'规划',collect:'搜集',review:'核验',report:'成稿'};
  const statuses={queued:'排队中',running:'进行中',done:'已完成',completed:'已完成',failed:'研究失败',cancelled:'已取消'};
  const active=p=>['queued','running'].includes(p.task_status || p.status);
  const conversation=conv=>conv==='voice-chat:main' ? 'main' : conv;
  const session=()=>conversation(S.conv);
  const inline=t=>t.kind==='research' && t.conversation_id===session();
  const node=(tag,cls,text)=>{
    const el=document.createElement(tag); if(cls)el.className=cls;
    if(text!=null)el.textContent=text; return el;
  };
  function link(url,title){
    try{
      const parsed=new URL(url);
      if(!['https:','http:'].includes(parsed.protocol))return node('span','',title || url);
      const el=node('a','',title || url);el.href=parsed.href;el.target='_blank';el.rel='noopener noreferrer';return el;
    }catch(_){return node('span','',title || url || '无链接');}
  }
  function seconds(value){
    if(!value)return 0;
    if(typeof value==='number')return value>1e12 ? value/1000 : value;
    return Date.parse(value)/1000 || 0;
  }
  function duration(value){
    const total=Math.max(0,Math.floor(value));
    return total<60 ? total+'秒' : Math.floor(total/60)+'分'+(total%60)+'秒';
  }
  function status(p){
    if(p.task_status==='failed' || p.status==='failed')return '研究失败';
    if(p.task_status==='cancelled' || p.status==='cancelled')return '已取消';
    if(!active(p) && p.limited_stages?.length)return '有限报告';
    if(p.delivery_pending)return '报告已交付 · 状态待同步';
    return statuses[p.task_status] || statuses[p.status] || '状态待同步';
  }
  function operation(item){
    const name=item.query ? '搜索：'+item.query : item.url ? '抓取：'+item.url : item.tool || '公开操作';
    return name+' · '+({running:'进行中',done:'完成',failed:'失败'}[item.status] || '状态待同步');
  }
  function metrics(p){
    const usage=p.usage || {},budget=p.budget || {};
    const count=key=>(usage[key] || 0)+(budget[key]!=null ? '/'+budget[key] : '');
    const end=active(p) ? Math.min(Date.now()/1000,seconds(p.deadline) || Infinity) : seconds(p.updated_at);
    const elapsed=seconds(p.created_at) ? duration(end-seconds(p.created_at)) : '待同步';
    return '搜索 '+count('searches')+' · 抓取 '+count('fetches')+' · 已用 '+elapsed+
      (budget.seconds ? '/'+duration(budget.seconds) : '')+' · Token '+count('tokens');
  }
  function steps(p){
    const el=node('ol','research-steps');
    Object.entries(stages).forEach(([key,label])=>{
      const limited=(p.limited_stages || []).includes(key);
      const done=!!p.checkpoints?.[key] && !limited;
      const current=p.stage===key;
      const word=limited ? '受限' : done ? '已提交' : current ? status(p) : '待进行';
      const item=node('li',current ? 'current' : '',label+' · '+word);
      if(current)item.setAttribute('aria-current','step');el.append(item);
    });return el;
  }
  function section(parent,title){
    const el=node('section','research-section');el.append(node('h4','',title));parent.append(el);return el;
  }
  function expanded(p){
    const body=node('div','research-body');
    const actions=section(body,'操作记录');
    if(!p.activity?.length)actions.append(node('p','','等待公开操作记录'));
    (p.activity || []).forEach(item=>{
      const row=node('div','research-operation');row.append(node('p','',operation(item)));
      if(item.url)row.append(link(item.url,'打开网页'));
      if(item.summary)row.append(node('p','research-muted',item.summary));actions.append(row);
    });
    const sources=section(body,'已取得来源（'+(p.sources?.length || 0)+'）');
    (p.sources || []).forEach(source=>{
      const row=node('p','');row.append(link(source.url,source.title || source.id));
      row.append(node('span','research-muted',' · '+(source.claims_count || 0)+'条证据'));sources.append(row);
    });
    const outputs=section(body,'阶段产出');
    Object.entries(stages).filter(([key])=>key!=='report' && p.checkpoints?.[key]).forEach(([key,label])=>{
      outputs.append(node('h5','',label),node('p','research-checkpoint',p.checkpoints[key]));
    });
    if(!Object.keys(p.checkpoints || {}).length)outputs.append(node('p','','尚无已提交产出'));
    if(p.limited_stages?.length)body.append(node('p','research-warning','受限阶段：'+p.limited_stages.map(key=>stages[key] || key).join('、')+'；未完成核验不代表全量完成。'));
    return body;
  }
  async function cancel(button,id){
    button.disabled=true;button.textContent='正在取消…';
    try{
      const response=await api('/tasks/'+encodeURIComponent(id)+'/stop',{method:'POST'});
      if(!response.ok)throw Error('取消失败');button.textContent='已请求取消';
    }catch(_){button.disabled=false;button.textContent='取消失败，重试';}
  }
  function card(record,old){
    const p=record.progress,el=old || node('article','research-card');
    el.dataset.researchCard=p.id;el.dataset.conv=S.conv;
    const isOpen=!!el.querySelector('details')?.open;
    const details=node('details',''),summary=node('summary','');details.open=isOpen;
    summary.append(node('strong','research-title','研究 · '+p.title),node('span','research-status',status(p)),steps(p));
    const current=(p.activity || []).findLast(item=>item.status==='running');
    summary.append(node('span','research-current',current && active(p) ? operation(current) : active(p) ? (stages[p.stage] || '研究')+' · 等待下一项公开操作' : '过程已保留，可展开查看'));
    summary.append(node('span','research-metrics',metrics(p)),node('span','research-expand','展开 / 收起研究过程'));
    details.append(summary,expanded(p));el.replaceChildren(details);
    if(active(p)){
      const button=node('button','research-cancel','取消研究');button.type='button';
      button.onclick=()=>cancel(button,p.id);el.append(button);
    }
    return el;
  }
  function render(){
    const wrap=document.getElementById('wrap');if(!wrap || S.histLoading===S.conv)return;
    const visible=[...records.values()].filter(record=>record.conv===session());
    for(const el of wrap.querySelectorAll('[data-research-card]')){
      if(!visible.some(record=>record.progress.id===el.dataset.researchCard))el.remove();
    }
    visible.forEach(record=>{
      const old=[...wrap.querySelectorAll('[data-research-card]')].find(el=>el.dataset.researchCard===record.progress.id);
      const el=card(record,old);if(!old)wrap.append(el);
    });
    if(visible.length && typeof updateEmpty==='function')updateEmpty();
  }
  function put(conv,p){
    if(!p?.id)return;
    const previous=records.get(p.id);
    if(previous && seconds(previous.progress.updated_at)>seconds(p.updated_at))return;
    records.set(p.id,{conv:conversation(conv),progress:p});render();
    window.refreshTaskBar?.();
  }
  async function restore(task,conv){
    if(fetching.has(task.id))return fetching.get(task.id);
    const before=records.get(task.id);
    const request=(async()=>{
      try{
        const response=await api('/tasks/'+encodeURIComponent(task.id)+'/research/progress');
        if(!response.ok)return;
        const progress=await response.json();
        // 请求期间 SSE 已带来新快照时，不能被较早的 HTTP 响应覆盖。
        if(records.get(task.id)===before)put(conv,progress);
      }catch(_){}finally{fetching.delete(task.id);}
    })();fetching.set(task.id,request);return request;
  }
  function tasks(rows){
    rows.filter(inline).forEach(task=>restore(task,S.conv));render();
  }
  function event(e){
    if(e.progress){put(e.conv,{...e.progress,id:e.progress.id || e.task_id});return;}
    const previous=records.get(e.task_id);
    if(e.type==='research_result' && previous){
      const terminal=['failed','cancelled'].includes(e.status) ? e.status : 'done';
      put(e.conv,{...previous.progress,status:terminal,task_status:terminal,delivery_pending:active(previous.progress)});
    }
    restore({id:e.task_id},e.conv);
  }
  setInterval(()=>{
    for(const el of document.querySelectorAll('[data-research-card]')){
      const record=records.get(el.dataset.researchCard);
      if(record && active(record.progress))el.querySelector('.research-metrics').textContent=metrics(record.progress);
    }
  },1000);
  const represented=t=>inline(t) && records.get(t.id)?.conv===session();
  return {inline,represented,tasks,event,render};
})();
