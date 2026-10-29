"""多个窗口收到同一审批,处理结果必须精确关闭对应弹窗。"""
from pathlib import Path
import asyncio
import json
import shutil
import subprocess

import anyio
import pytest

from vococo.gateway import clarify
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import notices

STATIC = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static"


@pytest.mark.parametrize("answer", ["允许一次", "拒绝", None])
def test_choice_settlement_broadcasts_id_to_all_windows(isolated, monkeypatch, answer):
    async def scenario():
        adapter = WebAdapter()
        queues = [asyncio.Queue() for _ in range(2)]
        adapter._clients.update(queues)
        monkeypatch.setattr(notices, "_listener", lambda change: adapter._emit({"type": "notices", **change}))
        p = clarify.register("web:c1", ["允许一次", "拒绝"], notice={"kind": "approval", "prompt": "批?"})
        for queue in queues:
            queue.get_nowait()  # 登记通知的刷新事件
        if answer is not None:
            clarify.resolve(p.clarify_id, answer)
        await clarify.wait(p.clarify_id, 0.01)
        for queue in queues:
            event = json.loads(queue.get_nowait()[1])
            assert event["clarify_id"] == p.clarify_id
            assert event["status"] == ("answered" if answer is not None else "expired")
    anyio.run(scenario)


def test_two_windows_close_only_settled_choice():
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node 执行真实前端事件处理代码")
    script = r'''
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const source=fs.readFileSync(STATIC+'/stream.js','utf8');
const noticeSource=fs.readFileSync(STATIC+'/notices.js','utf8');
function makeWindow(){
  const nodes={}, choices=[];
  function element(){return {dataset:{},hidden:true,children:[],classList:{add(){}},
    append(...xs){this.children.push(...xs);},querySelectorAll(){return this.children;},
    set innerHTML(v){this.children=[];},get innerHTML(){return '';}};}
  const ctx={S:{conv:'other',convs:[],pendingChoice:{}},window:{addEventListener(){}},
    IntersectionObserver:class{observe(){} unobserve(){}},
    ResizeObserver:class{observe(){} unobserve(){}},setInterval(){},setTimeout(){},
    IS_MOBILE_DEVICE:false,navigator:{userAgent:''},URLSearchParams,location:{search:''},
    document:{addEventListener(){},querySelectorAll(){return choices;}},
    $:s=>nodes[s]||(nodes[s]=element()),el:()=>element(),mdToHtml:s=>s,esc:s=>s,
    ic:()=>'',scrollDown(){},loadNotices(){},sendCmd(){},addBubble:()=>element()};
  vm.createContext(ctx);vm.runInContext(source,ctx);
  return {ctx,nodes,choices,event:e=>ctx.handleEvent(e)};
}
const choice=(id,conv='c1')=>({type:'choice',conv,prompt:'批准?',options:[['/clarify '+id+' 0','允许一次']]});
const a=makeWindow(),b=makeWindow();
for(const w of [a,b]) w.event(choice('first'));
assert.equal(b.nodes['#choiceModal'].hidden,false);
a.nodes['#choiceModalOpts'].children[0].onclick();
for(const w of [a,b]) w.event({type:'notices',clarify_id:'first',status:'answered'});
assert.equal(b.nodes['#choiceModal'].hidden,true,'其他窗口必须关闭已处理审批');
assert.equal(b.ctx.S.pendingChoice.c1,undefined);
// 普通铃铛刷新不得关闭仍然待批的弹窗;旧结果不得误关新审批。
b.event(choice('second'));
b.event({type:'notices'});
b.event({type:'notices',clarify_id:'first',status:'answered'});
assert.equal(b.nodes['#choiceModal'].hidden,false);
assert.equal(b.ctx.S.pendingChoice.c1.options[0][0],'/clarify second 0');
b.event({type:'notices',clarify_id:'second',status:'expired'});
assert.equal(b.nodes['#choiceModal'].hidden,true);
// 结束状态早于旧 choice 补发抵达时,也不能重新弹出。
b.event(choice('second'));
assert.equal(b.nodes['#choiceModal'].hidden,true);
// 内联按钮同样失效,不影响另一条待批卡。
const inline=makeWindow();inline.ctx.S.conv='c1';
const old= {dataset:{clarifyId:'inline'},querySelectorAll:()=>[button]},button={disabled:false};
inline.choices.push(old);
inline.event({type:'notices',clarify_id:'inline',status:'answered'});
assert.equal(button.disabled,true);
// 重连的内容快照先把游标推进,较早的审批结束事件仍必须生效。
b.event(choice('reconnect'));
vm.runInContext('FetchSSE.prototype._loop=function(){}; connect(); S.lastId=100;',b.ctx);
b.ctx.S.es.onmessage({data:JSON.stringify({type:'notices',id:99,clarify_id:'reconnect',status:'answered'})});
assert.equal(b.nodes['#choiceModal'].hidden,true);
assert.equal(b.ctx.S.lastId,100);
// 铃铛补出的自由回答提问也必须带编号;迟到的 HTTP 快照不能复活它。
vm.runInContext(noticeSource,b.ctx);
vm.runInContext("NTC.items=[{id:'nt1',status:'pending',clarify_id:'free',conv:'c1',prompt:'问?',options:[]}]; autoPopupNewPending();",b.ctx);
assert.equal(b.nodes['#choiceModal'].hidden,false);
b.event({type:'notices',clarify_id:'free',status:'answered'});
assert.equal(b.nodes['#choiceModal'].hidden,true);
assert.equal(b.ctx.S.pendingChoice.c1,undefined);
vm.runInContext("NTC.seen.clear(); autoPopupNewPending();",b.ctx);
assert.equal(b.nodes['#choiceModal'].hidden,true);
assert.equal(b.ctx.S.pendingChoice.c1,undefined);
'''.replace("STATIC", json.dumps(str(STATIC)))
    result = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
