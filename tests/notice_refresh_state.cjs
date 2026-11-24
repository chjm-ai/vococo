// 铃铛状态回归：用可控的网络响应测试真实 notices.js。跑法：node --test tests/notice_refresh_state.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const src = fs.readFileSync(path.join(__dirname, "../vococo/gateway/adapters/web_static/notices.js"), "utf8");
const pending = {id:"n1", status:"pending", kind:"ask", prompt:"要发布吗？", options:["发", "不发"]};

function setup(api){
  const pop = {hidden:false, querySelectorAll:()=>[]};
  const pendingChoice = {};
  const settled = new Set();
  const page = vm.createContext({
    api, $:sel=>sel==="#noticePop" ? pop : sel==="#noticeBtn" ? {} : null,
    document:{addEventListener(){}, querySelectorAll:()=>[], body:{classList:{toggle(){}}}},
    location:{search:""}, URLSearchParams,
    window:{}, fmtTime:()=>"", esc:x=>x, mdToHtml:x=>x, ic:()=>"",
    S:{pendingChoice}, _settledChoices:settled, choiceId:e=>e.clarify_id,
    settleChoice:id=>settled.add(id),
  });
  vm.runInContext(src, page);
  return {page, pop, pendingChoice, settled};
}

test("等待中的提问可忽略，但审批仍须明确回应", ()=>{
  const {page} = setup();
  assert.match(page.noticeItemHtml(pending), /data-ntdismiss="n1"/);
  assert.doesNotMatch(page.noticeItemHtml({...pending, kind:"approval"}), /data-ntdismiss/);
});

test("铃铛请求期间收到超时事件也会再取最新状态", async()=>{
  let finishFirst, calls = 0;
  const response = item=>({ok:true, json:async()=>({items:[item]})});
  const {page, pop} = setup(()=>{
    if(++calls===1) return new Promise(resolve=>{ finishFirst=resolve; });
    return Promise.resolve(response({...pending, status:"expired"}));
  });
  const loading = page.loadNotices();
  page.loadNotices(true); // SSE 到达时，请求仍在传回旧的 pending 快照
  finishFirst(response(pending));
  await loading;
  assert.equal(calls, 2);
  assert.match(pop.innerHTML, /错过了.*data-ntdismiss/s);
  assert.doesNotMatch(pop.innerHTML, /等你回答/);
});

test("SSE 重连后重新核对，不能复用断线前的请求", async()=>{
  let finishFirst, calls = 0;
  const {page, pop} = setup(()=>{
    if(++calls===1) return new Promise(resolve=>{ finishFirst=resolve; });
    return Promise.resolve({ok:true, json:async()=>({items:[]})});
  });
  const stream = fs.readFileSync(path.join(__dirname, "../vococo/gateway/adapters/web_static/stream.js"), "utf8");
  Object.assign(page, {FetchSSE:class{}, reloadHistory(){}, taskTree:{schedule(){}}, checkShellVersion(){}});
  vm.runInContext(stream.slice(stream.indexOf("function connect(){"), stream.indexOf("// 手机救命稻草")), page);
  const loading = page.loadNotices();
  page.connect();
  page.S.es.onopen(false);
  finishFirst({ok:true, json:async()=>({items:[pending]})});
  await loading;
  assert.equal(calls, 2);
  assert.match(pop.innerHTML, /没有待处理的事/);
});

test("打开铃铛时复用已经在途的状态请求", async()=>{
  let finish, calls = 0;
  const {page, pop} = setup(()=>{
    if(++calls>1) throw new Error("不该发第二次请求");
    return new Promise(resolve=>{ finish=resolve; });
  });
  const loading = page.loadNotices(true);
  assert.equal(page.loadNotices(), loading);
  finish({ok:true, json:async()=>({items:[{...pending, status:"expired"}]})});
  await loading;
  assert.equal(calls, 1);
  assert.match(pop.innerHTML, /错过了/);
});

test("漏掉收尾事件后核对旧输入框，不关仍在等待的选项", async()=>{
  const {page, pendingChoice, settled} = setup(async()=>({ok:true, json:async()=>({items:[], waiting_clarify_ids:["live"]})}));
  pendingChoice.old = {clarify_id:"ignored"};
  pendingChoice.new = {clarify_id:"live"};
  await page.loadNotices();
  assert.deepEqual([...settled], ["ignored"]);
});
