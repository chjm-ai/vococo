# Voice Companion（语音伴聊模式）— 总纲

> ⚠️ **2026-07-12 重大修订(读旧章节前必看)**:免提通话已收敛为 **Omni-Realtime
> WebRTC 唯一管线**(见 [ADR 0004](../../adr/0004-voice-omni-only.md))。本目录里
> 关于「级联 STT→Claude→TTS」与 P2 全双工(ws.py / DashScope 实时 WS / 声纹识别)
> 的架构描述**均已成为历史**——那些代码已删除,判定纯函数存档在
> [04-echo-heuristics-archive.md](04-echo-heuristics-archive.md)(原
> `claude_hermes/voice/heuristics.py`,2026-07-23 因零调用方从活代码降级为文档)。
> 仍然有效的部分:P1 任务板全套(派活/进度/
> 播报)、/voice/send 回复轮、按住说话兜底、§2 的后端隔离约束与 §5 验收哲学。
> 现行移除清单 = `rm -rf claude_hermes/voice/ tests/test_voice_*` + 摘 index.html
> 里 #callView 相关标签/样式与通话 IIFE 脚本段 + config.py 的 VOICE_ 常量 +
> `rm -rf data/voice/`。注意:通话前端**必须留在 index.html 的 IIFE 里**——
> 2026-07-12 曾把 Omni 段拆成独立 js 文件,因通话段与聊天逻辑靠 IIFE 隔离同名
> 符号(vdbg/setStatus 等全是闭包内符号),外部文件看不到,真机全挂,已回滚;
> 要拆必须先把共享状态收敛成显式对象再动。
>
> 本目录是「边聊天边干活」新功能的完整规划。实现按期拆分,每期一份自包含文档,
> 可以交给**互不知情的不同 AI** 分别实现:
> - [01-phase0-voice-entry.md](01-phase0-voice-entry.md) — P0:入口按钮 + 语音对话 MVP
> - [02-phase1-task-board.md](02-phase1-task-board.md) — P1:任务板 + 派活/查进度/汇报闭环
> - [03-phase2-experience.md](03-phase2-experience.md) — P2:体验进阶(流式识别/打断/更好的声音)
>
> 实现任何一期之前,必须先读完本文件——这里定义了所有期共用的**隔离约束**,违反即返工。

## 1. 产品定位

给 claude-hermes 增加一种语音聊天模式:用户在手机上像打电话一样跟 AI 说话,AI 用
1-2 句短话回答;需要干重活时,AI 把活儿派给后台独立会话去跑,用户继续聊,随时可问
"刚才那个任务怎么样了",干完了 AI 会主动汇报(在线时口头说,离线时推送通知)。

一个比喻:现有 Hermes 是"埋头干活的工程师"(收到消息→干完才回话);本功能给他配一个
"前台秘书"(秒回短句、重活转给工程师、随时可查进度)。

调研结论(2026-07,详见 git 历史中的调研会话):
- 该形态是业界空白——ChatGPT 语音模式至今不能派发后台任务;
- 语音层走**级联式**(语音识别 → Claude → 语音合成),不用端到端语音模型,
  因为大脑必须是 claude-agent-sdk,且延迟瓶颈在 Claude 首字(1-3s),不在语音链路;
- 任务系统抄三个已验证设计:派发即返回拿任务 ID(OpenAI Codex)、
  播报三档 INTERRUPT/WHEN_IDLE/SILENT(Google Gemini Live)、
  进度=增量动作摘要而非百分比(GitHub Copilot)。

## 2. ⚠️ 隔离约束(最高优先级,每期都必须遵守)

> **2026-07-09 修订**:前端不再隔离。独立 `/voice` 页已退休(路由重定向到 `/`),
> 语音通话改成主 SPA(`gateway/adapters/web_static/index.html`)里的一个原地叠加
> 视图(`#callView`),跟聊天视图共用侧栏/主题/登录态,不再走整页跳转——用户明确
> 要求"原地叠加、通话不退出",这跟"改一处 index.html ≤15 行"的预算天然冲突,
> 权衡后放弃前端隔离,换取更好的通话体验。**后端仍然隔离**:`claude_hermes/voice/`
> 包、`data/voice/` 独立数据库、`web.py`/`routes.py` 的挂载点没变,2.4 节的移除
> 清单里"1/3/4 步"依然成立;第 2 步里 index.html 的"≤15 行"预算作废,移除时改成
> 手动摘掉 `#callView` 相关标签/样式/脚本(已不是一个可孤立 diff 的小改动)。

**本功能是实验性的:如果体验不好,会被整体移除。** 因此代码必须做到"删干净不留疤":

### 2.1 代码归宿

- 所有新后端代码放 **`claude_hermes/voice/`**(新建包),前端独立页面放
  `claude_hermes/voice/static/`。
- 所有新数据文件放 **`data/voice/`** 目录(独立 SQLite,**不碰** `data/state.db`)。
- 测试放 `tests/test_voice_*.py`。

### 2.2 接触点预算(对现有文件的改动上限)

| 现有文件 | 允许改动 | 内容 |
|---|---|---|
| `gateway/adapters/web.py` | ≤ 5 行 | `import` + `VOICE_ENABLED` 判断 + 调 `voice.register_routes(app)` 一次(路由列表在 `_start_server()`,约 L1242) |
| `gateway/adapters/web_static/index.html` | ~~≤ 15 行~~ 已作废(2026-07-09) | 见上方修订说明:通话视图 `#callView` 整体并入此文件 |
| `config.py` | ≤ 10 行 | `VOICE_ENABLED` 等 voice 前缀的配置常量 |
| `core/agent.py` | ≤ 10 行,仅 P1 允许 | 给 `stream_turn()` 加一个**可选**参数注入额外工具,默认 `None` 时行为与现在完全一致(见 P1 文档) |
| **其余一切现有文件** | **0 行** | 尤其 `core/prompt.py`、`gateway/core.py`、`gateway/run.py`、`cron/`、`tools/`、`memory/` 一律不碰 |

### 2.3 依赖方向

- `claude_hermes/voice/` **可以 import** 现有代码(`core.agent.stream_turn`、`config`、
  `gateway.adapters.web_push` 等)——单向依赖。
- 现有代码**永远不 import** voice 模块,唯一例外是 2.2 表中 web.py 那 ≤5 行挂载钩子。

### 2.4 移除清单(必须始终成立)

彻底移除本功能 = 以下四步,之后 `uv run pytest` 全绿、serve 正常跑:

1. `rm -rf claude_hermes/voice/ tests/test_voice_* docs/design/voice-companion/`
2. 还原 web.py 的 ≤5 行、index.html 的 ≤15 行、config.py 的 ≤10 行、
   (P1 后)agent.py 的 ≤10 行
3. `rm -rf data/voice/`
4. `.env` 删掉 `VOICE_` 前缀变量

每期交付时,须在 PR/commit 说明里**重申当期结束后的移除清单**。

### 2.5 功能开关

- 环境变量 `VOICE_ENABLED`(默认 `1`)。设 `0` 时:voice 路由不注册、
  主界面入口按钮不渲染(按钮显隐由前端向 `/voice/config` 探测或后端注入,见 P0)。

## 3. 分期总览

| 期 | 一句话目标 | 关键交付 | 依赖 |
|---|---|---|---|
| **P0** | 手机上按住说话,听到 AI 短句回答 | 入口按钮、独立 `/voice` 页、录音→转写→Claude→edge-tts 播放、停止按钮 | 无 |
| **P1** | 边聊边干活:派活、查进度、完成汇报 | 任务表、dispatch/list/query 三工具、后台执行器、在线播报+离线推送、任务卡片 UI | P0(语音会话已存在);但任务系统本身与语音解耦,理论上可独立先做 |
| **P2** | 接近 ChatGPT 语音手感 | 流式识别边说边出字、CosyVoice2 音色、开口即打断、思考音效、主动插话档、审批卡片 | P0+P1 |

## 4. 全期共用的技术事实(实现前先核对,行号会漂移)

- **repo 根**:`claude_hermes/` 包;命令 `uv sync --extra dev` / `uv run pytest` /
  `uv run claude-hermes serve`。**必须用 `uv run` 从当前目录跑**(editable 安装会让
  worktree 改动被主仓库已安装包屏蔽)。
- **agent 入口**:`core/agent.py` 的
  `stream_turn(history, user_text, model=None, images=None, cwd=None, resume=None, session_key=None)`
  流式产出事件(`TextDelta`/`ToolStarted`/`Done` 等,定义见同文件);
  `run_turn(history, user_text, model)` 是非流式封装(cron/scheduler.py:89 有用例)。
  voice 模块直接调它们,**自带 session_key 命名空间**:前台会话 `voice:<id>`、
  后台任务 `voice-task:<id>`。
- **现有转写接口**:`web.py` 的 `POST /transcribe`(SenseVoice via SiliconFlow +
  LLM 清洗,配置在 `config.py` 的 `STT_*` 常量)。voice 模块可 import 其中的
  纯函数复用;若其实现与 handler 耦合,则在 voice 包内复制一份小实现(可接受的重复,
  换取零耦合)。
- **鉴权**:web.py 的 `_security_mw` 中间件是 app 级的,voice 路由挂进同一 app
  自动被 `WEB_AUTH_TOKEN` 保护,无需自己做鉴权。
- **推送**:`gateway/adapters/web_push.py`(VAPID + ServiceWorker)可直接 import 调用。
- **aiohttp 坑**:`web.Response(content_type=...)` 不能带 charset(会 500),
  用 `web.Response(text=..., content_type="text/html")` 这类安全形态。
- **重启 serve**:只允许会话内 `restart_self` 工具或外部 `zsh deploy/restart.sh`,
  禁止手搓 pgrep/kill。
- **合回 main**:`zsh deploy/merge-main.sh`(worktree 里 `git checkout main` 必失败)。
- **系统提示不可改**:语音模式的"短回复人设"通过**每轮在 user_text 前拼接固定指令块**
  实现(P0 文档给了模板),不改 `core/prompt.py`。

## 5. 验收哲学

- 每期以「用户故事跑通」验收,不以代码量验收;
- **纯语音优先**(2026-07-07 实测后两次修正,以此条为准):目标用户全程不看屏幕,
  靠语音完成整个交互。**模型输出的文字就是要被朗读的内容,没有"只写不读"这回事**——
  第一次改成"要点必须念出来"之后,实测发现模型一遇到"内容有点多"(比如笔记原文)
  就会靠"口头讲结论、原文丢屏幕"这条例外把实质内容全部丢进屏幕,用户根本不看屏幕,
  等于没回答。所以**彻底去掉了"屏幕专属、不朗读"的机制**:内容长就概括讲重点,
  不是逐字照读,但一定要讲出来。屏幕文字区仍然保留,但只是"念了什么就显示什么"的
  回显(方便回看/复制),不再存在任何只写不读的内容。
  ~~(旧版曾写"语音只播 1-2 句,完整内容永远落在页面文字区";后来又改成"细节可以
  口头讲结论、原文丢屏幕"——两版都已被上面这条取代)~~
- 干活要有反馈:预计会花时间的操作(查资料、跑代码)要先出声"稍等,我在查/在办",
  不能让用户对着沉默的手机干等;等待期间可以有轻量提示音/背景音效垫着,不能死寂。
- 长任务(几分钟到几十分钟)要派给后台,不能占住这一轮对话——这是 P1 的核心,
  过程中用户可以继续聊别的、可以随时问进度,做完了 AI 主动汇报。
- 汇报懂事:默认"等用户说完再说"(WHEN_IDLE),绝不默认抢话;
- 每期结束跑一遍第 2.4 节移除演练(git stash 式验证即可,不必真删)。
