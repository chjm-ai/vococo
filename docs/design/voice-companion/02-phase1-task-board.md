# P1 — 任务板:派活 / 查进度 / 完成汇报

> 前置阅读:[00-overview.md](00-overview.md)(隔离约束)+ [01-phase0-voice-entry.md](01-phase0-voice-entry.md)(语音会话已存在)。
> 本文档自包含,可独立交给一个 AI 实现。**本期是整个功能的差异化内核。**

## 1. 目标

语音(或文字)会话里,AI 能把耗时任务派发给**后台独立会话**执行,派发后立刻回话;
用户随时口头查进度;任务完成后 AI 懂事地汇报(在线等空闲播报,离线推送通知)。

本期明确**不做**:打断、流式识别、审批卡片(P2)、跨 Telegram 派发(留待后续)。

## 2. 用户故事(验收即按这四条)

1. **派活**:「帮我分析一下 data 目录里的日志,总结错误规律」→ AI 1-2 秒内口头回
   「好,我去办,好了叫你」,屏幕出现任务卡片(编号、标题、状态"进行中");
   随后继续问天气,对话完全不受影响;
2. **查进度**:「刚才那个任务怎么样了?」→ AI 口头一句话概括当前进展
   (「日志读完了,正在归类错误类型」);
3. **完成汇报(在线)**:任务完成,等我说完当前这句话后,AI 插播一句
   「对了,日志分析完了,主要是三类超时错误,详情在屏幕上」;
4. **完成通知(离线)**:我已关掉页面 → 手机收到系统推送「任务完成:日志分析」,
   点开进入 /voice 能看到结果全文。

## 3. 功能需求

| # | 需求 | 验收标准 |
|---|------|---------|
| F1 | 任务对象 | 每个任务有:`id`(短随机串)、`title`(AI 起的短名)、`prompt`(完整任务描述)、`cwd`(可选工作目录)、`status`(queued/running/done/failed/cancelled)、`progress_note`(一句话当前进展,持续更新)、`result_summary`(一句话结果)、`result_full`(完整回复)、时间戳。存 `data/voice/voice.db` 的 `tasks` 表 |
| F2 | 派发工具 `voice_dispatch_task` | Claude 在语音会话中可调用;参数 `title/prompt/cwd?`;**立即返回** task_id,不等执行;后台并发跑,互不阻塞 |
| F3 | 查询工具 `voice_query_task` | 参数 `task_id`(可省略=最近一个);返回 status + progress_note + (完成时)result_summary,供 AI 口语化转述 |
| F4 | 列表工具 `voice_list_tasks` | 返回未归档任务的 id/title/status/progress_note |
| F5 | 后台执行器 | 每个任务一个 asyncio task,调 `stream_turn(history=[], prompt, cwd=..., session_key=f"voice-task:{id}")`;消费事件流:每次 `ToolStarted` 之类的动作事件把"正在做什么"写入 `progress_note`(节流:≥5s 一次);`Done` 时写 result_full,并生成 result_summary |
| F6 | 进度摘要口语化 | `progress_note` 必须是人话短句(如「正在读取 app.log」),不是原始日志;实现:由事件的工具名+参数模板化生成即可,不必额外调 LLM |
| F7 | result_summary | 任务完成后压成 ≤50 字一句话:取 result_full 首段,若太长可用一次轻量 LLM 调用压缩(用 `run_turn([], 压缩提示词)`,失败降级为截断) |
| F8 | 在线播报(WHEN_IDLE) | 任务终态时,若 /voice 页面 SSE 在线:推 `event:task_done`;前端**等当前播放队列空且用户未在录音**时,把汇报句加入播放队列并上屏。绝不打断正在播放/录音的过程 |
| F9 | 离线推送 | SSE 不在线时,调现有 `gateway/adapters/web_push.py` 发系统通知(标题=任务 title,正文=result_summary,点击打开 /voice) |
| F10 | 任务卡片 UI | /voice 页面加任务抽屉:列表(状态色点+title+progress_note)、点开看 result_full、"停止"按钮(置 cancelled 并 cancel asyncio task) |
| F11 | 重启自愈 | serve 重启后,残留 running 任务标记为 `failed`(progress_note="服务重启,任务中断"),并照常走 F8/F9 通知;不尝试自动续跑 |
| F12 | 人设扩展 | 语音指令块(P0 的 prompts.py)追加派活规则,见 §4.4 |

## 4. 技术设计

### 4.1 新增文件(全部在 voice 包内)

```
vococo/voice/
  tasks.py       # 任务表 CRUD(sqlite3,data/voice/voice.db 的 tasks 表)
  executor.py    # 后台执行器:spawn / 进度采集 / 终态处理 / 重启自愈
  task_tools.py  # 三个工具的 MCP server 定义(供注入语音会话)
  notify.py      # 终态分发:SSE 在线 → task_done 事件;离线 → web_push
```

### 4.2 工具注入(本期唯一允许碰 core 的点)

Claude 要在语音会话里调这三个工具,而 `stream_turn()` 目前不接受额外工具。
方案:给 `core/agent.py` 的 `stream_turn`(及其内部往 SDK options 传参处)加一个
**可选参数** `extra_mcp_servers: dict | None = None`,为 None 时行为与现在
完全一致(≤10 行,加前先读懂现有 options 组装逻辑,跟随其 MCP server 注册方式——
参考 `tools/` 现有内置 MCP 工具是怎么挂进 options 的,用同一形态)。

- voice 会话调用:`stream_turn(..., extra_mcp_servers=task_tools.build_server())`;
- 现有一切调用点不传该参数,零影响;
- 工具名带 `voice_` 前缀,避免与现有工具撞名;
- **后台任务会话不注入这些工具**(防止任务里再派任务的套娃)。

### 4.3 执行器要点

- 派发:`tasks.create(...)` 落库(queued)→ `asyncio.create_task(executor.run(id))`
  → 状态置 running → 工具立即 return `{"task_id": id, "title": ...}`;
- 并发上限:同时 running ≤3(`VOICE_TASK_MAX_CONCURRENCY`),超出保持 queued 排队;
- 事件采集:遍历 stream_turn 事件,动作类事件 → 模板化写 progress_note
  (例:工具 Bash + 命令首词 → 「正在执行 grep」;工具 Read → 「正在读 app.log」);
- 超时:`VOICE_TASK_TIMEOUT_MIN`(默认 30 分钟)→ 置 failed;
- 取消:cancel asyncio task + 状态 cancelled;
- 危险操作:后台会话走的是现有 `stream_turn`,PreToolUse 危险分级 hook 天然生效;
  `escalate` 类操作在无人审批时会被现有机制处理(实测其默认行为并如实记录进
  progress_note,例如「等待审批被拒,已跳过」)——**本期不做审批转发**,P2 处理;
- 重启自愈:voice 模块 `register_routes()` 时顺带执行
  `tasks.mark_orphans_failed()` + 触发通知。

### 4.4 人设追加(prompts.py)

```
【派活规则】你有三个工具:voice_dispatch_task / voice_query_task / voice_list_tasks。
1. 预计要超过 30 秒才能完成的事(写代码、跑分析、查很多资料),不要自己干:
   先用一句话口头确认(如「好,我去办,好了叫你」),同时调 voice_dispatch_task 派发。
   title 用 20 字以内短名。
2. 用户问"怎么样了/好了没",调 voice_query_task,把返回内容压成一句口语转述,
   不要念字段名。
3. 几秒内能答的事(查天气、聊天、算数)直接答,不许派任务。
```

### 4.5 播报三档(本期只实现两档,留好第三档的位)

| 档 | 行为 | 本期 |
|---|---|---|
| WHEN_IDLE | 等播放队列空+未录音时插播 | ✅ 默认且唯一 |
| SILENT | 只更新任务卡片,不出声 | ✅ 提供 per-task 或全局开关(`VOICE_ANNOUNCE=idle/silent`) |
| INTERRUPT | 立刻打断插话 | ❌ P2(需要打断基建) |

### 4.6 对现有文件的改动(全部改动点)

1. `core/agent.py` ≤10 行(§4.2 的可选参数);
2. `config.py` ≤10 行内追加:`VOICE_TASK_MAX_CONCURRENCY`、`VOICE_TASK_TIMEOUT_MIN`、
   `VOICE_ANNOUNCE`(与 P0 合计仍控制在一个小节内);
3. web.py / index.html:**0 新增**(P0 的挂载已覆盖)。

## 5. 测试要求

`tests/test_voice_p1.py`:
- tasks CRUD 与状态机(含非法迁移);
- 执行器:mock stream_turn,验证 progress_note 节流更新、终态写入、超时、取消;
- 三工具的输入输出 schema;
- 重启自愈(造一条 running 记录 → mark_orphans_failed);
- 通知分发:SSE 在线走事件、离线走 web_push(mock)。

手工验收:§2 四个用户故事,外加「连派两个任务并行跑」「dispatch 后立刻 query」。

## 6. 交付物

代码 + 测试 + `02-phase1-实现记录.md`(实际接触点、agent.py 改动 diff 摘要、
移除清单复核——注意本期移除清单多了 agent.py 那 ≤10 行)。
合 main:`zsh deploy/merge-main.sh`;重启:`zsh deploy/restart.sh`。
