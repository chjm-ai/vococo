// 通话视图·Omni-Realtime WebRTC 段(从 index.html 拆出,2026-07-12,见 docs/adr/0004)
// 免提通话唯一管线:Omni 当"耳朵+嘴巴"(识别/VAD/朗读),大脑仍是 Claude
// (转写文字回灌 /voice/send,tts=false,sentence 事件进 omniReadQueue 逐句点播)。
// 依赖 index.html 的全局:S/$ /addMsg/renderAi/setStatus/vdbg/playTone 系列/
// wsState/handsFreeActive/currentTurn*/orbWrap/handsFreeUi 等——经典脚本共享
// 全局词法环境,本文件由 <script src> 紧随主内联脚本之后加载,声明先于一切调用。
  // ── P3 阶段二:Omni-Realtime WebRTC 通话(见 wireOmniDataChannel 一段)────────
  // S.omniEnabled(登录时预取,见 loadVoiceOmniConfig)开着就整条走 WebRTC 替代
  // 上面这套 ws.py + Web Audio API 播放队列,两条路径互斥、共用同一批 UI 元素
  // (orbWrap/statusEl/handsFreeUi 等),关着完全不影响原有路径。
  let omniPc=null, omniDc=null, omniMicStream=null, omniUserLiveEl=null;
  // Omni 出声模式的朗读队列:Claude 的回答按句子进队,靠 conversation.item.create +
  // response.create 让 Omni 逐句念出来(同一时刻只能有一个进行中的 response,
  // 靠 omniReadActive 串行化);omniReadPending 记录"我们主动点的 response.create
  // 还有几个没收到 response.created",用来区分自动回复(要 cancel)和我们的朗读。
  let omniReadQueue=[], omniReadActive=false, omniReadPending=0, omniAutoActive=false, omniAutoSince=0, omniTurnDone=true;
  let omniReconnectTimer=null;
  let omniReadSince=0, omniReadWatchdog=null;  // 朗读心跳时间戳 + 卡死看门狗(见 omniReadStallCheck)
  // 回声过滤(2026-07-11):iOS AEC 在 AI 每句开口头 1~2 秒未收敛,残留回声会被
  // 阿里云 VAD 当成用户说话,转写出来的是"刚念那句的开头几个字"(见 matchOmniEcho)。
  let omniRecentReads=[];        // 最近念过的句子 [{text, ts}],转写结果跟它们比对
  let omniReadCurrentText="";    // 正在念/刚点播还没念完的那句原文(恢复现场用)
  let omniEchoRestore=null;      // barge-in 现场快照,事后判定是回声就恢复被砍的朗读
  const omniAudioEl = $("#omniRemoteAudio");


  // ── P3 阶段二:Omni-Realtime WebRTC 通话 ─────────────────────────────────
  // 2026-07-10 架构(第二版,Omni 出声):Omni 当"耳朵+嘴巴",大脑仍然是 Claude。
  // - 耳朵:WebRTC 连线做识别(ASR)+断句(VAD)+打断信号,跟第一版相同。
  // - 大脑:识别到一整句话后转发给 /voice/send(Claude 全套工具/画像/记忆),
  //   这一步也不变;但 body 带 tts:false,服务端不再合成 TTS。
  // - 嘴巴(本次新增):Claude 的回答按句子经 conversation.item.create(【朗读】前缀)
  //   + response.create 交给 Omni 念,音频走 WebRTC 远端轨道(RTP)回来从
  //   #omniRemoteAudio 播——这是阿里云文档的标准链路,服务端回声消除+语义防
  //   误打断只有音频走 RTC 闭环才生效;之前"自己 TTS+Web Audio 播放"在这条
  //   链路外,AEC 拿不到参考信号,才有"AI 录到自己说话"的假对话问题。
  // - 已知约束:turn_detection.create_response 实测关不掉,用户每说一句 Omni
  //   都会自作主张生成一份回复——靠 session.instructions 把它压成一个字 +
  //   response.created 时对"不是我们点的"回复立即 response.cancel 双保险,
  //   见 wireOmniDataChannel 的 response.created 分支。
  let omniTurnGen = 0;  // 每次新一句开始/打断都 +1,过期的 /voice/send 流式结果据此判断该不该再应用

  async function startOmniHandsFree(){
    setStatus("连接中…", "");
    unlockAudio();
    try{ omniMicStream = await ensureMicStream(); }
    catch(e){
      handsFreeUi.hidden = true; talkBtn.hidden = false;
      addMsg("ai error", "麦克风权限被拒绝,请到系统设置里允许");
      return;
    }
    ensureAnalyser();        // 麦克风侧声纹(capturing 状态用)
    if(!orbAnimHandle) drawOrbWaveform();

    omniPc = new RTCPeerConnection();
    omniMicStream.getTracks().forEach(t => omniPc.addTrack(t, omniMicStream));
    // Omni 出声:AI 语音从远端轨道回来,接到常驻的 <audio> 元素上播放。
    omniPc.ontrack = ev => {
      vdbg("pc.ontrack", {kind: ev.track && ev.track.kind});
      const ms = (ev.streams && ev.streams[0]) || new MediaStream([ev.track]);
      omniAudioEl.srcObject = ms;
      omniAudioEl.play().then(
        ()=>vdbg("audio.play.ok"),
        err=>vdbg("audio.play.fail", String(err)));
    };
    omniPc.onconnectionstatechange = () => {
      if(!omniPc) return;
      const st = omniPc.connectionState;
      vdbg("pc.state", st);
      // 120 分钟会话上限/网络抖动都会走到这里;disconnected 可能自愈,多等一拍。
      if(st === "failed" || st === "closed") scheduleOmniReconnect("pc:"+st, 1500);
      else if(st === "disconnected") scheduleOmniReconnect("pc:disconnected", 4000);
    };

    const dc = omniPc.createDataChannel("events");
    omniDc = dc;
    dc.onopen = () => {
      vdbg("dc.open");
      dc.send(JSON.stringify({
        event_id: "session-init", type: "session.update",
        session: {
          // 出声模式:session 级别就带 audio——阿里云的回声消除/语义打断只认
          // 走自家 RTC 通道的音频。自动回复的音频靠 instructions+cancel 压制。
          modalities: ["text", "audio"],
          voice: S.omniVoice,
          instructions: "你是一个只负责朗读的语音引擎。收到以【朗读】开头的消息时," +
            "自然流畅地读出【朗读】后面的全部内容,一字不差,不要添加、省略或改动任何内容," +
            "也不要发表自己的看法。收到其他任何消息时,只回答一个字:嗯。",
          // 2026-07-10:改用 semantic_vad(阿里云文档明确给 qwen3.5-omni-realtime 系列
          // 推荐这个模式)——server_vad 纯按静音毫秒数判停,用户说话中间正常思考停顿
          // ("呃"、组织语言)会被硬切;semantic_vad 按语义完整性判断这句话说完没有,
          // 才是治这个问题的根本方式,不是死调 silence_duration_ms 数值。threshold/
          // silence_duration_ms 仍然传,作为语义判断之外的兜底(过长静音还是会强制切)。
          turn_detection: { type: "semantic_vad", threshold: S.vadThreshold, silence_duration_ms: S.vadSilenceMs },
        },
      }));
    };
    dc.onclose = () => { vdbg("dc.close"); scheduleOmniReconnect("dc:close", 1500); };
    wireOmniDataChannel(dc);
    omniPc.ondatachannel = ev => wireOmniDataChannel(ev.channel);

    const offer = await omniPc.createOffer();
    await omniPc.setLocalDescription(offer);
    await new Promise(resolve => {
      if(omniPc.iceGatheringState === "complete") return resolve();
      omniPc.onicegatheringstatechange = () => { if(omniPc.iceGatheringState === "complete") resolve(); };
    });

    let resp;
    try{
      resp = await fetch("/voice/omni/webrtc", {
        method: "POST",
        headers: {"Content-Type": "application/sdp", "X-Auth-Token": S.token},
        body: omniPc.localDescription.sdp,
      });
    }catch(e){
      addMsg("ai error", "WebRTC 信令请求失败(网络)"); return false;
    }
    if(!resp.ok){
      addMsg("ai error", "WebRTC 连接失败:" + resp.status);
      return false;
    }
    const answerSdp = await resp.text();
    await omniPc.setRemoteDescription({type: "answer", sdp: answerSdp});

    handsFreeActive = true;
    orbWrap.className = "idle";
    clearHint();
    setStatus("聆听中…", "listening");
    if(omniReadWatchdog) clearInterval(omniReadWatchdog);
    omniReadWatchdog = setInterval(omniReadStallCheck, 3000);
    return true;
  }

  // 断连重连兜底:阿里云单会话最长 120 分钟到点会主动断,网络切换(WiFi↔蜂窝)
  // 也会把 PC 打断。轻量策略:整个拆掉重建一条连线,间隔一拍防抖;handsFreeActive
  // 为 false(用户已挂断)就什么都不做。
  function scheduleOmniReconnect(reason, delayMs){
    if(!handsFreeActive || omniReconnectTimer) return;
    vdbg("reconnect.scheduled", reason);
    omniReconnectTimer = setTimeout(async () => {
      omniReconnectTimer = null;
      if(!handsFreeActive) return;
      // disconnected 可能已经自愈,重建前再看一眼当前状态
      if(omniPc && (omniPc.connectionState === "connected" || omniPc.connectionState === "connecting")){
        vdbg("reconnect.skipped", "pc recovered");
        return;
      }
      vdbg("reconnect.start", reason);
      setStatus("重连中…", "thinking");
      playDisconnectTone();  // 让用户知道刚才断了,而不是全程静默自愈
      stopOmniHandsFree();
      let ok = false;
      try{ ok = await startOmniHandsFree(); }
      catch(e){ vdbg("reconnect.fail", String(e)); }
      // 信令失败是 return false 不是 throw,统一在这里兜住继续重试
      if(!ok){ scheduleOmniReconnect("retry", 5000); return; }
      playReconnectTone();
      addMsg("ai", "(通话断开过一下,已经自动接回来了)");
      vdbg("reconnect.ok", reason);
    }, delayMs || 1500);
  }

  // 高频 delta 事件不上报调试日志(每秒几十条会刷爆),其余事件类型全量上报——
  // 这是 2026-07-10 两次盲改教训换来的"仪表盘",别省。
  const OMNI_QUIET_EVENTS = new Set([
    "conversation.item.input_audio_transcription.delta",
    "response.audio_transcript.delta", "response.text.delta", "response.audio.delta",
  ]);

  function wireOmniDataChannel(channel){
    channel.onmessage = ev => {
      let data;
      try{ data = JSON.parse(ev.data); }catch(e){ return; }
      // 朗读心跳:任何 response.* 事件(含高频 audio delta)都说明 Omni 还活着——
      // 看门狗只在"彻底没动静"时才出手,不会误杀正常的长句朗读。
      if(data.type && data.type.startsWith("response.")) omniReadSince = Date.now();
      if(!OMNI_QUIET_EVENTS.has(data.type)){
        vdbg("dc:" + data.type, {
          rp: omniReadPending, ra: omniReadActive, aa: omniAutoActive,
          q: omniReadQueue.length,
          err: data.error ? String(data.error.message || data.error.code || "") : undefined,
        });
      }
      switch(data.type){
        case "input_audio_buffer.speech_started":
          // 用户开口:如果上一轮还在思考/播放,当成打断处理——取消进行中的朗读、
          // 清空朗读队列,让过期的 /voice/send 流式结果(见 sendOmniTurn 的 gen
          // 判断)不再生效。
          if(wsState === "thinking" || wsState === "speaking"){
            // 先留现场再砍:几秒后转写出来若判定是回声(AI 自己的声音),要能把
            // 这里砍掉的朗读恢复回去——否则回声不止造一轮假对话,还把真回答的
            // 剩余句子全吞了(2026-07-11 实锤 q=6→0,用户没听到答案中段)。
            omniEchoRestore = {
              prevGen: omniTurnGen,
              sentences: ((omniReadActive || omniReadPending > 0) && omniReadCurrentText
                ? [omniReadCurrentText] : []).concat(omniReadQueue),
              ts: Date.now(),
            };
            omniTurnGen++;
            omniEchoRestore.newGen = omniTurnGen;
            cancelOmniReading("barge-in");
            // 任务完成播报(flushAnnouncements)仍走 Web Audio 播放,打断时一并停掉
            if(currentSource){ try{ currentSource.onended = null; currentSource.stop(); }catch(e){} currentSource = null; }
            playQueue = []; playing = false; pausedForInterrupt = false;
            // 打断要的是干净的静音,不再叠一个确认音——那个音在切断的同一瞬间响起,
            // 听感上像"声音被调小"而不是"直接停",干脆去掉,靠下面的 capturingTone 提示就够。
          }
          if(wsState !== "capturing") playCapturingTone();
          wsState = "capturing";
          setStatus(STATE_WORD.capturing, STATE_CLASS.capturing);
          if(handsFreeActive) orbWrap.className = "capturing";
          omniUserLiveEl = null;  // 新一句开始,上一句的实时字幕气泡不再更新
          break;
        case "conversation.item.input_audio_transcription.delta":
          // 实时字幕:text 是已确认的前缀,stash 是还没定论的尾巴,拼起来显示
          // 让用户看到"正在识别"的感觉,句子说完由 .completed 用权威结果覆盖一次。
          if(!omniUserLiveEl) omniUserLiveEl = addMsg("me", "");
          renderAi(omniUserLiveEl, (data.text || "") + (data.stash || ""));
          break;
        case "conversation.item.input_audio_transcription.completed": {
          const transcript = (data.transcript || "").trim();
          const restore = omniEchoRestore; omniEchoRestore = null;
          const echoSrc = transcript ? matchOmniEcho(transcript) : null;
          if(echoSrc){
            // 是 AI 自己的回声:丢弃,不发 Claude、不算一轮对话,字幕气泡撤掉
            vdbg("echo.discard", {heard: transcript.slice(0, 24), read: echoSrc.slice(0, 24)});
            if(omniUserLiveEl){
              const row = omniUserLiveEl.closest(".row");
              if(row) row.remove();
              omniUserLiveEl = null;
            }
            if(restore && omniTurnGen === restore.newGen && Date.now() - restore.ts < 15000){
              // 恢复 barge-in 现场:回退 gen 让还在飞的 /voice/send 流(若没被砍到)
              // 继续生效,被吞的句子塞回队列。不立刻 pump——阿里云对这条"用户语音"
              // 还会自作主张生成一个 response(create_response 关不掉),马上 pump 会
              // 跟它的 response.created 撞车导致 rp 计数错位;等它被 cancel 后的
              // response.done 自然接力 pump,再留一个定时器兜底。
              omniTurnGen = restore.prevGen;
              if(restore.sentences.length) omniReadQueue = restore.sentences.concat(omniReadQueue);
              vdbg("echo.restore", {q: omniReadQueue.length});
              wsState = "thinking";
              setStatus(STATE_WORD.thinking, STATE_CLASS.thinking);
              if(handsFreeActive) orbWrap.className = "thinking";
              const g = omniTurnGen;
              setTimeout(() => { if(omniTurnGen === g){ pumpOmniRead(); maybeOmniIdle(); } }, 1200);
              // 流已经被 gen 判定砍死的场景(reader.cancel 已执行),队列念完后
              // omniTurnDone 永远等不来 true,状态会卡在"思考中"——超时强制收尾。
              if(!omniTurnDone){
                setTimeout(() => {
                  if(omniTurnGen === g && wsState === "thinking" && !omniReadActive
                     && omniReadPending === 0 && omniReadQueue.length === 0){
                    omniTurnDone = true;
                    vdbg("echo.restore.idle-fallback");
                    maybeOmniIdle();
                  }
                }, 10000);
              }
            } else {
              // 没有可恢复的现场(回声出现在整轮念完之后):回到待命即可
              wsState = "thinking";  // 先离开 capturing,maybeOmniIdle 才肯接手收尾
              maybeOmniIdle();
              if(wsState !== "idle"){ wsState = "idle"; setStatus(STATE_WORD.idle); if(handsFreeActive) orbWrap.className = "idle"; }
            }
            break;
          }
          if(omniUserLiveEl) renderAi(omniUserLiveEl, transcript || "(没听清)");
          else if(transcript) addMsg("me", transcript);
          omniUserLiveEl = null;
          if(transcript) sendOmniTurn(transcript);
          break;
        }
        case "response.created":
          // 分两种:我们主动点的朗读(omniReadPending>0)放行;其余是 Omni 对用户
          // 话语自作主张的回复(create_response 关不掉,见顶部架构注释),立即取消,
          // 不让它出声——即便 cancel 慢半拍,instructions 也把它压成了一个字。
          if(omniReadPending > 0){
            omniReadPending--;
            omniReadActive = true;
            if(wsState !== "speaking"){
              playSpeakingTone();
              wsState = "speaking";
              setStatus(STATE_WORD.speaking, STATE_CLASS.speaking);
              if(handsFreeActive) orbWrap.className = "speaking";
            }
          } else {
            omniAutoActive = true; omniAutoSince = Date.now();
            try{ channel.send(JSON.stringify({type: "response.cancel"})); }catch(e){}
            vdbg("auto-response.cancelled");
          }
          break;
        case "response.done":
          if(omniReadActive){
            omniReadActive = false;
            omniReadCurrentText = "";
            pumpOmniRead();
          } else {
            omniAutoActive = false;
            pumpOmniRead();  // 自动回复清场了,轮到排队中的朗读
          }
          break;
        case "error": {
          const msg = (data.error && (data.error.message || data.error.code)) || "出错了";
          // response.cancel 撞上"没有进行中的回复"这类时序噪音只记日志不弹红气泡,
          // 用户看到也做不了什么;其余错误照旧可见。
          if(/cancel|active response|no active/i.test(String(msg))) break;
          addMsg("ai error", String(msg));
          break;
        }
        // 其余事件:response.audio_transcript.delta 等是 Omni 朗读的字幕,Claude 的
        // 原文已经在界面上了,不重复渲染;audio 本体走 RTP 轨道,不经过这里。
      }
    };
  }

  // ── Omni 出声:朗读队列 ────────────────────────────────────────────────
  // Claude 的回答按句子进队,一句一个 response(服务端同一时刻只允许一个进行中
  // 的 response,靠 omniReadActive/omniAutoActive 串行);【朗读】前缀跟 session
  // instructions 里的约定对上。
  function pumpOmniRead(){
    if(!omniDc || omniDc.readyState !== "open") return;
    // 保险丝:自动回复的 response.done 万一丢了(cancel 撞 done 的时序缝),别让
    // omniAutoActive 卡死整个朗读队列——超过 5 秒当它已经结束。
    if(omniAutoActive && Date.now() - omniAutoSince > 5000){
      omniAutoActive = false;
      vdbg("auto-active.timeout-cleared");
    }
    if(omniReadActive || omniAutoActive || omniReadPending > 0) return;
    if(omniReadQueue.length === 0){ maybeOmniIdle(); return; }
    const text = omniReadQueue.shift();
    try{
      omniDc.send(JSON.stringify({
        type: "conversation.item.create",
        item: {type: "message", role: "user", content: [{type: "input_text", text: "【朗读】" + text}]},
      }));
      omniDc.send(JSON.stringify({type: "response.create"}));
      omniReadPending++;
      omniReadSince = Date.now();
      omniReadCurrentText = text;
      omniRecentReads.push({text, ts: Date.now()});
      if(omniRecentReads.length > 8) omniRecentReads.shift();
      vdbg("read.create", text.slice(0, 40));
    }catch(e){ vdbg("read.create.fail", String(e)); }
  }

  function maybeOmniIdle(){
    // 整轮收尾:Claude 文字流结束 + 朗读队列清空 + 没有进行中的朗读才回 idle;
    // 用户已经又开口(capturing)就不抢状态。
    if(!omniTurnDone || omniReadActive || omniReadPending > 0 || omniReadQueue.length > 0) return;
    if(wsState === "capturing" || wsState === "idle") return;
    wsState = "idle";
    setStatus(STATE_WORD.idle);
    if(handsFreeActive) orbWrap.className = "idle";
    // 任务播报到达时若正忙会被攒进 pendingAnnouncements,以前只有下一条 task_done
    // 才会再触发 flush——正忙时来的播报就永远卡住不念(2026-07-10 真机:两个任务
    // 完成用户全程没听到)。回到空闲就是补播的正确时机。
    flushAnnouncements();
  }

  // Omni 朗读看门狗:read.create 已发/朗读进行中,但连续 12 秒没有任何 response.*
  // 事件(2026-07-10 真机:朗读 response 已创建后 Omni 彻底哑火,队列里 2 句永远
  // 没念,通话看起来"没回答")——主动 cancel 掉这个哑火的 response,放行队列。
  function omniReadStallCheck(){
    if(!omniDc || omniDc.readyState !== "open") return;
    if(!(omniReadActive || omniReadPending > 0)) return;
    if(Date.now() - omniReadSince <= 12000) return;
    vdbg("read.stall-cleared", {ra: omniReadActive, rp: omniReadPending, q: omniReadQueue.length});
    try{ omniDc.send(JSON.stringify({type: "response.cancel"})); }catch(e){}
    omniReadActive = false; omniReadPending = 0;
    pumpOmniRead();
  }

  // 出错/拒答时让 Omni 把提示念出来——通话中用户不看屏幕,静默失败=「没反应」
  // (2026-07-10 真机:一轮起步就异常,前端只画了个红气泡,用户以为整个系统死了)。
  function speakOmniNotice(text){
    if(!omniDc || omniDc.readyState !== "open") return;
    omniReadQueue.push(text);
    pumpOmniRead();
  }

  function cancelOmniReading(reason){
    omniReadQueue = [];
    if((omniReadActive || omniReadPending > 0 || omniAutoActive) && omniDc && omniDc.readyState === "open"){
      try{ omniDc.send(JSON.stringify({type: "response.cancel"})); }catch(e){}
    }
    omniReadActive = false; omniReadPending = 0;
    omniReadCurrentText = "";
    vdbg("read.cancel", reason);
  }

  // ── 回声过滤(2026-07-11 实锤,详见 memory/hermes-voice-omni-self-echo-rootcause)──
  // iOS 的回声消除是自适应滤波器,AI 每次开口后要 1~2 秒才收敛,这段"开口瞬间"
  // 的残留回声会漏进上行;阿里云只在 response 生成期间压 VAD,而生成比播放快,
  // 播放尾巴不设防——所以偶发"AI 开头几个字被当成用户说话"(还可能带误转写,
  // 如"那个"→"一个")。文字层兜底:转写结果跟最近念过的句子做【容错前缀】匹配。
  // 只认前缀不认中段/句尾:实锤两例都是句子开头(AEC 收敛特性决定),且用户复述
  // AI 句尾来确认("帮我装一个试试")是真实场景,不能误杀——中段命中只记日志观察。
  function normEchoText(s){
    return (s || "").toLowerCase().replace(/[^\p{L}\p{N}]/gu, "");
  }
  function editDist(a, b){
    const n = a.length, m = b.length;
    let prev = Array.from({length: m + 1}, (_, j) => j);
    for(let i = 1; i <= n; i++){
      const cur = [i];
      for(let j = 1; j <= m; j++){
        cur[j] = Math.min(prev[j] + 1, cur[j-1] + 1, prev[j-1] + (a[i-1] === b[j-1] ? 0 : 1));
      }
      prev = cur;
    }
    return prev[m];
  }
  function matchOmniEcho(transcript){
    const t = normEchoText(transcript);
    if(t.length < 3) return null;  // 太短没法可靠归因("嗯""好的"),放行
    const now = Date.now();
    omniRecentReads = omniRecentReads.filter(r => now - r.ts < 30000);
    const tol = t.length < 5 ? 0 : (t.length < 10 ? 1 : 2);
    for(const r of omniRecentReads){
      const cand = normEchoText(r.text);
      if(cand.length < t.length) continue;
      // 容错前缀:转写长度上下浮动 2 字取最小编辑距离(误转写/多听漏听一个字)
      for(let L = Math.max(3, t.length - 1); L <= Math.min(cand.length, t.length + 2); L++){
        if(editDist(t, cand.slice(0, L)) <= tol) return r.text;
      }
      // 命中中段但不是前缀:按当前证据不算回声,只上报观察,攒数据再决定要不要收紧
      if(cand.includes(t)) vdbg("echo.suspect", {heard: t.slice(0, 24), read: r.text.slice(0, 24)});
    }
    return null;
  }

  // 识别到一整句话后,转发给现有 /voice/send——跟文字聊天同一套 Claude 全套
  // 工具/画像/记忆,回答由 Claude 生成,不是 Omni 自己瞎编。tts:false 让服务端
  // 只发句子文本不合成音频,句子进 omniReadQueue 交给 Omni 念(见 pumpOmniRead)。
  // gen 机制:进来时记一个代号,期间被打断(omniTurnGen 变了)就不再把迟到的
  // 流式结果应用到界面/朗读队列上。
  async function sendOmniTurn(text){
    const gen = ++omniTurnGen;
    wsState = "thinking";
    setStatus(STATE_WORD.thinking, STATE_CLASS.thinking);
    if(handsFreeActive) orbWrap.className = "thinking";
    currentTurnAiEl = null; currentTurnFull = "";
    omniReadQueue = []; omniTurnDone = false;
    vdbg("turn.start", text.slice(0, 40));

    const toIdle = () => {
      wsState = "idle"; setStatus(STATE_WORD.idle);
      if(handsFreeActive) orbWrap.className = "idle";
    };

    let resp;
    try{
      resp = await fetch("/voice/send", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-Auth-Token": S.token},
        body: JSON.stringify({text, tts: false}),
      });
    }catch(e){
      if(gen === omniTurnGen){ addMsg("ai error", "连不上服务"); speakOmniNotice("现在连不上服务,稍等再说一次试试。"); omniTurnDone = true; toIdle(); }
      return;
    }
    if(gen !== omniTurnGen) return;  // 已经被打断,后面的响应不用管了
    if(!resp.ok || !resp.body){
      addMsg("ai error", resp.status === 409 ? "上一轮还没说完" : "出错了");
      speakOmniNotice(resp.status === 409 ? "上一句还在处理,稍等一下。" : "刚才那句出错了,再说一遍试试。");
      omniTurnDone = true;
      toIdle();
      return;
    }

    const reader = resp.body.getReader(), dec = new TextDecoder();
    let buf = "";
    try{
      while(true){
        const {value, done} = await reader.read();
        if(gen !== omniTurnGen){ try{ reader.cancel(); }catch(e){} return; }
        if(done) break;
        buf += dec.decode(value, {stream:true});
        let sep;
        while((sep = buf.indexOf("\n\n")) >= 0){
          const block = buf.slice(0, sep); buf = buf.slice(sep+2);
          const ev = parseSseBlock(block);
          if(!ev || gen !== omniTurnGen) continue;
          if(ev.event === "text"){
            if(!currentTurnAiEl) currentTurnAiEl = addMsg("ai", "");
            currentTurnFull += ev.data.text || "";
            renderAi(currentTurnAiEl, currentTurnFull);
            // 状态保持 thinking:真正的"播放中"由 Omni 朗读的 response.created
            // 驱动(见 wireOmniDataChannel),文字流出来不等于已经在出声。
          } else if(ev.event === "sentence"){
            if(ev.data.text){
              // 垫场话术(filler):念出来的同时也给一个半透明小气泡——用户反馈
              // "听到了但文字里看不到";它不是 Claude 回答正文,不进 currentTurnFull。
              if(ev.data.filler) addMsg("ai filler", ev.data.text);
              omniReadQueue.push(ev.data.text); pumpOmniRead();
            }
          } else if(ev.event === "done"){
            if(!currentTurnAiEl) currentTurnAiEl = addMsg("ai", "");
            if(ev.data.full_text){ currentTurnFull = ev.data.full_text; renderAi(currentTurnAiEl, currentTurnFull); }
            if(ev.data.error){
              markBubbleError(currentTurnAiEl); if(!currentTurnFull) renderAi(currentTurnAiEl, ev.data.error);
              // 通话中用户不看屏幕,出错必须让他听得到,否则就是"发了消息没反应"
              speakOmniNotice("刚才那句处理出错了,再说一遍试试。");
            }
          }
        }
      }
    }catch(e){ /* 流中断,不额外报错 */ }
    if(gen !== omniTurnGen) return;
    currentTurnAiEl = null; currentTurnFull = "";
    omniTurnDone = true;
    vdbg("turn.stream-done", {q: omniReadQueue.length});
    // 不直接回 idle:朗读队列可能还有没念完的,由 maybeOmniIdle 在念完后收尾。
    maybeOmniIdle();
    pumpOmniRead();
  }

  function stopOmniHandsFree(){
    omniTurnGen++;  // 让还在飞的 sendOmniTurn 流式结果失效
    // 先摘 handler 再 close:close 本身会触发 dc.onclose/pc.onconnectionstatechange,
    // 不摘的话主动关闭也会被当成"断线"排一次重连。
    if(omniDc){ try{ omniDc.onclose = null; omniDc.close(); }catch(e){} omniDc = null; }
    if(omniPc){ try{ omniPc.onconnectionstatechange = null; omniPc.ontrack = null; omniPc.close(); }catch(e){} omniPc = null; }
    omniMicStream = null;  // 麦克风轨道本身随 stream 变量一起在 teardownCallResources 里停
    omniUserLiveEl = null;
    omniReadQueue = []; omniReadActive = false; omniReadPending = 0; omniAutoActive = false; omniTurnDone = true;
    omniReadSince = 0;
    omniRecentReads = []; omniReadCurrentText = ""; omniEchoRestore = null;
    if(omniReadWatchdog){ clearInterval(omniReadWatchdog); omniReadWatchdog = null; }
    if(omniAudioEl){ try{ omniAudioEl.srcObject = null; }catch(e){} }
  }
