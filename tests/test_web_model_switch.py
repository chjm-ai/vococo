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
