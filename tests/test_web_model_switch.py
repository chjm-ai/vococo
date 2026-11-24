"""用真实的网页发送函数验证模型回执与语音转写的路由。"""
import shutil
import subprocess
from pathlib import Path

import pytest


STATIC = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static"


def _run_node(source, scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node.js")
    result = subprocess.run([node, "-e", source + "\n" + scenario], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_handoff_dialog_is_not_inside_a_surface_hidden_by_chat_mode():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    # 必须是通话、工作台和普通聊天之外的独立弹窗，否则 showModal 也不可见。
    dialog = page.index('<dialog id="modelHandoffDialog"')
    assert page.index('</main>', page.index('<main id="workbenchView"')) < dialog
    assert dialog < page.index('<main id="callView"')


def test_web_waits_for_model_ack_and_does_not_send_queue_implicitly():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    source = page[page.index("var modelSwitching ="):page.index('$("#modelBtn").onclick')]
    _run_node(source, r'''
const assert=require("node:assert/strict");
const old="agy-native:gemini-4-neon", next="gpt-6.2-sol";
const job={text:"未发送的原话"};
const S={conv:"x", models:{chosen_model:old}, sending:{}, pending:{x:[job]}};
const permConvId=x=>x, hideUsageTip=()=>{}, agyCopyActive=()=>false;
const curModel=()=>S.models.chosen_model;
const setModel=id=>{ S.models.chosen_model=id; };
const drawPending=()=>{}, updateUsageMeta=()=>{}, reloadSavedModels=async()=>{};
let modelsLoadSeq=0, disabled=false, calls=[], alerts=[], resolveChoice;
const box={};
const dialog={showModal(){}, addEventListener(_,fn){ resolveChoice=fn; }};
const $=name=>name==="#modelHandoffDialog" ? dialog : box;
const window={alert:text=>alerts.push(text)};
const updateSendBtn=()=>{ disabled=isModelSwitching(); };
let resolveAck;
const api=async (path, opts)=>{
  calls.push(JSON.parse(opts.body));
  if(calls.length===1) return {ok:false,json:async()=>({code:"handoff_required"})};
  return new Promise(resolve=>{ resolveAck=resolve; });
};
(async()=>{
  const pending=pickModel(next);
  assert.equal(curModel(),old);
  assert.equal(disabled,true);
  while(!resolveChoice) await Promise.resolve();
  dialog.returnValue="recent"; resolveChoice();
  while(!resolveAck) await Promise.resolve();
  assert.equal(curModel(),old,"等待服务端确认期间不得乐观改模型");
  assert.equal(calls[1].handoff,"recent");
  assert.equal(calls[1].previous_model,old);
  resolveAck({ok:true,json:async()=>({chosen_model:next})}); await pending;
  assert.equal(curModel(),next);
  assert.equal(disabled,false);
  assert.equal(job.modelMismatch,true,"原待发消息只能由用户明确重试");
  assert.equal(job.text,"未发送的原话");
  const second=pickModel(old);
  while(calls.length!==3) await Promise.resolve();
  resolveAck({ok:false,json:async()=>({error:"会话忙碌"})}); await second;
  assert.equal(curModel(),next);
  assert.deepEqual(alerts,["会话忙碌"]);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


def test_context_recovers_from_sidebar_without_hiding_ordinary_turn_usage():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    sidebar = (STATIC / "sidebar.js").read_text(encoding="utf-8")
    source = page[page.index("const CTX_WINDOW"):page.index("function updateUsageMeta(")]
    source += page[page.index("function metaRow("):page.index("function copyConvId(")]
    source += sidebar[sidebar.index("async function loadConvs(){"):sidebar.index("// 推送连上时")]
    _run_node(source, r'''
const assert=require("node:assert/strict");
const agy="agy-native:gemini-4-neon", sdk="claude-sonnet-5-5";
const S={conv:"x",models:{engine_segment:"a"},voiceSidebar:{},convs:[]};
const permConvId=x=>x, isDraftConv=()=>false, esc=x=>x, ic=()=>"", modelShort=x=>x;
const curModel=()=>S.model;
const setModel=x=>{ S.model=x; renderMeta(); };
const nodes=new Map();
const $=id=>nodes.get(id) || nodes.set(id,{style:{},classList:{toggle(){}}}).get(id);
const applyConvs=(d, refresh)=>{ S.convs=d; refillCurrentMeta(refresh); };
const prefetchHistories=()=>{}, idbSet=()=>{};
let _convsRequestId=0, respond;
const api=()=>new Promise(resolve=>{ respond=d=>resolve({json:async()=>d}); });
const snap=(used)=>({conv:"x",chosen_model:agy,model:agy,ctx_tokens:used,ctx_window:1000,engine_segment:"a"});
(async()=>{
  setMeta(snap(50));
  const missed=loadConvs(); respond([snap(200)]); await missed;
  assert.equal($("#ctxPct").textContent,"20%","侧栏能补回漏掉的同一会话段用量");
  const delayed=loadConvs(); setMeta(snap(300)); respond([snap(200)]); await delayed;
  assert.equal($("#ctxPct").textContent,"30%","迟到的侧栏回执不能覆盖实时快照");
  setMeta({chosen_model:sdk,model:sdk,last_in:12,last_out:9,last_cache:3,total_tokens:24});
  assert.equal($("#ctxPct").textContent,"—");
  for(const label of ["本轮新增输入","其中缓存复用","本轮输出","累计消耗"]){
    assert.ok($("#ctxPop").innerHTML.includes(label));
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


def test_manual_voice_transcript_has_its_pinned_model_and_preserves_draft():
    js = (STATIC / "composer.js").read_text(encoding="utf-8")
    source = js[js.index("function agyModelMarker("):js.index("async function send(")]
    source += js[js.index("function retainUnsentText("):js.index("// 转写没识别到内容")]
    _run_node(source, r'''
const assert=require("node:assert/strict");
const agy="agy-native:gemini-4-neon";
let model=agy, saved, resolveTranscript, sends=[];
const row={remove(){}};
const bubble={classList:{remove(){}},closest:()=>row};
const S={conv:"x",voiceRec:{x:{bubble}}, models:{engine_segment:"segment-1"},sendAcks:{}};
const curModel=()=>model, isModelSwitching=()=>false, agyCopyActive=()=>false;
const ta={value:"已有草稿"}, $=()=>ta, autoGrow=()=>{}, addBubble=()=>{};
const saveDraft=(key,text)=>{ saved={key,text}; };
const permConvId=x=>x, convOwnership=()=>({agent_id:"general"}), newClientRequestId=()=>"request-id";
const loadConvs=()=>{};
const api=(path,opts)=>{
  if(path==="/send") { sends.push(JSON.parse(opts.body)); return Promise.resolve({ok:true}); }
  return new Promise(resolve=>{resolveTranscript=()=>resolve({ok:true,json:async()=>({text:"口述内容"})});});
};
(async()=>{
  const first=sendVoice(new Blob(["x"]),"webm","x"); resolveTranscript(); await first;
  assert.equal(sends[0].agy_trial_model,agy);
  assert.equal(sends[0].engine_segment,"segment-1");
  assert.equal(sends[0].expected_model,agy);
  assert.equal(sends[0].images,undefined);
  assert.equal(ta.value,"已有草稿");
  S.voiceRec.x={bubble};
  const second=sendVoice(new Blob(["x"]),"webm","x"); model="gpt-6.2-sol";
  resolveTranscript(); await second;
  assert.equal(sends.length,1);
  assert.deepEqual(saved,{key:"x",text:"已有草稿\n口述内容"});
})().catch(e=>{console.error(e);process.exitCode=1;});
''')
