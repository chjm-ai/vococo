# ADR 0004:语音通话只保留 Omni 管线

- 状态:已接受(2026-07-11,分步落地中)
- 相关:[voice-companion 设计文档](../design/voice-companion/00-overview.md)(其分期描述的级联架构已被本决策取代)

## 背景

语音通话历史上并存过三条管线:

1. **P0 SSE 批处理**——按住说话/文字 → `/voice/send`(STT → Claude 一轮 → 断句 → Qwen-TTS → SSE 逐句下发);
2. **P2 WS 全双工**——`/voice/ws` + `voice/ws.py`(863 行):DashScope 实时识别上游、四态状态机、打断两阶段提交、防自回声、声纹核验、看门狗;
3. **P3 Omni WebRTC**——浏览器直连阿里云 Qwen-Omni-Realtime 当"耳朵+嘴巴",大脑仍是 Claude(转写文字回灌 `/voice/send`,tts=false,Omni 朗读)。

2026-07-10 起 `VOICE_OMNI_ENABLED=true` 后,前端免提**无条件走 Omni 且失败不回落**
(index.html `startHandsFree` 短路),P2 从此不可达;唯一复活条件是"登录时
/voice/config 预取失败"——而那种场景下 P2 也只会带来 404 重连死循环。
同时 P2 与 P0 各自维护一套几乎相同的「回复轮流水线」(消费事件流→垫话→断句→
并行合成→保序发送),双份维护税已实付三次(并行合成优化、垫话气泡功能各改两遍,
话术列表跨模块伸手)。

## 决策

**免提通话只保留 Omni 管线;P2 全双工整体退休。** 具体:

- 删 `/voice/ws` 路由与 `voice/ws.py`;前端 `startHandsFree` 在 `!omniEnabled`
  时回落按住说话(P0 路径,仍是兜底输入方式),不再连 WS;
- 删声纹模块 `voice/voiceprint.py` + ONNX 模型 + 其测试与配置——它的唯一调用方
  是 ws.py 的 `_voiceprint_gate`,且 **Omni 模式下音频走浏览器↔阿里云 WebRTC,
  服务端拿不到 PCM**,该模块在保留架构里没有任何可能的调用点;依赖
  onnxruntime/librosa/numba/setuptools 一并移除;
- 删 P2 专属配置(`VOICE_WS_ENABLED`/`VOICE_FALSE_POSITIVE_TIMEOUT_MS`/
  `DASHSCOPE_REALTIME_MODEL`/`VOICE_VAD_SILENCE_MS`/`VOICE_SELF_ECHO_THRESHOLD`/
  `VOICE_POST_DONE_ECHO_GUARD_MS`/`VOICE_MIN_SPEECH_MS`/`VOICE_VOICEPRINT_*`);
- **判定纯函数留档** 原保留为活代码 `voice/heuristics.py`(打断截断/语气词/物理时长/
  回声 containment,连同真机调优终值),配套测试 `tests/test_voice_heuristics.py`。
  2026-07-23 架构复盘确认零调用方(Omni 模式下服务端拿不到 PCM,没有任何可能的调用点)
  ——带一整套测试跑无人调用的代码成本高于"留档"应有的成本,故从生产代码删除,原样存档于
  [docs/design/voice-companion/04-echo-heuristics-archive.md](../design/voice-companion/04-echo-heuristics-archive.md)。
  Omni 链路的回声问题未根治,当前活的兜底是前端 matchOmniEcho(前缀+编辑距离),
  与存档的 containment 算法互补,将来加服务端第二道兜底可从存档原样迁回。

## 代价 / 已知取舍

- 失去"Omni 故障时切回自建全双工"的选项——接受:P2 自 7/10 起没再被真机验证,
  留着也只是未经维护的假保险;真出问题时兜底是按住说话(P0)。
- ws.py 里打断两阶段提交、看门狗等设计经验随代码删除——git 历史可考
  (删除前最后版本见本 ADR 引入时的提交),关键判定与调优值已留档 heuristics.py。
- `VOICE_OMNI_ENABLED` 的语义从"两条链路二选一"变成"免提有无"。

## 恢复路径

若将来要复活自建全双工:`git log --diff-filter=D -- vococo/voice/ws.py`
找到删除提交,恢复 ws.py + 本 ADR 列出的配置常量 + `/voice/ws` 路由注册即可;
但更可能的正确做法是基于 heuristics.py 留档重新设计(把轮次引擎与传输层分开,
见当时架构评审的候选 2)。
