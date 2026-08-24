"use strict";
// 2026-08-14 从 index.html 拆出(前端模块化):通话视图大 IIFE(免提 Omni WebRTC + 按住说话),对外只挂 window.openCallView 等入口。
// 与内联脚本同属全局作用域(无构建步骤),加载顺序见 index.html。

// ── 通话视图:语音通话(原独立 /voice 页,2026-07-09 并入主 SPA,见 00-overview.md
// §2 隔离约束的修订说明)。整段包一个 IIFE——不是为了模块化好看,是原 voice.html
// 里 ensureStream/analyser 这些名字跟上面聊天逻辑的同名符号会真的撞车,函数作用域
// 一裹就都是局部变量,不用逐个改名。对外只留 openCallView()/closeCallView() 两个
// 挂在 window 上的入口,供侧边栏「语音任务」行和 #voiceChatBtn 调用(见下方接线)。
(function(){
  const transcriptEl=$("#transcript"), statusEl=$("#status"), talkBtn=$("#talkBtn"),
    stopBtn=$("#stopBtn"), tasksBtn=$("#tasksBtn"), tasksBadge=$("#tasksBadge"),
    tasksDrawer=$("#tasksDrawer"), tasksList=$("#tasksList"), taskBar=$("#taskBar"),
    handsFreeUi=$("#handsFreeUi"), orbWrap=$("#orbWrap"), mascotEl=$("#mascotEl"),
    endHandsFreeBtn=$("#endHandsFreeBtn"), muteToggleBtn=$("#muteToggleBtn"),
    backLink=$("#callHead #backLink"), startBtn=$("#startCallBtn"),
    connDotEl=$("#connDot");

  setIcon(backLink, "panel");
  setIcon($("#tasksIcon"), "tasks");
  setIcon($("#stopIcon"), "stop");
  setIcon(endHandsFreeBtn, "phone");  // 挂断按钮:红色圆底+拨打图标转135°(见上面 CSS)
  setIcon(muteToggleBtn, "mic");  // 静音按钮:默认图标是"麦克风开着",点了切成"mute"
  setIcon(startBtn, "phone");  // 开始通话:电话图标
  setIcon($("#voiceIcon"), "voice");  // 音色切换:静音/挂断中间的圆钮,点开原生选择器
  setIcon($("#clearVoiceCtxBtn"), "eraser");  // 顶栏:清空语音通话上下文
  // 左上角只管展开侧栏(浮层叠在通话视图上),不碰通话本身——通话继续跑,
  // 收起侧栏后还在原来的通话画面。真正挂断走下面的"结束"/"停"按钮;
  // 侧栏里切到别的会话才会触发 closeCallView()(见 openConv 收口处)。
  backLink.onclick = e=>{ e.preventDefault(); expandSidebarResponsive(); };

  let stream=null, recorder=null, chunks=[], pressTs=0;
  let busy=false;
  let playQueue=[], playing=false;
  let pendingAnnouncements=[];  // task_done 到达时用户正忙(说话/播放/录音)→ 先攒着,空闲了再播
  let buttonMode=false;  // 按钮模式:true=一轮交互结束后回到按钮待机状态,不自动继续收音

  // ── 通话视图共享状态(免提=Omni WebRTC;按住说话共用同一套播放队列/气泡)────
  let handsFreeActive=false, micMuted=false;
  let wsState="idle";  // 通话状态机(idle/capturing/thinking/speaking):声波球/录音闸/调试上报都看它
  let pausedForInterrupt=false;
  let ttsFailureNoticedThisTurn=false;  // 每轮最多提示一次,避免连续几句都合成失败时刷屏
  let currentTurnAiEl=null, currentTurnFull="";

  // ── Omni-Realtime WebRTC 通话(免提唯一管线,ADR 0004;见 wireOmniDataChannel 一段)──
  // S.omniEnabled(登录时预取,见 loadVoiceOmniConfig)开着就整条走 WebRTC;
  // 关掉则通话视图只剩按住说话(录音→/voice/send)。自建 ws 全双工管线已退休,
  // 两条路径共用同一批 UI 元素(orbWrap/statusEl/handsFreeUi 等)。
  let omniPc=null, omniDc=null, omniMicStream=null, omniUserLiveEl=null;
  // Omni 出声模式的朗读队列:Claude 的回答按句子进队,靠 conversation.item.create +
  // response.create 让 Omni 逐句念出来(同一时刻只能有一个进行中的 response,
  // 靠 omniReadActive 串行化);omniReadPending 记录"我们主动点的 response.create
  // 还有几个没收到 response.created",用来区分自动回复(要 cancel)和我们的朗读。
  let omniReadQueue=[], omniReadActive=false, omniReadPending=0, omniAutoActive=false, omniAutoSince=0, omniTurnDone=true;
  let omniReconnectTimer=null;
  let omniReadTailTimer=null;  // response.done 后等音频缓冲区排空的定时器(见 response.done 分支)
  let omniReadSince=0, omniReadWatchdog=null;  // 朗读心跳时间戳 + 卡死看门狗(见 omniReadStallCheck)
  // 连接状态追踪 + capturing 看门狗(2026-07-15 加):捕捉 capturing 卡死、连接状态指示器、指数退避
  let omniConnStatus="disconnected"; // "connected"|"connecting"|"disconnected"
  let omniReconnectAttempts=0;       // 当前连续重连次数(指数退避用)
  let omniCapturingSince=0;          // 进入 capturing 状态的时间戳,看门狗判断卡住用
  let omniLastDcEvent=0;             // 最近一次收到 DataChannel 事件的时间戳,看门狗判断活性用
  // dc:error 气泡去重:自建 DC 和服务端推来的 DC 都接了 wireOmniDataChannel,
  // 同一个错误会到两份(相隔 ~10ms),不去重就弹两个一模一样的红气泡。
  let omniLastErrMsg="", omniLastErrTs=0;
  let omniHeartbeatTimer=null;       // 连接心跳定时器
  const OMNI_HEARTBEAT_MS = 15000;   // 心跳间隔
  const OMNI_CAPTURING_STALL_MS = 30000;  // capturing 卡死阈值(30 秒无进展)
  // 回声过滤(2026-07-11):iOS AEC 在 AI 每句开口头 1~2 秒未收敛,残留回声会被
  // 阿里云 VAD 当成用户说话,转写出来的是"刚念那句的开头几个字"(见 matchOmniEcho)。
  let omniRecentReads=[];        // 最近念过的句子 [{text, ts}],转写结果跟它们比对
  let omniReadCurrentText="";    // 正在念/刚点播还没念完的那句原文(恢复现场用)
  // 朗读内容校验(2026-07-31 加):此前 Omni【实际念出来的内容】完全没有观测——
  // response.audio_transcript.* 被当成高频噪音丢在 OMNI_QUIET_EVENTS 里直接扔掉,
  // 导致"念出来的不是 Claude 原文"这类故障在日志里零指纹(查下来只能看到一切正常)。
  // 两种成因都会落到这个症状上:①response.created 配对错位,把 Omni 自动回复
  // 当成我们的朗读放行(有 read.create.rejected 之类指纹);②我们塞给 Omni 的是
  // role:"user" 的 input_text,语义上等于"用户说了这句话",模型天然倾向【回应】
  // 而不是【复述】,只靠 instructions 概率性压制——这条路径配对完全正确、所有
  // 防护都通过,却照样吐出自编内容,现有日志一个字都看不见。
  // 【只观测不拦截】:transcript 是 Thinker 层的合成源文本,且 audio_transcript.done
  // 到达时音频早已按 RTP 推完;要在用户听到之前拦,必须先把整句音频缓冲下来校验
  // 完再放,首字延迟不可接受。所以这里只负责把问题变得可测量。
  let omniReadRespId=null;       // 当前朗读 response 的服务端 id(response.created 给)
  let omniReadExpectText="";     // 该 response 期望念出的原文快照(独立于 omniReadCurrentText,
                                 // 后者在 response.done 会被清空,而字幕可能晚于它到达)
  let omniSpokenBuf="";          // 该 response 实际念出的字幕累积(delta 拼)
  let omniSpokenChecked=false;   // 本次朗读是否已校验过,避免 done 事件重复上报
  let omniEchoRestore=null;      // barge-in 现场快照,事后判定是回声就恢复被砍的朗读
  let omniSpeechStartTs=0;       // 用户最近一次开口时刻(speech_started),回声归因用
  let omniEchoRestoreTimer=null; // 幽灵打断兜底:快照存下后转写迟迟不来就自动恢复(见 armOmniGhostTimer)
  // 拆句缓冲(2026-07-12,真机日志实锤 semantic_vad 语义判停会腰斩半句话就发给
  // Claude,silence_duration_ms 管不了这种情况——它只是"静音多久强制切"的兜底,
  // 不是"语义判完整"的开关。这里加一层客户端缓冲:转写完成后不立刻发,等一小段
  // 时间看是否又开口;真又开口就拼到一起,超时才当一轮发出去。
  let omniPendingText=null, omniPendingTimer=null;
  // 2026-07-22 状态错乱事故的修复:flush 时机改由「说话中/转写在途」两个状态决定,
  // 不再让 speech_started/committed 把安全网(现默认 180s,见 S.omniSafetyMs)
  // 覆盖成 1~1.5s 的短定时器——
  // 短定时器会在下一段转写(completed)到达前把前半句发出去,后半句成孤儿,
  // 表现为"消息被吞、要说两遍才有反应、回答的是上一句"。
  let omniSpeechActive=false;   // speech_started→true / speech_stopped→false
  let omniInflightSegs=0;       // committed 过、转写(completed/delta兜底)还没回来的段数
  let omniSpeechStoppedTs=0;    // 最近一次 speech_stopped 的时刻,用于 scheduleOmniFlush 的冷却期判断
  // 用户气泡归并(2026-07-23 截图反馈):semantic_vad 把"今年。"这类语义上像说完
  // 的前缀切成独立段,文字层面前端缓冲/后端 _prev_text 都会把段拼回一句,但气泡
  // 是每段各画一个,界面上看着就是"半句+整句"两个气泡。这里跟踪当前逻辑上还在
  // 续写的那个用户气泡:后续段的转写并进它,不再另起气泡。
  let omniUserBubble=null;      // 当前可续写的用户气泡元素
  let omniUserBubbleText="";    // 该气泡已显示的全文
  let omniUserSentTs=0;         // 该气泡内容最近一次 flush 给后端的时刻(0=还没发)
  // 转写完成事件的 delta 兜底(2026-07-13,真机+aiortc 探针实锤):我们每轮发的
  // response.cancel(砍 Omni 自作主张回复)会把服务端还没 finalize 的
  // input_audio_transcription.completed 一起砍死——delta 照回(前端字幕正常),
  // completed 永远不来,一个字都到不了 Claude。以前 completed 比 response.created
  // 早 ~200ms 落地躲过了 cancel,2026-07-13 阿里云侧时序变了(response.created
  // 几乎和 committed 同时到)就全灭。兜底:delta 全文一直缓存,committed 后
  // 1.5s 内 completed 还没来,就拿 delta 文本顶上走同一条发送链路。
  let omniDeltaFallback=null;        // {itemId, text} 最新一句的 delta 累积文本
  let omniDeltaFallbackTimer=null;
  let omniFallbackSentItem=null;     // 已被兜底发过的 item_id,completed 迟到时防重复
  // OMNI_PENDING_FLUSH_MS:确认「用户没在说话、也没有转写在途」之后,再等这么久
  // 就把缓冲发出去——它只在万事俱备时生效,所以可以很短;等待"下一段会不会来"的
  // 职责已经完全交给 omniSpeechActive/omniInflightSegs 两个状态(见 scheduleOmniFlush)。
  // 安全网:VAD 不报 speech_stopped/转写永远不回来时,缓冲最多压这么久必发,
  // 不至于卡死。阈值走 /voice/config 下发的 safety_ms(默认 180s,2026-08-10 从
  // 30s 调长,30s 对一次长口述不够;切段逻辑保留,只是放宽)。
  const OMNI_PENDING_FLUSH_MS = 600;
  // 语音结束冷却期:speech_stopped 后这段时间内不启用 600ms 短定时器,改用
  // 2000ms 中位定时器,给阿里云 semantic_vad 重新检测继续说话留出时间窗口。
  // 修复要点:连续长语音时 speech_stopped→下一次 speech_started 之间的间隔
  // 可能超过 600ms(阿里云内部处理+网络延时),短定时器会把前半段先发出去。
  const OMNI_PENDING_POST_SPEECH_MS = 2000;
  // 这段话看着还没说完时用的加长冷却(见 scheduleOmniFlush 的 looksIncomplete)。
  // 只对碎片/无句末标点的段生效,不拖慢正常问句的响应。
  const OMNI_PENDING_LONG_COOLDOWN_MS = 3500;
  // 首回复静音(2026-07-12,Wesley 提议):每次 WebRTC 新建连接后,iOS AEC 从零
  // 收敛,AI 第一段音频的回声漏得最凶——干脆在本连接第一次朗读期间把麦克风上行
  // 直接静音(track.enabled=false 只发静音帧,不动音频图,不踩 iOS 音频会话的雷),
  // 物理杜绝回声。代价:这几秒不能打断,用户确认可接受。第一段音频播完 AEC 也
  // 收敛得差不多了,后续轮次交给 matchOmniEcho 文字层兜底。
  let omniFirstReplyMuteUsed=false;  // 本连接的首回复静音是否已消耗
  let omniMicMuted=false;
  let omniMicMuteTimer=null;
  let omniMicEpoch=0;   // 见下注释:旧现场的开麦定时器到点后用它证伪
  const omniAudioEl = $("#omniRemoteAudio");
  // 【重要】输出通道默认静音反转策略(2026-08-10 漏音二次定案):2026-07-15 起
  // 一直奉行"音频输出通道永不静音、自动回复只靠 cancel 砍掉"——但真机日志证明
  // 自动回复【一直带音频】(它的 response.done 回执 modalities="text+audio",
  // session 级 text 物理隔离从未生效,见下),而静音式压制天生慢半拍(RTP 音频
  // 先于 DataChannel 事件到达),每次自动回复都漏开头;created 撞上朗读进行中时
  // 连静音都不触发,整个自动回复原样播出——这就是"偶尔漏几秒"的来源。v2 反转:
  // 元素【默认静音】,只在"确认是我们主动点的朗读在出声"期间开声(朗读 created
  // 开声 → 尾音排空/整轮收尾静音)。自动回复音频集中在"用户说完话后的几秒
  // 窗口"到达,该窗口内元素必然静音,从源头压住;朗读音频不受影响。
  let omniAudioMuted=true;
  // 自动回复音频阻断标志(2026-07-15):每当自动回复的 response.created
  // 到来时置起,阻止自动回复产生的任何音频片段被误放行;我们的 pump 在
  // response.created 确认是朗读后才会清除它,防止自动回复在 gap 期泄漏。
  let omniAutoAudioBlocked=false;
  // 自动回复保险丝定时器(2026-08-08):armOmniAutoFuse 武装后挂的超时定时器,
  // 到点主动解除并泵一次——保险丝只是状态,超时后没人调 pump 它会永远挡着
  // 朗读队列(SSE 句子到齐后不再有任何事件触发 pump,朗读会卡死)。
  let omniAutoFuseTimer=null;
  // 【物理隔离实测未生效】(2026-08-10 真机日志定案):session 级 modalities 只给
  // ["text"],指望 Omni 对用户话语自动生成的回复"物理上不产生音频"——但日志里
  // 自动回复的 response.done 回执 modalities="text+audio"(与我们的朗读相同),
  // 说明服务端对自动回复固定带音频,session 级 text 对自动回复【无效】,07-31
  // 的"物理隔离"从未真正生效,漏音防线一直是"created 到达时静音"这个迟到压制
  // (RTP 先到必漏开头,2026-07-31 真机日志"6 次开口 6 次 muted 还是漏"就是证据)。
  // 08-08 的"翻闸"更是把这个迟到的防线也拆了,漏音升级成完整复读。v2 不赌
  // 服务端行为:输出通道默认静音(见上注释),自动回复音频到达时元素必然是静的,
  // 朗读靠响应级 modalities 覆盖出声(朗读 created 回执恒 "text"、done 回执
  // "text+audio",2026-08-10 日志确认响应级覆盖服务端认)。朗读哑掉的兜底:
  // done 回执缺 audio 时上报 read.silent,不翻转 session(翻转 = 自动回复出声)。
  let omniSessionModalities=["text"];
  omniAudioEl.muted = true;
  // 调试信号上报:浏览器↔阿里云的 DataChannel 服务端完全看不见,2026-07-10 两次
  // 修复都因为拿不到真机证据只能盲改后 revert。关键事件(session/response 生命周期、
  // 连接状态、打断判定)全部悄悄 POST 回服务器日志,fire-and-forget 不影响主流程。
  function vdbg(tag, extra){
    try{
      const payload = {t: new Date().toISOString().slice(11,23), tag, state: wsState};
      if(extra !== undefined) payload.x = extra;
      fetch("/voice/debug", {
        method: "POST", keepalive: true,
        headers: {"Content-Type": "application/json", "X-Auth-Token": S.token},
        body: JSON.stringify(payload),
      }).catch(()=>{});
    }catch(e){}
  }
  // 全局兜底:通话链路里任何未捕获异常/未处理 rejection 都上报——2026-07-12 真机
  // 一次"卡在连接中"查因困难(静默死亡无痕迹),从此让它们留痕。
  window.addEventListener("error", e=>{
    vdbg("js.error", String(e.message).slice(0,180) + " @" + String(e.filename).split("/").pop() + ":" + e.lineno);
  });
  window.addEventListener("unhandledrejection", e=>{
    // Safari 的 Error.stack 不含 message,两个都要带上(2026-07-12 只记 stack 差点断案失败)
    const msg = String(e.reason).slice(0,140);
    const stk = String((e.reason && e.reason.stack) || "").slice(0,180);
    vdbg("js.unhandledrejection", msg + " || " + stk);
  });

  // ── 播放:Web Audio API,不用 <audio> 标签 ──────────────────────────────
  let audioCtx=null, currentSource=null;

  // 2026-07-09:曾经加过 WebRTC 环回 hack 想让浏览器原生 AEC 认出 TTS 音频当参考信号,
  // 一天内连续炸出"哑巴→双音重叠→再哑巴"三个不同故障,iOS Safari 音频会话的怪癖猜不动,
  // 拆掉换回最简单可靠的单路直连播放。回声消除这个需求单独另开工作评估。

  function setStatus(text, cls){ statusEl.textContent = text; statusEl.className = cls || ""; }
  function clearHint(){ const h = transcriptEl.querySelector(".hint"); if(h) h.remove(); }

  function addMsg(kind, text){
    clearHint();
    const parts = kind.split(" ");
    const who = parts[0];
    const isError = parts.includes("error");
    const isFiller = parts.includes("filler");
    const isActivity = parts.includes("activity");
    const row = document.createElement("div");
    row.className = "row " + (who === "me" ? "me" : "ai");
    const b = document.createElement("div");
    b.className = "bubble" + (isError ? " err" : "") + (isFiller ? " filler" : "") + (isActivity ? " activity" : "");
    if(isError){
      const icon = document.createElement("span");
      icon.className = "msgicon";
      icon.innerHTML = ic("warn");
      b.append(icon);
    }
    const span = document.createElement("span");
    span.className = "msgtext";
    span.textContent = text;
    b.append(span);
    row.append(b);
    transcriptEl.appendChild(row);
    row.scrollIntoView({block:"end"});
    return b;
  }
  function markBubbleError(b){
    b.classList.add("err");
    if(!b.querySelector(".msgicon")){
      const icon = document.createElement("span");
      icon.className = "msgicon";
      icon.innerHTML = ic("warn");
      b.prepend(icon);
    }
  }

  function unlockAudio(){
    // closed 的 ctx 是死的:resume 恒 reject,createOscillator 静默无声。
    // 原来这里只看 audioCtx 是否为 null,一旦留下个 closed 的实例就永久哑巴。
    if(audioCtx && audioCtx.state !== "closed") return;
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const buf = audioCtx.createBuffer(1, 1, 22050);
    const src = audioCtx.createBufferSource();
    src.buffer = buf;
    src.connect(audioCtx.destination);
    src.start(0);
    wireAudioContextInterruptionHandler();
  }

  // ── AudioContext 恢复闸(2026-08-24)────────────────────────────────────
  // 所有提示音(连接成功/思考中/回复中/发送/断线)都从 audioCtx 出声,ctx 一挂起
  // 就集体消失。iOS 会在两种时机挂起它:①WebRTC 建连/重连时 getUserMedia 重新
  // 协商音频会话;②息屏、来电等系统打断。挂起后有两种可能:suspended(resume
  // 能救回来)和 interrupted(WebKit 私有态,resume 会一直 reject,得整个重建)。
  // 这个闸把"恢复"收成一个入口:并发调用共用同一次 resume;连续救不回来就重建。
  let _ctxResuming = null;       // 进行中的 resume promise,防止并发重复发起
  let _ctxResumeFails = 0;       // 连续恢复失败次数,到阈值就重建
  let _ctxRebuiltAt = 0;         // 上次重建时刻,限流防抖(重建本身会打断正在播的音频)
  const CTX_REBUILD_COOLDOWN_MS = 10000;
  function ensureAudioCtxRunning(why){
    if(!audioCtx) return Promise.resolve(false);
    if(audioCtx.state === "running"){ _ctxResumeFails = 0; return Promise.resolve(true); }
    if(_ctxResuming) return _ctxResuming;
    if(audioCtx.state === "closed"){
      const revived = rebuildAudioCtx("closed:" + why);
      return Promise.resolve(revived);
    }
    _ctxResuming = Promise.resolve()
      .then(() => audioCtx.resume())
      .then(() => {
        _ctxResuming = null;
        const ok = !!audioCtx && audioCtx.state === "running";
        if(ok){ _ctxResumeFails = 0; vdbg("ctx.resumed", why); }
        else _ctxResumeFails++;
        return ok;
      })
      .catch(err => {
        _ctxResuming = null;
        _ctxResumeFails++;
        vdbg("ctx.resume.fail", {why, fails: _ctxResumeFails, err: String(err).slice(0, 80)});
        // 连续两次拉不回来 = 多半卡在 interrupted,resume 再试一万次也一样,重建
        if(_ctxResumeFails >= 2) return rebuildAudioCtx("stuck:" + why);
        return false;
      });
    return _ctxResuming;
  }

  // 重建 AudioContext:老实例 close 掉换一个新的。挂在老 ctx 上的节点(麦克风
  // analyser、输出 analyser/tap、工作音效的 gain)会随之全部作废,必须一并重置
  // 再按需重建,否则后面 micStreamHealthy/尾音排空全在读一个死节点。
  function rebuildAudioCtx(why){
    const now = Date.now();
    if(now - _ctxRebuiltAt < CTX_REBUILD_COOLDOWN_MS){ vdbg("ctx.rebuild.skip", why); return false; }
    _ctxRebuiltAt = now;
    vdbg("ctx.rebuild", {why, prev: audioCtx && audioCtx.state});
    stopWorkSound();  // gain 挂在老 ctx 上,先收掉,别留个野定时器
    const old = audioCtx;
    audioCtx = null;
    // 先摘 handler 再 close:close 本身会派发 statechange,不摘就是拿着一个正在
    // 被替换的全局变量回调自己。
    if(old){ try{ old.onstatechange = null; }catch(e){} try{ old.close(); }catch(e){} }
    if(analyserSource){ try{ analyserSource.disconnect(); }catch(e){} }
    if(omniOutTap){ try{ omniOutTap.disconnect(); }catch(e){} }
    analyser = null; analyserSource = null; outputAnalyser = null; omniOutTap = null;
    try{ unlockAudio(); }catch(e){ vdbg("ctx.rebuild.fail", String(e)); return false; }
    _ctxResumeFails = 0;
    // 新建的 ctx 在自动播放策略下可能落在 suspended,推一把;这次没赶上的音效
    // 由下一次调用补(思考音每 1.2s 一响,听感上察觉不到)。
    if(audioCtx.state !== "running"){ try{ audioCtx.resume().catch(()=>{}); }catch(e){} }
    ensureAnalyser();  // 麦克风电平(僵尸流判定/吉祥物呼吸)靠它,新 ctx 上重挂
    // 远端输出 tap 也重挂:尾音排空监听靠它感知 AI 是否还在出声
    if(omniAudioEl && omniAudioEl.srcObject){
      ensureOutputAnalyser();
      if(outputAnalyser){
        try{
          omniOutTap = audioCtx.createMediaStreamSource(omniAudioEl.srcObject);
          omniOutTap.connect(outputAnalyser);
        }catch(e){ omniOutTap = null; vdbg("ctx.rebuild.tap-fail", String(e)); }
      }
    }
    return !!audioCtx && audioCtx.state === "running";
  }

  // 设备切换检测:track.onended/onmute + devicechange + visibilitychange 都往
  // 这上面累积计数,连续超过门槛才触发重连——避免短暂中断(切回前台时的过渡期)
  // 导致不必要的 WebRTC 重建。2026-07-14 真机:iOS 电话打断/蓝牙断开会令
  // getUserMedia 轨道 mute 或 ended,但没有 devicechange 监听和健康检查时,
  // ensureMicStream 会永远返回老化的流,用户无声无息变哑。
  let micHealthSuspectCount = 0;
  let micHealthSuspectTs = 0;
  const MIC_HEALTH_WINDOW = 3500;
  const MIC_HEALTH_THRESHOLD = 2;
  let _resettingMic = 0;  // resetMicStream 重入守卫:阻止 onended→suspect→reconnect 自循环
  let _micSilentCount = 0;
  let _micSilentTs = 0;
  let _ctxStuckTicks = 0;  // AudioContext 连续被判"非 running"的看门狗次数(见 omniReadStallCheck)

  function micStreamHealthy(){
    if(!stream) return false;
    const tracks = stream.getAudioTracks();
    if(!tracks.length) return false;
    if(!tracks.some(t => t.readyState === "live")) return false;
    // 2026-08-11:首回复静音期间轨道 enabled=false,analyser 电平恒静音是预期,
    // 不是僵尸流——跳过电平判定,否则静音 ≥15s 会被判 mic.zombie 强制换流重连。
    if(omniMicMuted || micMuted) return true;
    // 音频活动检测:analyser 就绪时读取电平,排除 readyState=live 但不出声的僵尸流
    if(analyser && audioCtx && audioCtx.state === "running"){
      const buf = new Uint8Array(analyser.frequencyBinCount);
      analyser.getByteTimeDomainData(buf);
      const diff = Math.max(...buf) - Math.min(...buf);
      if(diff < 2){
        const now = Date.now();
        if(now - _micSilentTs > 4000){ _micSilentCount = 0; _micSilentTs = now; }
        _micSilentCount++;
        if(_micSilentCount >= 5){
          _micSilentCount = 0;
          vdbg("mic.zombie", {diff});
          return false;
        }
      } else {
        _micSilentCount = 0;
      }
    }
    return true;
  }

  function suspectMicProblem(reason){
    if(!handsFreeActive) return;
    const now = Date.now();
    if(now - micHealthSuspectTs > MIC_HEALTH_WINDOW){
      micHealthSuspectCount = 0;
      micHealthSuspectTs = now;
    }
    micHealthSuspectCount++;
    vdbg("mic.suspect", {reason, count: micHealthSuspectCount,
      ready: stream?.getAudioTracks().map(t=>t.readyState).join(",")});
    if(micHealthSuspectCount >= MIC_HEALTH_THRESHOLD){
      micHealthSuspectCount = 0;
      if(!micStreamHealthy()) resetMicStream();
      // _resettingMic>0 说明我们正在 resetMicStream 的 onended 回调里——
      // 重连已经在 startOmniHandsFree/scheduleOmniReconnect 的路上,不额外调度。
      if(_resettingMic > 0) return;
      scheduleOmniReconnect("mic:"+reason, 500);
    }
  }


  // 麦克风流音频活动检测:读取 analyser 电平,在 watchdog 中周期性调用。
  // diff<2 说明接近纯静音——可能不是用户没说话,而是流已僵尸(iOS 中断后)。
  function checkMicAudioActivity(){
    if(omniMicMuted || micMuted) return;  // 主动静音时电平恒静,跳过僵尸判定(2026-08-11)
    if(!analyser || !audioCtx) return;
    if(audioCtx.state !== "running") return;  // 被挂起时 analyser 不产生数据
    const buf = new Uint8Array(analyser.frequencyBinCount);
    analyser.getByteTimeDomainData(buf);
    const diff = Math.max(...buf) - Math.min(...buf);
    if(diff < 2){
      const now = Date.now();
      if(now - _micSilentTs > 4000){ _micSilentCount = 0; _micSilentTs = now; }
      _micSilentCount++;
      if(_micSilentCount >= 5){
        _micSilentCount = 0;
        vdbg("mic.zombie", {diff});
        suspectMicProblem("zombie");
      }
    } else {
      _micSilentCount = 0;
    }
  }

  // AudioContext 中断恢复监听:iOS 在电话/闹钟/控制中心音频打断时会
  // 把 AudioContext 从 running→suspended,恢复时→running。此时麦克风流
  // 可能已变僵尸(readyState=live 但不出声)。只监不治——状态变化触发健康
  // 检查,真有问题由 suspectMicProblem 发起重连。
  function wireAudioContextInterruptionHandler(){
    if(!audioCtx) return;
    try{ audioCtx.onstatechange = null; }catch(e){}
    audioCtx.onstatechange = () => {
      // 重建过程中老实例 close 也会触发本回调,而那一刻 audioCtx 已被置空/换新,
      // 直接读 .state 会抛 TypeError(2026-08-24)。
      if(!audioCtx) return;
      vdbg("audioctx.state", audioCtx.state);
      if(audioCtx.state === "running" && handsFreeActive){
        setTimeout(() => {
          if(!micStreamHealthy()) suspectMicProblem("audioctx-resume");
        }, 300);
      } else if(audioCtx.state !== "running" && handsFreeActive){
        // 通话期间被系统挂起:立刻尝试拉回来。挂着不管的后果不只是没音效——
        // micStreamHealthy/checkMicAudioActivity 的僵尸流判定都以 state==="running"
        // 为前提,ctx 一直挂着等于把麦克风自检整个关掉(2026-08-24 根因之一)。
        ensureAudioCtxRunning("statechange");
      }
    };
  }

  // ── 息屏/切后台回来的音频链路复活(2026-08-24)───────────────────────────
  // 历史上三次"息屏断连"修复(wakeLock / 保活音轨 / 防熄屏视频)全部是【预防】,
  // 没有一层负责【息屏之后怎么恢复】——而息屏总有防不住的时候(用户主动按电源键、
  // 来电打断、iOS 18.4 以下的 PWA wakeLock bug)。这里补上恢复:
  //   ① AudioContext 被 iOS 挂起后必须显式 resume,否则麦克风自检永久瘫痪;
  //   ② 保活音轨/防熄屏视频被系统暂停后要重新起播(以前只重启了视频);
  //   ③ 远端 <audio> 被音频中断暂停后要重新 play(),否则 AI 出声全程听不见;
  //   ④ 收尾做一次【真·探活】:ctx 拉不回来 / 麦克风轨道死了 / 电平恒静音,
  //      就直接强制重连,而不是像以前那样界面写着"聆听中"实则全链路失聪。
  let resumeProbeTimer = null;
  function resumeAudioPipeline(why){
    vdbg("resume.begin", {why, ctx: audioCtx && audioCtx.state,
      ka: !!(keepAliveEl && !keepAliveEl.paused), kv: !!(keepAliveVideo && !keepAliveVideo.paused)});
    ensureAudioCtxRunning("pipeline:" + why);
    startKeepAliveAudio();
    startKeepAliveVideo();
    if(omniAudioEl && omniAudioEl.srcObject && omniAudioEl.paused){
      try{ omniAudioEl.play().catch(err=>vdbg("resume.remote.fail", String(err))); }catch(e){}
    }
    if(resumeProbeTimer) clearTimeout(resumeProbeTimer);
    resumeProbeTimer = setTimeout(()=>{ resumeProbeTimer = null; probeAfterResume(why); }, 1200);
  }

  // 复活后的真·探活:只在通话仍激活时跑,判死就强制重连(force=true 跳过
  // "pc 看着还 connected 就不重建"那道闸——息屏杀掉的是麦克风和音频会话,
  // WebRTC 连接状态在这种故障里恰恰是【一直显示 connected】的,不能信它)。
  function probeAfterResume(why){
    if(!handsFreeActive) return;
    if(audioCtx && audioCtx.state !== "running"){
      vdbg("resume.probe.ctx-stuck", audioCtx.state);
      scheduleOmniReconnect("resume:ctx-stuck", 300, true);
      return;
    }
    const tracks = stream ? stream.getAudioTracks() : [];
    if(!tracks.length || !tracks.some(t => t.readyState === "live")){
      vdbg("resume.probe.track-dead", tracks.map(t=>t.readyState).join(","));
      scheduleOmniReconnect("resume:track-dead", 300, true);
      return;
    }
    // 主动静音期间电平本来就恒静,没法判,交给常规看门狗
    if(omniMicMuted || micMuted || !analyser){ vdbg("resume.probe.skip"); return; }
    // 连采 6 次(约 1.2s)全是纯静音 = 僵尸流:真实麦克风即使安静房间也有底噪抖动。
    let samples = 0, flat = 0;
    const buf = new Uint8Array(analyser.frequencyBinCount);
    const tick = setInterval(()=>{
      if(!handsFreeActive || !analyser || omniMicMuted || micMuted){ clearInterval(tick); return; }
      analyser.getByteTimeDomainData(buf);
      if(Math.max(...buf) - Math.min(...buf) < 2) flat++;
      if(++samples < 6) return;
      clearInterval(tick);
      if(flat === samples){
        vdbg("resume.probe.mic-zombie", {why});
        resetMicStream();
        scheduleOmniReconnect("resume:mic-zombie", 300, true);
      } else {
        vdbg("resume.probe.ok", {flat, samples});
      }
    }, 200);
  }

  // 给麦克风流的每条音轨挂监听:iOS 电话打断/蓝牙断开时 track 会 mute/ended
  function wireMicTrackListeners(ms){
    if(!ms) return;
    ms.getAudioTracks().forEach(t => {
      try{ t.onended = null; }catch(e){}
      try{ t.onmute = null; }catch(e){}
      t.onended = () => suspectMicProblem("ended");
      t.onmute = () => {
        // 2026-08-11:首回复静音(omniMicMuted)/手动静音(micMuted)是主动行为——
        // 轨道 enabled=false 时 iOS WebKit 会派发 mute 事件,不是故障。跳过嫌疑
        // 计数,否则 3.5s 窗口 ≥2 次就触发强制重连、通话被拆(真机日志 12:05:27
        // 静音 → 12:05:38/39 mic.suspect×2 → reconnect.scheduled,每通只剩第一轮)。
        if(omniMicMuted || micMuted) return;
        suspectMicProblem("muted");
      };
    });
  }

  async function ensureMicStream(){
    // 流存在但不健康(僵尸)时强制换新——iOS 中断后流可 report live 但不出数据
    if(stream && !micStreamHealthy()){
      vdbg("mic.ensure-refresh", "existing stream unhealthy");
      resetMicStream();
    }
    if(!stream) {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {echoCancellation:true, noiseSuppression:true, autoGainControl:true}
      });
      wireMicTrackListeners(stream);
    }
    return stream;
  }

  // 音频设备变化监听:iOS 蓝牙断开/耳机插拔触发 devicechange,此时现有流可能已废
  if(navigator.mediaDevices && typeof navigator.mediaDevices.addEventListener === "function"){
    navigator.mediaDevices.addEventListener("devicechange", () => {
      if(handsFreeActive) suspectMicProblem("devicechange");
    });
  }

  // iOS 中断恢复检测:页面由后台切回前台时检查麦克风健康。
  // iOS 在电话/闹钟/控制中心音频打断结束后可能留下僵尸流。
  document.addEventListener("visibilitychange", () => {
    if(!document.hidden && handsFreeActive){
      setTimeout(() => {
        if(!micStreamHealthy()) suspectMicProblem("visibility");
      }, 300);
    }
  });

  // iOS 中断恢复检测:页面由后台切回前台时检查麦克风健康。
  // iOS 在电话/闹钟/控制中心音频打断结束后可能留下僵尸流。
  // 音轨再 addTrack 进新 PC,音频能过阿里云 VAD(speech_started 正常),但输入转写
  // (input_audio_transcription.completed)再也不回事件——没有转写就永远不会发给
  // Claude,表现为"AI 不回复"(2026-07-12 真机日志实锤:新开通话必好、重连/切音色
  // 复用旧流必哑)。2026-07-13 复现:两次 handsfree.start 靠得很近但没走重连/切
  // 音色那两条显式调用路径,同样哑掉——干脆挪进 startOmniHandsFree 自己开头无条件
  // 调用,不用再看是谁触发的重开。这里停掉旧流让 ensureMicStream 重新要一条;
  // 声纹 analyser 挂在旧流上,一并拆掉,startOmniHandsFree 里的 ensureAnalyser()
  // 会用新流重建。
  function resetMicStream(){
    _resettingMic++;
    try{
      if(stream){
        // 【关键】先摘 handler 再停轨:避免 onended→suspectMicProblem→scheduleOmniReconnect
        // 自循环(停轨触发 onended 时仍在 startOmniHandsFree 的重连路径中,新 reconnect
        // 会拆掉刚建好的连线,永续循环)。
        stream.getAudioTracks().forEach(t => {
          try{ t.onended = null; }catch(e){}
          try{ t.onmute = null; }catch(e){}
        });
        stream.getTracks().forEach(t => t.stop());
        stream = null;
      }
      if(analyserSource){ try{ analyserSource.disconnect(); }catch(e){} analyserSource = null; }
      analyser = null;
    }finally{
      _resettingMic--;
    }
  }

  async function startRecording(){
    if(busy) return;
    unlockAudio();
    try{ await ensureMicStream(); }
    catch(e){ addMsg("ai error", "麦克风权限被拒绝,请到系统设置里允许"); return; }
    chunks = [];
    recorder = new MediaRecorder(stream);
    recorder.ondataavailable = e=>{ if(e.data && e.data.size>0) chunks.push(e.data); };
    pressTs = Date.now();
    recorder.start();
    talkBtn.classList.add("recording");
    setStatus("聆听中…", "listening");
  }

  function stopRecording(){
    if(!recorder || recorder.state === "inactive") return;
    talkBtn.classList.remove("recording");
    const dur = Date.now() - pressTs;
    const rec = recorder;
    recorder = null;
    rec.onstop = ()=>{
      if(dur < 500){ setStatus("空闲"); flushAnnouncements(); return; }
      const blob = new Blob(chunks, {type: rec.mimeType || "audio/webm"});
      handleTurn(blob);
    };
    rec.stop();
  }

  talkBtn.addEventListener("pointerdown", e=>{ e.preventDefault(); startRecording(); });
  talkBtn.addEventListener("pointerup", stopRecording);
  talkBtn.addEventListener("pointercancel", stopRecording);
  talkBtn.addEventListener("pointerleave", e=>{ if(recorder && recorder.state==="recording") stopRecording(); });

  async function handleTurn(blob){
    const fd = new FormData();
    fd.append("audio", blob, "voice.webm");
    await sendTurn(fd);
  }

  function makeVoiceTurnId(){
    if(window.crypto?.randomUUID) return window.crypto.randomUUID();
    const hex = "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx";
    return hex.replace(/[xy]/g, char=>{
      const value = Math.floor(Math.random() * 16);
      return (char === "x" ? value : (value & 3) | 8).toString(16);
    });
  }

  let activeVoiceTurn = null;
  function isActiveVoiceTurn(turn){ return activeVoiceTurn === turn; }
  function finishVoiceTurn(turn){ if(isActiveVoiceTurn(turn)) activeVoiceTurn = null; }
  function cancelActiveVoiceTurn(reason){
    const turn = activeVoiceTurn;
    if(!turn) return;
    activeVoiceTurn = null;
    fetch("/voice/stop", {
      method:"POST",
      headers:{"Content-Type":"application/json", "X-Auth-Token":S.token},
      body:JSON.stringify({turn_id:turn.id, reason}),
    }).catch(()=>{});
    turn.controller.abort();
  }
  function beginVoiceTurn(){
    cancelActiveVoiceTurn("superseded");
    const turn = {id:makeVoiceTurnId(), controller:new AbortController()};
    activeVoiceTurn = turn;
    return turn;
  }

  async function sendTurn(body){
    const turn = beginVoiceTurn();
    busy = true; talkBtn.disabled = true; stopBtn.hidden = false;
    ttsFailureNoticedThisTurn = false;
    setStatus("识别中…", "thinking");
    const headers = {"X-Auth-Token": S.token, "X-Voice-Turn-Id": turn.id};
    const init = {method: "POST", headers, signal:turn.controller.signal};
    if(body instanceof FormData) init.body = body;
    else { headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }

    const finish = ()=>{
      if(activeVoiceTurn && !isActiveVoiceTurn(turn)) return;
      finishVoiceTurn(turn);
      busy = false; talkBtn.disabled = false; stopBtn.hidden = true; setStatus("空闲");
      endActivity();  // 回合结束:动作行保留在气泡流里,只断开指针,下一轮另起一条
      flushAnnouncements();
    };

    let resp;
    try{ resp = await fetch("/voice/send", init); }
    catch(e){ if(isActiveVoiceTurn(turn)) addMsg("ai error", "连不上服务"); finish(); return; }
    if(!isActiveVoiceTurn(turn)) return;
    if(resp.status === 401){ authError(); finish(); return; }
    if(!resp.ok || !resp.body){
      addMsg("ai error", resp.status === 409 ? "上一轮还没说完" : "出错了");
      finish(); return;
    }

    let aiEl = null, full = "";
    const reader = resp.body.getReader(), dec = new TextDecoder();
    let buf = "";
    try{
      while(true){
        const {value, done} = await reader.read();
        if(!isActiveVoiceTurn(turn)){ try{ reader.cancel(); }catch(e){} return; }
        if(done) break;
        buf += dec.decode(value, {stream:true});
        let sep;
        while((sep = buf.indexOf("\n\n")) >= 0){
          const block = buf.slice(0, sep); buf = buf.slice(sep+2);
          const ev = parseSseBlock(block);
          if(!ev) continue;
          if(ev.event === "transcript"){
            addMsg("me", ev.data.text || "");
            setStatus("思考中…", "thinking");
          } else if(ev.event === "text"){
            stopWorkSound();  // AI 开口了,工作音效退场
            if(!aiEl) aiEl = addMsg("ai", "");
            full += ev.data.text || "";
            renderAi(aiEl, full);
          } else if(ev.event === "activity"){
            showActivity(ev.data.text);
          } else if(ev.event === "sentence"){
            playQueue.push(ev.data);
            pumpPlayback();
          } else if(ev.event === "done"){
            if(!aiEl) aiEl = addMsg("ai", "");
            if(ev.data.full_text){ full = ev.data.full_text; renderAi(aiEl, full); }
            if(ev.data.error){ markBubbleError(aiEl); if(!full) renderAi(aiEl, ev.data.error); }
          }
        }
      }
    }catch(e){ /* 流中断:下面统一收尾,不额外报错打扰用户 */ }
    finish();
  }

  function parseSseBlock(block){
    let event = "message", data = null;
    for(const line of block.split("\n")){
      if(line.startsWith("event:")) event = line.slice(6).trim();
      else if(line.startsWith("data:")) data = line.slice(5).trim();
    }
    if(data == null) return null;
    try{ return {event, data: JSON.parse(data)}; }catch(e){ return null; }
  }

  function renderAi(elm, full){
    elm.querySelector(".msgtext").textContent = full;
    elm.scrollIntoView({block:"end"});
  }

  // ── 前台动作行:AI 本轮查记录/跑脚本时,转写区一条小字实时显示在干什么
  //    (SSE activity 事件驱动)。动作变了原地更新并挪到气泡流末尾;
  //    回合结束只断开指针不删 DOM——留下最后一条当"这轮 AI 干了什么"的痕迹。
  let activityLineEl = null;

  function showActivity(text){
    if(!text) return;
    startWorkSound();  // AI 在干活(还没开口)→ 起工作音效,首个 text/回合结束时停
    if(!activityLineEl){ activityLineEl = addMsg("ai activity", text); return; }
    activityLineEl.querySelector(".msgtext").textContent = text;
    const row = activityLineEl.parentElement;
    transcriptEl.appendChild(row);
    row.scrollIntoView({block:"end"});
  }

  function endActivity(){ activityLineEl = null; stopWorkSound(); }

  // ── 工作音效:AI 调工具期间循环播一段很轻的"双音滴答"(Web Audio 合成,无素材)。
  //    音量刻意压低且是纯音不是语音——本地 Web Audio 出声在 RTC 回环之外,
  //    麦克风可能录到(2026-07-10 自回声教训),纯音短滴答不会被 ASR 转出文字,
  //    VAD 阈值 0.7 也不至于被这个音量触发;真机若误触发,直接把这两个函数体清空即可。
  //    0.15 太响(e212137 调上去过):它是唯一会和 AI 语音贴身共存的音效,
  //    盖住语音尾字还可能漏进麦克风,回落到 0.08。
  let workSound = null;

  function startWorkSound(){
    if(workSound || !audioCtx) return;
    vdbg("worksound.start", audioCtx.state);  // 排障:state 不是 running 就是 iOS 把 ctx 挂起了
    const master = audioCtx.createGain();
    master.gain.value = 0.08;
    master.connect(audioCtx.destination);
    const blip = ()=>{
      if(!audioCtx) return;
      // AI 正在出声/即将出声(Omni 朗读 response 在飞或已排队、自动回复在放、
      // 旧链路 TTS 在播)时这一响跳过,声音一停自动恢复。不能看 wsState:它念完
      // 垫场话后在整轮结束前一直挂在 speaking,会把干活期间的滴答全挡掉
      // (2026-07-12 真机:查任务进度时听不到音效,就是这个原因)。
      // omniReadTailTimer/队列非空也要压:response.done 后标志已清零但 WebRTC
      // 缓冲还有 ~400ms 尾音在播,这个窗口里响一声正好砸在 AI 的最后几个字上
      // (2026-07-13 真机:执行任务时音效吞尾字,就是这个窗口)。
      if(omniReadActive || omniReadPending > 0 || omniAutoActive || playing
         || omniReadTailTimer || omniReadQueue.length > 0) return;
      // iOS 在 getUserMedia/通话音频会话切换时会把 AudioContext 挂起(state 变
      // suspended/interrupted),挂起期间 currentTime 不走,此刻排的音全落在过去
      // =无声——先 resume,这一轮跳过,下一轮 interval 再响。
      if(audioCtx.state !== "running"){ ensureAudioCtxRunning("worksound"); return; }
      const t0 = audioCtx.currentTime;
      [1318.5, 1760].forEach((freq, i)=>{  // E6→A6 上行小滴答,间隔 180ms
        const osc = audioCtx.createOscillator();
        const env = audioCtx.createGain();
        osc.type = "sine"; osc.frequency.value = freq;
        env.gain.setValueAtTime(0, t0 + i*0.18);
        env.gain.linearRampToValueAtTime(1, t0 + i*0.18 + 0.02);
        env.gain.exponentialRampToValueAtTime(0.001, t0 + i*0.18 + 0.22);
        osc.connect(env); env.connect(master);
        osc.start(t0 + i*0.18); osc.stop(t0 + i*0.18 + 0.25);
      });
    };
    blip();
    workSound = {timer: setInterval(blip, 1600), master};
  }

  function stopWorkSound(){
    if(!workSound) return;
    vdbg("worksound.stop");
    clearInterval(workSound.timer);
    try{ workSound.master.disconnect(); }catch(e){}
    workSound = null;
  }

  function base64ToArrayBuffer(b64){
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for(let i=0;i<bin.length;i++) bytes[i] = bin.charCodeAt(i);
    return bytes.buffer;
  }

  function noticeTtsFailure(){
    // audio_b64 为空 = 这句话确实要读但合成失败(见 voice/tts.py),不是"本来就没有音频"——
    // 静默跳过会让用户以为卡住了,提示一次让用户知道去看文字。
    // 2026-07-09 补:免提通话时用户根本不看屏幕,光加文字提示等于没提示——
    // 真实事故复盘过一次(intertrade-bot 任务派发确认那句被 TTS 429 限流吞掉,
    // 用户体感像 AI 中途掉线),这里加一声可听见的提示音兜底。
    playTtsFailTone();
    if(ttsFailureNoticedThisTurn) return;
    ttsFailureNoticedThisTurn = true;
    addMsg("ai", "⚠ 有一句没能读出来,内容已经显示在文字里了");
  }

  async function pumpPlayback(){
    if(playing || playQueue.length === 0 || pausedForInterrupt) return;
    const item = playQueue.shift();
    if(!item.audio_b64){ noticeTtsFailure(); pumpPlayback(); return; }
    playing = true;
    setStatus("工作中…", "playing");
    try{
      // 走统一恢复闸:只认 suspended 会漏掉 iOS 的 interrupted 私有态,而
      // 裸 resume 一旦 reject 就被外层 catch 吞成"跳过这句",整段回复无声播完。
      await ensureAudioCtxRunning("playback");
      const audioBuffer = await audioCtx.decodeAudioData(base64ToArrayBuffer(item.audio_b64));
      await new Promise((resolve, reject)=>{
        const source = audioCtx.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(audioCtx.destination);
        if(outputAnalyser) source.connect(outputAnalyser);
        currentSource = source;
        source.onended = ()=>{ resolve(); };
        try{ source.start(0); }catch(e){ reject(e); }
      });
    }catch(e){ /* 解码/播放失败就跳过这句,不卡住队列 */ }
    currentSource = null;
    playing = false;
    pumpPlayback();
  }

  stopBtn.addEventListener("click", ()=>{
    playQueue = [];
    if(currentSource){ try{ currentSource.onended = null; currentSource.stop(); }catch(e){} currentSource = null; }
    playing = false;
    cancelActiveVoiceTurn("manual");
  });

  function authError(){
    addMsg("ai error", "口令失效,请回主界面重新登录");
    showLogin();
  }

  // ── P1 任务板:抽屉列表 + 完成播报(WHEN_IDLE:等空闲/不在录音才插播,F8/F10) ──────
  const STATUS_WORD = {queued:"排队中", pending:"排队中", running:"进行中", in_progress:"进行中", paused:"已暂停", completed:"已完成", done:"已完成", failed:"失败", cancelled:"已取消", deleted:"已删除"};

  function taskSessionKey(){
    return S.conv === "voice-chat:main" ? "main" : (S.conv || "main");
  }

  async function loadTasks(){
    try{
      const sessionKey = taskSessionKey();
      // 语音和文本任务按同一个会话查询,输入方式不再决定任务归属。
      const url = `/tasks?session_key=${encodeURIComponent(sessionKey)}`;
      const main = await fetch(url, {headers:{"X-Auth-Token":S.token}});
      if(!main.ok) return;
      const rows = await main.json();
      // 切会话时上一个请求可能后到;不能让旧会话任务覆盖当前输入框状态条。
      if(sessionKey !== taskSessionKey()) return;
      renderTasks(rows);
      // 顺带全量校准状态条;接口只补漏,不覆盖 SSE 已经收到的更新。
      for(const t of rows){
        // 同 isCurrentConversationTask:活跃(重新激活)任务不受清空时间戳过滤
        if(t.created_at && t.origin === "voice" && t.created_at < voiceTasksClearedAt
           && !TASK_ACTIVE_STATUSES.includes(t.status)) continue;
        const prev = barTasks.get(t.id);
        if(prev && (prev.updated_at ?? prev.created_at) > (t.updated_at ?? t.created_at)) continue;
        barTasks.set(t.id, t);
      }
      renderTaskBar();
    }catch(e){}
  }

  // ── 任务状态条:所有输入方式共用同一个会话任务池。─────────────────────────
  // TASK_ACTIVE_STATUSES 与 barTasks 都定义在顶层(见声明处):顶层的
  // taskBarDoneHidden/scheduleDoneHide 需要访问，不能再收进 IIFE。
  const TASKBAR_MAX = 7;
  const CHAT_TASK_ACTIVE_MAX = 5;
  let voiceTasksClearedAt = Number(localStorage.getItem("vococo_voice_tasks_cleared_at") || 0);

  function isCurrentConversationTask(task){
    if(task.origin !== "voice" && task.origin !== "chat") return false;
    // 「清空上下文」时间戳只该滤掉旧对话遗留的终态任务;任务被续接/追问重新
    // 激活(活跃状态)后不再受它限制——否则老任务重开时状态条永远停在旧的
    // "已完成"打叉不刷新(2026-08-17 修复,见 task_update 链路)。
    if(task.origin === "voice" && task.created_at && task.created_at < voiceTasksClearedAt
       && !TASK_ACTIVE_STATUSES.includes(task.status)) return false;
    if(task.dispatch_chat_id) return task.dispatch_chat_id === taskSessionKey();
    return task.origin === "voice" && taskSessionKey() === "main";
  }

  function upsertTaskBar(t){
    if(!isCurrentConversationTask(t)) return;
    if(t.deleted){ barTasks.delete(t.id); renderTaskBar(); return; }
    barTasks.set(t.id, t);
    renderTaskBar();
  }

  function taskbarNote(t){
    if(t.status === "running") return t.progress_note || "进行中";
    if(t.status === "in_progress") return t.description || "进行中";
    if(t.status === "queued" || t.status === "pending") return "排队中";
    return t.result_summary || STATUS_WORD[t.status] || t.status;
  }

  function isTaskDone(t){ return !TASK_ACTIVE_STATUSES.includes(t.status); }
  function taskbarTitle(t){ return t.title; }

  function renderTaskBar(){
    const rows = [...barTasks.values()]
      .filter(isCurrentConversationTask)
      .filter(t=>!taskBarDoneHidden(t))
      .sort((a,b)=>(b.updated_at ?? b.created_at) - (a.updated_at ?? a.created_at));
    const inCall = !$("#callView").hidden;

    renderBar($("#taskBar"), inCall ? rows.slice(0, TASKBAR_MAX) : []);
    const active = rows.filter(t=>!isTaskDone(t));
    const done = rows.filter(t=>isTaskDone(t));
    renderChatTaskBar(inCall ? [] : active, inCall ? [] : done);
    scheduleDoneHide();
  }
  // 差量渲染单个任务条(#taskBar 用):同 id 的行存在且状态/文案没变就不重建
  // DOM —— 旧版每次 task_update 都整段 innerHTML 重写,running 圆点的脉冲动画
  // 随之反复重启,视觉上一直闪,像"还在持续输出"。行结构同 renderTaskBarRow
  // (dot 两态 running/other,无 ✕ 按钮)。
  function renderBar(el, rows){
    el.hidden = !rows.length;
    for(const child of [...el.querySelectorAll(".taskbar-row")]){
      if(!rows.some(t=>t.id===child.dataset.tid)) child.remove();
    }
    rows.forEach((t, i)=>{
      let row = el.querySelector(`.taskbar-row[data-tid="${t.id}"]`);
      if(!row){
        row = document.createElement("div");
        row.className = "taskbar-row";
        row.dataset.tid = t.id;
      }
      if(el.children[i] !== row) el.insertBefore(row, el.children[i]);  // 按排序归位
      row.classList.toggle("done", isTaskDone(t));
      let dot = row.querySelector(".dot");
      if(!dot){ dot = document.createElement("span"); dot.className = "dot"; row.appendChild(dot); }
      // 圆点按状态语义色渲染(queued=灰/running=绿闪/failed=红…,见 styles.css);
      // 状态未知时落到默认灰
      const dotCls = "dot " + (t.status || "");
      if(dot.className !== dotCls) dot.className = dotCls;  // 状态没变不动 class,动画不重启
      let ttl = row.querySelector(".t");
      if(!ttl){ ttl = document.createElement("span"); ttl.className = "t"; row.appendChild(ttl); }
      const title = taskbarTitle(t);
      if(ttl.textContent !== title) ttl.textContent = title;
      const note = taskbarNote(t);
      let nEl = row.querySelector(".n");
      if(!nEl){ nEl = document.createElement("span"); nEl.className = "n"; row.appendChild(nEl); }
      if(nEl.textContent !== note) nEl.textContent = note;
    });
  }

  // 聊天视图任务条:活跃区行差量渲染(同 id 行存在且状态/文案没变不重建,
  // 圆点动画不因整段重写反复重启——旧版 innerHTML 全量重写,任务高频更新时
  // 一直闪,像"还在持续输出");分组/折叠按钮变化少,整段重建无感。
  function renderChatTaskBar(active, done){
    const chatTaskBar = $("#chatTaskBar");
    const any = active.length || done.length;
    chatTaskBar.hidden = !any;
    if(!any){ chatTaskBar.innerHTML = ""; return; }

    const expanded = chatTaskBar.dataset.activeExpanded === "1";
    const showActive = expanded ? active : active.slice(0, CHAT_TASK_ACTIVE_MAX);
    const doneExpanded = chatTaskBar.dataset.doneExpanded === "1";

    // 容器/分组结构只建一次,行高频变化走差量
    let box = chatTaskBar.querySelector(".chat-taskbar-box");
    if(!box){
      chatTaskBar.innerHTML = '<div class="chat-taskbar-box">' +
        '<div class="ctb-section ctb-active"></div><div class="ctb-section ctb-done"></div></div>';
      box = chatTaskBar.querySelector(".chat-taskbar-box");
    }
    const activeSec = box.querySelector(".ctb-active");
    const doneSec = box.querySelector(".ctb-done");

    // 活跃任务区:行差量 + 显示更多按钮(数量变化才增删,文案变了才改)
    renderChatRows(activeSec, showActive);
    let moreBtn = activeSec.querySelector(".ctb-more");
    if(active.length > CHAT_TASK_ACTIVE_MAX){
      const remain = active.length - CHAT_TASK_ACTIVE_MAX;
      if(!moreBtn){
        moreBtn = document.createElement("button");
        moreBtn.type = "button"; moreBtn.className = "ctb-more"; moreBtn.dataset.type = "active";
        activeSec.append(moreBtn);
      }
      moreBtn.textContent = expanded ? "收起" : `显示更多 (${remain})`;
    } else if(moreBtn){
      moreBtn.remove();
    }

    // 已完成任务区:默认折叠,展开时才渲染行(低频,整段重建即可)
    doneSec.innerHTML = "";
    if(done.length){
      const toggle = document.createElement("button");
      toggle.type = "button"; toggle.className = "ctb-toggle";
      const chev = document.createElement("span");
      chev.className = "ctb-chev" + (doneExpanded ? " open" : "");
      toggle.append(chev, document.createTextNode(`已完成 (${done.length})`));
      doneSec.append(toggle);
      if(doneExpanded) renderChatRows(doneSec, done);
    }
  }

  // 差量渲染一组 ctb 行:同 id 行存在且状态/文案没变不重建(dot 动画不重启)
  function renderChatRows(section, rows){
    for(const child of [...section.querySelectorAll(".ctb-row")]){
      if(!rows.some(t=>t.id===child.dataset.tid)) child.remove();
    }
    rows.forEach((t, i)=>{
      let row = section.querySelector(`.ctb-row[data-tid="${t.id}"]`);
      if(!row){
        row = document.createElement("div");
        row.className = "ctb-row";
        row.dataset.tid = t.id;
        row.append(document.createElement("span"), document.createElement("span"), document.createElement("span"));
        row.children[0].className = "ctb-dot";
        row.children[1].className = "ctb-title";
        row.children[2].className = "ctb-note";
        section.append(row);
      }
      if(section.children[i] !== row) section.insertBefore(row, section.children[i]);  // 按排序归位
      row.classList.toggle("done", isTaskDone(t));
      const dot = row.children[0];
      // 圆点按状态语义色渲染(与 #taskBar 同款,见 styles.css);状态未知落到默认灰
      const dotCls = "ctb-dot " + (t.status || "");
      if(dot.className !== dotCls) dot.className = dotCls;  // 状态没变不动 class,动画不重启
      const ttl = row.children[1];
      const title = taskbarTitle(t);
      if(ttl.textContent !== title) ttl.textContent = title;
      const note = taskbarNote(t);
      const nEl = row.children[2];
      if(nEl.textContent !== note) nEl.textContent = note;
    });
  }

  taskBar.addEventListener("click", (e)=>{
    tasksDrawer.classList.add("open");
    loadTasks();
  });

  // 聊天视图输入框顶部的任务条:与通话视图 #taskBar 共享数据,点击打开任务抽屉
  $("#chatTaskBar").addEventListener("click", (e)=>{
    const moreBtn = e.target.closest(".ctb-more");
    if(moreBtn){
      e.stopPropagation();
      const chatTaskBar = $("#chatTaskBar");
      const key = moreBtn.dataset.type + "Expanded";
      chatTaskBar.dataset[key] = chatTaskBar.dataset[key] === "1" ? "" : "1";
      renderTaskBar();
      return;
    }
    const toggleBtn = e.target.closest(".ctb-toggle");
    if(toggleBtn){
      e.stopPropagation();
      const chatTaskBar = $("#chatTaskBar");
      chatTaskBar.dataset.doneExpanded = chatTaskBar.dataset.doneExpanded === "1" ? "" : "1";
      renderTaskBar();
      return;
    }
    tasksDrawer.classList.add("open");
    loadTasks();
  });

  function renderTasks(rows){
    tasksList.innerHTML = "";
    if(!rows.length){ tasksList.innerHTML = '<div class="task-empty">还没有任务</div>'; return; }
    for(const t of rows){
      const active = t.status === "queued" || t.status === "running";
      const rowEl = document.createElement("div");
      rowEl.className = "task";
      rowEl.innerHTML =
        `<div class="row"><span class="dot ${t.status}"></span>` +
        `<span class="title">${esc(t.title)}</span><span>${STATUS_WORD[t.status] || t.status}</span>` +
        (active ? '<button class="stop">停止</button>' : "") + `</div>` +
        `<div class="note">${esc(t.status === "running" ? t.progress_note : (t.result_summary || ""))}</div>` +
        `<div class="full">${esc(t.result_full || "")}</div>`;
      rowEl.querySelector(".row").addEventListener("click", (e)=>{
        if(e.target.closest(".stop")) return;
        rowEl.querySelector(".full").classList.toggle("open");
      });
      const stopEl = rowEl.querySelector(".stop");
      if(stopEl) stopEl.addEventListener("click", async (e)=>{
        e.stopPropagation();
        await fetch(`/tasks/${t.id}/stop`, {method:"POST", headers:{"X-Auth-Token":S.token}});
        loadTasks();
      });
      tasksList.appendChild(rowEl);
    }
  }

  tasksBtn.addEventListener("click", ()=>{ tasksBadge.classList.remove("on"); tasksDrawer.classList.add("open"); loadTasks(); });
  tasksDrawer.addEventListener("click", e=>{ if(e.target === tasksDrawer) tasksDrawer.classList.remove("open"); });

  function flushAnnouncements(){
    if(busy || (recorder && recorder.state === "recording") || (handsFreeActive && wsState !== "idle")) return;
    let queuedForOmni = false;
    while(pendingAnnouncements.length){
      const data = pendingAnnouncements.shift();
      addMsg("ai", data.announce_text);
      if(omniDc && omniDc.readyState === "open"){
        // Omni 出声模式:播报交给 Omni 念——跟对话同一把声音、同一条 RTC 链路,
        // 服务端回声消除拿得到参考信号;不再走旧 TTS 的 Web Audio 播放
        // (两套声音并存=语气割裂+自回声风险,2026-07-10 定案)。
        omniReadQueue.push(data.announce_text);
        queuedForOmni = true;
      } else if(audioCtx && data.audio_b64){ playQueue.push({seq:-1, audio_b64: data.audio_b64}); pumpPlayback(); }
    }
    if(queuedForOmni) pumpOmniRead();
  }
  // P0 修复(2026-07-27):flushAnnouncements 只在忙碌状态转换的具体事件里被叫到,
  // 如果 task_done 到达时你正忙(比如同一个语音会话里还在问别的),播报会一直攒着,
  // 只能等下次交互才被想起来——加个定时兜底,别指望状态转换事件一定会来。
  setInterval(()=>{ if(pendingAnnouncements.length) flushAnnouncements(); }, 8000);

  // 音频补发(2026-08-10):服务端广播先行、TTS 合成后补——同 id 的 audio_patch
  // 事件带 audio_b64,只服务"Omni 断开、只能靠 Web Audio 播"的场景:
  // - 原播报还在 pending(用户正忙没念)→ 给它补上音频,等下轮 flush 连气泡一起播;
  // - Omni 通话中 → 文字已念/将念,音频冗余,忽略;
  // - 其余(气泡已加、Omni 已断)→ 直接补播音频。
  function applyAnnouncementAudio(data){
    const pending = pendingAnnouncements.find(p => p.id === data.id);
    if(pending){ pending.audio_b64 = data.audio_b64; return; }
    if(omniDc && omniDc.readyState === "open") return;
    if(audioCtx && data.audio_b64){ playQueue.push({seq:-1, audio_b64: data.audio_b64}); pumpPlayback(); }
  }

  // ── 通话状态字/声波球/提示音(免提 Omni 与按住说话共用)─────────────────────
  const STATE_WORD = {idle:"空闲", capturing:"聆听中…", thinking:"思考中…", speaking:"工作中…"};
  const STATE_CLASS = {capturing:"listening", thinking:"thinking", speaking:"playing"};

  function vadCapable(){
    return typeof AudioWorkletNode !== "undefined";
  }

  let analyser=null, analyserSource=null, outputAnalyser=null, omniOutTap=null, orbAnimHandle=null;

  function ensureAnalyser(){
    if(analyser || !audioCtx || !stream) return;
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 64;
    analyserSource = audioCtx.createMediaStreamSource(stream);
    analyserSource.connect(analyser);
  }

  // 输出侧(TTS 播放)也接一个 analyser,这样"speaking"状态的声纹是真实跟着
  // AI 语音波动的,不是瞎动的假动画——不需要再往下连到 destination,
  // AnalyserNode 只要接了输入就会处理,这点跟上面 ensureAnalyser() 的麦克风分支一致。
  function ensureOutputAnalyser(){
    if(outputAnalyser || !audioCtx) return;
    outputAnalyser = audioCtx.createAnalyser();
    outputAnalyser.fftSize = 64;
  }

  // ═══ 声波区 v4:小幽吉祥物接管 ═══════════════════════════════════════════
  // 原来这里是 WebGL/2D 画的横向声波竖条(WAVE_BARS/ORB_BANDS/着色器),2026-08-21
  // 换成吉祥物本体演——8 态引擎(mascot.js)里 idle/busy 原样复用,新增了一个
  // conn(连接中,呼吸+闭眼)专门区分"正在建连"和"AI在想":这两者以前很难在这么
  // 小的视觉里分清,现在"AI思考中"干脆直接复用 busy 的摇摆姿态,靠已有的思考
  // 循环音/任务滴答音效区分,不用为它单凑一个视觉。
  // "聆听中"不再画外挂音柱,吉祥物身体本身随真实麦克风电平呼吸式伸缩——
  // driveOrbMascot 每帧直接算 box-shadow 覆盖掉 mascot.css 的 CSS 关键帧动画,
  // 离开聆听态就把内联覆盖清空,交还给 CSS(见 mascot.js STATES.listening 注释)。
  let orbConnecting=false;  // startOmniHandsFree 置起,SDP 应答落地/挂断清掉
  let orbMascotTgt=null;    // 当前已应用到吉祥物身上的目标态,变化时才动 DOM
  let orbMicSmoothed=1;     // 聆听态身体呼吸幅度的指数平滑值,跨帧保留消除跳变

  function driveOrbMascot(){
    orbAnimHandle = requestAnimationFrame(driveOrbMascot);
    const tgt = orbConnecting ? "conn"
      : wsState === "capturing" ? "listening"
      : (wsState === "thinking" || wsState === "speaking") ? "busy" : "idle";
    const iEl = mascotEl.querySelector("i.vmi");
    if(tgt !== orbMascotTgt){
      orbMascotTgt = tgt;
      mascotEl.dataset.state = tgt;
      if(iEl){ iEl.style.animation = ""; iEl.style.boxShadow = ""; }  // 交还给 CSS 关键帧
      orbMicSmoothed = 1;
    }
    if(tgt !== "listening" || !analyser || !iEl) return;
    const data = new Uint8Array(analyser.frequencyBinCount);
    analyser.getByteFrequencyData(data);
    let sum = 0; for(let i=0;i<data.length;i++) sum += data[i];
    const level = sum / data.length / 255;
    const target = 1 + Math.min(0.42, level * 1.5);
    orbMicSmoothed += (target - orbMicSmoothed) * 0.35;
    iEl.style.animation = "none";
    iEl.style.boxShadow = VocoMascot.frameShadow({eyes:"open", feet:"waveA", scale:orbMicSmoothed});
  }

  // playTone:ctx 在跑就直接出声;被 iOS 挂起就先恢复【再把这一声补上】。
  // 2026-08-24 之前这里是 `resume(); return;`——发起恢复后把本次音效直接丢掉,
  // 而 resume 是异步的,真正恢复要到下一次调用才看得到。断连重连时 iOS 会因为
  // resetMicStream+getUserMedia 重新协商音频会话而把 ctx 打成 suspended/interrupted,
  // 于是"连接成功""思考中""回复中"这几声正好全落在这个窗口里被丢干净;若 ctx
  // 卡在 interrupted(iOS 私有态,resume 会一直 reject),就一路静音到下次重连
  // 重新激活音频会话为止——这就是用户报的"断连后音效不响、重连又好了"。
  function playTone(freq, durMs, shape, vol){
    if(!audioCtx) return;
    if(audioCtx.state === "running"){ emitTone(freq, durMs, shape, vol); return; }
    const askedAt = Date.now();
    ensureAudioCtxRunning("tone").then(ok => {
      // 恢复本身可能要几百毫秒;超过 1.5s 再补这一声就不合时宜了(状态早变了),
      // 老老实实丢掉,别在用户已经说下一句时冒出个上一轮的提示音。
      if(ok && Date.now() - askedAt < 1500) emitTone(freq, durMs, shape, vol);
    });
  }
  function emitTone(freq, durMs, shape, vol){
    if(!audioCtx || audioCtx.state !== "running") return;
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = shape || "sine";
    osc.frequency.value = freq;
    const now = audioCtx.currentTime;
    const dur = durMs / 1000;
    gain.gain.setValueAtTime(0, now);
    const v = vol != null ? vol : 0.15;
    gain.gain.linearRampToValueAtTime(v, now + Math.min(0.005, dur/4));
    gain.gain.linearRampToValueAtTime(0, now + dur);
    osc.connect(gain).connect(audioCtx.destination);
    osc.start(now);
    osc.stop(now + dur + 0.02);
  }
  function playCapturingTone(){ playTone(880, 80, "sine"); }
  function playSpeakingTone(){ playTone(660, 90, "sine"); }  // 单音节,原来的双音(660→990)高音太刺耳
  function playTtsFailTone(){ playTone(300, 140, "square"); }  // 免提通话听不到文字提示,得有声音信号
  // 断线/重连提示音:通话静默重连过去用户毫无感知,只能靠"hello hello 能听到吗"
  // 人肉探活(2026-07-10 真机一晚三次)。降调=掉线了,升调=接回来了。
  function playDisconnectTone(){ playTone(520, 110, "sine"); setTimeout(()=>playTone(330, 150, "sine"), 120); }
  function playReconnectTone(){ playTone(330, 110, "sine"); setTimeout(()=>playTone(660, 150, "sine"), 120); }
  // 首次拨打提示音:点击通话按钮、进入"连接中…"那一刻响一声,让用户确认点击生效、
  // 系统正在建连——跟下面的"接通音"用同一单音风格但音高不同,一耳朵能分辨两个阶段。
  function playDialConnectingTone(){ playTone(600, 90, "sine", 0.13); }
  function playDialConnectedTone(){ playTone(880, 100, "sine", 0.13); }
  // 发送音效:用户语音转写完成、提交给 Claude 时轻短"咻"一声(高音短促,低音量)
  function playSendTone(){ playTone(1350, 70, "sine", 0.15); }
  // AI 思考音效:提交后等待回复时循环响,音色跟"工作中"(playSpeakingTone)统一,
  // 用户听感上只分"在等/说完了"两种状态,不用再区分"思考"和"工作"两种音色。
  let thinkingToneTimer = null;
  function playThinkingTone(){
    stopThinkingTone();
    const beep = () => { if(!audioCtx) return; playSpeakingTone(); };
    beep();
    thinkingToneTimer = setInterval(beep, 1200);
  }
  function stopThinkingTone(){
    if(thinkingToneTimer){ clearInterval(thinkingToneTimer); thinkingToneTimer = null; }
  }

  async function startHandsFree(){
    vdbg("handsfree.start", {omni: S.omniEnabled, fn: typeof startOmniHandsFree});
    // 返回值必须透传给按钮模式的点击处理器:吞掉返回值会让调用方永远拿到
    // undefined,连接明明成功也被当失败处理(2026-07-15 踩坑)。
    if(S.omniEnabled){ return await startOmniHandsFree(); }
    // P2 WS 免提链路已下线(/voice/ws 路由已摘,见 docs/adr/0004):走到这里说明
    // Omni 配置没取到(登录预取失败)或 VOICE_OMNI_ENABLED=0——不能再连 WS
    // (只会 404 →1.5s 重连死循环+断线音),直接回落按住说话,跟"免提启动失败"
    // 同一姿态。
    handsFreeUi.hidden = true;
    talkBtn.hidden = false;
    addMsg("ai error", "免提通话需要 Omni 模式(检查 VOICE_OMNI_ENABLED / 网络),已切换到按住说话");
    return false;
  }

  function manualInterrupt(){
    stopThinkingTone();
    if(currentSource){ try{ currentSource.onended = null; currentSource.stop(); }catch(e){} currentSource = null; }
    playQueue = [];
    playing = false;
    pausedForInterrupt = false;
    // Omni 出声模式:点声纹球打断=取消 Omni 正在念/排队要念的内容 + 作废在飞的 Claude 流
    if(omniDc){
      omniTurnGen++;
      cancelActiveVoiceTurn("manual");
      cancelOmniReading("manual");
      omniTurnDone = true;
      wsState = "idle"; setStatus(STATE_WORD.idle);
      if(handsFreeActive) orbWrap.className = "idle";
    }
  }

  function connectTaskStream(){
    const es = new EventSource("/tasks/stream?token=" + encodeURIComponent(S.token));
    es.addEventListener("task_update", e=>{
      const t=JSON.parse(e.data);
      upsertTaskBar(t);  // 派发/起跑/进度变化 → 状态条实时刷新
      syncSidebarTaskStatus(t);  // 侧边栏默认项目任务行的进行中圆点同步亮/灭
    });
    es.addEventListener("task_done", e=>{
      const data = JSON.parse(e.data);
      tasksBadge.classList.add("on");
      loadTasks();
      loadVoiceSidebar();  // 任务终态 → 同步刷新侧边栏默认项目里那条后台任务的状态点
      upsertTaskBar(data);  // 终态 → 状态条里标记"已完成",留在条里等用户手动点叉关闭
      // 音频补发(2026-08-10):服务端广播先行、TTS 合成后补,同 id 的 audio_patch
      // 事件带 audio_b64——第一条通常没有音频,播报文字先到;合成完这条才来。
      if(data.audio_patch){
        applyAnnouncementAudio(data);
        return;
      }
      // 当前处于语音视图时,同一会话创建的任务都可以进入语音播报。
      if(!$("#callView").hidden && isCurrentConversationTask(data)){
        pendingAnnouncements.push(data);
        flushAnnouncements();
      }
    });
    // SSE 断线重连成功后全量校准一次:重连期间错过的 task_done 靠这次拉全量补上
    es.onopen = ()=>{ loadTasks(); };
  }

  // ── Omni-Realtime WebRTC 通话(免提唯一管线,ADR 0004)──────────────────────
  // 2026-07-10 架构(第二版,Omni 出声):Omni 当"耳朵+嘴巴",大脑仍然是 Claude。
  // - 耳朵:WebRTC 连线做识别(ASR)+断句(VAD)+打断信号,跟第一版相同。
  // - 大脑:识别到一整句话后转发给 /voice/send(Claude 全套工具/画像/记忆),
  //   这一步也不变;但 body 带 tts:false,服务端不再合成 TTS。
  // - 嘴巴(本次新增):Claude 的回答按句子经 conversation.item.create(纯文字
  //   input_text,不带字符串标记,见 pumpOmniRead)+ response.create 交给 Omni 念,
  //   音频走 WebRTC 远端轨道(RTP)回来从
  //   #omniRemoteAudio 播——这是阿里云文档的标准链路,服务端回声消除+语义防
  //   误打断只有音频走 RTC 闭环才生效;之前"自己 TTS+Web Audio 播放"在这条
  //   链路外,AEC 拿不到参考信号,才有"AI 录到自己说话"的假对话问题。
  // - 已知约束:turn_detection.create_response 实测关不掉,用户每说一句 Omni
  //   都会自作主张生成一份回复——靠 session.instructions 把它压成一个字 +
  //   response.created 时对"不是我们点的"回复立即 response.cancel 双保险,
  //   见 wireOmniDataChannel 的 response.created 分支。2026-07-13 真机实锤这俩
  //   保险仍会漏("嗯"字生成得比 cancel 网络往返还快),加第三层物理保险:
  //   远端 <audio> 永不静音,自动回复只靠 cancel 砍掉(2026-07-15:不静音输出通道)
  let omniTurnGen = 0;  // 每次新一句开始/打断都 +1,过期的 /voice/send 流式结果据此判断该不该再应用
  // 按钮模式空闲收线(2026-07-15,Wesley 定的 B 方案):一轮答完不立刻挂断,保持
  // 连接继续听,像真电话;但连续空闲满 5 分钟(不说/不想/不念)自动收线回按钮待机,
  // 长时间不用不让麦克风一直热着。计时状态由 3s 看门狗 omniReadStallCheck 驱动。
  const BUTTON_IDLE_HANG_MS = 5 * 60 * 1000;
  let omniIdleSince = 0;  // 本段连续空闲的起点时间戳;0 = 当前没在空闲计时

  async function startOmniHandsFree(){
    vdbg("omni.start");
    setStatus("连接中…", "");
    setOmniConnStatus("connecting");
    orbConnecting = true;  // 声波球切「连接中」形态(彗星扫动),SDP 应答落地后清掉
    unlockAudio();
    resetMicStream();  // 每次开新连线都强制换新麦克风流,见 resetMicStream 注释
    try{ omniMicStream = await ensureMicStream(); }
    catch(e){
      orbConnecting = false;
      handsFreeUi.hidden = true; talkBtn.hidden = false;
      addMsg("ai error", "麦克风权限被拒绝,请到系统设置里允许");
      return false;
    }
    vdbg("omni.mic.ok");
    // 【音效静音的正面修复点】(2026-08-24):上面 resetMicStream + getUserMedia 会让
    // iOS 重新协商音频会话(playback ↔ play-and-record),AudioContext 常在这一刻被
    // 打成 suspended/interrupted。重连路径正好每次都走这里,所以"断连之后提示音
    // 全哑、下次重连又好了"是必然而不是玄学。拿到新流就立刻把 ctx 拉回来,
    // 别等 playReconnectTone 撞上挂起态。
    ensureAudioCtxRunning("mic-acquired");
    // 新连接(含重连/切音色的全新流):重置首回复静音状态;用户之前按的手动静音
    // (micMuted)要带到新流上——都交给 applyMicEnabled 统一仲裁重算。
    omniFirstReplyMuteUsed = false; omniMicMuted = false;
    // 朗读校验现场跟着连接走,别把上一通电话的残留带进来
    omniReadRespId = null; omniReadExpectText = ""; omniSpokenBuf = ""; omniSpokenChecked = false;
    // 物理隔离状态也跟着连接走(2026-08-04):omniSessionModalities 是页面级变量,
    // 一旦被 modalities.fallback 翻成 ["text","audio"] 又不重置,之后每通电话的
    // 自动回复都物理出声(回读漏音)。每通新电话恢复 text-only;万一响应级覆盖
    // 真被服务端忽略,response.done 分支的自愈闸会重新 flip,降级语义不变。
    omniSessionModalities = ["text"];
    // 响应级参数的降级标志每条新连接重置:给它一次重试机会——万一上次是偶发错误
    // 被误判成"服务端不认",不至于就此永久失去每句重申角色的加固。
    omniReadCreateDegraded = false;
    if(omniMicMuteTimer){ clearTimeout(omniMicMuteTimer); omniMicMuteTimer = null; }
    applyMicEnabled();
    // 输出通道保持静音(2026-08-10 反转):连接建立时没有朗读在出声,自动回复
    // 音频却可能在用户开口后随时到达——默认静音直到朗读 created 确认才开声。
    // 新连接:上一条连接没等到 completed 的兜底现场作废,别把旧句在新会话上重发
    if(omniDeltaFallbackTimer){ clearTimeout(omniDeltaFallbackTimer); omniDeltaFallbackTimer = null; }
    omniDeltaFallback = null; omniFallbackSentItem = null;
    ensureAnalyser();        // 麦克风侧电平(capturing 状态时驱动吉祥物呼吸幅度用)
    if(!orbAnimHandle) driveOrbMascot();

    omniPc = new RTCPeerConnection();
    omniMicStream.getTracks().forEach(t => omniPc.addTrack(t, omniMicStream));
    // Omni 出声:AI 语音从远端轨道回来,接到常驻的 <audio> 元素上播放。
    omniPc.ontrack = ev => {
      vdbg("pc.ontrack", {kind: ev.track && ev.track.kind});
      const ms = (ev.streams && ev.streams[0]) || new MediaStream([ev.track]);
      omniAudioEl.srcObject = ms;
      // 远端轨道 tap 进输出 analyser:尾音排空监听(armOmniReadTailWatch)靠它
      // 感知 AI 是否真的还在出声。只做分析不改道,播放仍走上面的 <audio>——
      // 别重走 2026-07-10 loopback 改道的老路。tap 失败只损失波形检测,
      // 排空监听自动退回字数模型兜底。
      ensureOutputAnalyser();
      if(outputAnalyser){
        try{
          if(omniOutTap){ try{ omniOutTap.disconnect(); }catch(e){} }
          omniOutTap = audioCtx.createMediaStreamSource(ms);
          omniOutTap.connect(outputAnalyser);
        }catch(e){ omniOutTap = null; vdbg("out-tap.fail", String(e)); }
      }
      omniAudioEl.play().then(
        ()=>vdbg("audio.play.ok"),
        err=>vdbg("audio.play.fail", String(err)));
    };
    omniPc.onconnectionstatechange = () => {
      if(!omniPc) return;
      const st = omniPc.connectionState;
      vdbg("pc.state", st);
      if(st === "connected"){
        setOmniConnStatus("connected");
        omniReconnectAttempts = 0;  // 连上了就重置退避计数
      } else if(st === "connecting"){
        setOmniConnStatus("connecting");
      } else {
        setOmniConnStatus("disconnected");
      }
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
          // 2026-07-31 改为默认只给 text:自动回复物理上不出声,详见
          // omniSessionModalities 声明处。音频轨道本身照旧走 WebRTC(AEC 是
          // WebRTC 接入自带的,不是靠 session modalities 开的),朗读靠响应级覆盖出声。
          modalities: omniSessionModalities,
          voice: S.omniVoice,
          // 2026-07-12 真机反馈:老指令只说"读出【朗读】后面的内容",没明确禁念标记,
          // Omni 一句一个"朗读"地念出来——加了"标记本身绝对不念"之后一度好转,
          // 但 2026-07-28 长通话真机复现又冒出同一症状:标记字符串"【朗读】"本身
          // 每句都会进 Omni 的上下文,通话越长、句子越多,这个词出现频次越高,
          // "标记不要念"这条指令遵循就越容易松动,字面把"朗读"两个字念出来。
          // 根治:不再靠"塞一个字符串标记 + 指令要求模型自觉不念"这种概率性
          // 方案,改成让 Omni 按消息的内容类型区分——我们主动推给它朗读的都是
          // 纯文字(input_text,见 pumpOmniRead),用户真实说话产生的是语音输入
          // (input_audio),两者类型不同,不需要也不应该在文本正文里额外嵌一个
          // 有被读出来风险的标记词。
          instructions: "你是一个只负责朗读的语音引擎。收到纯文字消息(input_text)时," +
            "把这段文字一字不差地朗读出来,自然流畅,不要添加、省略或改动任何内容," +
            "也不要发表自己的看法,不要念出正文之外的任何说明。" +
            "收到语音输入(用户开口说话)时,不要自动生成回复," +
            "如果非说不可就简短确认收到即可;尽可能保持沉默。",
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
    vdbg("omni.ice.wait");
    // ICE 收集等待必须有超时:iOS 真机复现过 gathering 永远不到 complete,这里原本
    // 无限 await → 通话永远卡在"连接中…"且无任何日志(2026-07-12)。8s 到就带着
    // 已有候选继续——信令服务器接受部分候选,大不了连接失败走 pc.state 重连兜底。
    await new Promise(resolve => {
      const timer = setTimeout(()=>{ vdbg("omni.ice.timeout"); resolve(); }, 8000);
      const done = ()=>{ clearTimeout(timer); resolve(); };
      if(omniPc.iceGatheringState === "complete") return done();
      omniPc.onicegatheringstatechange = () => { if(omniPc.iceGatheringState === "complete") done(); };
    });
    vdbg("omni.sdp.post");

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
    startKeepAliveAudio();  // 通话保活音轨:锁屏防挂起(见函数注释)
    startKeepAliveVideo();  // 通话防熄屏视频:阻止 iOS 自动锁屏(见函数注释)
    acquireWakeLock();      // 通话真正开始:申请屏幕常亮,防自动熄屏(见函数注释)
    orbConnecting = false;
    setOmniConnStatus("connected");
    orbWrap.className = "idle";
    clearHint();
    setStatus("聆听中…", "listening");
    omniIdleSince = 0;  // 新连接从零开始数空闲,别继承上一段连接的计时
    if(omniReadWatchdog) clearInterval(omniReadWatchdog);
    omniReadWatchdog = setInterval(omniReadStallCheck, 3000);
    return true;
  }

  // 断连重连兜底:阿里云单会话最长 120 分钟到点会主动断,网络切换(WiFi↔蜂窝)
  // 也会把 PC 打断。轻量策略:整个拆掉重建一条连线,间隔一拍防抖;handsFreeActive
  // 为 false(用户已挂断)就什么都不做。
  // force=true:服务端会话已确认死透(如 dc:error 推回 InternalError)时用——
  // 跳过"pc 看着还 connected 就不重建"和"朗读中暂缓"两道闸。这种场景 pc 层
  // 往往还要挂 5~10 秒才跳 disconnected,等它自己发现黄花菜都凉了。
  function scheduleOmniReconnect(reason, delayMs, force){
    if(!handsFreeActive || omniReconnectTimer) return;
    // 连接要重建了:缓冲里已定稿的用户话先发出去——它走 HTTP /voice/send,不依赖
    // 这条 WebRTC;不发的话重建的 teardown 会把 omniPendingText 清掉,用户的问题
    // 就人间蒸发了(2026-07-22 真机 13:03:29 实锤:提问压在安全网里等 flush,
    // 20 秒后连接断开触发重连,缓冲被清,一直"思考中"没有任何回复)。
    if(omniPendingText){
      vdbg("pending.flush-before-reconnect", {len: omniPendingText.length});
      if(omniPendingTimer){ clearTimeout(omniPendingTimer); }
      flushOmniPending();
    }
    vdbg("reconnect.scheduled", {reason, attempts: omniReconnectAttempts});
    omniReconnectAttempts++;
    setOmniConnStatus("disconnected");
    // 指数退避:每次重连失败后翻倍,上限 60 秒;若入参 delayMs 比退避值大则用入参
    const backoffMs = Math.min((delayMs || 1500) * Math.pow(2, omniReconnectAttempts - 1), 60000);
    const actualDelay = Math.max(delayMs || 1500, backoffMs);
    omniReconnectTimer = setTimeout(async () => {
      omniReconnectTimer = null;
      if(!handsFreeActive) return;
      // disconnected 可能已经自愈,重建前再看一眼当前状态。
      // 但 mic:开头的原因(devicechange/ended/stale)是麦克风轨道本身的问题,
      // 跟 WebRTC 网络层连接状态无关——pc 显示 connected 不代表麦克风还活着。
      // 2026-07-27 真机实锤:蓝牙切手机麦克风时 pc 全程 connected,导致这条
      // "pc 还连着就跳过"的短路把 mic:devicechange/mic:stale 重连全部拦下,
      // 麦克风轨道 ended 后再也没被真正 reset+reacquire,通话表面"已接回"实际
      // 永久失聪。mic 类原因必须无视 pc 状态,老老实实走一遍真重连。
      const isMicReason = reason.startsWith("mic:");
      if(!force && !isMicReason && omniPc && (omniPc.connectionState === "connected" || omniPc.connectionState === "connecting")){
        vdbg("reconnect.skipped", "pc recovered");
        // 跳过 ≠ 重连失败:计数器必须清零。2026-07-15 事故——通话中心跳误报攒了
        // 7 次"调度后发现 pc 还活着",计数器只增不减(原本只在 pc 状态跳变到
        // connected 时清零,而 pc 一直 connected 就永不跳变),真断线那刻退避
        // 直接顶到 60 秒,用户盯着"聆听中"干等一分钟。
        omniReconnectAttempts = 0;
        setOmniConnStatus(omniPc.connectionState === "connected" ? "connected" : "connecting");
        return;
      }
      // 朗读进行中时暂缓重连:停止当前播放再重建 WebRTC 会掐断用户正在听的
      // 音频输出(最后几句永远听不到)。等朗读自然结束(或 8s 超时兜底)再重建。
      // force 时不暂缓——服务端会话已死,这个"朗读"永远不会结束。
      if(!force && (omniReadActive || omniReadPending > 0 || omniReadQueue.length > 0)){
        vdbg("reconnect.deferred", {ra:omniReadActive, rp:omniReadPending, q:omniReadQueue.length});
        // 8s 后再试一次,如果朗读还没结束就强制重连(避免因朗读卡住导致永久不重连)
        scheduleOmniReconnect(reason + "-deferred", 8000);
        return;
      }
      vdbg("reconnect.start", reason);
      setStatus("重连中…", "thinking");
      setOmniConnStatus("connecting");
      playDisconnectTone();  // 让用户知道刚才断了,而不是全程静默自愈
      stopOmniHandsFree();
      let ok = false;
      try{ ok = await startOmniHandsFree(); }
      catch(e){ vdbg("reconnect.fail", String(e)); }
      // 信令失败是 return false 不是 throw,统一在这里兜住继续重试
      if(!ok){ scheduleOmniReconnect("retry"); return; }
      playReconnectTone();
      addMsg("ai", "(通话断开过一下,已经自动接回来了)");
      vdbg("reconnect.ok", reason);
    }, actualDelay);
  }

  // 音色切换实时生效:音色是建连时随 session-init 带上的,已经出过声的会话改不了
  // voice 字段——通话进行中切音色,唯一可靠的路径是把整条 Omni 连线拆掉重建
  // (新音色在 startOmniHandsFree 里自然读到)。不在通话中就什么都不用做。
  // 挂在 window 上是因为下拉框的接线(initVoiceSelect)在 IIFE 外面。
  window.omniApplyVoiceChange = async function(){
    if(!handsFreeActive || !omniPc) return;
    vdbg("voice.switch", S.omniVoice);
    setStatus("切换音色中…", "thinking");
    stopOmniHandsFree();
    let ok = false;
    try{ ok = await startOmniHandsFree(); }
    catch(e){ vdbg("voice.switch.fail", String(e)); }
    if(!ok) scheduleOmniReconnect("voice-switch", 1500);
  };

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
      // 全局 DC 事件时间戳:所有 DataChannel 事件都计入,看门狗据此判断连接是否还活着
      omniLastDcEvent = Date.now();
      // 朗读心跳:任何 response.* 事件(含高频 audio delta)都说明 Omni 还活着——
      // 看门狗只在"彻底没动静"时才出手,不会误杀正常的长句朗读。
      if(data.type && data.type.startsWith("response.")) omniReadSince = Date.now();
      if(!OMNI_QUIET_EVENTS.has(data.type)){
        vdbg("dc:" + data.type, {
          rp: omniReadPending, ra: omniReadActive, aa: omniAutoActive,
          q: omniReadQueue.length,
          // response.* 事件带上这次 response 的实际 modalities:2026-08-10 真机
          // 日志定案——created 回执恒 "text"(session 级快照),done 回执才是实际
          // 值;而自动回复的 done 也是 "text+audio",证明物理隔离从未生效。
          mod: (data.response && data.response.modalities)
            ? data.response.modalities.join("+") : undefined,
          // 能否"源头区分"两条 response 的验证埋点(2026-08-10):若服务端回显
          // 响应级 instructions(我们的朗读带 OMNI_READ_INSTRUCTIONS 独特指令)
          // 或 trigger 字段(turn_detected 之类),就能精确区分自动回复、源头丢弃;
          // 真机日志里这两个字段是否出现/内容如何,决定未来能否升级方案。
          ins: (data.response && data.response.instructions)
            ? String(data.response.instructions).slice(0, 24) : undefined,
          trig: (data.response && data.response.trigger) || undefined,
          err: data.error ? String(data.error.message || data.error.code || "") : undefined,
        });
      }
      switch(data.type){
        case "input_audio_buffer.speech_started":
          // 用户开口:如果上一轮还在思考/播放,当成打断处理——取消进行中的朗读、
          // 清空朗读队列,让过期的 /voice/send 流式结果(见 sendOmniTurn 的 gen
          // 判断)不再生效。
          omniSpeechStartTs = Date.now();
          omniSpeechActive = true;
          // 主动拉起自动回复防护——用户一开口 Omni 必定自动生成回复(create_response
          // 关不掉),不等 response.created 回执就把泵朗读的闸拉住,防止 SSE 句子
          // 先到、自动回复的 response.created 后到时的竞态(自动回复的 response.
          // created 若晚于我们的 response.create 到达,会误吃 omniReadPending
          // 计数器,假扮成我们点的朗读并开声)。
          armOmniAutoFuse();
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
            armOmniGhostTimer();
            // 任务完成播报(flushAnnouncements)仍走 Web Audio 播放,打断时一并停掉
            if(currentSource){ try{ currentSource.onended = null; currentSource.stop(); }catch(e){} currentSource = null; }
            playQueue = []; playing = false; pausedForInterrupt = false;
            // 打断要的是干净的静音,不再叠一个确认音——那个音在切断的同一瞬间响起,
            // 听感上像"声音被调小"而不是"直接停",干脆去掉,靠下面的 capturingTone 提示就够。
          } else {
            // 非 barge-in 场景开口:清掉可能残留的旧快照(上一次幽灵打断没等到转写),
            // 别让 matchOmniEcho 误以为这句话发生在 AI 说话期间。
            omniEchoRestore = null;
            // 纵深防御(2026-08-04):一开口就静音输出通道。自动回复的 RTP 音频比
            // DataChannel 的 response.created 先到,等回执再静音必漏开头几百毫秒;
            // 物理隔离万一被降级(session 带 audio),这层就是唯一防线。我们的朗读
            // 会在 read.created 重新开声,提示音走 Web Audio 不受影响,无副作用。
            setOmniAudioMuted(true, "speech_started");
            // 又开口了,说明上一段大概率没说完:把待发送缓冲的倒计时顺延成安全网,
            // 等这句话的转写到齐再一起发。【2026-07-22 事故根因】这里以前把
            // 安全网覆盖成 1.5s 短定时器——短定时器在新一段转写回来之前到点,把
            // 前半句先发了出去,后半句成孤儿,整个对话从此错位。就算这句话最终被
            // 判成回声丢弃,安全网到点也会自愈式地把缓冲发出去,不会卡死。
            scheduleOmniFlush("speech_started");
          }
          if(wsState !== "capturing") playCapturingTone();
          omniCapturingSince = Date.now();  // 看门狗:记录 capturing 开始时刻
          wsState = "capturing";
          setStatus(STATE_WORD.capturing, STATE_CLASS.capturing);
          if(handsFreeActive) orbWrap.className = "capturing";
          omniUserLiveEl = null;  // 新一句开始,上一句的实时字幕气泡不再更新
          break;
        case "input_audio_buffer.committed":
          // 第二道防护闸:speech_started 被 VAD 漏报时(如太短/太轻)也拉住泵朗读
          // 的闸,不等 auto-reply 的 response.created 回执;auto-reply 完成后
          // 的 response.done 会清掉它。第一道闸在 speech_started(见上)。
          // omniAutoSince 这里必须无条件刷新到"此刻"(2026-07-28 实锤修复长指令
          // 复述 bug):它是 pumpOmniRead 里 5 秒保险丝的计时锚点,以前只在
          // omniAutoActive 还是 false 时才设,锚点停留在 speech_started(用户
          // 刚开口那一刻)。用户话越长,说到这段真正 commit 时,保险丝早在说话
          // 过程中就已经在计时——auto-reply 的 response.created 还没到,
          // pumpOmniRead 就把 omniAutoActive 提前清成 false、放行真朗读抢先开
          // 声;等姗姗来迟的 auto-reply(内容是 Omni 把用户刚说的长指令复述回来)
          // 到达时 omniReadActive 已经是 true,response.created 分支「只在不朗读
          // 自己内容时才静音」的判断被跳过,复述音频原样播了出来。短句因为总耗时
          // 远小于 5 秒,踩不中这个窗口,只有长指令(voice_dispatch_task 常见场景)
          // 会暴露。锚点挪到"这段语音刚 commit 完"这一刻,才是保险丝真正该起算
          // 的时间点。armOmniAutoFuse 内部同样以此刻为锚。
          armOmniAutoFuse();
          // 这句话已定型,completed 理应几百毫秒内跟到。到点没来就是被我们的
          // response.cancel 连带砍死了(见 omniDeltaFallback 注释)——拿 delta
          // 缓存顶上,别让用户的话烂在半路。
          armOmniDeltaFallback();
          // 这一段的转写正式进入在途状态:completed(或 delta 兜底)回来之前,
          // 缓冲绝不能 flush——转写消费时在 handleUserTranscript 顶部递减。
          omniInflightSegs++;
          // 有积压时把倒计时重排成安全网,等这段转写到齐一起发(以前这里设 1.5s
          // 短定时器,是 2026-07-22 对话错位事故的帮凶之一,见 scheduleOmniFlush)。
          scheduleOmniFlush("committed");
          break;
        case "input_audio_buffer.speech_stopped":
          // 用户说完了。若最后一段转写还在途(inflight>0),flush 由转写到达驱动;
          // 都到齐了才短等待后发出(以前这里无条件 1s flush,在途转写直接被甩下)。
          omniSpeechActive = false;
          omniSpeechStoppedTs = Date.now();
          scheduleOmniFlush("speech_stopped");
          break;
        case "conversation.item.input_audio_transcription.delta":
          // 转写在正常推进,说明不是幽灵打断——兜底定时器往后顺延,别在用户
          // 长句说到一半时误判超时、把砍掉的朗读恢复回来盖住真提问。
          if(omniEchoRestore) armOmniGhostTimer();
          // delta 全文一直缓存:completed 被 cancel 砍死时靠它兜底发给 Claude
          omniDeltaFallback = {itemId: data.item_id || null, text: ((data.text || "") + (data.stash || "")).trim()};
          // 实时字幕:text 是已确认的前缀,stash 是还没定论的尾巴,拼起来显示
          // 让用户看到"正在识别"的感觉,句子说完由 .completed 用权威结果覆盖一次。
          if(!omniUserLiveEl) omniUserLiveEl = addMsg("me", "");
          renderAi(omniUserLiveEl, (data.text || "") + (data.stash || ""));
          break;
        case "conversation.item.input_audio_transcription.completed": {
          if(omniDeltaFallbackTimer){ clearTimeout(omniDeltaFallbackTimer); omniDeltaFallbackTimer = null; }
          omniDeltaFallback = null;
          if(omniFallbackSentItem && data.item_id === omniFallbackSentItem){
            // 这句已被 delta 兜底发过了,迟到的权威结果不再重复发
            vdbg("transcript.late", {item: data.item_id});
            omniFallbackSentItem = null;
            break;
          }
          omniFallbackSentItem = null;
          handleUserTranscript((data.transcript || "").trim());
          break;
        }
        // Omni 正在念的字幕(高频,不打日志见 OMNI_QUIET_EVENTS,但事件本身要处理)。
        // 只收属于当前朗读 response 的部分,自动回复的字幕不参与校验——见 isOwnReadEvent。
        case "response.audio_transcript.delta":
          if(isOwnReadEvent(data)) omniSpokenBuf += (data.delta || "");
          break;
        case "response.audio_transcript.done":
          // done 带完整 transcript,比 delta 拼出来的权威,优先用它
          if(isOwnReadEvent(data)) checkOmniSpoken(data.transcript || omniSpokenBuf, "transcript.done");
          break;
        case "response.created":
          stopThinkingTone();
          // 分两种:我们主动点的朗读(omniReadPending>0)放行;其余是 Omni 对用户
          // 话语自作主张的回复(create_response 关不掉,见顶部架构注释),立即取消,
          // 不让它出声——即便 cancel 慢半拍,instructions 也把它压成了一个字。
          if(omniReadPending > 0){
            omniReadPending--;
            omniReadActive = true;
            // 记下服务端给这次朗读分配的 response.id:后续 audio_transcript.delta/done
            // 都带 response_id,靠它把字幕准确归属到这次朗读(阿里云不支持 metadata
            // 回显,response_id 是唯一能用的关联字段)。期望文本也在这里快照——
            // omniReadCurrentText 在 response.done 就被清空,字幕可能比它晚到。
            omniReadRespId = (data.response && data.response.id) || null;
            omniReadExpectText = omniReadCurrentText;
            omniSpokenBuf = ""; omniSpokenChecked = false;
            // 【2026-08-04 纠错】这里原来有自愈闸:拿 created 回执的 modalities 判
            // "响应级覆盖被忽略"就 flip session——实测 created 回的是 session 级
            // 快照(恒 ["text"]),100% 误报,把物理隔离误拆、自动回复物理出声,
            // 用户听到"AI 复读我"(回读漏音根因,3/3 次实锤)。闸已挪到
            // response.done 分支,done 里的 modalities 才是覆盖后的实际值。
            // 确认是我们主动点的朗读才开声(2026-07-14 修复),不让自动回复偷用。
            omniAutoAudioBlocked = false;  // 我们的朗读开始,清除自动回复阻断
            setOmniAudioMuted(false, "read.created");
            if(wsState !== "speaking"){
              playSpeakingTone();
              wsState = "speaking";
              setStatus(STATE_WORD.speaking, STATE_CLASS.speaking);
              if(handsFreeActive) orbWrap.className = "speaking";
            }
          } else {
            armOmniAutoFuse();
            // 无条件静音(2026-08-10 二次定案):自动回复【一直带音频】(真机日志
            // 它的 done 回执 text+audio),created 只是"它开始生成"的信号,音频
            // RTP 紧随其后。此前"只在未朗读时静音"的例外(2026-07-15)会让撞上
            // 朗读进行中的自动回复整个播出来——而服务端同一时刻只允许一个 active
            // response,自动回复的 created 撞上朗读,说明朗读已被服务端顶替/排队,
            // 此刻静音丢的只是死朗读的尾巴,换来的却是自动回复一字不漏。v2 反转
            // 策略下这是兜底闸:正常流程里自动回复到达时元素本来就静音(默认静音),
            // 这里只在异常时序(朗读窗口内自动回复才 created)时把缺口补上。
            setOmniAudioMuted(true, "auto-reply");
            omniAutoAudioBlocked = true;
            // 不发送 response.cancel——它会连带杀死连续说话时后续片段的
            // transcription.completed 事件。自动回复的音频已被静音,且
            // pumpOmniRead 启动朗读时 response.create 会自动取代自动回复。
            vdbg("auto-response.muted");
          }
          break;
        case "response.done":
          if(omniReadActive){
            omniReadActive = false;
            // 服务端是否认了响应级 modalities 覆盖,只看不回翻(2026-08-08 漏音
            // 定案):这里原来(08-04 自愈闸)在 done 里发现缺 audio 就把 session
            // 翻回 ["text","audio"] 重念——但这会让 Omni 对用户话语的自动回复
            // 【也】物理出声(session 级对自动回复同样生效),而自动回复的静音
            // 防线天然漏(它的 RTP 音频先于 DataChannel 事件到达,收到 created
            // 再静音必漏开头;created 撞上朗读进行中时连静音都不触发)。主人明确
            // 要求"这条音频任何情况下完全静音"——宁可一句朗读哑掉,绝不翻转
            // session 给自动回复任何出声可能。真哑了只上报,留给真机日志判断
            // 该服务端版本是否支持响应级覆盖。
            const doneMods = (data.response && data.response.modalities) || null;
            if(doneMods && !doneMods.includes("audio")){
              vdbg("read.silent", {got: doneMods});
            }
            // 兜底校验:audio_transcript.done 没来过(被 cancel 砍掉/服务端没发)时,
            // 拿 delta 累积的字幕补校验一次,然后清场。checkOmniSpoken 内部有
            // omniSpokenChecked 去重,transcript.done 已经校验过的不会重复上报。
            checkOmniSpoken(omniSpokenBuf, "response.done");
            omniReadRespId = null; omniReadExpectText = ""; omniSpokenBuf = "";
            // 【2026-07-23 实锤 13:14】这句"生成完了"是回声过滤的重要时刻:把这句在
            // 近期朗读窗口里的时间戳更新为 done 时刻,播放期估算(inTail)从"真正开始
            // 播这句"起算。否则生成比播放快(队列还压着前句在念),一句 20s 后才开口
            // 的句子按 read.create 起算,窗口在 AI 念到一半就关闭——这句的回声漏进
            // 麦克风转写成"用户的话"被发出去(世界杯"他们在二零二二年世界杯决赛中"
            // 就是这么来的一轮假对话)。如果连播,queue.shift 的下一句马上会把自己的
            // ts 登记成它的 read.create,时长接力正确。
            const r = omniRecentReads.findLast(x => x.text === omniReadCurrentText);
            if(r) r.ts = Date.now();
            omniReadCurrentText = "";
            // response.done 是 Omni 生成完毕的信号,但 RTP 是按实际语速推流的,
            // 长句生成快于播放,done 时可能还欠好几秒没播——用 armOmniReadTailWatch
            // 的波形安静+跨句模型下限双判据等音频真正播完再泵下一句。
            // 音频输出通道永不静音(2026-07-15 修复),队列非空时直接泵下一句,
            // 尾音在生成间隙自然排空。
            if(omniReadQueue.length > 0){
              pumpOmniRead();
            } else {
              armOmniReadTailWatch();
            }
          } else {
            clearOmniAutoFuse();
            omniAutoActive = false;
            // 自动回复 done 只代表"生成完毕",RTP 尾巴可能还欠着几百毫秒到几秒
            // 没播(生成快于播放)——此刻立即静音,把自动回复的尾巴掐掉(它是不该
            // 播的内容),别让它漏进下一句朗读的开头(2026-08-10 反转策略)。
            setOmniAudioMuted(true, "auto-done");
            pumpOmniRead();  // 自动回复清场了,轮到排队中的朗读
          }
          break;
        case "error": {
          const msg = String((data.error && (data.error.message || data.error.code)) || "出错了");
          // 2026-07-30 根因修复:我们主动发起的朗读(pumpOmniRead 已 omniReadPending++)
          // 撞上 Omni 自己"关不掉"的自动回复还没让出 response 槽位,服务端拒绝并报
          // "already has an active response"(2026-07-23 事故日志 14:17:49 就是这个错误,
          // 当时只在"新一轮开始"入口做了防护,见 sendOmniTurn 注释,句子与句子之间正常
          // 轮转时命中同一个错误此前被下面这行原样吞掉——omniReadPending 没回滚、
          // 这句话的原文也没塞回队列,该句朗读凭空消失。更糟的是计数器留着 >0,
          // 下一个到达的 response.created(很可能是 Omni 自己那句自动回复)会被
          // 误判成"我们的朗读"而放行出声——用户听到的就变成 Omni 自己编的话,
          // 跟 Claude 的原文对不上。这里回滚状态、把没念成的原文塞回队列头部,
          // 短延迟后重新泵一次,而不是让这句话默默消失。
          // 响应级参数被拒:response.create 带 response 对象(instructions/modalities)
          // 是从官方 SDK 源码扒来的未文档化用法,万一这个服务端版本严格校验拒收,
          // 就一次性降级成空 response.create 并把这句重新入队,别让整通电话的朗读
          // 全废。排除 InternalError——那是服务端内部管线抽风(见下面的分支),
          // 跟我们发的参数无关,不能误判成降级信号。
          if(!omniReadCreateDegraded && omniReadPending > 0
             && !/InternalError/i.test(msg)
             && /invalid|unknown|unsupported|unrecogniz|param/i.test(msg)){
            omniReadCreateDegraded = true;
            omniReadPending--;
            if(omniReadCurrentText) omniReadQueue.unshift(omniReadCurrentText);
            omniReadCurrentText = "";
            vdbg("read.create.degraded", msg);
            setTimeout(() => pumpOmniRead(), 200);
            break;
          }
          if(/active response|no active/i.test(msg) && omniReadPending > 0){
            omniReadPending--;
            if(omniReadCurrentText) omniReadQueue.unshift(omniReadCurrentText);
            omniReadCurrentText = "";
            vdbg("read.create.rejected", msg);
            setTimeout(() => pumpOmniRead(), 300);
            break;
          }
          // response.cancel 撞上"没有进行中的回复"这类时序噪音只记日志不弹红气泡,
          // 用户看到也做不了什么;其余错误照旧可见。
          if(/cancel|active response|no active/i.test(msg)) break;
          // 双通道去重:同样的错误 3 秒内只处理一次(见 omniLastErrMsg 注释)。
          const errNow = Date.now();
          if(msg === omniLastErrMsg && errNow - omniLastErrTs < 3000) break;
          omniLastErrMsg = msg; omniLastErrTs = errNow;
          // 阿里云服务端内部错误(2026-07-15 两次真机复现:InternalError.Algo.
          // InvalidParameter "provided URL not valid",我们发的消息里根本没有 URL,
          // 是它内部管线抽风):这种错误之后服务端几秒内必掐断连接,原文用户看了
          // 也做不了什么——转成人话,并立即强制重建,不等 pc 层自己发现断线。
          if(/InternalError/i.test(msg)){
            addMsg("ai", "(语音服务那边出了点问题,正在重新接回…)");
            omniReconnectAttempts = 0;
            scheduleOmniReconnect("dc:internal-error", 1500, true);
          } else {
            addMsg("ai error", msg);
          }
          break;
        }
        // 其余事件:response.audio_transcript.delta 等是 Omni 朗读的字幕,Claude 的
        // 原文已经在界面上了,不重复渲染;audio 本体走 RTP 轨道,不经过这里。
      }
    };
  }

  // 字幕事件是不是属于我们这次朗读:优先用服务端的 response_id 严格比对
  // (阿里云不回显 metadata,response_id 是唯一可用的关联字段,值就是
  // response.created 里的 response.id);拿不到 id 时退回"当前正在朗读"这个
  // 较弱的判据——宁可漏判,也不要把自动回复的字幕算进校验。
  function isOwnReadEvent(data){
    if(omniReadRespId && data.response_id) return data.response_id === omniReadRespId;
    return omniReadActive;
  }

  // 把 Omni 实际念出来的字幕跟我们期望它念的原文比对,对不上就上报。
  // 【只上报不干预】原因见 omniReadRespId 定义处的注释:走到这一步音频已经播出去了。
  function checkOmniSpoken(spoken, tag){
    if(omniSpokenChecked) return;
    const a = normEchoText(omniReadExpectText), b = normEchoText(spoken);
    if(!a || !b) return;   // 有一边是空的判不了,留给后面的兜底调用
    omniSpokenChecked = true;
    if(a === b) return;    // 一字不差,正常
    // 编辑距离算相似度:Omni 偶尔会微调数字/单位的念法(这类差异相似度仍在 0.9 以上),
    // 但"自己另起一句发挥"整句都对不上,相似度会掉得很低。
    const sim = 1 - editDist(a, b) / Math.max(a.length, b.length);
    if(sim >= 0.9) return;
    vdbg("read.mismatch", {
      tag, sim: +sim.toFixed(2),
      expect: omniReadExpectText.slice(0, 60),
      spoken: String(spoken).slice(0, 60),
    });
  }

  // 响应级朗读指令(2026-07-31):session 级 instructions 在长通话里会被不断堆积的
  // 朗读文本稀释,"我只是个朗读引擎"的角色认知逐渐松动(2026-07-28 那次"【朗读】
  // 两个字被念出来"是同一机制的另一种表现)。阿里云客户端事件文档没写 response.create
  // 能带参数,但官方 dashscope SDK 源码(omni_realtime.py 的 create_response)实际
  // 会把 instructions/modalities 放进 response 对象一起发——每次朗读都重申一遍角色,
  // 直接对冲衰减,不用等长通话跑偏了才发现。
  const OMNI_READ_INSTRUCTIONS =
    "把用户这条纯文字消息一字不差地朗读出来,自然流畅。" +
    "不要添加、省略或改动任何内容,不要回应或评论这段文字的内容,不要说正文之外的任何话。";
  // 未文档化字段的降级开关:万一服务端严格校验、拒收带 response 对象的 response.create,
  // error 分支会把它置起,之后退回发空 response.create,不至于让整通电话的朗读全废。
  let omniReadCreateDegraded = false;

  // ── Omni 出声:朗读队列 ────────────────────────────────────────────────
  // Claude 的回答按句子进队,一句一个 response(服务端同一时刻只允许一个进行中
  // 的 response,靠 omniReadActive/omniAutoActive 串行);发的是纯文字消息
  // (input_text),不带任何字符串标记——跟 session instructions 里"纯文字消息
  // 直接一字不差朗读"的约定对上(2026-07-28 起不再用【朗读】前缀,见那段
  // instructions 定义处的注释)。

  // 自动回复在途保险丝的统一武装点(2026-08-08 漏音排查重构):
  // 用户每说一句,Omni 必定自动生成一份回复(create_response 关不掉)。它的
  // response.created 到达前,谁都不能放行朗读 pump——否则自动回复的 created
  // 会误吃 omniReadPending 计数,假扮成"我们的朗读"开声,自动回复音频原样
  // 播出来(漏音)。武装后 pump 被挡,直到:①自动回复 response.done 自然清场
  // (正常路径,1~3 秒);②保险丝超时主动解除(OMNI_AUTO_FUSE_MS,兜底 done
  // 丢失/从未生成的情况,到点后必须主动 pump 一次,见 omniAutoFuseTimer)。
  // 所有"预计自动回复在途"的时机都调它,不再各自散写 omniAutoActive 赋值:
  // speech_started / committed / 自动回复 created / restoreOmniBargeIn /
  // sendOmniTurn——这几处以前各清各的,清早/清晚都会让自动回复 created 趁虚
  // 而入(2026-07-15 注释自认"网络时序逆转场景"其实就是这个洞)。
  const OMNI_AUTO_FUSE_MS = 5000;
  function armOmniAutoFuse(){
    omniAutoActive = true;
    omniAutoSince = Date.now();
    if(omniAutoFuseTimer) clearTimeout(omniAutoFuseTimer);
    omniAutoFuseTimer = setTimeout(() => {
      omniAutoFuseTimer = null;
      if(!omniAutoActive) return;  // 已被自动回复 done 清场
      omniAutoActive = false;
      vdbg("auto-active.fuse-expired");
      pumpOmniRead();  // 保险丝到点:自动回复大概率不会再来了,放行朗读
    }, OMNI_AUTO_FUSE_MS);
  }
  function clearOmniAutoFuse(){
    if(omniAutoFuseTimer){ clearTimeout(omniAutoFuseTimer); omniAutoFuseTimer = null; }
  }

  // 朗读全部结束 → 恢复开麦(2026-08-11 根因修复):首回复静音(mic.mute
  // first-reply)的恢复原本只挂在 maybeOmniIdle,而它第一道闸要求 omniTurnDone
  // ——那要等 sendOmniTurn 的 SSE 流走完才置位。流慢/挂起(iOS Safari fetch
  // streaming 实测会挂)或 gen 被打断时 omniTurnDone 永远 false,麦克风跟着
  // 永久静音,下一轮说话全废(真机 11:06 轮:朗读 11:06:35 念完,流没走完,
  // 无恢复,用户挂断重开)。这里独立于 omniTurnDone:只要朗读队列清空且没有
  // 进行中的朗读/自动回复,立即恢复开麦,不等流。
  function maybeRestoreMic(){
    if(omniReadActive || omniReadPending > 0 || omniReadQueue.length > 0) return;
    if(omniAutoActive && Date.now() - omniAutoSince < OMNI_AUTO_FUSE_MS) return;  // 自动回复在途,再等等
    if(!omniMicMuted) return;
    if(omniMicMuteTimer){ clearTimeout(omniMicMuteTimer); omniMicMuteTimer = null; }
    setOmniMicMuted(false, "read-finished");
  }

  function pumpOmniRead(){
    if(!omniDc || omniDc.readyState !== "open") return;
    // 保险丝(被动兜底):自动回复的 response.done 万一丢了(cancel 撞 done 的
    // 时序缝),别让 omniAutoActive 卡死整个朗读队列——超过保险丝时长当它已经
    // 结束。主动超时解除见 armOmniAutoFuse 的定时器,这里主要服务"武装点之后
    // 才进 pump"的路径。
    if(omniAutoActive && Date.now() - omniAutoSince > OMNI_AUTO_FUSE_MS){
      clearOmniAutoFuse();
      omniAutoActive = false;
      vdbg("auto-active.timeout-cleared");
    }
    if(omniReadActive || omniAutoActive || omniReadPending > 0) return;
    if(omniReadQueue.length === 0){ maybeOmniIdle(); maybeRestoreMic(); return; }
    const text = omniReadQueue.shift();
    try{
      omniDc.send(JSON.stringify({
        type: "conversation.item.create",
        item: {type: "message", role: "user", content: [{type: "input_text", text}]},
      }));
      // 带响应级 instructions 重申朗读角色(见 OMNI_READ_INSTRUCTIONS);modalities
      // 显式带上,当前跟 session 一致,同时也把"session 只给 text、只有朗读才出声"
      // 那套物理隔离方案的接口先留好。服务端不认这些字段就降级发空的。
      omniDc.send(JSON.stringify(omniReadCreateDegraded
        ? {type: "response.create"}
        : {type: "response.create", response: {
            modalities: ["text", "audio"],
            instructions: OMNI_READ_INSTRUCTIONS,
          }}));
      omniReadPending++;
      omniReadSince = Date.now();
      omniReadCurrentText = text;
      // 开声移到 response.created 确认是我们的朗读后再做(2026-07-14 修复:
      // 提前开声会被自动回复的 response.created 偷走,造成 AI 回读用户字词)。
      omniRecentReads.push({text, ts: Date.now()});
      if(omniRecentReads.length > 8) omniRecentReads.shift();
      vdbg("read.create", text.slice(0, 40));
      // 本连接第一次出声:静音麦克风上行,给 AEC 一个无干扰的收敛窗口。
      // 15s 硬上限兜底——第一答太长时用户不能永远失去打断能力,超时后
      // 剩余回声交文字层。正常恢复走 maybeOmniIdle 的播放尾巴估算。
      if(!omniFirstReplyMuteUsed){
        omniFirstReplyMuteUsed = true;
        setOmniMicMuted(true, "first-reply");
        if(omniMicMuteTimer) clearTimeout(omniMicMuteTimer);
        const micEpoch = omniMicEpoch;  // 快照:sendOmniTurn 新轮开场会自增,到点过期自动放弃
        omniMicMuteTimer = setTimeout(() => {
          omniMicMuteTimer = null;
          if(micEpoch !== omniMicEpoch) return;  // 现场已换新,别把新状态覆写回去
          // 15s 硬上限【无条件开麦】(2026-08-11 恢复 07-12 原意):07-14 加的
          // "朗读还在进行就不开麦,等 maybeOmniIdle"依赖 omniTurnDone,而它可能
          // 因 SSE 流不结束永远 false——硬上限就失效,第一答太长时用户永远失去
          // 打断能力(真机 11:06 轮朗读 30s,cap 到点时朗读中,恢复落空,下一轮
          // 收音全废)。开麦后剩余回声交文字层兜底,这正是原设计的取舍。
          if(omniReadTailTimer){ clearTimeout(omniReadTailTimer); omniReadTailTimer = null; }
          setOmniMicMuted(false, "first-reply-cap");
        }, 15000);
      }
    }catch(e){ vdbg("read.create.fail", String(e)); }
  }

  // 麦克风轨道开关的唯一仲裁点:手动静音按钮(micMuted)和首回复静音(omniMicMuted)
  // 操作的是同一个 getUserMedia 流(ensureMicStream 复用),谁都不能直接写 enabled,
  // 否则互相覆盖(比如用户手动静音了,首回复恢复逻辑把它强行打开)。
  function applyMicEnabled(){
    const on = !micMuted && !omniMicMuted;
    const s = omniMicStream || stream;
    if(s) s.getAudioTracks().forEach(t => { t.enabled = on; });
  }
  function setOmniMicMuted(muted, why){
    if(omniMicMuted === muted) return;
    omniMicMuted = muted;
    applyMicEnabled();
    vdbg(muted ? "mic.mute" : "mic.unmute", why);
  }

  function setOmniAudioMuted(muted, why){
    if(omniAudioMuted === muted) return;
    // 自动回复音频阻断:自动回复期间阻止任何 UNMUTE 操作,防止其音频片段泄漏。
    // 自动回复的 cancel 走网络异步,即使 cancel 发出去,嗯的 RTP 包可能已经
    // 在链路中——omniAutoAudioBlocked 确保即使时序逆转也不会误放行自动回复音频。
    if(!muted && omniAutoAudioBlocked){
      vdbg("audio.unmute-blocked", why);
      return;
    }
    omniAudioMuted = muted;
    omniAudioEl.muted = muted;
    vdbg(muted ? "audio.mute" : "audio.unmute", why);
  }

  function updateCallModeTabs(){
    // 通话进行中(连接中/已接通/断线待重连)隐藏「语音/文本」切换,挂断回到默认状态才显示,
    // 避免通话中遮挡 UI。按住说话兜底模式无 Omni 状态,恒显示(该模式无持续"通话中"态)。
    const tabs = document.getElementById("callModeTabs");
    if(tabs) tabs.hidden = (omniConnStatus !== "disconnected" || handsFreeActive);
  }

  function setOmniConnStatus(status){
    if(omniConnStatus === status) return;
    omniConnStatus = status;
    if(connDotEl) connDotEl.className = "conn-" + status;
    updateCallModeTabs();
    vdbg("conn.status", status);
  }

  function maybeOmniIdle(){
    // 整轮收尾:Claude 文字流结束 + 朗读队列清空 + 没有进行中的朗读才回 idle;
    // 用户已经又开口(capturing)就不抢状态。
    if(!omniTurnDone || omniReadActive || omniReadPending > 0 || omniReadQueue.length > 0) return;
    if(wsState === "capturing" || wsState === "idle") return;
    wsState = "idle";
    setStatus(STATE_WORD.idle);
    if(handsFreeActive) orbWrap.className = "idle";
    // 整轮念完,输出通道收回静音(2026-08-10 反转策略):下一次开声只发生在
    // 确认朗读的 response.created。此刻用户已说完话,自动回复随时可能到达,
    // 元素必须回到默认静音态。
    setOmniAudioMuted(true, "read-idle");
    // 首回复静音期间整轮念完:等估算播放尾巴过去再开麦。用【顺序播放模型】——
    // 音频从首句起连续播,逐句累加(230ms/字),句间有生成间隙才从该句自己的
    // read.create 重新起算;缓冲只留 0.4s。之前每句独立加 2s 缓冲,两句连播时
    // 重复叠加多静音了 ~2.6s,把用户听完就答的开头几个字吞掉转写成碎片
    // (2026-07-12 真机实锤「记得。」)。宁可偏早:尾巴回声漏了有文字层兜底,
    // 吞用户的话没有任何兜底。
    if(omniMicMuted){
      const now = Date.now();
      let end = 0;
      for(const r of omniRecentReads){
        const start = Math.max(end, r.ts + 700);
        end = start + normEchoText(r.text).length * 220;
      }
      const tail = Math.max(300, Math.min(end + 200 - now, 8000));
      if(omniMicMuteTimer) clearTimeout(omniMicMuteTimer);
      const micEpoch = omniMicEpoch;  // 快照:新轮开场自增后,这个旧现场的开麦安排作废
      omniMicMuteTimer = setTimeout(() => {
        omniMicMuteTimer = null;
        if(micEpoch !== omniMicEpoch) return;
        setOmniMicMuted(false, "first-reply-tail");
      }, tail);
    }
    // 任务播报到达时若正忙会被攒进 pendingAnnouncements,以前只有下一条 task_done
    // 才会再触发 flush——正忙时来的播报就永远卡住不念(2026-07-10 真机:两个任务
    // 完成用户全程没听到)。回到空闲就是补播的正确时机。
    flushAnnouncements();
    // 按钮模式:一轮答完不再立刻收线——保持连接继续听,空闲满 5 分钟才由
    // omniReadStallCheck 里的空闲收线逻辑断开回按钮待机(B 方案,2026-07-15)。
  }

  // Omni 朗读看门狗:read.create 已发/朗读进行中,但连续 12 秒没有任何 response.*
  // 事件(2026-07-10 真机:朗读 response 已创建后 Omni 彻底哑火,队列里 2 句永远
  // 没念,通话看起来"没回答")——主动 cancel 掉这个哑火的 response,放行队列。
  function omniReadStallCheck(){
    // 3s 一次的周期性秒杀也检查麦克风健康——设备切换不一定触发 onended/mute,
    // 比如 iOS 在后台杀音频会话后,轨道可能静默 ended 而不发事件。
    if(handsFreeActive){
      // AudioContext 卡在挂起态的自愈(2026-08-24):没有 visibilitychange 事件的
      // 场景(来电打断、控制中心、部分安卓浏览器直接冻结页面而不派发事件)也要
      // 兜住——ctx 不 running 时上面两个自检全都是空转,连续 5 次(约 15s)拉不
      // 回来就当作真中断,交给探活流程判死重连。
      if(audioCtx && audioCtx.state !== "running"){
        ensureAudioCtxRunning("watchdog");
        if(++_ctxStuckTicks >= 5){
          _ctxStuckTicks = 0;
          vdbg("watchdog.ctx-stuck", audioCtx.state);
          resumeAudioPipeline("watchdog");
        }
      } else {
        _ctxStuckTicks = 0;
      }
      if(!micStreamHealthy()) suspectMicProblem("stale");
      checkMicAudioActivity();
    }
    // 即使在聆听/空闲阶段也检查 WebRTC 连接健康——静默断连(如 120 分钟
    // 会话上限到点)若不及时发现,用户说话永远没回复,表现像 AI 掉线。
    // 原代码仅朗读中做 DC/PC 检查,聆听期静默断连无感知。
    if(handsFreeActive && omniPc){
      const pcSt = omniPc.connectionState;
      if(pcSt === "failed" || pcSt === "closed"){
        vdbg("watchdog.pc-down", pcSt);
        scheduleOmniReconnect("watchdog:"+pcSt, 500);
        return;
      }
      if(pcSt === "disconnected" && (!omniDc || omniDc.readyState !== "open")){
        vdbg("watchdog.pc-disconnected");
        scheduleOmniReconnect("watchdog:disconnected", 2000);
        return;
      }
    }
    // ── 按钮模式空闲收线:挂在这个 3s 看门狗上,正常收尾/出错回落/手动打断/
    // 回声丢弃等所有回到 idle 的路径统一覆盖,不用逐处埋定时器。任何活动
    // (说话/思考/朗读/队列未清)都把计时清零,从头再数 5 分钟。
    if(buttonMode && handsFreeActive){
      const busyNow = wsState !== "idle" || omniReadActive || omniReadPending > 0 || omniReadQueue.length > 0;
      if(busyNow){ omniIdleSince = 0; }
      else if(!omniIdleSince){ omniIdleSince = Date.now(); }
      else if(Date.now() - omniIdleSince >= BUTTON_IDLE_HANG_MS){
        vdbg("button-mode.idle-hang", {idleMs: Date.now() - omniIdleSince});
        returnToButtonState();
        return;
      }
    } else {
      omniIdleSince = 0;
    }
    if(!omniDc || omniDc.readyState !== "open") return;
    // 连接活跃心跳:如果超过心跳间隔没收到任何 DC 事件,主动发一个 cancel(无害的
    // 空操作)验证通道是否还活着——dc.send 静默失败就触发重连。
    // 朗读/播音进行中不探测:①长句 RTP 还在播时 DC 本来就可能安静半分钟,不是断线
    // (2026-07-15 真机:一通电话里误报 7 次 heartbeat:silent,把重连退避计数器灌满);
    // ②cancel 探针会把正在进行的朗读 response 真的取消掉,不是"无害空操作"。
    // 朗读中真断线由上面的 pc.state 检查和 12s 朗读哑火看门狗兜底。
    if(handsFreeActive && omniPc && omniPc.connectionState === "connected"
       && !omniReadActive && omniReadPending === 0 && !omniAutoActive && wsState !== "speaking"
       && Date.now() - omniLastDcEvent > OMNI_HEARTBEAT_MS * 2){
      try{
        omniDc.send(JSON.stringify({type: "response.cancel"}));
        if(Date.now() - omniLastDcEvent > OMNI_HEARTBEAT_MS * 3){
          vdbg("heartbeat.no-echo", {lastDc: Date.now() - omniLastDcEvent});
          scheduleOmniReconnect("heartbeat:silent", 1500);
        }
      }catch(e){
        vdbg("heartbeat.send-fail", String(e));
        scheduleOmniReconnect("heartbeat:send-fail", 1500);
      }
    }
    // ── capturing 卡死检测(2026-07-15 加):用户长录音时不设硬超时断录音,
    // 但状态卡住(如 VAD 错误触发 speech_started 后永远不 committed)时自动重置。
    // 判据:capturing 超过 30 秒 + 最后一条 DC 事件超过 20 秒前 = 卡死
    if(wsState === "capturing" && omniCapturingSince > 0){
      const now = Date.now();
      if(now - omniCapturingSince > OMNI_CAPTURING_STALL_MS && now - omniLastDcEvent > 20000){
        vdbg("capturing.stall-reset", {since: now - omniCapturingSince, lastDc: now - omniLastDcEvent});
        wsState = "idle";
        setStatus(STATE_WORD.idle);
        if(handsFreeActive) orbWrap.className = "idle";
        omniCapturingSince = 0;
      }
      return;
    }
    if(!(omniReadActive || omniReadPending > 0)) return;
    if(Date.now() - omniReadSince <= 12000) return;
    vdbg("read.stall-cleared", {ra: omniReadActive, rp: omniReadPending, q: omniReadQueue.length});
    if(omniReadTailTimer){ clearTimeout(omniReadTailTimer); omniReadTailTimer = null; }
    try{ omniDc.send(JSON.stringify({type: "response.cancel"})); }catch(e){}
    omniReadActive = false; omniReadPending = 0;
    // 哑火朗读已被判死,输出通道收回静音(2026-08-10 反转策略)——不静音的话
    // 元素保持上一句朗读的开声状态,迟到的自动回复音频会趁虚播出。
    setOmniAudioMuted(true, "read-stall");
    pumpOmniRead();
  }

  // ── 尾音排空监听:等 AI 真的把最后几个字播完再静音 ─────────────────────
  // 远端输出电平(0~1 峰值);analyser 没接上返回 -1(tap 失败/audioCtx 没起来)。
  let omniOutLevelBuf = null;
  function omniOutputLevel(){
    if(!outputAnalyser || !omniOutTap) return -1;
    if(!omniOutLevelBuf || omniOutLevelBuf.length !== outputAnalyser.fftSize)
      omniOutLevelBuf = new Uint8Array(outputAnalyser.fftSize);
    outputAnalyser.getByteTimeDomainData(omniOutLevelBuf);
    let peak = 0;
    for(let i = 0; i < omniOutLevelBuf.length; i++){
      const d = Math.abs(omniOutLevelBuf[i] - 128);
      if(d > peak) peak = d;
    }
    return peak / 128;
  }

  // response.done 后每 150ms 采一次输出波形:听到过声音、又连续 ~600ms 安静
  // =尾音真播完了,这时才静音。
  // 但"安静"单独不可信:背靠背泵句时,最后一句 response.done 到达的瞬间,音轨上
  // 可能还欠着「前一句的尾巴 + 这一整句」——两个 response 之间有 ~0.5s 的天然
  // 空档,会被当成"全部播完"(2026-07-13 真机:38 字整句播进了已静音的 <audio>,
  // 用户一个字没听到)。所以再加一个跨句模型下限:把 omniRecentReads 里的句子
  // 按顺序播放模型(启动 700ms + 每字 280ms,句间取 max 衔接,同 maybeOmniIdle)
  // 估出总播放终点,模型说"不可能播完"之前,再安静也不静音。
  // analyser 全程无信号(iOS 把 ctx 挂起/远端流进 WebAudio 的兼容问题)则由
  // deadline 兜底:模型终点 + 3s 强制静音。
  function armOmniReadTailWatch(){
    if(omniReadTailTimer) clearTimeout(omniReadTailTimer);
    let end = 0;
    for(const r of omniRecentReads){
      const start = Math.max(end, r.ts + 700);
      end = start + normEchoText(r.text).length * 280;
    }
    // 安全余量从 300ms 放大到 2000ms,自然停顿(逗号/换气)约 500~800ms,
    // 原 600ms(quiet>=4)易在最后一句播放中途误判静默结束。同时提高
    // 静默连续检测门槛从 4→8,给正常语流停顿留空间。
    const minMute = end + 2000;
    const deadline = Math.max(Date.now() + 3000, end + 5000);
    let seen = false, quiet = 0;
    const check = () => {
      omniReadTailTimer = null;
      // 新朗读已接管(泵了下一句或新 SSE 填充),开声状态归它管,监听退场
      if(omniReadActive || omniReadPending > 0 || omniReadQueue.length > 0) return;
      const lvl = omniOutputLevel();
      if(lvl > 0.04){ seen = true; quiet = 0; }
      else if(seen) quiet++;
      if((seen && quiet >= 4 && Date.now() >= minMute) || Date.now() >= deadline){
        pumpOmniRead();  // 音频播完→泵下一句或 maybeOmniIdle 收尾;不静音输出通道
        return;
      }
      omniReadTailTimer = setTimeout(check, 150);
    };
    omniReadTailTimer = setTimeout(check, 150);
  }

  // 出错/拒答时让 Omni 把提示念出来——通话中用户不看屏幕,静默失败=「没反应」
  // (2026-07-10 真机:一轮起步就异常,前端只画了个红气泡,用户以为整个系统死了)。
  function speakOmniNotice(text){
    if(!omniDc || omniDc.readyState !== "open") return;
    omniReadQueue.push(text);
    pumpOmniRead();
  }

  function cancelOmniReading(reason){
    if(omniReadTailTimer){ clearTimeout(omniReadTailTimer); omniReadTailTimer = null; }
    omniReadQueue = [];
    if((omniReadActive || omniReadPending > 0 || omniAutoActive) && omniDc && omniDc.readyState === "open"){
      try{ omniDc.send(JSON.stringify({type: "response.cancel"})); }catch(e){}
    }
    // 立即静音:防止 Omni 自动生成"嗯"的音频在 cancel 到达前漏出(2026-07-15 修复)
    setOmniAudioMuted(true, "cancel");
    try{ omniAudioEl.muted = true; }catch(e){}
    omniReadActive = false; omniReadPending = 0;
    omniReadCurrentText = "";
    // 打断的朗读念了一半,拿它去比对必然对不上——直接清场,不留给 response.done
    // 的兜底校验误报成"念错了"。
    omniReadRespId = null; omniReadExpectText = ""; omniSpokenBuf = ""; omniSpokenChecked = true;
    vdbg("read.cancel", reason);
  }

  // 恢复 barge-in 现场(echo.discard 判回声后 / 幽灵打断兜底共用):回退 gen 让
  // 还在飞的 /voice/send 流(若没被砍到)继续生效,被吞的句子塞回队列。不立刻
  // pump——阿里云对这条"用户语音"还会自作主张生成一个 response(create_response
  // 关不掉),马上 pump 会跟它的 response.created 撞车导致 rp 计数错位;等它被
  // cancel 后的 response.done 自然接力 pump,再留一个定时器兜底。
  function restoreOmniBargeIn(restore, tag){
    // 重新武装自动回复保险丝(2026-08-08):这里原来直接 omniAutoActive = false
    // 放行朗读,但这条"用户语音"(哪怕判了回声)在服务端照样触发自动回复生成,
    // 其 response.created 若晚于 1200ms 恢复定时器到达,会误吃 omniReadPending
    // 计数假扮成朗读开声(漏音)。武装后 pump 被挡到自动回复 done 清场;done
    // 万一不来,armOmniAutoFuse 的超时兜底会主动解除并 pump,不会卡死。
    armOmniAutoFuse();
    omniTurnGen = restore.prevGen;
    if(restore.sentences.length) omniReadQueue = restore.sentences.concat(omniReadQueue);
    vdbg(tag, {q: omniReadQueue.length});
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
          vdbg(tag + ".idle-fallback");
          maybeOmniIdle();
        }
      }, 10000);
    }
  }

  // 幽灵打断兜底(2026-07-12 实锤 13:37:29:speech_started→barge-in 砍掉朗读,
  // 之后只有 speech_stopped,transcription.completed 永远没来——大概率是无法
  // 转写的回声/噪音):恢复逻辑只挂在 .completed 分支的话,被砍的剩余句子就永久
  // 丢了,用户听不到回答后半段。快照存下 8 秒内没被任何转写结果消费,就视为
  // 幽灵打断,自动恢复朗读现场;转写 delta 每次到达都重新武装,给真实长句让路。
  function armOmniGhostTimer(){
    if(omniEchoRestoreTimer) clearTimeout(omniEchoRestoreTimer);
    const snap = omniEchoRestore;
    if(!snap) return;
    omniEchoRestoreTimer = setTimeout(() => {
      omniEchoRestoreTimer = null;
      if(omniEchoRestore !== snap || omniTurnGen !== snap.newGen) return;
      omniEchoRestore = null;
      // delta 到过但 completed 没来的半截字幕气泡一并撤掉
      if(omniUserLiveEl){
        const row = omniUserLiveEl.closest(".row");
        if(row) row.remove();
        omniUserLiveEl = null;
      }
      restoreOmniBargeIn(snap, "echo.ghost-restore");
    }, 8000);
  }

  // ── 回声过滤(2026-07-11 实锤,详见 memory/hermes-voice-omni-self-echo-rootcause)──
  // iOS 的回声消除是自适应滤波器,AI 每次开口后要 1~2 秒才收敛,这段"开口瞬间"
  // 的残留回声会漏进上行;阿里云只在 response 生成期间压 VAD,而生成比播放快,
  // 播放尾巴不设防。每次重连(重启后尤甚)滤波器都从零收敛,第一轮回复漏得最凶,
  // 且漏出的片段可以是句子【任意连续部位】,不只开头(2026-07-12 全日志 12 条
  // 漏网实锤:"来叫你"=「好了叫你」句尾误转写、"做一个"=句尾、"去查"=中段)。
  // 两层判据:① 容错前缀,不需要现场证据(保守,老逻辑);② 容错子串,要求存在
  // "回声上下文"——barge-in 现场,或开口时刻落在句子的估算播放期内(补状态机
  // 已回 idle 但音频还在播的尾巴缺口)。用户听完 AI 说完再复述句尾回答
  // ("帮我装一个试试")开口在播放期之后,不会误杀。误吞由恢复朗读机制自愈。
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
  // needle 相对 hay 任意连续片段的最小编辑距离(DP 首行置 0=起点任选):
  // 抓"回声是句子中段/尾部"的场景,普通 editDist 只能比整串。
  function fuzzySubstrDist(needle, hay){
    const n = needle.length, m = hay.length;
    let prev = new Array(m + 1).fill(0);
    for(let i = 1; i <= n; i++){
      const cur = [i];
      for(let j = 1; j <= m; j++){
        cur[j] = Math.min(prev[j] + 1, cur[j-1] + 1, prev[j-1] + (needle[i-1] === hay[j-1] ? 0 : 1));
      }
      prev = cur;
    }
    return Math.min(...prev);
  }
  function matchOmniEcho(transcript, hasBargeIn){
    const t = normEchoText(transcript);
    if(t.length < 2) return null;  // 单字没法归因("嗯"),放行
    const now = Date.now();
    omniRecentReads = omniRecentReads.filter(r => now - r.ts < 45000);  // 窗口含「生成→播完」全链路,别再按 read.create 砍断(2026-07-23)
    const tol = t.length < 5 ? 0 : (t.length < 10 ? 1 : 2);
    for(const r of omniRecentReads){
      const cand = normEchoText(r.text);
      if(cand.length < t.length) continue;
      // 判据①容错前缀:转写长度上下浮动 2 字取最小编辑距离(误转写/多听漏听一个字)
      if(t.length >= 3){
        for(let L = Math.max(3, t.length - 1); L <= Math.min(cand.length, t.length + 2); L++){
          if(editDist(t, cand.slice(0, L)) <= tol) return r.text;
        }
      }
      // 判据②容错子串:回声上下文 = 有 barge-in 现场,或用户【开口时刻】落在该句
      // 的估算播放期内(念速约 4 字/秒 → 250ms/字,叠加出声延迟+队列积压给 3s 余量)。
      // 后者补 idle 尾巴缺口——21:48 实锤「放笔记的任务」:长句文字已生成完、状态机
      // 回了 idle、音频还在播,回声到达时无快照,只看快照就漏。用真实开口时刻
      // (speech_started)而非转写完成时刻,能把"AI 音频未停就被录进的回声"和
      // "用户听完后才复述句尾的真实回答"区分开(回放实锤:同一个"你好"字面,用户
      // 真说的在播放期外放行,5 秒后的回声版在播放期内拦下)。
      // 2 字词只认精确子串——编辑距离 1 会命中任何共享一个字的窗口,太松。
      const age = now - r.ts;
      const onset = omniSpeechStartTs || now;
      const inTail = onset - r.ts < cand.length * 250 + 3000;
      if((hasBargeIn || inTail) && age < (t.length < 3 ? 8000 : 20000)){
        const subTol = t.length < 3 ? 0 : (t.length < 6 ? 1 : 2);
        if(fuzzySubstrDist(t, cand) <= subTol){
          vdbg("echo.substr-hit", {heard: t.slice(0, 24), age: Math.round(age / 100) / 10, tail: inTail, barge: hasBargeIn});
          return r.text;
        }
      }
      // 子串命中但没走到判据②(无现场/太久):不归因,只上报攒数据(idle 尾巴缺口)
      if(cand.includes(t)) vdbg("echo.suspect", {heard: t.slice(0, 24), read: r.text.slice(0, 24)});
    }
    return null;
  }

  // 转写完成(或 delta 兜底顶上)后的统一入口:回声判定→字幕定稿→进拆句缓冲。
  // 原本是 .completed 分支的内联逻辑,2026-07-13 抽出来让兜底路径走同一条链。
  function handleUserTranscript(transcript){
    // 一段在途转写被消费(completed 或 delta 兜底都汇到这里),计数递减——
    // 归零且用户没在说话时,scheduleOmniFlush 才允许真正 flush。
    if(omniInflightSegs > 0) omniInflightSegs--;
    const restore = omniEchoRestore; omniEchoRestore = null;
    if(omniEchoRestoreTimer){ clearTimeout(omniEchoRestoreTimer); omniEchoRestoreTimer = null; }
    const echoSrc = transcript ? matchOmniEcho(transcript, !!restore) : null;
    if(echoSrc){
      omniAutoActive = false;  // 回声不可能有真正的自动回复,清掉防护(2026-07-14)
      // 是 AI 自己的回声:丢弃,不发 Claude、不算一轮对话,字幕气泡撤掉
      vdbg("echo.discard", {heard: transcript.slice(0, 24), read: echoSrc.slice(0, 24)});
      if(omniUserLiveEl){
        const row = omniUserLiveEl.closest(".row");
        if(row) row.remove();
        omniUserLiveEl = null;
      }
      if(restore && omniTurnGen === restore.newGen && Date.now() - restore.ts < 15000){
        restoreOmniBargeIn(restore, "echo.restore");
      } else {
        // 没有可恢复的现场(回声出现在整轮念完之后):回到待命即可
        wsState = "thinking";  // 先离开 capturing,maybeOmniIdle 才肯接手收尾
        maybeOmniIdle();
        if(wsState !== "idle"){ wsState = "idle"; setStatus(STATE_WORD.idle); if(handsFreeActive) orbWrap.className = "idle"; }
      }
      // 这段被判回声丢弃,但缓冲里可能还压着真话——按当前状态重排倒计时,别让它烂在安全网里
      scheduleOmniFlush("echo-discard");
      return;
    }
    if(!transcript){
      if(omniUserLiveEl) renderAi(omniUserLiveEl, "(没听清)");
      omniUserLiveEl = null;
      scheduleOmniFlush("empty-transcript");
      return;
    }
    // 气泡归并:本段若是上一段的续写(缓冲里还有没发的,或刚发出去且那轮还没答完
    // ——后端 _prev_text 的 3s 合并窗口内),并进原气泡,不另起一个。
    const mergeable = omniUserBubble && omniUserBubble.isConnected
      && (omniPendingText || (!omniTurnDone && omniUserSentTs && Date.now() - omniUserSentTs < 3000));
    if(mergeable){
      if(omniUserLiveEl){
        const row = omniUserLiveEl.closest(".row");
        if(row) row.remove();
        omniUserLiveEl = null;
      }
      omniUserBubbleText += transcript;
      renderAi(omniUserBubble, omniUserBubbleText);
      vdbg("bubble.merge", {len: omniUserBubbleText.length});
    } else {
      if(omniUserLiveEl){ renderAi(omniUserLiveEl, transcript); omniUserBubble = omniUserLiveEl; }
      else omniUserBubble = addMsg("me", transcript);
      omniUserBubbleText = transcript;
      omniUserSentTs = 0;
    }
    omniUserLiveEl = null;
    queueOmniTurn(transcript);
  }

  // committed 后 completed 迟迟不来的兜底(根因见 omniDeltaFallback 注释)。
  // 定时器闭包里捕获当下这句的快照——1.5s 里就算用户又开口、delta 缓存被下一句
  // 覆盖,到点发的仍是本句文本,不会串句。
  const OMNI_TRANSCRIPT_DONE_TIMEOUT_MS = 1500;
  function armOmniDeltaFallback(){
    if(omniDeltaFallbackTimer) clearTimeout(omniDeltaFallbackTimer);
    const snap = omniDeltaFallback;
    omniDeltaFallbackTimer = setTimeout(() => {
      omniDeltaFallbackTimer = null;
      if(!snap || !snap.text){
        // 幽灵段(2026-07-22 真机 13:03 实锤):回声/噪音触发了 committed,但连
        // delta 都转写不出——completed 永远不来,handleUserTranscript 不会被调,
        // omniInflightSegs 就地泄漏。泄漏 1 之后,后面每句真话都误判"转写在途"
        // 走 30s 安全网,期间连接一断缓冲被清,用户的问题人间蒸发。这里把死段
        // 核销掉,并按当前状态重排 flush。
        if(omniInflightSegs > 0){
          omniInflightSegs--;
          vdbg("inflight.dead-segment", {inflight: omniInflightSegs});
          scheduleOmniFlush("dead-segment");
        }
        return;
      }
      if(!handsFreeActive) return;                       // 通话已挂断,别往外发
      omniFallbackSentItem = snap.itemId;                // completed 迟到时据此防重发
      if(omniDeltaFallback && omniDeltaFallback.itemId === snap.itemId) omniDeltaFallback = null;
      vdbg("transcript.fallback", snap.text.slice(0, 40));
      handleUserTranscript(snap.text);
    }, OMNI_TRANSCRIPT_DONE_TIMEOUT_MS);
  }

  // 拆句缓冲的入口:转写完成不立刻发,累进 omniPendingText,直接拼接不加分隔符。
  // flush 时机统一由 scheduleOmniFlush 按状态决定(2026-07-22 重构,事故根因见
  // omniSpeechActive 声明处注释):
  //   · 用户还在说话(omniSpeechActive)或有转写在途(omniInflightSegs>0)
  //     → 只挂安全网(S.omniSafetyMs,默认 180s),绝不短定时器误发前半句;
  //   · 都到齐了 → OMNI_PENDING_FLUSH_MS(600ms)短等待后发出。
  // 长语音各段可能间隔 >10s(Omni 服务端 force-commit 最大音频缓冲区),
  // 中间态一律走安全网,由下一段的转写到达来推进。
  function queueOmniTurn(text){
    omniPendingText = (omniPendingText || "") + text;
    scheduleOmniFlush("queue");
  }
  function scheduleOmniFlush(why){
    if(!omniPendingText) return;
    if(omniPendingTimer) clearTimeout(omniPendingTimer);
    const waiting = omniSpeechActive || omniInflightSegs > 0;
    // 冷却期:刚 speech_stopped 不久,用户可能还在继续说(阿里云 semantic_vad 正在
    // 重新检测下一段的边界)。这时不启用 600ms 短定时器——下段 speech_started 收到
    // 前,给足时间窗口让缓冲继续累积,避免连续长语音被提早腰斩成多段发出去。
    // 冷却期内用中位定时器(2000ms),过了冷却期才切短定时器(600ms)。
    // 动态冷却(2026-07-31):固定 2s 冷却对真人说长句时的换气/思考停顿太短,超过
    // 就被切成两轮独立发给 Claude(后端 _prev_text 兜底指望不上——它的 3s 窗口从
    // "前段启动时刻"起算、且轮次干净收尾会清空,历史累计触发 0 次)。但一刀切调长
    // 会让"今天天气怎么样?"这种明显说完的短问句也白等一截。所以按刚攒下的这段
    // 像不像话说完了来定:碎片(≤4 字,如 semantic_vad 切出来的"目前。")或者结尾
    // 没有句末标点 → 多等 OMNI_PENDING_LONG_COOLDOWN_MS;像完整句子就维持原节奏。
    const tail = (omniPendingText || "").trim();
    const looksIncomplete = tail.length <= 4 || !/[。？?！!]$/.test(tail);
    const cooldownMs = looksIncomplete ? OMNI_PENDING_LONG_COOLDOWN_MS : OMNI_PENDING_POST_SPEECH_MS;
    const cooldown = !waiting && omniSpeechStoppedTs && Date.now() - omniSpeechStoppedTs < cooldownMs;
    const delay = waiting ? S.omniSafetyMs : cooldown ? cooldownMs : OMNI_PENDING_FLUSH_MS;
    omniPendingTimer = setTimeout(flushOmniPending, delay);
    vdbg("pending.arm", {why, delay, cooldown, incomplete: looksIncomplete,
      inflight: omniInflightSegs, speaking: omniSpeechActive});
  }
  function flushOmniPending(){
    omniPendingTimer = null;
    // 安全网到点强发(转写丢了/VAD 没报 stopped):在途计数一并清零自愈,
    // 防止计数泄漏后所有后续缓冲永远只挂安全网。
    omniInflightSegs = 0;
    const text = omniPendingText; omniPendingText = null;
    if(text){
      omniUserSentTs = Date.now();  // 气泡归并的后端合并窗口从发出时刻起算
      sendOmniTurn(text);
    }
  }

  // 识别到一整句话后,转发给现有 /voice/send——跟文字聊天同一套 Claude 全套
  // 工具/画像/记忆,回答由 Claude 生成,不是 Omni 自己瞎编。tts:false 让服务端
  // 只发句子文本不合成音频,句子进 omniReadQueue 交给 Omni 念(见 pumpOmniRead)。
  // gen 机制:进来时记一个代号,期间被打断(omniTurnGen 变了)就不再把迟到的
  // 流式结果应用到界面/朗读队列上。
  async function sendOmniTurn(text){
    const turn = beginVoiceTurn();
    const gen = ++omniTurnGen;
    omniCapturingSince = 0;  // 离开 capturing 状态,看门狗不再盯
    wsState = "thinking";
    setStatus(STATE_WORD.thinking, STATE_CLASS.thinking);
    if(handsFreeActive) orbWrap.className = "thinking";
    // 上一轮的回答被这轮抢占、话说到一半:气泡补个被打断标记,跟落库文案对齐
    // ——否则界面上回答戛然而止,看着像系统坏了(2026-07-23 截图反馈)。
    if(currentTurnAiEl && currentTurnFull && !omniTurnDone){
      renderAi(currentTurnAiEl, currentTurnFull + " …(被打断)");
    }
    currentTurnAiEl = null; currentTurnFull = "";
    endActivity();
    // 新轮开始:清掉自动回复防护和上一轮残留的朗读状态(2026-07-14 修复)
    // speech_started 被 VAD 漏报时(见 committed 的第二道闸),上一轮的老句子
    // 可能还在泵朗读(omniReadPending > 0),auto-reply 的 response.created
    // 会错吃计数器假扮成朗读并开声——cancel 砍掉残留读取,清防护闸让新轮朗读
    // 不会被卡住。
    // 2026-07-23 补:omniAutoActive 也要进 cancel 条件——只清客户端标志
    // 不 cancel 服务端的话,auto-reply 的 response 在第一句朗读替换后会在
    // response.done 后复活(它的 conversation item 还在队列里),跟第二句的
    // response.create 竞争,服务端报"already has an active response"拒掉,
    // 导致多句回复只念第一句。见 data/logs/vococo.out.log 14:17:49.
    if(omniDc && (omniReadActive || omniReadPending > 0 || omniReadQueue.length > 0 || omniAutoActive)){
      try{ omniDc.send(JSON.stringify({type: "response.cancel"})); }catch(e){}
    }
    // 重新武装自动回复保险丝(2026-08-08):这句用户语音在服务端触发的自动回复
    // 此刻大概率还在途(created/done 未回)——这里原来直接清 omniAutoActive,
    // SSE 句子一到就 pump 抢先 response.create,迟到的自动回复 created 会误吃
    // 计数器假扮成朗读开声(漏音)。武装后:自动回复 done 自然清场(cancel 已发,
    // 正常 1~3 秒),万一 done 丢了由保险丝超时兜底解除,不会卡死朗读。
    armOmniAutoFuse();
    omniReadActive = false;
    omniReadPending = 0;
    omniReadQueue = []; omniTurnDone = false;
    // 【2026-07-23 实锤 13:14】新轮开场必须明确"干净状态":上一轮未播放完的句子
    // 已被丢弃,但它们还在 omniRecentReads 的 30s 窗口里占着数——回声过滤的播放期
    // 估算(inTail)按这句的 read.create 起算,会把未来好几分钟都算成"播放期",
    // 期间用户说的任何话只要撞上句面就被误杀。这些句子今生不会有 RTP,必须赶出
    // 窗口,让剩余句子的时长估算回归真实。
    omniRecentReads = omniRecentReads.filter(r => r.ts + 8000 >= Date.now());
    omniMicEpoch++;  // 朗读现场换新,旧现场注册的开麦定时器一律作废(见 applyMicEnabled)
    applyMicEnabled();  // 若麦克风还关着(首回复静音未消耗),上一轮的静音估算不再适用,立即开麦
    vdbg("turn.start", text.slice(0, 40));
    // 音效是锦上添花,不是核心链路——2026-07-13 playTone 漏参数的 bug 曾经因为
    // 这两行没包 try/catch,直接把整轮请求(下面的 fetch)炸没了,表现成"识别到
    // 文字、思考动效转了,但永远没回复"。音效以后再出岔子也不能拖垮真正的回复。
    try{ playSendTone(); playThinkingTone(); }catch(e){ vdbg("turn.tone-error", String(e)); }

    const toIdle = () => {
      wsState = "idle"; setStatus(STATE_WORD.idle);
      if(handsFreeActive) orbWrap.className = "idle";
    };

    let resp;
    try{
      resp = await fetch("/voice/send", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-Auth-Token": S.token, "X-Voice-Turn-Id": turn.id},
        body: JSON.stringify({text, tts: false}),
        signal: turn.controller.signal,
      });
    }catch(e){
      if(gen === omniTurnGen && isActiveVoiceTurn(turn)){ addMsg("ai error", "连不上服务"); speakOmniNotice("现在连不上服务,稍等再说一次试试。"); omniTurnDone = true; toIdle(); }
      finishVoiceTurn(turn);
      return;
    }
    if(gen !== omniTurnGen || !isActiveVoiceTurn(turn)) return;  // 已经被打断,后面的响应不用管了
    if(!resp.ok || !resp.body){
      addMsg("ai error", resp.status === 409 ? "上一轮还没说完" : "出错了");
      speakOmniNotice(resp.status === 409 ? "上一句还在处理,稍等一下。" : "刚才那句出错了,再说一遍试试。");
      omniTurnDone = true;
      toIdle();
      finishVoiceTurn(turn);
      return;
    }

    const reader = resp.body.getReader(), dec = new TextDecoder();
    let buf = "";
    try{
      while(true){
        const {value, done} = await reader.read();
        if(gen !== omniTurnGen || !isActiveVoiceTurn(turn)){ try{ reader.cancel(); }catch(e){} return; }
        if(done) break;
        buf += dec.decode(value, {stream:true});
        let sep;
        while((sep = buf.indexOf("\n\n")) >= 0){
          const block = buf.slice(0, sep); buf = buf.slice(sep+2);
          const ev = parseSseBlock(block);
          if(!ev || gen !== omniTurnGen || !isActiveVoiceTurn(turn)) continue;
          if(ev.event === "text"){
            stopWorkSound();  // AI 开口了,工作音效退场
            if(!currentTurnAiEl) currentTurnAiEl = addMsg("ai", "");
            currentTurnFull += ev.data.text || "";
            renderAi(currentTurnAiEl, currentTurnFull);
            // 状态保持 thinking:真正的"工作中"由 Omni 朗读的 response.created
            // 驱动(见 wireOmniDataChannel),文字流出来不等于已经在出声。
          } else if(ev.event === "activity"){
            showActivity(ev.data.text);
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
    if(gen !== omniTurnGen || !isActiveVoiceTurn(turn)) return;
    currentTurnAiEl = null; currentTurnFull = "";
    endActivity();
    omniTurnDone = true;
    vdbg("turn.stream-done", {q: omniReadQueue.length});
    // 不直接回 idle:朗读队列可能还有没念完的,由 maybeOmniIdle 在念完后收尾。
    maybeOmniIdle();
    pumpOmniRead();
    finishVoiceTurn(turn);
  }

  function stopOmniHandsFree(){
    stopThinkingTone();
    omniTurnGen++;  // 让还在飞的 sendOmniTurn 流式结果失效
    cancelActiveVoiceTurn("hangup");
    // 先摘 handler 再 close:close 本身会触发 dc.onclose/pc.onconnectionstatechange,
    // 不摘的话主动关闭也会被当成"断线"排一次重连。
    if(omniDc){ try{ omniDc.onclose = null; omniDc.close(); }catch(e){} omniDc = null; }
    if(omniPc){ try{ omniPc.onconnectionstatechange = null; omniPc.ontrack = null; omniPc.close(); }catch(e){} omniPc = null; }
    if(omniMicMuteTimer){ clearTimeout(omniMicMuteTimer); omniMicMuteTimer = null; }
    if(omniReadTailTimer){ clearTimeout(omniReadTailTimer); omniReadTailTimer = null; }
    omniMicMuted = false; omniFirstReplyMuteUsed = false;
    applyMicEnabled();
    omniMicStream = null;  // 麦克风轨道本身随 stream 变量一起在 teardownCallResources 里停
    omniUserLiveEl = null;
    omniReadQueue = []; omniReadActive = false; omniReadPending = 0; omniAutoActive = false; omniTurnDone = true;
    clearOmniAutoFuse();  // 保险丝定时器一并清掉,别在挂断后还来 pump
    omniReadSince = 0;
    omniRecentReads = []; omniReadCurrentText = ""; omniEchoRestore = null;
    if(omniEchoRestoreTimer){ clearTimeout(omniEchoRestoreTimer); omniEchoRestoreTimer = null; }
    if(omniDeltaFallbackTimer){ clearTimeout(omniDeltaFallbackTimer); omniDeltaFallbackTimer = null; }
    omniDeltaFallback = null; omniFallbackSentItem = null;
    if(omniPendingTimer){ clearTimeout(omniPendingTimer); omniPendingTimer = null; }
    omniPendingText = null;
    omniSpeechActive = false; omniInflightSegs = 0; omniSpeechStoppedTs = 0;
    omniUserBubble = null; omniUserBubbleText = ""; omniUserSentTs = 0;
    if(omniReadWatchdog){ clearInterval(omniReadWatchdog); omniReadWatchdog = null; }
    // 复活探针作废:连线已经拆了,再让它到点去判"麦克风死了"会顶掉新建的连接
    if(resumeProbeTimer){ clearTimeout(resumeProbeTimer); resumeProbeTimer = null; }
    _ctxStuckTicks = 0;
    // 【只在真挂断时才收保活】(2026-08-24):stopOmniHandsFree 同时被重连/切音色
    // 复用,那两条路径 handsFreeActive 仍为 true。原来无条件停保活,等于在重连的
    // 几秒空窗里把"页面别被冻结"的唯一依靠拆了——锁屏状态下正好卡在这个窗口,
    // 页面直接冻住,重连永远跑不完,表现就是"息屏后再也接不回来"。
    if(!handsFreeActive){
      stopKeepAliveAudio();  // 通话已收线,保活音轨一并停掉(锁屏防挂起)
      stopKeepAliveVideo();  // 通话已结束,防熄屏视频一并停掉
      releaseWakeLock();     // 通话已结束,释放常亮锁让系统正常熄屏节能
    }
    if(omniAudioEl){ try{ omniAudioEl.srcObject = null; }catch(e){} }
    setOmniAudioMuted(true, "hangup");  // 挂断收口:输出通道回默认静音态
  }

  // ── 按钮模式:一轮交互完成后回到按钮待机状态,等待用户再次点击 ──────────────
  function returnToButtonState(){
    vdbg("button-mode.return");
    omniIdleSince = 0;
    if(omniReconnectTimer){ clearTimeout(omniReconnectTimer); omniReconnectTimer = null; }
    stopOmniHandsFree();
    resetMicStream();  // 停掉麦克风,避免 LED 常亮
    handsFreeActive = false;
    // 真收线:保活媒体与常亮锁由本路径自己收(stopOmniHandsFree 里那段现在
    // 只认 handsFreeActive=false,而它被调用时这里还没置 false)。
    stopKeepAliveAudio();
    stopKeepAliveVideo();
    releaseWakeLock();
    handsFreeUi.hidden = true;
    startBtn.hidden = false;
    startBtn.title = "通话"; startBtn.setAttribute("aria-label", "通话");
    startBtn.disabled = false;
    setOmniConnStatus("disconnected");
    setStatus("点击按钮通话", "");
    orbWrap.className = "idle";
  }

  // 开始/继续录音按钮:第一次建立 WebRTC 连线,后续轮次重新连接
  startBtn.addEventListener("click", async () => {
    if(startBtn.disabled) return;
    startBtn.disabled = true;
    startBtn.hidden = true;  // 连接期间收起按钮,由声波球的"连接中"形态+状态文字接管
    handsFreeUi.hidden = false;
    setStatus("连接中…", "");
    setOmniConnStatus("connecting");
    // 点击本身就是一次用户手势,这里先解锁 AudioContext 再响连接音——不解锁的话
    // startOmniHandsFree 里的 unlockAudio() 还没跑到,playTone 会因 audioCtx 为空静默不响。
    unlockAudio();
    playDialConnectingTone();
    vdbg("button-mode.start");
    // startHandsFree 内部抛异常(createOffer/setRemoteDescription 等)也要落回
    // 失败分支,否则按钮永远停在 disabled 态,整个通话视图卡死。
    let ok = false;
    try{ ok = await startHandsFree(); }
    catch(e){ vdbg("button-mode.start.fail", String(e)); }
    startBtn.disabled = false;
    // 连接过程中用户可能已退出通话视图,此时不再继续
    if($("#callView").hidden){ return; }
    // 已回退到按住说话模式(Omni 未启用),隐藏按钮让 talkBtn 接管
    if(!talkBtn.hidden){
      handsFreeUi.hidden = true;
      startBtn.hidden = true;
      return;
    }
    if(ok){
      startBtn.hidden = true;
      buttonMode = true;
      playDialConnectedTone();  // 接通瞬间响一声,音高比连接音更高,跟"连接中"区分开
    } else {
      handsFreeUi.hidden = true;
      startBtn.hidden = false;
      // 具体错误原因由 startHandsFree 各失败路径的 addMsg 错误气泡呈现,
      // 按钮本身只留"可以再点一次"的语义
      startBtn.title = "连接失败,点击重试"; startBtn.setAttribute("aria-label", "连接失败,点击重试");
      setOmniConnStatus("disconnected");
      setStatus("连接失败,点击重试", "");
    }
  });

  async function loadMainHistory(){
    try{
      const resp = await fetch("/history?conv=" + encodeURIComponent("voice-chat:main"), {
        headers: {"X-Auth-Token": S.token},
      });
      if(!resp.ok) return;
      const d = await resp.json();
      if(!d.turns || !d.turns.length) return;
      clearHint();
      for(const t of d.turns){
        if(t.user) addMsg("me", t.user);
        if(t.assistant) addMsg("ai", t.assistant);
      }
    }catch(e){ /* 历史加载失败不阻塞进入通话,当作空历史处理 */ }
  }

  // 挂断=直接退出通话视图,回到聊天界面(电话挂断的直觉),不是留在这里等重新开始
  endHandsFreeBtn.addEventListener("click", ()=>{ returnToButtonState(); });  // 挂断但留在语音页面
  orbWrap.addEventListener("click", ()=>{
    if(wsState === "thinking" || wsState === "speaking") manualInterrupt();
  });

  // 静音麦克风:只在客户端拦,不发消息给后端(跟"打断 AI 说话"那个 mute WS 消息
  // 是两回事)——关掉音轨 + 停发 PCM 双保险,图标切换成"已静音"提醒状态。
  muteToggleBtn.addEventListener("click", ()=>{
    micMuted = !micMuted;
    applyMicEnabled();
    muteToggleBtn.classList.toggle("muted", micMuted);
    muteToggleBtn.title = micMuted ? "取消静音" : "静音麦克风";
    setIcon(muteToggleBtn, micMuted ? "mute" : "mic");
  });

  // 清空语音通话上下文:清的是 voice-chat:main(见后端 /voice/clear),下一轮从零开始;
  // 同时抹掉本地已显示的对话与半截的流式态,视觉上也回到干净起点。
  $("#clearVoiceCtxBtn").addEventListener("click", async ()=>{
    if(!confirm("清空主会话的上下文?(AI 会从头开始,历史记录仍保留)")) return;
    try{
      const resp = await fetch("/voice/clear", {method:"POST", headers:{"X-Auth-Token":S.token}});
      if(!resp.ok) throw new Error("HTTP "+resp.status);
      transcriptEl.innerHTML = "";
      currentTurnAiEl = null; currentTurnFull = "";
      addMsg("ai filler", "上下文已清空,我们重新开始吧。");
      // 顶部任务状态条也跟着这次清空同步重置:旧对话遗留的任务不该继续挂在条上
      // (见 voiceTasksClearedAt 声明处的说明)。
      voiceTasksClearedAt = Date.now() / 1000;
      localStorage.setItem("vococo_voice_tasks_cleared_at", String(voiceTasksClearedAt));
      barTasks.clear();
      renderTaskBar();
    }catch(e){ addMsg("ai error", "清空失败:"+e.message); }
  });

  function initUiMode(){
    if(!vadCapable()){
      handsFreeUi.hidden = true;
      talkBtn.hidden = false;
      const hint = document.createElement("div");
      hint.className = "hint";
      hint.textContent = "按住下面的圆钮说话,松手发送";
      transcriptEl.appendChild(hint);
    }
  }

  // ── 屏幕常亮(Screen Wake Lock):通话激活期间用户不碰屏幕,iOS 自动熄屏会
  // 把 PWA 冻结、麦克风和 WebRTC 一起断掉——所以建连成功(通话真正开始)才申请
  // 常亮锁,挂断/空闲收线/退出视图时释放(待机界面不申请,不白耗电)。
  // 与保活音轨分工:wakeLock 管【防自动熄屏】(屏幕不锁,页面不冻结),
  // 保活音轨管【锁屏后的保活】(wakeLock 失效/iOS PWA bug 时锁了屏也不断),
  // 两者并存、互不替代。
  // 锁在页面切后台时会被系统自动收回(规范行为),回到前台得重新申请,
  // 靠下面的 visibilitychange 补(仅通话激活期间)。
  // 兼容性:iOS 16.4+ 支持该 API,不支持的浏览器走 wakelock.unsupported 静默
  // 降级不报错;但"加到主屏幕"的 PWA 里有 WebKit bug(bugs.webkit.org/254545:
  // request 成功 resolve 但屏幕照样熄),iOS 18.4 才修好——失败/不生效无法
  // 从 JS 侧检测,只能靠 vdbg 上报 + 真机验证,所以保活音轨必须一直开着。
  let wakeLockSentinel = null;
  async function acquireWakeLock(){
    if(!("wakeLock" in navigator)){ vdbg("wakelock.unsupported"); return; }
    if(wakeLockSentinel) return;  // 已持有(release 事件会把它清回 null,不会泄漏)
    try{
      wakeLockSentinel = await navigator.wakeLock.request("screen");
      wakeLockSentinel.addEventListener("release", ()=>{ wakeLockSentinel = null; });
      vdbg("wakelock.acquired");
    }catch(e){ wakeLockSentinel = null; vdbg("wakelock.fail", String(e)); }
  }
  function releaseWakeLock(){
    if(!wakeLockSentinel) return;
    try{ wakeLockSentinel.release(); }catch(e){}
    wakeLockSentinel = null;
  }
  document.addEventListener("visibilitychange", ()=>{
    if(document.hidden){
      if(handsFreeActive) vdbg("bg.enter", {ctx: audioCtx && audioCtx.state});
      return;
    }
    if(handsFreeActive) acquireWakeLock();  // 通话激活期间回前台:重拿常亮锁
    // 息屏/切后台回来的完整复活流程(2026-08-24):以前这里只补了 wakeLock 和
    // 防熄屏视频,保活音轨、AudioContext、远端音频元素三样都没人管——见 resumeAudioPipeline。
    if(handsFreeActive) resumeAudioPipeline("vischange");
    // 切回前台时可能刚经历过电话打断/蓝牙断开:怀疑麦克风有问题
    if(handsFreeActive) suspectMicProblem("vischange");
    // P0 修复(2026-07-27):切后台/锁屏期间 SSE 事件可能已经到了但没被处理,
    // 回到前台立刻补播,别等下次交互才想起来。
    flushAnnouncements();
  });

  // ── 通话保活音轨(iOS 锁屏防挂起,2026-08-12 降级方案落地)───────────────
  // wakeLock 在 iOS「加到主屏幕」的 PWA 上有 WebKit bug(254545,iOS 18.4 才修):
  // request 成功 resolve 但屏幕照样熄,且 JS 侧无法检测"拿了锁却没生效"——
  // 所以 wakeLock 只是尽力而为,真机熄屏时通话照样断,本音轨就是兜底。
  // 原理:iOS 对"正在播放音频"的页面按后台音频 App 对待,锁屏后【不冻结】
  // 页面——定时器、SSE fetch 流、WebRTC 全链路继续跑。因此通话期间循环播放
  // 一条【内容本身静音】的 0.25s WAV(不是 muted 属性,音量属性正常,只有 PCM
  // 数据全零),听感为零、不占带宽(内联 data URI),换来锁屏后通话不断。
  // 与 wakeLock 双保险:锁生效时屏幕不锁,这条音轨无感;锁失效(iOS PWA bug)
  // 时它兜住。挂断即停,重连时随 stop/startOmniHandsFree 自然重启。
  // 副作用:锁屏界面可能出现"网页音频"播放条——那是保活还在的标志,不是 bug。
  const KEEP_ALIVE_WAV = "data:audio/wav;base64,UklGRvQHAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YdAHAACAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgA==";
  let keepAliveEl = null;
  function startKeepAliveAudio(){
    if(keepAliveEl){ try{ keepAliveEl.play().catch(()=>{}); }catch(e){} return; }
    const el = document.createElement("audio");
    el.loop = true; el.preload = "auto"; el.setAttribute("playsinline", "");
    el.src = KEEP_ALIVE_WAV;
    el.addEventListener("error", ()=>{ vdbg("keepalive.err"); keepAliveEl = null; });
    // 【必须挂进文档】(2026-08-24):游离(没有父节点)的媒体元素在 iOS WebKit 上
    // 不参与渲染,后台音频会话/防熄屏豁免统统不认它——2026-08-12、08-17 两次保活
    // 修复都栽在这里,元素建了、play() 也 resolve 了,系统侧却当它不存在。
    document.body.appendChild(el);
    keepAliveEl = el;
    const p = el.play();
    if(p && p.catch) p.catch(err => { vdbg("keepalive.playfail", String(err)); });
    vdbg("keepalive.on");
  }
  function stopKeepAliveAudio(){
    if(!keepAliveEl) return;
    try{ keepAliveEl.pause(); }catch(e){}
    try{ keepAliveEl.src = ""; }catch(e){}
    try{ keepAliveEl.remove(); }catch(e){}
    keepAliveEl = null;
  }

  // ── 通话防熄屏视频(iOS 自动锁屏兜底,2026-08-17 真机反馈后落地)──────────
  // wakeLock 在 iOS PWA 上受 WebKit bug 254545 影响(request 成功也照样熄屏,
  // iOS 18.4 才修),而 iOS 对【正在播放的 video】豁免自动锁屏(NoSleep.js 同款
  // hack),静音 audio 不行——audio 只能保"锁了屏之后页面不冻结",防不了
  // "系统自动锁屏"本身。因此通话期间再循环播放一条 1s 黑帧静音 mp4
  // (muted+playsinline+1px 隐藏元素,无听感无画面),让 iOS 认为页面有视频
  // 在播、不自动熄屏。三层分工互不替代:wakeLock(18.4+ 生效,最省电)→
  // video(防自动熄屏)→ audio(锁屏后保活)。
  // 注意:data URI 播放依赖 web.py 的 CSP 已放行 media-src data:(2026-08-17 加)。
  // 副作用:极少机型可能短暂出现媒体播放指示,无害。
  const KEEP_ALIVE_MP4 = "data:video/mp4;base64,AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAa+bW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAA+gAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwAAAph0cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAA+gAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAEAAAABAAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAAPoAAAAAAABAAAAAAIQbWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAAAoAAAAKABVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAABu21pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAAXtzdGJsAAAAt3N0c2QAAAAAAAAAAQAAAKdhdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAEAAQABIAAAASAAAAAAAAAABFUxhdmM2Mi4xMS4xMDAgbGlieDI2NAAAAAAAAAAAAAAAGP//AAAALWF2Y0MBQsAe/+EAFmdCwB7aEJsBEAAAAwAQAAADAUDxYuoBAARozgRyAAAAEHBhc3AAAAABAAAAAQAAABRidHJ0AAAAAAAAF1gAAAAAAAAAGHN0dHMAAAAAAAAAAQAAAAoAAAQAAAAAFHN0c3MAAAAAAAAAAQAAAAEAAAAcc3RzYwAAAAAAAAABAAAAAQAAAAEAAAABAAAAPHN0c3oAAAAAAAAAAAAAAAoAAAKRAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAAOHN0Y28AAAAAAAAACgAABwMAAAmoAAAJwgAACdwAAAn6AAAKFAAACi4AAApMAAAKZgAACoAAAANRdHJhawAAAFx0a2hkAAAAAwAAAAAAAAAAAAAAAgAAAAAAAAPnAAAAAAAAAAAAAAABAQAAAAABAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAJGVkdHMAAAAcZWxzdAAAAAAAAAABAAAD5gAABAAAAQAAAAACyW1kaWEAAAAgbWRoZAAAAAAAAAAAAAAAAAAArEQAALAAVcQAAAAAAC1oZGxyAAAAAAAAAABzb3VuAAAAAAAAAAAAAAAAU291bmRIYW5kbGVyAAAAAnRtaW5mAAAAEHNtaGQAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAAjhzdGJsAAAAfnN0c2QAAAAAAAAAAQAAAG5tcDRhAAAAAAAAAAEAAAAAAAAAAAABABAAAAAArEQAAAAAADZlc2RzAAAAAAOAgIAlAAIABICAgBdAFQAAAAAAXcAAAAXnBYCAgAUSCFblAAaAgIABAgAAABRidHJ0AAAAAAAAXcAAAAXnAAAAGHN0dHMAAAAAAAAAAQAAACwAAAQAAAAAZHN0c2MAAAAAAAAABwAAAAEAAAABAAAAAQAAAAIAAAAFAAAAAQAAAAMAAAAEAAAAAQAAAAUAAAAFAAAAAQAAAAYAAAAEAAAAAQAAAAgAAAAFAAAAAQAAAAkAAAAEAAAAAQAAAMRzdHN6AAAAAAAAAAAAAAAsAAAAFQAAAAQAAAAEAAAABAAAAAQAAAAEAAAABAAAAAQAAAAEAAAABAAAAAQAAAAEAAAABAAAAAQAAAAEAAAABAAAAAQAAAAEAAAABAAAAAQAAAAEAAAABAAAAAQAAAAEAAAABAAAAAQAAAAEAAAABAAAAAQAAAAEAAAABAAAAAQAAAAEAAAABAAAAAQAAAAEAAAABAAAAAQAAAAEAAAABAAAAAQAAAAEAAAABAAAAAQAAAA8c3RjbwAAAAAAAAALAAAG7gAACZQAAAmyAAAJzAAACeYAAAoEAAAKHgAACjgAAApWAAAKcAAACooAAAAac2dwZAEAAAByb2xsAAAAAgAAAAH//wAAABxzYmdwAAAAAHJvbGwAAAABAAAALAAAAAEAAABhdWR0YQAAAFltZXRhAAAAAAAAACFoZGxyAAAAAAAAAABtZGlyYXBwbAAAAAAAAAAAAAAAACxpbHN0AAAAJKl0b28AAAAcZGF0YQAAAAEAAAAATGF2ZjYyLjMuMTAwAAAACGZyZWUAAAO0bWRhdN4CAExhdmM2Mi4xMS4xMDAAAjBADgAAAnAGBf//bNxF6b3m2Ui3lizYINkj7u94MjY0IC0gY29yZSAxNjUgcjMyMjIgYjM1NjA1YSAtIEguMjY0L01QRUctNCBBVkMgY29kZWMgLSBDb3B5bGVmdCAyMDAzLTIwMjUgLSBodHRwOi8vd3d3LnZpZGVvbGFuLm9yZy94MjY0Lmh0bWwgLSBvcHRpb25zOiBjYWJhYz0wIHJlZj0xIGRlYmxvY2s9MTowOjAgYW5hbHlzZT0weDE6MHgxMTEgbWU9aGV4IHN1Ym1lPTIgcHN5PTEgcHN5X3JkPTEuMDA6MC4wMCBtaXhlZF9yZWY9MCBtZV9yYW5nZT0xNiBjaHJvbWFfbWU9MSB0cmVsbGlzPTAgOHg4ZGN0PTAgY3FtPTAgZGVhZHpvbmU9MjEsMTEgZmFzdF9wc2tpcD0xIGNocm9tYV9xcF9vZmZzZXQ9MCB0aHJlYWRzPTIgbG9va2FoZWFkX3RocmVhZHM9MSBzbGljZWRfdGhyZWFkcz0wIG5yPTAgZGVjaW1hdGU9MSBpbnRlcmxhY2VkPTAgYmx1cmF5X2NvbXBhdD0wIGNvbnN0cmFpbmVkX2ludHJhPTAgYmZyYW1lcz0wIHdlaWdodHA9MCBrZXlpbnQ9MjUwIGtleWludF9taW49MTAgc2NlbmVjdXQ9NDAgaW50cmFfcmVmcmVzaD0wIHJjX2xvb2thaGVhZD0xMCByYz1jcmYgbWJ0cmVlPTEgY3JmPTMwLjAgcWNvbXA9MC42MCBxcG1pbj0wIHFwbWF4PTY5IHFwc3RlcD00IGlwX3JhdGlvPTEuNDAgYXE9MToxLjAwAIAAAAAZZYiED////D0UAAQ3/JycnXXXXXXXXXXXXgEYIAcBGCAHARggBwEYIAcBGCAHAAAABkGaID/CMAEYIAcBGCAHARggBwEYIAcAAAAGQZpAP8IwARggBwEYIAcBGCAHARggBwAAAAZBmmA/wjABGCAHARggBwEYIAcBGCAHARggBwAAAAZBmoA/wjABGCAHARggBwEYIAcBGCAHAAAABkGaoD/CMAEYIAcBGCAHARggBwEYIAcAAAAGQZrAP8IwARggBwEYIAcBGCAHARggBwEYIAcAAAAGQZrgP8IwARggBwEYIAcBGCAHARggBwAAAAZBmwA7wjABGCAHARggBwEYIAcBGCAHAAAABkGbIDfCMAEYIAcBGCAHARggBwEYIAc=";
  let keepAliveVideo = null;
  function startKeepAliveVideo(){
    if(keepAliveVideo){ try{ keepAliveVideo.play().catch(()=>{}); }catch(e){} return; }
    const v = document.createElement("video");
    v.loop = true; v.muted = true; v.preload = "auto";
    v.setAttribute("playsinline", "");
    v.setAttribute("webkit-playsinline", "");
    // 隐藏但必须【真的在渲染】:display:none / visibility:hidden / 移出视口太远
    // 都可能被 WebKit 判为"不可见"从而不给 idle-lock 豁免,opacity 也不能给 0
    // (NoSleep.js 同款约束)。压到 1px、留在视口左下角、透明度 0.01 肉眼不可见。
    v.style.cssText = "position:fixed;left:0;bottom:0;width:1px;height:1px;opacity:0.01;pointer-events:none;z-index:-1";
    v.src = KEEP_ALIVE_MP4;
    v.addEventListener("error", ()=>{ vdbg("keepalive-video.err"); });
    // 【必须挂进文档】(2026-08-24 根因):原实现建完元素直接 play(),从未
    // appendChild——游离元素没有渲染树节点,iOS 的"有视频在播就不自动锁屏"
    // 豁免根本轮不到它,2026-08-17 那次修复实际全程未生效。
    document.body.appendChild(v);
    keepAliveVideo = v;
    const p = v.play();
    if(p && p.catch) p.catch(err => { vdbg("keepalive-video.playfail", String(err)); });
    vdbg("keepalive-video.on");
  }
  function stopKeepAliveVideo(){
    if(!keepAliveVideo) return;
    try{ keepAliveVideo.pause(); }catch(e){}
    try{ keepAliveVideo.src = ""; }catch(e){}
    try{ keepAliveVideo.remove(); }catch(e){}
    keepAliveVideo = null;
  }
  // iOS 要求媒体播放由用户手势发起:建连链路里的直接调用若因 async 丢失
  // 手势窗口被拒,这里在每次触摸屏幕时顺手恢复(幂等,已在播则 no-op)。
  // 音轨也一并恢复:两条保活媒体都可能被系统中断暂停,只补视频等于漏了一半。
  function resumeKeepAliveOnGesture(){
    if(!handsFreeActive) return;
    startKeepAliveVideo();
    startKeepAliveAudio();
    ensureAudioCtxRunning("gesture");
  }
  document.addEventListener("pointerdown", resumeKeepAliveOnGesture);
  document.addEventListener("touchstart", resumeKeepAliveOnGesture);

  // ── 视图切换入口(统一对话视图复用主会话 composer)────────────────────────
  // initOnce:原页面这些初始化(常驻任务播报订阅/拉历史/挑一次交互模式)靠整页
  // 加载天然只跑一次;现在同一个 DOM 可以反复进出通话视图,得自己拿个标记位挡住
  // 重复跑。
  let initialized = false;
  let callInputMode = "voice";
  const sharedComposer = $("#composer");
  function setCallInputMode(mode){
    callInputMode = mode === "text" ? "text" : "voice";
    const isText = callInputMode === "text";
    $("#voiceModeTab").setAttribute("aria-selected", String(!isText));
    $("#textModeTab").setAttribute("aria-selected", String(isText));
    $("#callVoicePanel").hidden = isText;
    $("#callTextPanel").hidden = !isText;
    if(isText) $("#ta").focus();
    renderTaskBar();
  }
  $("#voiceModeTab").onclick = ()=>setCallInputMode("voice");
  $("#textModeTab").onclick = ()=>setCallInputMode("text");

  function mountSharedComposer(inCall){
    if(inCall){
      if(sharedComposer.parentElement !== $("#callTextPanel")) $("#callTextPanel").appendChild(sharedComposer);
      return;
    }
    if(sharedComposer.parentElement !== $("#chatMain")) $("#chatMain").appendChild(sharedComposer);
  }

  async function sendCallText(text){
    text=(text||"").trim();
    if(!text) return;
    if(S.images.length || S.audios.length){
      addMsg("ai error", "统一对话暂不支持附件发送,请切回普通会话处理附件");
      return;
    }
    closeCmdMenu();
    clearDraft();
    $("#ta").value="";
    autoGrow();
    await sendTurn({text, tts:false});
  }
  window.sendCallText = sendCallText;
  function initOnce(){
    if(initialized) return;
    initialized = true;
    loadMainHistory();
    initUiMode();
  }

  // 任务 SSE 流与全量初始化:登录后即启动,不依赖是否进入通话视图——聊天输入框顶部
  // 的任务状态条也需要实时更新。
  window.connectTaskStreamOnce = function(){
    if(window.__taskStreamConnected) return;
    window.__taskStreamConnected = true;
    connectTaskStream();
    loadTasks();  // 顺带初始化任务状态条:页面刷新前就在跑的任务也能立刻出现在条上
  };

  // teardownCallResources:退出通话视图时收麦克风/播放/WS——原页面靠浏览器整页
  // 导航卸载页面顺带回收这些资源,现在视图常驻 DOM 不会自动卸载,不手动收就是
  // 麦克风一直"热着"、WS 一直连着,电量和隐私都不划算。
  function teardownCallResources(){
    setOmniConnStatus("disconnected");
    releaseWakeLock();  // 挂断就不需要屏幕常亮了,让系统恢复正常锁屏节能
    stopWorkSound();    // 工作音效跟着通话走,挂断即停(audioCtx 也会在下面 close)
    handsFreeActive = false;  // 先立 flag:让 scheduleOmniReconnect 的回调知道用户已挂断
    updateCallModeTabs();  // 上面 setOmniConnStatus("disconnected") 时 handsFreeActive 还置着,此刻补刷一次,下次进通话视图切换钮是显示的
    if(omniReconnectTimer){ clearTimeout(omniReconnectTimer); omniReconnectTimer = null; }
    stopOmniHandsFree();
    if(currentSource){ try{ currentSource.onended = null; currentSource.stop(); }catch(e){} currentSource = null; }
    playQueue = []; playing = false; pausedForInterrupt = false;
    if(recorder && recorder.state === "recording"){ try{ recorder.stop(); }catch(e){} }
    recorder = null;
    if(stream){ stream.getTracks().forEach(t=>t.stop()); stream = null; }
    if(orbAnimHandle){ cancelAnimationFrame(orbAnimHandle); orbAnimHandle = null; }
    analyser = null; analyserSource = null; outputAnalyser = null; omniOutTap = null;
    // 吉祥物回到干净的空闲态,下次通话不带上一通的残余状态
    orbConnecting = false; orbMascotTgt = null; orbMicSmoothed = 1;
    if(mascotEl){
      mascotEl.dataset.state = "idle";
      const iEl = mascotEl.querySelector("i.vmi");
      if(iEl){ iEl.style.animation = ""; iEl.style.boxShadow = ""; }
    }
    if(audioCtx){ try{ audioCtx.close(); }catch(e){} audioCtx = null; }
    handsFreeActive = false; micMuted = false; wsState = "idle";
    busy = false; talkBtn.disabled = false; talkBtn.classList.remove("recording");
    stopBtn.hidden = true;
    orbWrap.className = "idle";
    muteToggleBtn.classList.remove("muted"); muteToggleBtn.title = "静音麦克风";
    setIcon(muteToggleBtn, "mic");
    tasksDrawer.classList.remove("open");
    startBtn.hidden = true;
    startBtn.disabled = false;
    buttonMode = false;
    setStatus("空闲");
  }

  window.openCallView = function(){
    closeWorkbench();
    S.surface = "call";
    closeSidebar();
    // 从普通会话进入主会话时记住原位置。重复点入口时 S.conv 已是语音主会话,
    // 不能覆盖掉返回位置。
    if(S.conv !== "voice-chat:main") S.callReturnConv = S.conv || "main";
    S.conv = "voice-chat:main";
    renderProjSelChip();  // 草稿会话切入主会话时，收起仅草稿可见的项目选择器
    mountSharedComposer(true);
    setCallInputMode("text");
    $("#chatMain").hidden = true;
    $("#callView").hidden = false;
    // 常亮锁不再在这里申请:待机(未点开始通话)不常亮省电,通话建连成功后由
    // startOmniHandsFree 申请、挂断/收线由 stopOmniHandsFree 释放(见 wakeLock 段)。
    initOnce();
    // 按钮启动模式:不自动开始录音,显示"开始录音"按钮等待用户点击
    buttonMode = true;
    if(vadCapable()){
      handsFreeUi.hidden = true;
      startBtn.hidden = false;
      startBtn.title = "通话"; startBtn.setAttribute("aria-label", "通话");
      startBtn.disabled = false;
      setStatus("点击按钮通话", "");
      orbWrap.className = "idle";
    } else {
      // 不支持 VAD 时回退到按住说话兜底
      handsFreeUi.hidden = true;
      talkBtn.hidden = false;
    }
    renderConvs();   // 侧栏按通话状态重绘,让「语音通话」行高亮
  };
  window.closeCallView = function(){
    mountSharedComposer(false);
    // 缓存版本错位或上次加载中断时,composer 可能留在隐藏的文本面板里。
    // 即使 callView 已隐藏也先归位,保证普通会话始终有输入框。
    if($("#callView").hidden) return;
    $("#callView").hidden = true;
    $("#chatMain").hidden = false;
    S.conv = S.callReturnConv;
    renderProjSelChip();  // 返回草稿会话时，恢复项目选择器
    teardownCallResources();
    renderConvs();   // 挂断后切回聊天侧栏,取消「语音通话」行高亮
  };
  // 任务状态条:切会话时 openConv(IIFE 外)要按当前会话刷新任务,loadTasks 是
  // 本 IIFE 的局部函数(严格模式不泄漏全局),照 openCallView 模式挂 window 出口。
  window.refreshTaskStatus = loadTasks;
  // 终态任务的隐藏定时器在 IIFE 外，不能直接引用这里的局部函数；暴露一个明确出口，
  // 否则到期时会抛 ReferenceError，后续页面脚本也会被中断。
  window.refreshTaskBar = renderTaskBar;
})();
