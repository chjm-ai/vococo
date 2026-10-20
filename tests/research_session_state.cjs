// 研究事件必须独立于聊天流；不启动服务，不访问模型。
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const dir = path.join(__dirname, "../vococo/gateway/adapters/web_static");
const stream = fs.readFileSync(path.join(dir, "stream.js"), "utf8");
const html = fs.readFileSync(path.join(dir, "index.html"), "utf8");
const begin = stream.indexOf('  if(e.type==="research_update"');
const end = stream.indexOf('  if(e.type==="sync")', begin);
assert.ok(begin > 0 && end > begin);
const handler = new Function("e", "S", "$", "addBubble", "idbDel", "loadConvs", "researchProgress", stream.slice(begin, end));
const updates=[];
const researchProgress={event:e=>updates.push(e)};
const active = { text: "正常生成中的回复", timer: 7 };
const S = { conv: "source", stream: active, live: {source: true}, histCache: {source: []} };
let rows = [];
const wrap = {querySelectorAll: () => rows, querySelector: () => null};
const $ = () => wrap;
function addBubble(who, text) {
  const row = { dataset: {}, remove() { rows = rows.filter(other => other !== row); } };
  rows.push(row);
  return { textContent: text, closest: () => row };
}
const emit = event => handler(event, S, $, addBubble, () => {}, () => {}, researchProgress);
emit({type: "research_update", conv: "source", task_id: "research", id: 10, title: "主题", status: "running"});
emit({type: "research_update", conv: "source", task_id: "research", id: 11, title: "主题", progress_note: "搜集资料"});
assert.equal(rows.length, 0, "进度交给活动卡，不追加普通消息");
assert.equal(updates.length, 2, "不同事件序号均交给研究卡去重更新");
emit({type: "research_result", conv: "source", task_id: "research", id: 12, text: "最终报告"});
assert.equal(rows.length, 1, "报告独立显示，不动活动卡和主回复");
assert.equal(S.stream, active);
assert.equal(S.live.source, true);
assert.equal(S.histCache.source, undefined);
emit({type: "research_result", conv: "source", task_id: "research", id: 12, text: "最终报告"});
assert.equal(rows.length, 1, "补发相同报告不能产生重复气泡");
emit({type: "research_update", conv: "other", task_id: "other", title: "别的会话"});
assert.equal(rows.length, 1);

S.histLoading = "source";
emit({type: "research_result", conv: "source", task_id: "during-load", text: "加载期间完成", turn_id: 9});
assert.equal(S.researchPending[JSON.stringify(['during-load',9])].text, "加载期间完成");
S.histLoading = null;
emit(S.researchPending[JSON.stringify(['during-load',9])]);
assert.equal(rows.length, 2, "加载期间的终态事件必须保留到历史铺完后展示");
wrap.querySelector = () => ({dataset: {tid: "9"}});
emit(S.researchPending[JSON.stringify(['during-load',9])]);
assert.equal(rows.length, 2, "历史已经包含报告时不能重复追加");

// 同一任务恢复后生成新的投递轮次，不得按任务 ID 吞掉新报告。
wrap.querySelector = () => null;
emit({type:'research_result',conv:'source',task_id:'research',turn_id:20,text:'恢复后的报告'});
emit({type:'research_result',conv:'source',task_id:'research',turn_id:20,id:99,text:'恢复后的报告'});
assert.equal(rows.length,3,'新 turn 显示一次，重投不重复');
S.histLoading='source';
for(const turn_id of [21,22])emit({type:'research_result',conv:'source',task_id:'research',turn_id,text:'缓冲报告'+turn_id});
S.histLoading=null;
for(const event of Object.values(S.researchPending))emit(event);
assert.equal(rows.length,5,'历史加载期间同任务的不同报告均保留');

// 回前台时的权威历史核对：最后一条是研究报告，不代表普通聊天已完成。
const start = html.indexOf("async function reloadHistory(force){");
const finish = html.indexOf("\n// ── 新会话", start);
assert.ok(start > 0 && finish > start);
const reload = new Function("S", "api", "streamText", "finalizeStream", "mergeLatestHistory",
  "histCacheSet", "idbSetHist", "renderTurns", "return (" + html.slice(start, finish).trim() + ")(true)");
let finalized = false;
S.stream.tools = [];
const api = async () => ({json: async () => ({turns: [
  {id: 1, pending: true, draft: ""}, {id: 2, pending: false, assistant: "研究报告"}
]})});
(async () => {
  await reload(S, api, () => "主回复正文", () => { finalized = true; }, () => {}, () => {}, () => {}, () => {});
  assert.equal(finalized, false, "研究报告排在历史末尾时不能提前结束普通聊天");
  assert.equal(S.stream, active);
  console.log("研究来源会话前端状态回归通过");
})().catch(error => { console.error(error); process.exitCode = 1; });
