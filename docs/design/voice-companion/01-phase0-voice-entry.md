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
5. 我说"帮我查一下笔记里关于 XX 的内容",AI 先放一声科技感提示音暗示"正在处理",
   查完之后把查到的要点**用语音完整讲出来**(不是只说一句"查到了"就把内容丢给屏幕)——
   我全程不用看屏幕也能拿到答案;屏幕上仍同步展示完整文字,方便回看/复制;
6. 关掉 `.env` 里的 `VOICE_ENABLED` 重启后,🎙️ 按钮消失,一切如旧。

> 2026-07-07 修正:原 v1 的用户故事 5 是"AI 口头只说一句话结论,细节全部丢给屏幕",
> 真机实测后发现不对——目标用户是完全不看屏幕的纯语音场景,语音里必须把要点讲完整,
> 屏幕只是兜底,详见 [00-overview.md](00-overview.md) §5「纯语音优先」。

## 3. 功能需求

| # | 需求 | 验收标准 |
|---|------|---------|
| F1 | 主界面入口按钮 | `web_static/index.html` 增加一个 🎙️ 按钮(位置:顶部栏或输入区旁,与现有 UI 风格一致);点击跳转 `/voice`;`VOICE_ENABLED=0` 时不显示。改动 ≤15 行:建议 index.html 只放一个占位元素+一段 `fetch('/voice/config')` 探测显隐的极小脚本,样式等富逻辑放 voice 自己的静态文件里 |
| F2 | 独立语音页 `/voice` | 由 voice 模块自己 serve 的独立 HTML(不复用 index.html 的 JS);含:按住说话大圆钮、对话字幕区(我说的/AI 说的)、状态指示(空闲/聆听/思考/播放)、■停止按钮、返回主界面链接 |
| F3 | 按住说话录音 | 按住开始录(`getUserMedia` + `MediaRecorder`),松手停止并上传;录音期间圆钮有视觉反馈;误触(<0.5s)丢弃 |
| F4 | 语音→文字 | 上传到 `POST /voice/stt`,后端调 SenseVoice 转写(复用/复刻现有 `/transcribe` 的实现,STT 配置直接 import `config.STT_*`);转写文本立即回显到字幕区 |
| F5 | 对话轮 | 转写文本进入语音会话(独立 `session_key="voice:main"`,历史存 `data/voice/voice.db`),调 `core.agent.stream_turn()`;AI 文字流式显示在字幕区 |
| F6 | 语音人设 | 每轮 user_text 前拼接固定指令块(模板见 §4.4);验收:回答的实质内容(结论/要点/查到的东西)都被念出来,不因为内容多就只说一句空话把细节全丢给屏幕;仍然禁止 markdown 符号被读出来 |
| F7 | 文字→语音播放 | 后端把 AI 回复**按句切分**,逐句用 edge-tts 合成音频,前端按顺序排队播放(边生成边播,不等全文);中文声音用 `zh-CN-XiaoxiaoNeural`(可配 `VOICE_TTS_VOICE`) |
| F8 | 停止按钮 | 点击立即:清空前端播放队列 + 通知后端停止本轮 TTS 合成;不影响下一轮 |
| F9 | 功能开关 | `VOICE_ENABLED=0`:voice 路由不注册、按钮隐藏(`/voice/config` 返回 404 或 `{"enabled":false}`) |
| F10 | 干活垫话(2026-07-07 补,2026-07-09 改成音效) | 本轮第一次出现顶层工具调用(查资料/跑命令等)时,立即插播一声预置的科技感提示音(暗示"正在处理"),不等模型自己说,也不再念白;和正式回复共用同一条播放队列 |

## 4. 技术设计

### 4.1 新增文件

```
vococo/voice/
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

> 2026-07-07 三次修正(同一天,真机实测出来的,以此版为准):
> v1"最多 2 句话、细节丢屏幕"——错,查笔记这类操作 AI 只说"查到了",实质内容
> 用户完全听不到。v2 改成"纯语音优先"但保留了【屏幕】例外——还是错,模型一遇到
> "内容有点多"就用这条例外把实质内容全丢进屏幕。v2.5 去掉屏幕专属这条路,改成
> "内容长就概括讲重点"——又冒出新问题:模型"概括"出来的内容本身还是偏长
> (一次答几大类、每类还展开讲),听着像连续独白而非对话。**v3 加了硬性字数上限
> (150字)+ "留钩子问要不要展开"的收尾方式**,把继续听多少的决定权交还给用户。

```
【语音模式】用户正通过语音跟你对话,他现在很可能没在看屏幕——这是一个纯语音助手,
你说的每一句话都会被朗读出来,规则:
1. 你输出的文字就是要被朗读的内容,没有"只写在屏幕上不用念"这回事——
   不要因为内容长就只说一句空话搪塞(比如不能只说"帮你查到了/写好了"就不讲具体内容)。
2. 每次回复控制在 150 字以内(正常聊天几句话的量),像打电话一样一次说一小段、
   留出对方接话的空间,不要一次性把所有内容都倒出来。
3. 内容明显讲不完时:先讲最关键的一两点,然后主动问一句"要不要听 xx"或
   "要不要我展开讲讲 YY",把是否继续的决定权交给用户,不要因为想讲全就超字数。
4. 口语化中文,自然说话;禁止 markdown、代码块、链接符号——没法读;
   列表/要点改成"第一…第二…"或"分别是…"这样说出来,不要用星号/编号。
5. 预计要花一点时间才能干完的事(查资料、跑命令),后端会自动垫一声提示音
   (见 F10),你不用自己说等待话术,专心把结果讲清楚就行。
正例(查笔记,多于一条时):「找到了三篇,分别是会议纪要、项目复盘和一篇读书笔记,
  要不要我先说说会议纪要里讲了什么?」
反例:「帮你查到了,细节看屏幕。」/ 一口气把三篇笔记的内容全部展开讲完

用户说:{user_text}
```

- TTS 侧配套:`tts.py` 的句子切分器朗读模型输出的**全部**文字,没有任何
  "只上屏不朗读"的分支;屏幕文字区就是纯粹的朗读内容回显。
- 字数上限(150字)目前是**纯提示词层面的软约束**,不是后端硬截断——模型仍可能
  偶尔超出,若实测发现超得离谱,再考虑加后端兜底(如超过 N 句强制收尾+补一句
  "要继续吗")。

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

代码 + 测试 + 本文档同目录追加 `archive/01-phase0-实现记录.md`(实际接触点行数、
遇到的坑、移除清单复核)。合回 main 用 `zsh deploy/merge-main.sh`,
重启验证用 `zsh deploy/restart.sh`(禁手搓 kill)。
