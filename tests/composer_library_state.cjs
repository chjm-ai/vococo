// 执行真实 composer 函数；仅替换 DOM 和网络，验证附件状态而非代码文本。
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../vococo/gateway/adapters/web_static/composer.js'),'utf8');
const input={value:'原草稿'}, callView={hidden:true};
const S={conv:'one',images:[{data:'image',media_type:'image/png'}],audios:[{id:'audio',filename:'audio'}],
  files:[{id:'file',filename:'file'}],libraryRefs:[{id:'art',title:'作品'}],composerAttachments:{},
  sending:{},inflightComposer:{},localSent:{},sendAcks:{},convs:[],stream:null};
const original=JSON.parse(JSON.stringify({images:S.images,audios:S.audios,files:S.files,libraryRefs:S.libraryRefs}));
const drafts={};
const noop=()=>{};
let payload,release;
const context=vm.createContext({S,console,setTimeout,clearTimeout,Promise,_chatAnchor:null,
  $:selector=>selector==='#ta'?input:callView,renderThumbs:noop,autoGrow:noop,closeCmdMenu:noop,
  saveDraft:(conv,text)=>drafts[conv]=text??input.value,restoreDraft:conv=>input.value=drafts[conv]||'',
  clearDraft:conv=>delete drafts[conv],ensureStream:()=>S.stream={},
  addBubble:()=>({closest:()=>({remove:noop}),append:noop}),
  api:async(url,options)=>{payload=JSON.parse(options.body);return await new Promise(resolve=>release=resolve);}
});
vm.runInContext(source.slice(0,source.indexOf('// 静默发命令')),context);
const plain=value=>JSON.parse(JSON.stringify(value));
(async()=>{
  const scenario=process.argv[2];
  if(scenario==='isolation'){
    context.saveComposerState();
    S.conv='two';context.restoreComposerState('two');
    assert.deepEqual(plain({images:S.images,audios:S.audios,files:S.files,libraryRefs:S.libraryRefs}),
      {images:[],audios:[],files:[],libraryRefs:[]});
    S.libraryRefs=[{id:'two-art'}];context.saveComposerState();
    S.conv='one';context.restoreComposerState('one');
    assert.deepEqual(plain(S.composerAttachments.one),original);
    assert.deepEqual(plain(S.libraryRefs),original.libraryRefs);
    assert.deepEqual(plain(S.composerAttachments.two.libraryRefs),[{id:'two-art'}]);
  }else{
    const pending=context.send('原草稿',null,{reuseBubble:true});
    assert.deepEqual(plain({images:S.images,audios:S.audios,files:S.files,libraryRefs:S.libraryRefs}),
      {images:[],audios:[],files:[],libraryRefs:[]});
    assert.equal(input.value,'');
    assert.deepEqual(plain(S.inflightComposer.one),{text:'原草稿',...original});
    assert.deepEqual(payload.files,[{id:'file'}]);
    assert.deepEqual(payload.library_refs,['art']);
    if(scenario==='background'){
      S.conv='two';S.libraryRefs=[{id:'new-art'}];input.value='第二会话草稿';
    }
    release({ok:false,status:400,json:async()=>({error:'拒绝'})});
    await pending;
    assert.deepEqual(plain(S.composerAttachments.one),original);
    assert.equal(drafts.one,'原草稿');
    assert.equal(S.sending.one,undefined);
    if(scenario==='background'){
      assert.equal(input.value,'第二会话草稿');
      assert.deepEqual(plain(S.libraryRefs),[{id:'new-art'}]);
      S.conv='one';context.restoreComposerState('one');
    }
    assert.equal(input.value,'原草稿');
    assert.deepEqual(plain({images:S.images,audios:S.audios,files:S.files,libraryRefs:S.libraryRefs}),original);
  }
  console.log('通过：'+scenario);
})().catch(error=>{console.error(error);process.exitCode=1;});
