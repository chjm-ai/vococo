# P2 实现记录(2-C + 2-D:全双工免提连续对话 + 打断)

> 只做了 2-C/2-D 合并后的范围,不是整份 03-phase2-experience.md 的全部子项。
> 2-A(思考音效/固定话术)、2-B(CosyVoice2)、2-E(INTERRUPT 播报档)、
> 2-F(审批卡片)本期未动,留给以后按需做。

## 与设计文档的关键差异(用户已确认)

- **识别方案只换传输方式,不上本地流式 ASR**:STT 仍是现有远程 SenseVoice
  整段识别,只是音频改成边说边通过 WS 传给服务端(服务端攒够一句再调用
  `stt.transcribe()`),减少"说完还要等上传"的尾巴。没有 sherpa-onnx,没有
  本地模型下载,没有实时草稿字幕。
- **旧请求未完成时来了新请求 = 打断重来**,不是排队——免提模式下没有按钮
  这个动作了,任何检测到的新说话本身就是打断信号,这两件事是同一个机制。

## 新增文件

```
claude_hermes/voice/
  ws.py                          # /voice/ws 状态机、打断截断、假打断回滚、WAV 包装
  static/pcm-forwarder-worklet.js  # 手写的小 AudioWorkletProcessor,PCM16 转发
  static/vad/                    # vendored @ricky0123/vad-web + onnxruntime-web
    bundle.min.js                    # ~68KB,vad-web 主线程 API
    vad.worklet.bundle.min.js        # ~2.4KB,VAD 的 AudioWorklet 处理器
    silero_vad_v5.onnx               # ~2.2MB,VAD 模型本体
    ort.min.js                       # ~530KB,onnxruntime-web JS 胶水
    ort-wasm-simd.wasm               # ~10.1MB,ONNX 推理引擎(只带 SIMD 单线程版,
                                      # 不带 threaded 版——threaded 需要 COOP/COEP
                                      # 响应头,这个单文件 app 没有这层基建)
tests/test_voice_p2_realtime.py  # 13 个新测试
```

约 13MB 的 vendored 静态资源,来源 unpkg.com(代理 npm 官方 registry),版本
锁定 `@ricky0123/vad-web@0.0.30` + `onnxruntime-web@1.17.3`——以后要升级版本,
重新下载这几个文件替换即可,没有 build 工具链依赖。

## 接触点实际改动

| 文件 | 改动 |
|---|---|
| `config.py` | 净 +4 行:`VOICE_WS_ENABLED`(默认开)、`VOICE_FALSE_POSITIVE_TIMEOUT_MS`(默认 1500) |
| `pyproject.toml` | `package-data` 的 glob 加 `static/*.js`、`static/vad/*.{js,wasm,onnx}`,否则装包时新静态资源不会被打进去 |
| `core/agent.py` | 0 行改动——`stream_turn()` 已有的 `CancelledError` 处理(`client.interrupt()` + 不回池)直接够用,真机冒烟测试已验证(见下) |
| `gateway/adapters/web.py` | 0 行改动——`register_routes(app)` 已经能挂 WS 路由和静态资源路由,不用碰这层 |

voice 包内部(不占预算,自己的模块随便改):

- `voice/routes.py`:`register_routes()` 加 `/voice/ws`(仅当 `VOICE_WS_ENABLED`)
  + `aiohttp` 自带的目录静态服务 `/voice/static/`(VAD 资源是公开文件,不校验
  token,浏览器 `<script src>`/`AudioWorklet.addModule` 天然不带自定义 header)。
- `voice/static/voice.html`:新增免提模式的完整实现(VAD 初始化、WS 客户端、
  打断暂停/确认/回滚的播放队列逻辑),老的按住说话流程**原样保留**,不删除
  ——首次点按钮时顺带异步升级成免提(升级期间用户完全无感,还是老流程),
  免提就绪且当前没有手动录音在跑时才真正切换;不支持 `AudioWorklet`/`WASM`
  的浏览器永远停留在按钮模式。老按钮升级成免提后变成"静音/打断"手动按钮。

## 核心设计:两阶段提交(打断截断 + 假打断回滚共用同一个 pending 状态)

1. `speech_start` 一到,如果服务端正在 thinking/speaking → 立刻 cancel 掉正在
   跑的 turn task(省 token,不等它说完),但这时候还不知道用户到底听到了
   几句——服务端已经发出去的 `sentence` 事件可能比客户端已经**播完**的多
   (生成快于播放)。于是只记一个 `PendingTruncation`,不落库,同时起一个
   1.5 秒的兜底看门狗。
2. 随后到达的 `speech_end` 带着 `client_played_sentences`(客户端
   `source.onended` 真正播完的句数,不是"收到了几句"):
   - 转写出来是有效文本 → 确认打断,`emitted_sentences[:played_count]` 拼起来
     + 追加"(此处被用户打断)"落库,发 `interrupted`,客户端这才真正清空播放
     队列,新一轮正常开始。
   - 转写为空/失败,或者 1.5 秒内一直没等到(看门狗兜底)→ 判定误触发,
     什么都不落库(反正原 turn 从没走到 `Done`,本来就没东西可落),发
     `resumed`,客户端从暂停处接着播——打断那一刻音频已经停了,但没被真的
     销毁,只是"暂停等确认",这是设计文档里特意强调、容易漏的点。

## 采样率:没有硬编码 16kHz

浏览器 `AudioContext` 常见原生采样率是 44.1k/48k,不是 16k;如果服务端拼
WAV 头时写死 16000,STT 听到的就是被拉长/变调的音频,转写直接废。改成
`speech_start` 帧里带 `sample_rate`(取 `audioCtx.sampleRate`),服务端用这个
真实值给 `wave.open()` 设置帧率,`_MIN_UTTERANCE_BYTES`(200ms 阈值)也跟着
按这个采样率动态算,不是写死的字节数。这是设计阶段没预料到、实现时发现的
真实坑,记在这里防止以后走回头路。

## 测试

- `tests/test_voice_p2_realtime.py`(13 个新测试,全部 mock `session.run_turn`/
  `stt.transcribe`,用 aiohttp 自带的 `client.ws_connect()` 测试工具,不需要
  真实浏览器):打断截断纯函数(含 client 上报数超过服务端已发送数的防御性
  clamp)、WAV 头字段、正常一轮状态机迁移、太短的录音被丢弃不触发转写、
  打断确认落截断、假打断两条回滚路径(转写为空 / 看门狗超时)、二次开口在
  上一句还没转写完时的赛跑(作废旧的、不产生 pending)、重复 `speech_start`
  空操作、与 `/voice/send` 的互斥。
- `uv run pytest`:221 通过(208 原有 + 13 新增),零回归。
- **真实 SDK 取消冒烟测试**(不进 pytest,单独脚本跑的):真实
  `stream_turn()` 跑到一半 `task.cancel()`,取消到 `await task` 返回耗时
  **0.568 秒**(远低于 `core/agent.py` 里 `client.interrupt()` 的 5 秒兜底),
  且**没有残留的 claude CLI 子进程**——打断机制在真实场景下可靠。
- **真实服务器端到端冒烟**(隔离测试服务器,真实 `stt.transcribe`,不 mock):
  连接 `/voice/ws` → `speech_start` → 发一段静音 PCM → `speech_end` →
  状态机正确走完 `capturing → transcribing → idle`(静音转写为空,判定失败,
  不进入 turn),没有崩溃或挂起。VAD 静态资源(含 10MB 的 wasm)通过
  `/voice/static/vad/*` 正确返回 200。

## 免提模式激活不了:桌面 Chrome 调试出的 3 个真实 bug

用户反馈"手机上还是对讲机模式,免提模式没激活"后,借 claude-in-chrome 打开同一
个页面复现、直接读浏览器控制台,而不是隔着手机瞎猜——连续挖出 3 层问题,
每一层都不是"猜的",是控制台报错直接指出来的:

1. **VAD 模型文件名不对**:`@ricky0123/vad-web@0.0.30` 默认模型是
   `DEFAULT_MODEL = "legacy"`,只 vendored 了 `silero_vad_v5.onnx` 就直接去请求
   不存在的 `silero_vad_legacy.onnx`(404,静默被 `initHandsFree()` 的 catch 吞掉,
   界面上完全看不出发生了什么)。修法:`MicVAD.new()` 显式传 `model: "v5"`。
2. **onnxruntime-web 版本对不上**:vendored 的 `bundle.min.js` 内部实际绑定的是
   `onnxruntime-web@1.22.0`(bundle 自带的 LICENSE.txt 和内嵌版本号都能看到),
   但一开始下载的 wasm/js 是 `@1.17.3` 的——版本差了几个大版本,连文件命名规则
   都变了(1.22.0 已经不提供 `ort-wasm-simd.wasm` 这个非线程版文件名,只剩
   `ort-wasm-simd-threaded.{mjs,wasm}`)。表现是模型文件本身能 200 下载下来,
   但 ONNX Runtime 解析/建 session 直接失败。修法:换成
   `onnxruntime-web@1.22.0` 对应的 `ort-wasm-simd-threaded.mjs` +
   `ort-wasm-simd-threaded.wasm`,删掉旧版本的 `ort.min.js`/`ort-wasm-simd.wasm`
   (bundle.min.js 已经把 ORT 的 JS 胶水内联了,不再需要单独的 ort.min.js)。
3. **CSP 挡了 WASM 编译**:`gateway/adapters/web.py` 里全站 CSP 的
   `script-src` 只有 `'self' 'unsafe-inline'`,浏览器编译/实例化任何
   WebAssembly 模块都需要专门的 `'wasm-unsafe-eval'` token(注意不是
   `'unsafe-eval'`——后者是放开 `eval()`/`new Function()` 那类字符串转代码,
   前者只管 WASM,两者不是一回事,加了不等于削弱 CSP 本来防的那类注入)。
   没有这个 token,`WebAssembly.instantiate()` 直接被 CSP 拒绝,报
   `CompileError`。修法:`_CSP` 常量的 `script-src` 加上 `'wasm-unsafe-eval'`
   ——这是本期唯一一处 touch 了 `voice/` 包以外、且不在"计划里预告过"的文件的
   改动,但必要且影响面很窄(只多开一个 WASM 编译权限)。

排查方法上值得记一笔:没有等着"用户在真机上再测一次告诉我现象",而是用
claude-in-chrome 在桌面浏览器里打开同一个页面、手动点按钮触发
`initHandsFree()`、直接读 `read_console_messages` 拿到真实报错——三层问题
都是控制台报错原文直接指出来的,不是靠猜代码逻辑推出来的。免提相关的静默
`catch` 吞异常这件事本身也是一个教训:调试时应该临时加 `console.error`
再复现,不要对着"什么反应都没有"干猜。

## UI 交互重做 + 识别延迟排查(2026-07-08,真机反馈驱动)

用户真机反馈两件事:①"按住说话"这个按钮标签跟免提场景根本对不上,交互要
重做;②免提模式下卡在"聆听中"不动,识别/回复都很慢。

**交互重做**:免提场景没有"按下/松开"这个动作,不该硬塞进同一个按钮里。
改成三段式——① 支持免提的设备默认显示「🎙️ 开始语音对话」入口按钮(不是
"按住说话");② 点了之后界面换成一个跟着状态变色/脉动的"状态球" + 「🔇 静音」
/「✕ 结束」两个小按钮,点状态球本身就是"让 AI 立刻闭嘴"(手动打断);
③ 不支持免提的设备(没有 AudioWorklet/WASM)才会看到老的"按住说话",两种
UI 由 `initUiMode()` 在页面加载时一次性决定显示哪个,不再靠同一个按钮在
运行时切换身份(那是之前踩的坑,详见下一节)。

**排查中连带修了 3 个真 bug**(过程见上一节"3 个真实 bug",本节是后续的第 4/5 个):

4. **AudioWorkletNode 没接到输出**:自己手写的 `pcm-forwarder-worklet.js` 只
   从麦克风接了输入,自己的输出没接到任何地方(没连到 `audioCtx.destination`)
   ——浏览器的音频图是拉取式的,一个节点如果没有路径通到最终输出,可能被直接
   跳过不处理,`process()` 根本不会被调用。表现是 VAD 自己的识别状态正常变化
   (它有自己独立的 worklet,接线是对的),但服务端一直收不到任何 PCM 字节。
   修法:接一个零增益的 `GainNode` 到 `destination`,让这个节点真正进入活跃
   的音频图、但不会真的出声。用 `port.onmessage` 计数验证过修复前后的区别
   (修复后 1.5 秒内收到 518 帧,之前是 0)。
5. **`hidden` 属性被自己的 CSS 覆盖**:`#handsFreeUi{display:flex}` 这种
   ID 选择器的优先级比浏览器默认的 `[hidden]{display:none}` 高,导致设了
   `hidden` 属性元素照样显示("开始语音对话"按钮和状态球同时出现在界面上)。
   修法:补一条 `#handsFreeUi[hidden]{display:none}` 显式覆盖回去——凡是
   给会被 `hidden` 属性切换的元素单独写了 `display` 值,都要记得配这条。

**识别慢的根因排查**:没有瞎猜优化点,先用隔离脚本裸测 SiliconFlow 的
`FunAudioLLM/SenseVoiceSmall` 接口本身——3 次单独调用耗时 10.2s/8.6s/10.0s,
换成复用连接再测 3 次反而更慢(17.3s/17.0s/10.6s),证明连接开销不是原因,
是那个接口本身响应就这么慢,跟我们代码怎么写无关。裸测 SiliconFlow 上另一个
模型 `TeleAI/TeleSpeechASR`(0.4~1.2秒)和阿里 DashScope 的 `qwen3-asr-flash`
(0.5~1秒)都快了一个数量级,用同一段"Claude Code / Obsidian / Anthropic /
API"混说测试句对比准确度,qwen3-asr-flash 明显最准(只错"Claude→Cloud"一处,
Obsidian/Anthropic 都对;另外两个都错更多)。最终选了 qwen3-asr-flash——
详见 `voice/stt.py` 和 `config.py` 里的 `DASHSCOPE_*` 配置项,协议从
SiliconFlow 的 multipart 文件上传换成了 DashScope 的 JSON+base64 audio data
URI,是完全不同的两套调用方式,不是改个模型名字那么简单。清洗步骤(_cleanup)
没动,仍用 SiliconFlow 的 Qwen2.5-72B chat/completions。

真机 `/voice/stt` 端到端实测:同一段音频,总耗时从大几秒/十几秒降到 **1.48秒**
(其中识别本体 0.60s + 清洗 0.85s)。

## 待用户在真机上验证(单测/脚本没法覆盖)

- VAD 在真实嘈杂手机环境下的误触发率(300ms 阈值是起点,大概率要再调)。
- 真实说话场景下的打断体验:开口打断的实际延迟感、假打断(咳嗽/环境音)
  能不能正确恢复播放而不是白白断一次。
- 回声:AI 声音从手机扬声器漏回麦克风会不会自我打断(`echoCancellation`是
  缓解不是根治)。
- Cloudflare 隧道下 WS 长连接的稳定性(锁屏、切换 WiFi/蜂窝网络)——前端有
  简单的断线退避重连,但没有真实弱网环境测过。
- 首次访问约 13MB 静态资源的下载体验(已做成"先旧 UI 能用、后台异步升级",
  但没有真实设备上的观感验证)。~~已作废~~,见下一节——客户端 VAD 整个被
  替换掉了,这个风险不复存在。
- 老的按住说话流程在这次改动后是否还完全正常(代码上没删除任何东西,理论上
  该没问题,但没有真机复测)。

## 客户端 VAD 换成 DashScope 实时语音 WS(2026-07-08,同一天的第二次架构调整)

上一节的 UI 重做刚上线,用户就反馈免提模式经常卡在"聆听中"不动——查证发现
服务端压根没收到 speech_end,是客户端那套 `@ricky0123/vad-web`(Silero VAD
ML 模型)在真实手机环境下判断不出"说完了"。用户给了阿里 DashScope 的
`qwen-asr-realtime` 文档和一个 key,建议评估要不要换。

### 新架构

客户端不再跑本地 VAD,免提激活后麦克风持续把 PCM 转发给 `/voice/ws`(不再有
"检测到说话才发"的门槛)。服务端每条客户端连接对应开一条到 DashScope
`qwen3-asr-flash-realtime` 的上游 WS 连接,把音频原样中继过去,状态机完全由
上游吐回来的事件驱动:

- `input_audio_buffer.speech_started` → 如果正在 thinking/speaking,走原有的
  两阶段提交打断;否则切 capturing。
- `conversation.item.input_audio_transcription.completed` → 权威的"说完了,
  原文是这个",直接用来开始新一轮——识别本身已经在这条流式连接里做完了,
  不再需要额外调 `stt.transcribe()`(那是给老的按钮流程用的,没动)。

打断截断要用的"客户端听到几句"不再跟某条 speech_end 消息绑一起发,改成客户端
每次 `pumpPlayback()` 里播完一句就上报一条 `played_progress`,服务端只记
最新值,`completed` 事件到达时直接取用——不用再纠结"这条该跟哪次打断配对"。

### 连带清理

- `voice/static/vad/`(~13MB,`@ricky0123/vad-web` + onnxruntime-web + Silero
  模型)整个删掉。
- `gateway/adapters/web.py` 的 CSP 里为了跑 WASM 加的 `'wasm-unsafe-eval'`
  撤回——浏览器不再跑本地 ONNX 推理了,接触点预算重新回到最小。
- `pyproject.toml` 的 `package-data` 里那几条 `static/vad/*` glob 一并删掉。
- 静态资源目录从 ~13MB 降到 36KB。

### 真机测试中暴露的第 6 个真实 bug:假打断回滚超时太短

用真实音频(不 mock)跑通整条链路时,第一次尝试打断确认总是被误判成回滚——
加了调试日志查证:不是超时判定错了,是 `VOICE_FALSE_POSITIVE_TIMEOUT_MS`
默认值(1500ms,继承自旧的客户端 VAD 架构)对新架构来说太短了。旧架构下这个
窗口只需要覆盖"客户端本地已经判完、只等一个网络包"的极短时间;新架构下这个
窗口要覆盖"用户真的把这句话说完 + DashScope 转写出结果"的完整耗时——一句
4-5 秒的话加上上游网络往返,1.5 秒经常等不到 `completed` 事件就被误判成
假打断,白白撤销了一次正常的追问。改成 8000ms 后问题消失。

调试过程中还发现一个纯测试脚本的坑(不是产品代码 bug,记一笔防止以后误判):
第一次真机模拟测试里,发完录音文件的音频就断流不发了,导致 DashScope 的
`server_vad` 一直等不到"持续静音"去判定"说完了"——真实浏览器场景麦克风是
一直开着的,说完话之后本来就会继续送静音帧过来,测试脚本得模拟这个持续流,
不能验证完就断流。

### 真实端到端验证(不 mock,真实 DashScope + 真实 Claude SDK)

用一段"今天天气怎么样,我想去公园散步"的双句录音模拟真实场景(第二句在
第一句还在等 Claude 回复、即 thinking 状态时开始播放,天然构成一次打断):

```
capturing → transcript"今天天气怎么样？" → thinking
  → capturing(上游检测到用户又开口)→ interrupted(truncated_at_seq: 0,
    第一轮还没来得及说一个字就被打断,截断结果正确)
  → transcript"我想去公园散步。" → thinking → speaking
  → 完整真实 Claude 回复"好主意,今天天气适合走走,呼吸一下新鲜空气。
    注意带伞就行。" → sentence(真实 TTS 音频)→ done
```

打断、截断、新一轮启动,全链路一次串通,`uv run pytest` 221 个测试(含重写后
的 13 个 P2 测试)全绿。

## 回声兜底:AI 自我打断(2026-07-08,同一天的第三次调整)

真机验证识别速度/灵敏度都达标后,用户反馈了新问题:AI 自己讲话时,手机
扬声器的声音漏回麦克风,DashScope 把这段声音识别成"用户开口了",触发打断——
AI 打断自己,陷入自我打断循环。这正是设计阶段就点名过的风险(见"已知风险"
一节的回声条目),`echoCancellation` 只是缓解不是根治,现在真的踩上了。

**修法**:在 `_handle_upstream_event` 处理 `completed` 事件时,新增
`looks_like_self_echo(transcript, emitted_sentences, threshold)`(纯函数,
`voice/ws.py`)——拿这次打断转写出来的文本,跟 `PendingTruncation` 里记的
"AI 已经说出口的句子"做 containment 比对(转写内容有多大比例的字符能在 AI
刚说的话里找到匹配片段,而不是对称相似度——回声通常只是 AI 那句话的一个片段,
用对称相似度会因为长度差太多被稀释)。超过阈值(`VOICE_SELF_ECHO_THRESHOLD`,
默认 0.6)就当成回声误触发,走跟"转写为空"一样的回滚路径——不落库、不拿这段
话去问模型、发 `resumed` 恢复播放。

```python
matcher = difflib.SequenceMatcher(None, transcript, said)
matched_chars = sum(block.size for block in matcher.get_matching_blocks())
containment = matched_chars / len(transcript)
```

真实例子验证过:AI 说"好主意，今天天气适合走走，呼吸一下新鲜空气。"期间,
如果打断转写出来的是"呼吸一下新鲜空气"(AI 这句话的原文片段)→ 判定回声,
不打断;如果是"明天股票会涨吗"这种无关内容 → 正常当真打断处理。

**已知局限(阈值是经验值,没法预先精确定死)**:
- 用户如果真的复述/引用 AI 刚说的话(比如"你刚说的呼吸新鲜空气是什么意思"),
  可能被误判成回声压掉——这种情况只发生在 AI 还在说话/思考的打断窗口内,
  AI 说完之后正常追问不受影响(那时候没有活跃的 `pending`,回声检测压根不生效)。
- 回声如果被扬声器/房间混响进一步失真,转写出来的文本可能跟原文对不上,
  containment 比例会偏低,过滤不掉——这个兜底不是根治,只是把明显的回声挡掉
  一部分,真出现顽固的自我打断循环,下一步要考虑物理层面(降低音量/戴耳机)
  或者更彻底的回声消除方案。
- 阈值 0.6 是拍脑袋定的起点,真机环境(音量、房间混响、手机型号)不同,
  大概率要再调,同上面 VAD 阈值一样的调优套路(改 `.env` 里的
  `VOICE_SELF_ECHO_THRESHOLD` 不用碰代码)。

`uv run pytest`:227 个测试全绿(P2 测试从 13 个增加到 19 个,含 5 个
`looks_like_self_echo` 纯函数测试 + 1 个端到端回声场景集成测试)。

## 识别太灵敏 + 连环打断吃掉真实问题(2026-07-08,同一天的第四次调整)

真机连续对话一段时间后,用户反馈两件事,拿实际落库的 `data/voice/voice.db`
的 `turns` 表核对,不是猜的:

```
60|哈喽哈喽，听到吗？|(此处被用户打断)
61|听到吗？|(此处被用户打断)
62|嗯。|(此处被用户打断)
63|知道。|(此处被用户打断)
64|很奇特。|(此处被用户打断)
65|今天天气怎么样？|(此处被用户打断)
66|稍等。|(此处被用户打断)
67|哦。|(此处被用户打断)
...
71|今天天气怎么样？|(此处被用户打断)
72|有台风吗？|(此处被用户打断)
73|有台风吗？|(此处被用户打断)
```

真实问题("今天天气怎么样？"、"有台风吗？")连着两次都没能问出结果,每次都被
紧跟着的一句语气词打断掉了。

**根因一:VAD 阈值比 DashScope 自己的默认值还灵敏。** 早先为了图省事验证协议,
`VOICE_VAD_THRESHOLD` 定成了 0.0(数值越低越灵敏),比 DashScope 自己
`session.created` 里报的默认值 0.2 还要敏感。真机有环境噪音的房间里,呼吸声/
杂音被当成开口,识别模型对这种含糊音频又倾向于编一个说得过去的短词
("嗯"/"哦"/"知道"/"稍等")而不是老实返回空——这就是"识别太灵敏"的直接原因。
调回比官方默认更保守一点的 0.3(threshold)/500ms(silence_duration_ms)。

**根因二:一个语气词打断正在进行的回答后,这个语气词本身又会被当成新一轮的
输入,而这一轮如果又被下一个语气词打断,如此循环。** 打断这个动作本身
(`speech_started` 一到就 cancel 掉正在跑的 turn)没法延迟——不能等确认了
"这句话有没有实际内容"才决定要不要打断,那样真正的开口打断就会有延迟。
但"要不要拿这段转写内容去问模型、要不要真的提交这次打断"是可以延后判断的
——加了 `looks_like_filler_only(transcript)`(纯函数,`voice/ws.py`):
转写内容去掉标点后如果精确匹配"嗯/呃/啊/哦/是的/知道/好的/对/行/稍等"这类
已知语气词表,判定为噪音误触发,跟"转写为空"走一样的回滚路径——不落库、
不拿去问模型、发 `resumed`。**这条判断只在"正要打断一个已经在跑的回答"这个
场景生效**——从空闲状态触发的语气词照常当一句真话处理(用户确实可能就是想说
"嗯"表示回应),危害只在"拿它去打断别的东西"这一步。

**诚实说明这个修法解决了什么、没解决什么**:`speech_started` 一到就会立刻
cancel 掉正在跑的那次生成,这一步没法回退——所以"今天天气怎么样"这次生成
一旦被下一句(哪怕是语气词)打断,那次回答确实是真的没了,追不回来。这个修法
解决的是"连环"——语气词本身不会再被当成一个新问题拿去问模型、也不会再产生
新的可被打断的一轮,链条到这里就断了,不会一直"打断→语气词→打断→语气词"
下去。要从根上减少"真问题被语气词打断"的概率,还是得靠根因一的阈值调优——
两个修法互补,不是二选一。

`uv run pytest`:241 个测试全绿(新增 `looks_like_filler_only` 的 8 个参数化
纯函数测试 + 1 个还原真机连环打断场景的集成测试)。

## 灵敏度继续调低(2026-07-08,同一天的第五次调整)

调到 0.3/500ms 后用户真机反馈仍然偏灵敏,没说话也会被识别成短词/短语。
继续调保守:`VOICE_VAD_THRESHOLD` 0.3→0.5,`VOICE_VAD_SILENCE_MS` 500→600。
threshold 0.5 已经明显高于 DashScope 官方默认的 0.2,如果真机测试后还是
偏灵敏,下一步可以试 0.6~0.7(上限 1.0),或者把 `silence_duration_ms` 再往上
提——但要注意这两个值调太高会反过来损失"打断响应速度"和"识别到停顿的
灵敏度",是一个此消彼长的权衡,不是越高越好。

## 灵敏度再次大幅调低(2026-07-08,同一天的第六次调整)

用户明确要求"两个时间都再大幅提升"(相对上一轮 0.5/600ms)。这次直接调到
`VOICE_VAD_THRESHOLD` 0.5→0.7(逼近 DashScope 允许的上限 1.0)、
`VOICE_VAD_SILENCE_MS` 600→1000ms。已提前告知用户代价:threshold 越接近
上限,轻声/尾音收不到的风险越大;silence_duration_ms 拉到 1000ms 意味着
用户说完话后要多等约 1 秒系统才会判定"说完了",打断和响应都会感觉更慢。
如果这轮矫枉过正(该识别的没识别到,或者停顿感明显变迟钝),再往回收,别
继续往上加。

## 连环打断内容累积,不再只回复最后一句(2026-07-08,同一天的第七次调整)

用户反馈:多轮连续打断时,前面被打断的几句问题容易遗漏,AI 好像只回复了
最后一条。真实原因是——打断本身没法回退("speech_started 一到就 cancel 掉
正在跑的生成"这条在前几轮已经明确写过、没法绕开),但被打断那句话的**原文**
之前完全没有被带到下一轮的 prompt 里,只是记进了 voice.db 的历史日志(给
Web UI 显示用),模型实际回答的只是最新这一句,前面被打断的问题模型根本
没读到。

修法(`voice/ws.py`):新增 `self._carried_text` 累积区。每次真打断确认
(`_resolve_pending` 的 commit 分支,即"确实是新内容,不是语气词/回声/超时
误触发")时,把那句被打断的话原文记进 `self._carried_text`;下一次
`_start_turn` 会把它拼在新说的话前面(`f"{carried}{new_text}"`,DashScope
转写自带句末标点,直接拼接就是一句完整的话,不用加额外说明文字)一起问
模型,并在拼接的同时清空累积区。这样即使连续打断两三次,最终真正跑完的
那一轮 prompt 里也带着全部几句话,模型能一次性回复,不会遗漏。落库到
voice.db 的 user_text 也是这个合并后的完整文本,历史记录里看得到完整问题,
不是只有最后一句。

只在"真打断确认"这条路径累积——语气词/自我回声/假打断超时回滚那三种情况
(`commit=False`)不累积,因为那几种场景本来就被判定为"这次打断不算数",
延续之前的语义(见"识别太灵敏"一节)。

`uv run pytest`:242 个测试全绿(改了 1 个既有打断确认测试的断言以匹配新的
拼接行为,新增 1 个还原真机连续追问场景的集成测试
`test_cascading_real_interrupts_combine_into_one_prompt`)。

## 回声兜底补漏洞:idle 之后的尾音也要拦(2026-07-08,同一天的第八次调整)

用户提问"现在是怎么实现防止 AI 录到自己说的话的",顺带反馈偶发还是会录到
AI 自己说的话,虽然频率不高、录得也不精确,怀疑是尾音。

**现有三层防线(回答用户的问题)**:
1. 客户端 `getUserMedia` 的 `echoCancellation`——浏览器原生回声消除,缓解不是
   根治。
2. `looks_like_self_echo`(containment ratio):打断触发时,把新转写内容跟
   "pending 里记的、AI 这一轮已经说过的话"比对,重合度够高就判定是回声。
3. `looks_like_filler_only`:打断触发时,新转写如果是已知语气词表里的内容,
   也判定为误触发。

**排查后发现第 2、3 层都有一个共同的作用域漏洞**:两者都写成
`interrupting and (...)`,`interrupting` 定义是 `self._pending is not None`——
也就是说只有"正在打断一个还没说完的回答"这个场景才生效。但用户描述的
"尾音"发生在另一个时间点:AI 说完最后一句、`Done` 已经触发、服务端状态已经
翻回 `idle`(`self._pending` 这时已经是 `None`)——但客户端音箱这时可能还在
把最后一两句话播完(网络传输 + 音频解码 + 实际播放都需要时间,服务端"生成
完成"跟客户端"物理上真的放完了"之间有个滞后窗口)。这段尾音漏回麦克风时,
`interrupting` 是 `False`,回声/语气词判断整层被跳过,尾音转写出来的内容
就被当成一句全新的真话,直接开了新一轮。

**修法(`voice/ws.py`)**:新增 `self._recent_emitted_sentences` +
`self._recent_emitted_at`,在每轮 `Done` 时记下"这一轮说了什么、什么时候
说完的"。转写完成事件里,除了原来"正在打断"时的回声判断,新增一个
`within_post_done_guard`:不在打断场景、但距离上一轮说完还在
`VOICE_POST_DONE_ECHO_GUARD_MS`(默认 1500ms)以内、且新转写内容跟刚说过的
话重合度够高,同样判定为回声,丢弃、状态收回 idle,不开新一轮。

时间窗故意给得比较短(1.5 秒):这类回声兜底是内容比对不是"这段时间一律
不理你",窗口太大会有真实的、恰好用词和上一轮很像的追问被误伤的风险(比如
AI 说"要不要去公园散步",用户马上说"去公园散步"表示同意,这种情况不该被
当回声吃掉)。

新增 2 个测试:`test_post_done_tail_echo_within_guard_window_is_discarded`
(还原尾音场景,确认 discard、不起新一轮)、
`test_post_done_unrelated_utterance_still_starts_new_turn`(确认这个兜底
只挡"内容重合"的回声,刚说完话之后问一个完全不相关的新问题照常起新一轮,
不会被一并误伤)。`uv run pytest`:244 个测试全绿。

**诚实说明这个修法解决了什么、没解决什么**:这只覆盖"尾音内容跟 AI 刚说的
话高度重合"这一种情况;如果尾音被识别模型转写成完全不搭边的幻觉文本(音频
质量太差、模型瞎编了一个跟原话毫无关系的词),containment 比对不出重合度,
这个兜底就拦不住——这属于 ASR 在低质量回声音频上的幻觉问题,不是这次能
根治的范围,真机如果还有这类残留,得回头看是不是要收紧
`VOICE_VAD_THRESHOLD` 或者从硬件层面(比如降低外放音量、戴耳机)缓解。

## WebRTC loopback:让浏览器的回声消除真正生效(2026-07-08,同一天的第九次调整)

用户追问"其他人是怎么解决这种问题的",查了业内做法后发现一个关键信息:
**`getUserMedia` 的 `echoCancellation:true` 只对"通过 WebRTC 收到的音频"生效,
对 Web Audio API 直接 `connect(audioCtx.destination)` 播放的本地音频不生效**
(浏览器压根不知道这段声音被放出来了,没法拿它当参考信号去消除)。查了咱们
播放 TTS 的代码([voice.html](../../../claude_hermes/voice/static/voice.html)
的 `pumpPlayback`),确认就是纯 Web Audio API 播放,不走 WebRTC——也就是说
`ensureStream` 里开的那个 `echoCancellation:true` 很可能从一开始就没真正把
AI 自己的声音当成待消除的参考信号,这也是为什么前面几轮内容层面的兜底
(containment 比对、时间窗)已经上线了,回声还是会偶发漏进来的更底层原因。

**修法**:业内通用的绕过办法——本地开两个 `RTCPeerConnection` 互相连起来
(纯本机 loopback,不出网、不连真实服务器、不需要 STUN/TURN),把 TTS 音频
从直连 `audioCtx.destination` 改成先接到一个
`audioCtx.createMediaStreamDestination()`,这个 MediaStream 通过 pc1 发送、
pc2 收到后用一个真正的 `<audio>` 元素播放出来。这样一来,浏览器的回声消除
引擎会把这段音频当成一次真实的"通话音频",从而真正参与 AEC 的参考信号
计算——不是在应用层判断"这像不像回声"(前几轮做的事),而是从物理信号层面
真的把这段声音从麦克风输入里减掉。

搭建过程整个包在 `setupEchoLoopback()`(`voice.html`),在 `unlockAudio()`
里发起(不等它,搭建期间照常先用直连 `audioCtx.destination` 播放,搭好了
`pumpPlayback` 自动切过去用 loopback destination);任何一步失败(旧浏览器
不支持 `RTCPeerConnection`、协商出错等)都静默捕获、回退到原来的直连播放,
不影响播放功能本身,只是这层回声兜底缺失。`pc1`/`pc2`/`<audio>` 元素都存进
模块级变量长期持有引用——最初实现漏了这步,只在函数局部变量里,协商完
函数返回后就没人再引用它们了,有被 GC 提前回收、播放中途断掉的风险(尤其
`<audio>` 元素没挂进 DOM,更依赖 JS 侧的引用存活),真机验证前发现并改掉了。

**真实验证(不是纸面推导)**:用 claude-in-chrome 打开隔离测试服务器的
`/voice` 页面,点开始语音对话触发 `unlockAudio→setupEchoLoopback`,读页面
JS 状态确认 `echoLoopbackReady:true`、两条 `RTCPeerConnection` 都是
`connected`、回声 loopback 用的 `<audio>` 元素 `paused:false`(真的在播);
读浏览器控制台确认零页面自身的报错(混进来的一堆 `chrome-extension://`
报错是装的密码管理器插件自己的,跟这次改动无关)。这是桌面 Chrome 的验证,
手机 Safari 的真实效果(iOS 的 WebRTC 实现跟桌面 Chrome 有不少差异)还没测,
需要真机确认。

`uv run pytest`:244 个测试全绿(这次是纯客户端 JS 改动,服务端逻辑没动,
不需要新增 Python 测试)。

## 声纹识别:区分"本人"和"背景里别人说话"(2026-07-08,同一天的第十次调整)

用户反馈:免提场景背景有人说话时,只能录到自己的声音,但更常见的是反过来
——录进了背景说话人的内容。查了业内做法(见上一节"声纹识别调查"的对话
记录,当时只是口头调研,这次落地实现):普通降噪(`noiseSuppression`)分不出
"哪个人声是你",两个人说话在声学上是同一类信号;真正对症的是"目标说话人
识别"(speaker verification)——给每句话提取一个声纹向量,跟本人的声纹参照
比对,越不像就越可能不是本人在说话。

**方案 B:异步、不卡对话速度**。转写完成后立刻正常起一轮回复(跟现在完全
一样快),声纹比对在后台并行跑;判定"不像是本人"就把这一轮撤回(复用现有
的打断/取消机制,不是另起一套)。这个决定背后有硬数据支撑:实测提取一句
2-3 秒话的声纹只要 30ms 左右,哪怕改成同步等待都感觉不到延迟,做成异步只是
双重保险,不是勉强够用。

**冷启动 + 跨会话持久化**:第一次用没有声纹参照,前几句话只用来建立参照、
不做拦截判定(`VOICE_VOICEPRINT_MIN_SAMPLES`,默认 3 条);声纹参照存盘
(`data/voice/voiceprint.npy`),跨会话复用,不用每次重新学。参照本身是"最近
20 条样本"(不是无限增长的滑动平均),采纳新样本前会先跟现有参照比对一下,
对不上就不采纳——防止某次误判(比如背景说话人的话被当成本人)污染了后续
判断。

### 模型:resemblyzer 权重转 ONNX,不在生产环境装 torch

选型过程踩了不少环境坑,记录下来省得以后重复踩:

1. **resemblyzer**(Apache 2.0,Google GE2E d-vector 思路,3 层 LSTM + 线性
   投影,256 维声纹)是这类"单用户个人助理"场景最常被推荐的轻量起点,
   `pip install resemblyzer` 本身没问题。
2. 但它依赖 PyTorch,而生产这台机器是 **x86_64 macOS + Python 3.13**——
   PyTorch 新版本已经不发 Intel Mac 的 wheel 了(`torch==2.12.1` 只有
   `macosx_14_0_arm64`,没有 x86_64),装不上。
3. 于是换思路:**只在能装 torch 的独立环境(Python 3.11 的沙箱)里,把
   resemblyzer 官方预训练权重(`pretrained.pt`,包内自带,不用另外下载)
   转成 ONNX**,生产环境只需要 `onnxruntime`(1.23.2 是最后一个还发
   x86_64 macOS + cp313 wheel 的版本,再新的版本也跟 torch 一样弃了 Intel
   Mac)跑推理,不需要 torch。
4. 转换脚本里做了数值校验:同一份输入,PyTorch 原始模型 vs 转换后的 ONNX
   模型,输出最大误差 ~1e-6(几乎完全一致)——不是"能跑就行",是真的验证过
   转换没有偷偷跑偏。
5. mel 频谱提取用 `librosa`(装的时候踩过 `numba`→`llvmlite` 解析到没有
   预编译 wheel 的老版本、被逼着从源码编译失败的坑,显式钉死
   `numba==0.62.1` 让 uv 选到有 wheel 的版本才解决);`webrtcvad`(resemblyzer
   预处理里用来裁剪长静音的)装上后导入报 `pkg_resources` 缺失(新版
   Python 不再默认带 setuptools)——干脆去掉这一步,因为我们的音频本来就是
   DashScope VAD 已经框定过的一小段话,不是任意长度的原始录音,不裁剪静音
   影响不大。
6. 最终生产依赖:`onnxruntime`、`librosa`、`numba`(见 pyproject.toml),
   **不含 torch/resemblyzer 包本身**——模型文件是转换产物
   `claude_hermes/voice/models/voiceprint_encoder.onnx`(约 5.7MB,已提交
   进仓库)。

### 实测延迟(真实 ONNX 模型 + 真实 librosa,不是占位模型)

| 音频时长 | p50 | p95 |
|------|------|------|
| 1s | 4.6ms | 6.7ms |
| 2s | 9.8ms | 21.1ms |
| 3s | 12.7ms | 14.6ms |
| 5s | 19.5ms | 29.0ms |

单核 CPU,onnxruntime 比预想的 PyTorch 基准还快(onnxruntime 对推理场景做
过专门优化)。

### 验证

- `tests/test_voiceprint.py`:纯函数 + 真实模型冒烟测试(不 mock,真的跑一遍
  ONNX 推理,确认这条产线是通的)——声纹提取的短音频兜底、余弦相似度、
  声纹参照的防污染采纳规则、存盘/读盘往返。
- `tests/test_voice_p2_realtime.py` 新增声纹相关集成测试:背景说话人被识别
  出方向不匹配后撤回这一轮且不落库、冷启动阶段不拦截、匹配的样本正常放行
  并让参照继续增长、总开关关掉时完全不生效。
- 真实端到端验证(不是纸面推导):隔离测试服务器 + 真实 DashScope 连接跑
  一遍完整对话,`done_seen = True`,服务端日志无异常,`data/voice/
  voiceprint.npy` 确实被创建(冷启动第一条样本被正确采纳)。

### 诚实说明这个功能解决了什么、没解决什么

- 阈值(`VOICE_VOICEPRINT_MATCH_THRESHOLD`,默认 0.75)是凭经验给的起点,
  没有用真实用户的多轮真机音频做过校准——真机用下来大概率需要重新调,
  调太低会漏过真的背景说话人,调太高会把本人语气变化大的正常发言也误判。
- 方案 B 的固有代价(前面讨论过、这里再确认一次):判定"不是本人"发生在
  AI 已经开始生成回复之后,如果 Claude 反应特别快(实测里几乎不会,LLM
  调用远比 30ms 声纹提取慢),AI 有极小概率已经开口说了半句才被追回来——
  这跟现有打断机制的固有限制是同一类代价,不是这个功能新引入的。
- 手机 Safari/真实用户声音的识别效果还没有真机验证过,这次验证到"产线跑
  得通、数值转换没错、延迟够快"这一层,识别准确率(阈值给得合不合适、
  真的能不能把背景说话人筛掉)需要真机用一阵子才能判断。

**真机反馈(用了一阵子之后)**:效果不错,加了声纹识别之后环境嘈杂声也不再
被误识别成语气词了(声纹核验 + VAD 阈值 0.7 两层叠加的效果)。同时反馈
停顿判定还可以再放宽一点,避免一段完整的话中间停顿稍长就被切成好几句——
`VOICE_VAD_SILENCE_MS` 从 1000→1500ms,threshold 不用再动了。
