# 语音与主会话合并 — 会话统一与代码复用优化方案

- 状态：方案草案，待实施
- 日期：2026-08-10
- 前置阅读：
  - [00-overview.md](00-overview.md)
  - [ADR 0004：语音通话只保留 Omni 管线](../../adr/0004-voice-omni-only.md)
  - [03-phase2-实现记录.md](03-phase2-实现记录.md)
- 说明：本文件以当前代码为准。`00-overview.md` 中关于独立 `/voice` 页面、独立 `data/voice/` 数据库以及“现有代码不依赖 voice”的早期约束，部分已经被后续实现记录取代，不能直接作为本次重构的实施依据。

> 本方案的目标不是“在语音页面补一个输入框”，而是把文本、按住说话、免提语音统一成同一种对话能力，再删除重复实现。
>
> 最终产品只保留一个“对话”入口；语音只是输入/输出方式之一，不再是另一套会话系统。

---

## 1. 目标与范围

### 1.1 目标

1. **统一产品入口**：主会话和语音对话合并为一个“对话”页面。
2. **统一会话上下文**：文本输入、语音输入、Omni 免提输入进入同一个会话历史。
3. **统一 Agent 轮次**：不同输入方式共用同一套锁、取消、resume、事件和历史落库逻辑。
4. **复用现有能力**：复用主会话已有的文本框、附件、模型、思考深度、草稿、命令和历史能力。
5. **降低维护成本**：删除重复的语音会话包装、回复轮、历史处理和任务通知逻辑。
6. **保留语音差异**：语音输入、Omni WebRTC、TTS、播放队列仍保留为传输/输出适配器，不把它们硬塞进普通文本层。
7. **可回滚**：旧链接、旧 API 和旧语音历史在迁移期间仍然可访问。

### 1.2 本次范围

- Web 主会话与语音会话的合并。
- 会话 Key、会话锁、SDK resume、历史落库的统一。
- 文本输入和语音输入共用一套 turn 执行流程。
- 主会话 composer、历史、任务状态和语音控制的复用。
- `voice-chat:main` 历史迁移到 canonical 会话。
- 通用任务接口与语音专用接口的边界整理。
- 旧入口、旧 API 的兼容与最终删除计划。

### 1.3 非目标

本次不做以下事情：

- 不重新选择 Omni 供应商或重新设计语音协议。
- 不恢复已经由 ADR 0004 退休的自建 WebSocket 全双工管线。
- 不重写整个前端视觉系统。
- 不把后台任务改造成当前对话的同步工具调用。
- 不为了“架构漂亮”新增大量抽象层；每增加一个模块都必须能删除一段重复代码。
- 不在没有真实测试证据的情况下删除 Omni 的静音、回声和打断保护。

---

## 2. 当前实现与主要问题

### 2.1 前端已经共处，但仍是两套视图

当前主 SPA 同时包含：

- 主会话：`#chatMain`
- 语音视图：`#callView`
- 切换函数：`openCallView()` / `closeCallView()`

位置：

```text
vococo/gateway/adapters/web_static/index.html:281-373
vococo/gateway/adapters/web_static/index.html:3550-3577
```

主会话已经有完整 composer：

- 文本输入
- 图片/音频附件
- 语音输入
- 模型选择
- 思考深度
- 草稿
- 命令菜单
- 发送/停止

位置：

```text
vococo/gateway/adapters/web_static/index.html:281-328
```

语音视图只有 transcript 和语音控制，没有文本 composer：

```text
vococo/gateway/adapters/web_static/index.html:333-373
```

**问题**：如果直接在语音视图重新写一套文本输入，会复制主会话已有逻辑，后续两边必然继续分叉。

### 2.2 会话存储共用数据库，但会话身份分裂

主会话默认使用：

```text
main
```

语音会话固定使用：

```text
voice-chat:main
```

代码位置：

```text
vococo/config.py:289-320
vococo/gateway/adapters/web.py:812-819
vococo/voice/session.py:18
```

语音历史加载也直接写死 `voice-chat:main`：

```text
vococo/gateway/adapters/web_static/index.html:3402-3413
```

**问题**：两者虽然落在同一个 `state.db`，但上下文、SDK resume ID、标题和清空操作仍然是两套语义。

### 2.3 Agent 轮次入口重复

主会话和语音会话各自处理：

- 获取历史
- 获取 resume ID
- 加锁
- 调用 `stream_turn()`
- 处理文本增量
- 处理工具事件
- 处理异常和取消
- 保存历史
- 写回 SDK session ID

语音路由仍然直接在自己的 handler 中运行一整轮：

```text
vococo/voice/routes.py:247-362
```

主会话则通过 Web Gateway 的另一套 dispatch 链路运行：

```text
vococo/gateway/adapters/web.py:667-741
vococo/gateway/run.py:54-64
vococo/gateway/core.py:184-194
```

**问题**：同一个会话一旦同时允许两条链路运行，就会出现两把锁、两个取消入口、两个 SDK resume 写入者。

### 2.4 语音模式通过用户消息包装实现

语音模式的短回复和行为规则目前通过每轮拼接 prompt 实现：

```text
vococo/voice/prompts.py:55-166
vococo/voice/prompts.py:219-243
```

语音还通过独立的 session 包装控制工具集合：

```text
vococo/voice/session.py:21-27
vococo/voice/session.py:69-83
```

**问题**：文本和语音共用历史后，模型可能继承上一轮的模式规则；同一会话有时允许文件操作，有时又被语音限制禁止，行为不稳定。

### 2.5 通用任务能力仍挂在 voice 命名空间

任务引擎本身已经是通用的，但以下能力仍使用 `/voice/*` 路由或 `voice.notify`：

- 任务列表
- 任务状态流
- SDK 任务状态
- 任务停止
- 任务完成通知
- 主 SSE 的任务桥接

相关位置：

```text
vococo/voice/routes.py:502-598
vococo/core/task_runner.py:92-99
vococo/core/task_runner.py:166-170
vococo/core/task_runner.py:306-321
vococo/gateway/adapters/web_static/index.html:1167-1281
```

**问题**：不能因为下架“语音入口”就删除整个 `vococo/voice/` 包，否则会误伤普通聊天的任务栏和后台任务。

### 2.6 Voice task 与普通 task 的合并判断

**可以合并，而且任务执行层实际上已经完成了大半合并。**

当前 `vococo/core/tasks.py` 已经明确记录：语音派发、cron 定时、普通会话发起的后台任务，本质都是同一种“后台跑一轮、可追踪、可通知”的任务。

已经统一的部分：

```text
统一 tasks 表
统一 task:<id> session key 前缀
统一状态机
统一并发上限
统一 task_runner.dispatch()
统一 task_runner.append()
统一进度和结果字段
```

当前仍然分裂的部分：

```text
API 路径       /voice/tasks* 仍然带有 voice 命名
来源字段       origin=voice/chat 仍然影响前端分组和播报策略
通知实现       通用 task_runner 仍然直接依赖 voice.notify
前端展示       voice task bar 与 chat task bar 仍然分开过滤
数据库路径     通用任务仍放在 data/voice/voice.db(历史遗留)
```

因此，不应该再做“voice task 复制一套普通 task”或“普通 task 再造一套任务板”，而应该完成剩余的**领域统一、接口中性化和展示解耦**。

#### 2.6.1 哪些必须合并

以下概念必须只有一份：

- 任务实体和数据库表。
- `queued → running → done/failed/cancelled` 状态机。
- 并发控制和排队器。
- 进度、结果、重试和追加指令。
- 任务对应的 `task:<id>` 会话。
- 任务详情、停止和状态流 API。
- 任务完成事件。

语音输入和文本输入只是任务的不同创建渠道，不应生成不同类型的执行对象。

#### 2.6.2 哪些不能直接合并

以下部分有真实语义差异，应保留为适配器：

- 语音播报、TTS 和 Omni 朗读。
- Web 页面任务栏和离线 Web Push。
- cron 的定时触发和无条件外部通知。
- SDK `TaskCreate` 清单。

特别是 `sdk_task_views` 不是后台执行任务：它只是当前会话里的待办清单，不能并入真正的 `tasks` 执行表。两者可以在前端统一成一个任务面板，但后端数据模型仍应分开，避免把“待办”误当成“正在执行的后台任务”。

#### 2.6.3 `origin` 的后续处理

当前 `origin=voice/chat/cron` 同时承担了两个职责：

1. 记录任务从哪里创建。
2. 决定任务完成后发什么通知、在什么 UI 分组。

统一会话后，文本和语音都可能从同一个 `main` 会话创建任务，不能再用 `origin` 把它们分成两种任务。

建议后续拆成：

```text
source       conversation / cron / sdk_task
input_mode   text / push_to_talk / omni
session_key  任务由哪个对话派出
```

迁移期间保留旧的 `origin` 字段兼容历史数据；新代码不再用 `origin=voice` 判断“是否可以播报”。是否播报应由当前会话的输出能力和用户设置决定。

### 2.7 当前复杂度来源

当前重复不是单纯的代码行重复，而是以下概念重复：

```text
会话身份       main / voice-chat:main
轮次执行       gateway 链路 / voice 链路
并发控制       Web session lock / voice 全局锁
事件通道       主 SSE / voice SSE / Omni DataChannel
历史处理       Web history / voice history
任务接口       /voice/tasks* / 普通会话任务调用
任务通知       通用任务 / voice notify
输出处理       文本增量 / TTS 句子 / Omni 朗读队列
```

本次优化必须先统一这些概念，再谈删除文件。否则只是把重复代码从一个文件搬到另一个文件。

---

## 3. 目标产品形态

### 3.1 统一入口

侧栏和首页只保留一个根入口：

```text
对话
```

不再同时显示：

```text
主会话
语音通话
```

旧的 `/voice` 链接继续跳转到统一对话页面，旧的主会话链接也进入同一个页面。

### 3.2 一个消息区域，多个输入/输出方式

统一页面包含：

```text
消息时间线
后台任务状态
统一 composer
可选语音控制面板
```

输入方式：

```text
文本
按住说话
免提语音 / Omni
上传图片或音频
```

输出方式：

```text
文本显示
TTS 播放
Omni 朗读
```

### 3.3 输入方式不决定 Agent 能力

语音不再因为“是语音”而使用另一套能力边界。默认情况下，文本和语音共用主会话的工具、权限和审批规则。

语音的短句、朗读和输出格式，属于 `interaction_mode` / `output_mode`，不属于另一套会话。

### 3.4 默认输出策略

| 输入方式 | 默认输出 | 说明 |
|---|---|---|
| 文本 | 文本 | 不自动播放声音 |
| 按住说话 | 文本 + TTS | 保留当前语音体验 |
| Omni 免提 | 文本 + Omni 朗读 | 文字和音频分别管理 |
| 文本 + 用户开启朗读 | 文本 + TTS/Omni | 显式开启，不隐式播放 |

---

## 4. 目标架构

### 4.1 统一会话模型

所有前台输入都使用同一套会话模型：

```text
Conversation
  ├── session_key
  ├── turns
  ├── sdk_session_id
  ├── chosen_model
  ├── effort
  └── metadata
```

前台主会话的 canonical key：

```text
main
```

普通历史会话继续使用已有会话 Key；不能因为统一入口就把所有会话强行变成一个会话。

### 4.2 统一轮次请求

建议定义一个内部统一请求对象，字段保持小而明确：

```python
ConversationTurnRequest(
    session_key: str,
    user_text: str,
    input_mode: str,       # text / push_to_talk / omni
    output_mode: str,      # text / tts / omni
    model: str | None,
    effort: str | None,
    images: list | None,
    audio: object | None,
    cwd: str | None,
)
```

不建议把语音专属字段散落在多个 handler 参数中。

### 4.3 统一 Turn Coordinator

新增一个小型的共享执行层，建议放在：

```text
vococo/core/conversation.py
```

职责只有以下几项：

1. 按 `session_key` 获取同一把锁。
2. 读取历史和 SDK resume ID。
3. 调用 `stream_turn()`。
4. 为每轮生成唯一 `turn_id`。
5. 统一转发文本、工具、错误、完成和取消事件。
6. 统一保存历史和 SDK resume ID。
7. 提供当前轮次取消能力。

它不负责：

- 浏览器 WebRTC
- 麦克风录音
- TTS 音频合成
- Omni 连接管理
- 页面 DOM

这些属于适配器。

### 4.4 输入与输出适配器

目标依赖方向：

```text
HTTP / SSE / Omni / TTS
          ↓
    Conversation Adapter
          ↓
    Turn Coordinator
          ↓
    core.agent.stream_turn
```

具体职责：

| 模块 | 保留职责 |
|---|---|
| `gateway` | 文本请求、主 SSE、历史和会话 API |
| `voice` | Omni、按住说话、语音识别、TTS、音频播放事件 |
| `core.conversation` | 统一会话轮次、锁、resume、历史和事件 |
| `core.tasks` | 后台任务执行和任务生命周期 |
| Web 前端 | 展示时间线、发送输入、控制语音和播放 |

关键原则：

> `voice` 可以依赖 `core.conversation`，但 `core.conversation` 不能依赖 `voice`。

### 4.5 统一事件模型

所有前台轮次都使用相同事件字段：

```text
turn_id
session_key
type
created_at
payload
```

事件类型至少包括：

```text
turn_start
user
thinking
text
tool_start
tool_input
tool_end
interrupted
error
done
```

语音特有的音频事件作为同一个 `turn_id` 下的附加事件：

```text
sentence
audio_start
audio_end
omni_read_start
omni_read_end
```

这样可以区分：

```text
Claude 文本生成完成
```

和：

```text
语音实际播放完成
```

不能再依赖前端的多个全局布尔值猜测这两件事是否完成。

### 4.6 任务事件与语音播报解耦

后台任务只产生通用任务事件：

```text
task_created
task_progress
task_done
task_failed
task_cancelled
```

然后由不同适配器决定如何展示：

```text
Web → 任务栏 / SSE
语音 → 播报队列
推送 → Web Push
```

建议把 `voice.notify` 拆成两部分：

```text
core/task_events.py       # 通用任务事件
网关/语音适配器             # 各自订阅并呈现
```

不要让通用 `task_runner` 继续直接依赖 `voice.notify`。

---

## 5. 代码复用与删减清单

### 5.1 必须复用的现有代码

| 能力 | 复用来源 | 目标 |
|---|---|---|
| 文本 composer | `index.html:#composer` | 统一入口只保留一份 |
| 图片/音频上传 | Web `/upload` 与 `/send` | 语音页面也使用同一套 |
| 模型选择 | 主会话 composer | 语音和文本共享选择结果 |
| 思考深度 | 主会话 composer | 不再在语音页面另造 |
| 草稿 | 主会话 draft 逻辑 | 统一草稿 Key |
| 会话列表 | `renderConvs()` / `openConv()` | 统一历史入口 |
| 任务状态 | 现有 `barTasks` / task stream | 统一任务栏 |
| 工具时间线 | 主会话事件渲染器 | 语音也能查看完整执行过程 |
| 认证、主题、侧栏 | 主 SPA | 不重复维护 |

### 5.2 保留但收窄职责的代码

`vococo/voice/` 不需要立即删除，但应逐步收窄成语音适配器：

```text
保留：
- Omni WebRTC / DataChannel
- 按住说话录音
- STT / TTS
- 音频播放队列
- 语音打断、静音、回声保护

迁出：
- 会话 Key 管理
- 历史读取和保存
- SDK resume 管理
- Agent 轮次
- 通用任务通知
- 通用任务状态 API
```

### 5.3 最终应删除的重复实现

完成迁移并稳定后，删除或合并：

1. `voice/session.py` 中重复的前台会话包装。
2. `voice/routes.py` 中重复的 Agent turn 生命周期代码。
3. 语音独立的历史读取、清空和 SDK resume 处理。
4. `/voice/send` 中与主会话重复的文本流处理部分。
5. `voice.notify` 中属于通用任务事件的部分。
6. `#chatMain` 与 `#callView` 两套重复消息渲染入口。
7. 只为“语音主会话”存在的 `voice-chat:main` 固定 Key。
8. 只为旧入口服务、且已完成兼容期的跳转和别名代码。

删除前必须完成一次移除演练，并确认：

```bash
uv run pytest
uv run vococo doctor
uv run vococo serve
```

均正常。

### 5.4 不应为了复用而强行合并的部分

以下部分仍然应该保持独立：

- Omni WebRTC 会话生命周期。
- 录音设备和麦克风权限处理。
- TTS/Omni 音频队列。
- 音频回声和播放完成判断。
- 语音专属 UI 状态球、静音和挂断按钮。

这些是不同的传输和设备问题，强行塞进通用对话层会让核心代码变复杂。

---

## 6. API 与路由整理

### 6.1 Canonical API

不建议长期维护两套完整的文本回复接口。建议让主会话接口成为 canonical API：

```text
POST /send
GET  /events
GET  /history
GET  /conversations
```

`/send` 增加必要的模式字段：

```json
{
  "text": "用户输入",
  "input_mode": "text|push_to_talk|omni",
  "output_mode": "text|tts|omni",
  "conv": "main"
}
```

### 6.2 语音 API 的定位

语音专属接口只处理传输：

```text
/voice/config
/voice/stt
/voice/omni/session
```

如果保留 `/voice/send`，它只能是短期兼容层：

```text
/voice/send → 转换参数 → /send / ConversationTurn
```

不得继续复制一份完整 Agent 轮次。

### 6.3 任务 API 迁移

将通用任务接口迁移到中性路径：

```text
/tasks
/tasks/stream
/tasks/{id}
/tasks/{id}/stop
/sdk-tasks
```

迁移期间保留：

```text
/voice/tasks
/voice/tasks/stream
/voice/sdk-tasks
```

旧路径只做转发，不新增业务逻辑。

---

## 7. 数据迁移与兼容

### 7.1 Canonical 会话

统一前台主会话：

```text
main
```

旧语音主会话：

```text
voice-chat:main
```

迁移规则：

1. 启动或首次打开统一会话时检查两边是否有历史。
2. 若 `main` 没有新历史，把 `voice-chat:main` 的 turns 合并到 `main`。
3. 若两边都有历史，按时间排序合并，并记录一次迁移事件。
4. 合并后清空旧会话的 SDK resume ID。
5. 用统一后的历史重新建立 SDK 上下文。
6. 旧 Key 保留只读别名，至少覆盖一个发布周期。

### 7.2 为什么不能直接复用旧 SDK resume ID

语音历史过去使用了不同的 prompt 和工具边界。即使数据库历史可以合并，旧的 SDK session 也不一定代表相同的 Agent 上下文。

更稳妥的做法是：

```text
迁移历史文本
→ 废弃旧 resume ID
→ 新会话从合并后的历史继续
```

### 7.3 历史字段

统一后的每个 turn 至少应保留：

```text
user_text
assistant_text
created_at
input_mode
output_mode
turn_id
```

音频原始文件不默认永久保存；如果当前实现没有保存原始音频，不为了本次合并强行引入大文件存储。需要回放时，另立需求。

### 7.4 兼容和回滚

兼容开关建议：

```text
CONVERSATION_UNIFIED=1
```

回滚时：

1. 停止写入新的统一格式。
2. 保留迁移后的 `main` 数据，不删除。
3. 旧 `/voice/*` 兼容路由继续可用。
4. 依据 `turn_id` 和迁移日志定位异常。
5. 不执行不可逆数据库删除。

---

## 8. 分阶段实施计划

### Phase 0：冻结现状与补齐契约

目标：先把当前行为记录下来，避免重构后无法判断是新问题还是旧问题。

交付：

- 会话 Key、锁、resume、历史 API 的单元测试。
- 文本一轮、语音一轮、任务一轮的事件快照。
- 当前 Omni 静音、打断、播放完成的真机基线。
- `/voice/send` 与 `/send` 的行为差异清单。

原则：本阶段不改变用户路径。

### Phase 1：抽取统一 Turn Coordinator

目标：文本和语音先共用后端轮次，不改变前端入口。

动作：

- 新增 `core/conversation.py`。
- 把锁、历史、resume、事件、保存逻辑迁入。
- `gateway.core` 改为调用统一层。
- `voice.routes` 改为调用统一层。
- 保留旧 API 作为适配入口。

验收：

- 文本和语音串行执行时历史一致。
- 同一 session 并发请求只能有一个活动 turn。
- 取消不会覆盖另一轮的 SDK resume ID。

### Phase 2：统一前端输入

目标：语音视图获得主会话的完整输入能力。

动作：

- 复用现有 `#composer`。
- 统一消息时间线和事件渲染。
- 文本输入走 canonical `/send`。
- 语音输入只负责产生文本或音频事件，再交给统一 turn。
- 语音控制面板变成可展开/可隐藏区域。

验收：

- 文本、按住说话、Omni 可以交替使用。
- 图片、音频、模型和思考深度仍然可用。
- 任务栏在统一页面正常显示。

### Phase 3：统一会话与历史

目标：让用户真正得到一条连续上下文。

动作：

- 使用 canonical `main`。
- 执行 `voice-chat:main` → `main` 迁移。
- 统一清空、标题、模型和历史读取语义。
- 保留旧 Key 只读 alias。

验收：

- 旧语音历史能在统一会话中查看。
- 迁移后新文本和语音都能正确接续。
- 刷新、重启、切换设备后上下文不丢。

### Phase 4：统一入口

目标：用户只看到一个“对话”入口。

动作：

- 默认进入统一对话页面。
- 移除侧栏中的独立“主会话”和“语音通话”重复入口。
- `/voice` 和旧推送链接保留兼容跳转。
- `VOICE_ENABLED` 改为控制语音能力，不再控制整个对话入口。

验收：

- 新用户只看到一个入口。
- 旧书签、旧推送、旧链接不 404。
- 关闭语音能力后文本对话仍可用。

### Phase 5：迁移通用任务能力并清理

目标：让 voice 包只剩语音传输和输出适配器。

动作：

- 迁移通用任务 API 到 `/tasks*`。
- 把通用任务事件从 `voice.notify` 中拆出。
- 删除旧 `/voice/*` 业务实现，仅保留兼容转发。
- 删除重复 session、turn、history 代码。
- 执行移除演练。

验收：

- `uv run pytest` 全绿。
- Web 普通任务、语音任务、cron 任务均正常。
- 删除语音适配器后，文本对话和后台任务仍能独立运行。

---

## 9. 风险、取舍与观测

### 9.1 主要风险

| 风险 | 后果 | 控制措施 |
|---|---|---|
| 两条链路仍然各自加锁 | 同一会话并发污染 | 统一 Coordinator，锁只按 canonical session 获取 |
| 直接复用旧 SDK resume | prompt/工具上下文不一致 | 历史迁移后新建 SDK 上下文 |
| 语音输出晚于文本完成 | 播放和下一轮状态错位 | `turn_id` + 独立音频完成事件 |
| 删除 voice 包过早 | 任务栏和普通聊天任务失效 | 先迁移任务 API，再删除语音业务代码 |
| 复制主会话 composer | 两套输入逻辑继续分叉 | 直接复用现有 DOM/逻辑 |
| 合并后权限边界变宽 | 语音模式误调用工具 | 统一使用主会话权限/审批，不靠语音 prompt 禁用 |
| Omni 自动回复和 Claude 朗读抢声音 | 重复播放或自回声 | 保留现有静音隔离和真机回归测试 |

### 9.2 观测字段

统一上线后，为每轮记录：

```text
session_key
turn_id
input_mode
output_mode
started_at
first_text_at
done_at
audio_done_at
interrupted
error_type
```

重点观察：

- 文本首字延迟。
- 语音转写到 Claude 首字延迟。
- 语音播放完成率。
- 打断后错误恢复率。
- 同一 session 并发冲突次数。
- 旧历史迁移失败数。
- 兼容 API 调用量。

---

## 10. 测试要求

### 10.1 单元测试

- 同一 `session_key` 只能有一个活动 turn。
- 不同 session 可以并行。
- 取消只影响目标 `turn_id`。
- 旧 turn 不能覆盖新 turn 的 SDK resume ID。
- 文本/语音/Omni 三种输入生成相同的核心 Agent 事件。
- `voice-chat:main` 迁移后历史顺序正确。
- 迁移重复执行不会重复追加 turns。
- 旧 API 转发参数正确。

### 10.2 集成测试

- `/send` 文本 → 统一 turn → `/events`。
- `/voice/send` 兼容入口 → 统一 turn。
- Omni 转写文本 → 统一 turn → Omni 朗读事件。
- 任务创建、任务进度、任务完成在统一任务流可见。
- 图片/音频附件仍能进入同一会话。
- 清空上下文后下一轮从零开始。

### 10.3 浏览器与真机测试

至少验证：

1. 文本 → 语音 → 文本连续对话。
2. 语音朗读中输入文本并打断。
3. Omni 说话时 Claude 不重复回声。
4. iOS PWA 锁屏、切后台、切换网络后可恢复。
5. 麦克风拒绝权限时，文本输入仍可用。
6. 任务完成播报不会和当前回复抢音频。
7. 旧 `/voice` 链接能正确进入统一页面。
8. 刷新页面后当前会话和任务状态可恢复。

---

## 11. 交付物与完成标准

### 11.1 代码交付物

- 统一的会话轮次执行层。
- 统一的前台输入和消息时间线。
- 统一的 `main` 会话历史。
- 语音作为输入/输出适配器。
- 中性任务 API 与任务事件。
- 旧 API、旧链接和旧历史兼容层。
- 删除重复 session/turn/history/notify 实现。

### 11.2 文档交付物

- 本方案实施记录。
- 如最终架构边界与本方案一致，再新增 ADR 0005，记录已接受的决策。
- 更新 `00-overview.md` 中已经过时的独立会话和移除清单。
- 更新语音目录中仍然有效的入口、路由和数据说明。
- 记录一次兼容层删除条件和移除演练结果。

### 11.3 完成标准

只有同时满足以下条件，才算完成合并：

- 用户只看到一个对话入口。
- 文本和语音可以交替使用同一上下文。
- 同一会话不会发生并发污染。
- 主会话原有的附件、模型、思考深度、任务和历史能力没有丢失。
- 语音特有的 Omni、打断、静音和播放逻辑仍然可靠。
- 主会话和语音没有两份独立的 Agent 轮次实现。
- 通用任务不再依赖语音模块才能运行。
- 旧链接和旧历史有明确兼容或迁移结果。
- `uv run pytest`、`uv run vococo doctor` 和真实浏览器/手机验证通过。

---

## 12. 最终判断

本次不应被定义为“给语音页面加文本框”，而应定义为：

> **把语音和文本收敛成一个可插拔输入/输出适配器体系，并删除两套会话运行时之间的重复。**

推荐的最终边界是：

```text
core.conversation 负责一次对话轮次
core.tasks         负责后台任务
 gateway           负责文本 Web 传输
 voice             负责语音传输与音频输出
 frontend          负责统一对话界面
```

实施顺序必须是：

```text
统一轮次
→ 复用输入和事件渲染
→ 迁移历史
→ 统一入口
→ 迁移任务依赖
→ 删除重复代码
```

不要先隐藏入口、再靠补丁把两个会话勉强接在一起。那样短期看起来快，长期会保留两套状态机和两套上下文问题，达不到“代码更简洁、更复用”的目标。
