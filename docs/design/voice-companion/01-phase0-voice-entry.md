# P0 — 语音入口与对话 MVP

> 前置阅读:[00-overview.md](00-overview.md)(隔离约束是硬性的,先读)。
> 本文档自包含,可独立交给一个 AI 实现。

## 1. 目标

手机浏览器(iOS PWA)上:主界面多一个语音入口按钮 → 进入独立语音页 →
**按住说话、松手发送 → AI 用 1-2 句口语回答并播放语音**。
一次往返(松手到听到第一句)≤ 6 秒即合格。

本期明确**不做**:后台任务、打断检测(只做停止按钮)、流式识别、Telegram 入口。

## 2. 用户故事

1. 我打开 Hermes 网页,看到主界面上有个 🎙️ 按钮,点它进入语音页;
2. 语音页有一个大的"按住说话"圆钮,我按住说"明天天气怎么样",松手;
3. 屏幕先显示我说的话的文字(转写回显),接着 AI 的文字逐句浮现,同时语音播出来,
   回答只有一两句;
4. AI 说话时我可以点"■ 停"立刻闭嘴;
5. 我说"帮我写一份周报大纲",AI 口头只说一句话结论,屏幕上有完整大纲文字;
6. 关掉 `.env` 里的 `VOICE_ENABLED` 重启后,🎙️ 按钮消失,一切如旧。

## 3. 功能需求

| # | 需求 | 验收标准 |
|---|------|---------|
| F1 | 主界面入口按钮 | `web_static/index.html` 增加一个 🎙️ 按钮(位置:顶部栏或输入区旁,与现有 UI 风格一致);点击跳转 `/voice`;`VOICE_ENABLED=0` 时不显示。改动 ≤15 行:建议 index.html 只放一个占位元素+一段 `fetch('/voice/config')` 探测显隐的极小脚本,样式等富逻辑放 voice 自己的静态文件里 |
| F2 | 独立语音页 `/voice` | 由 voice 模块自己 serve 的独立 HTML(不复用 index.html 的 JS);含:按住说话大圆钮、对话字幕区(我说的/AI 说的)、状态指示(空闲/聆听/思考/播放)、■停止按钮、返回主界面链接 |
| F3 | 按住说话录音 | 按住开始录(`getUserMedia` + `MediaRecorder`),松手停止并上传;录音期间圆钮有视觉反馈;误触(<0.5s)丢弃 |
| F4 | 语音→文字 | 上传到 `POST /voice/stt`,后端调 SenseVoice 转写(复用/复刻现有 `/transcribe` 的实现,STT 配置直接 import `config.STT_*`);转写文本立即回显到字幕区 |
| F5 | 对话轮 | 转写文本进入语音会话(独立 `session_key="voice:main"`,历史存 `data/voice/voice.db`),调 `core.agent.stream_turn()`;AI 文字流式显示在字幕区 |
| F6 | 短回复人设 | 每轮 user_text 前拼接固定指令块(模板见 §4.4);验收:普通闲聊问题回答 ≤2 句、无 markdown 符号被读出来 |
| F7 | 文字→语音播放 | 后端把 AI 回复**按句切分**,逐句用 edge-tts 合成音频,前端按顺序排队播放(边生成边播,不等全文);中文声音用 `zh-CN-XiaoxiaoNeural`(可配 `VOICE_TTS_VOICE`) |
| F8 | 停止按钮 | 点击立即:清空前端播放队列 + 通知后端停止本轮 TTS 合成;不影响下一轮 |
| F9 | 功能开关 | `VOICE_ENABLED=0`:voice 路由不注册、按钮隐藏(`/voice/config` 返回 404 或 `{"enabled":false}`) |

## 4. 技术设计

### 4.1 新增文件

```
claude_hermes/voice/
  __init__.py        # register_routes(app) 唯一对外入口
  routes.py          # aiohttp 路由:页面 + stt + 对话 SSE + tts
  session.py         # 语音会话:历史读写(data/voice/voice.db) + 调 stream_turn
  stt.py             # SenseVoice 转写(参考 web.py 的 /transcribe 实现)
  tts.py             # edge-tts 分句合成,async 生成器逐句吐 mp3 bytes
  prompts.py         # 语音模式指令块模板
  static/
    voice.html       # 独立页面(HTML+CSS+JS 单文件即可,对齐现有 index.html 的暗色风格)
```

新增依赖:`edge-tts`(pyproject.toml 加一行;它是非官方微软接口,tts.py 里做好
异常兜底——合成失败时降级为纯文字,不崩对话)。

### 4.2 路由与协议

| 路由 | 说明 |
|---|---|
| `GET /voice` | 返回 voice.html(注意 aiohttp `content_type` 不带 charset) |
| `GET /voice/config` | `{"enabled": true}`;开关关闭时整组路由不注册 |
| `POST /voice/stt` | multipart 音频 → `{"text": "..."}` |
| `POST /voice/send` | `{"text": "..."}` 发起一轮;响应用 **SSE** 流:`event:text`(增量文字)、`event:sentence`(完整一句+该句音频的取用 URL 或 base64)、`event:done` |
| `GET /voice/tts?sid=...` | (若 sentence 事件走 URL 方案)取某句合成好的音频 |
| `POST /voice/stop` | 停止当前轮的 TTS 合成 |

实现者可在「音频随 SSE base64 内嵌」和「音频走独立 URL」间自选,标准:iOS Safari
上排队播放稳定即可。

### 4.3 对话轮流程

```
松手 → POST /voice/stt → 回显转写文字
     → POST /voice/send(SSE)
        后端:load 历史(≤20 轮) → stream_turn(history, 指令块+text, session_key="voice:main")
              ├─ TextDelta → event:text 推给前端
              ├─ 句子聚合器:攒到句号/问号/感叹号/换行 → edge-tts 合成 → event:sentence
              └─ Done → 落库(user_text 存原文,不存指令块) → event:done
        前端:文字实时上屏;sentence 音频入队,<audio> 顺序播放
```

- 播放解锁:iOS 要求用户手势后才能播音频——"按住说话"这个手势天然满足,但要在
  **首次按下时**就 `new Audio()` / `AudioContext.resume()` 预热。
- 会话历史:`Turn` 结构与 `core.agent` 期望的一致(参考 `memory/session_store.py`
  里 Turn 的字段,但**存储自己实现**,写 `data/voice/voice.db`,不碰 state.db)。
- 并发:同一时刻只允许一轮在跑(前端按钮置灰即可,后端再加一把 asyncio.Lock 兜底)。

### 4.4 语音模式指令块(prompts.py,可迭代措辞但保留要点)

```
【语音模式】用户正通过语音跟你对话,你的回答会被朗读出来。规则:
1. 口语化中文,最多 2 句话,先说结论。
2. 禁止 markdown、列表、代码块、链接、emoji——这些会被读出来变成噪音。
3. 内容确实复杂时,口头只说一句话结论,并以"细节我写在屏幕上了"收尾,
   然后另起一行输出【屏幕】,后面接完整内容(这部分不会被朗读)。
正例:「明天多云二十八度,适合出门。」
反例:「好的!以下是几点建议:1. …」(列表会被读出来,禁止)

用户说:{user_text}
```

- TTS 侧配套:`tts.py` 只朗读【屏幕】标记之前的部分;【屏幕】之后的内容仅上屏。

### 4.5 对现有文件的改动(全部改动点,不得超出)

1. `config.py`(≤10 行):`VOICE_ENABLED`、`VOICE_TTS_VOICE`,跟随现有常量风格;
2. `web.py`(≤5 行):`_start_server()` 里
   `if config.VOICE_ENABLED: from ...voice import register_routes; register_routes(app)`;
3. `index.html`(≤15 行):入口按钮 + 显隐探测。

### 4.6 iOS/PWA 已知坑(实现时逐条对照)

- `getUserMedia` 权限每次页面加载都要重新授权,不要缓存 deviceId;
- 同页第二次 `getUserMedia` 会把旧 track 静音 → 整个页面生命周期**复用同一个 stream**;
- 锁屏/切后台 JS 挂起,SSE 会断 → 页面恢复时检测断线并允许重试上一轮;
- 录音格式:iOS Safari 的 MediaRecorder 产出 `audio/mp4`,SiliconFlow 接口接受即可,
  实测现有 `/transcribe` 已处理,照抄。

## 5. 测试要求

- `tests/test_voice_p0.py`:
  - 句子切分器(标点/【屏幕】截断/超长兜底)单测;
  - `/voice/config` 开关行为;
  - `/voice/send` 的 SSE 事件序列(mock stream_turn 与 edge-tts);
  - 指令块拼接与落库剥离(存的是原文)。
- 手工验收:手机 Safari 走一遍 §2 用户故事;`VOICE_ENABLED=0` 回归。

## 6. 交付物

代码 + 测试 + 本文档同目录追加 `01-phase0-实现记录.md`(实际接触点行数、
遇到的坑、移除清单复核)。合回 main 用 `zsh deploy/merge-main.sh`,
重启验证用 `zsh deploy/restart.sh`(禁手搓 kill)。
