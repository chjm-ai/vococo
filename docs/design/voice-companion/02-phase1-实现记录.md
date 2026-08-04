# P1 实现记录

## 新增文件

```
claude_hermes/voice/
  tasks.py       # 任务表 CRUD + 状态机(data/voice/voice.db 的 tasks 表)
  executor.py    # 后台执行器:派发/并发排队/进度采集节流/终态/超时/取消/重启自愈
  task_tools.py  # 三个 MCP 工具:voice_dispatch_task / voice_query_task / voice_list_tasks
  notify.py      # 终态分发:SSE 在线走 event:task_done;离线走 web_push
tests/test_voice_p1.py
```

## 接触点实际改动(对照 00-overview.md §2.2 + 02-phase1-task-board.md §4.6 预算)

| 文件 | 预算 | 实际 | 内容 |
|---|---|---|---|
| `core/agent.py` | ≤10 行(P1 才允许) | 净 +7 行(+8/-1) | `stream_turn` 加可选参数 `extra_mcp_servers`,默认 `None` 零影响;顺带把它的 key 集合并入 `_compat_base_key` 的哈希,防止保温池把"带任务工具"和"不带"的 client 串用 |
| `config.py` | ≤10 行 | 净 +4 行 | `VOICE_TASK_MAX_CONCURRENCY`(默认 3)、`VOICE_TASK_TIMEOUT_MIN`(默认 30)、`VOICE_ANNOUNCE`(默认 idle,留了 silent 位) |
| `web.py` / `index.html` | 0 新增 | 0 | 未碰,P0 的挂载已覆盖 |
| 其余现有文件 | 0 | 0(除下方"顺带改动"外)未碰 | — |

全部在预算内。`git diff --stat -- claude_hermes/config.py claude_hermes/core/agent.py` 复核见下方「移除清单复核」。

### voice 包内部的顺带改动(不占用上表预算,自己的模块随便改)

- `voice/prompts.py`:追加【派活规则】指令块(§4.4),更新版本历史注释。
- `voice/session.py`:`run_turn()` 加 `extra_mcp_servers` 透传参数,默认 `None`。
- `voice/routes.py`:`/voice/send` 调 `session.run_turn` 时注入 `task_tools.build_server()`;
  新增 `/voice/tasks`(列表)、`/voice/tasks/{id}`(详情)、`/voice/tasks/{id}/stop`(取消)、
  `/voice/tasks/stream`(常驻 SSE,F8 在线播报用);`register_routes()` 末尾
  `asyncio.ensure_future(executor.heal_after_restart())` 触发 F11 重启自愈。
- `voice/static/voice.html`:任务抽屉 UI(📋 按钮 + 底部弹层列表 + 停止按钮)、
  `EventSource("/voice/tasks/stream?token=...")` 监听 `task_done`、
  `pendingAnnouncements` 队列实现 WHEN_IDLE(忙/录音中就先攒着,空闲了再插播)。
- `tests/test_voice_p0.py`:`voice_db` fixture 补上 `tasks._DB` 的重置(P1 起
  `register_routes()` 会顺带摸这张表);4 处 `fake_run_turn` mock 补上
  `extra_mcp_servers=None` 形参(否则被新增的关键字参数 TypeError)。

## 技术决策 / 与设计文档的差异

- **鉴权**:`/voice/tasks/stream` 用浏览器原生 `EventSource`,不能带自定义 header,
  只能走 query 参数 `?token=`——`_ok_token()` 补了一条 `request.query.get("token")`
  的 fallback,其余路由不受影响(header 优先)。
- **"在线"判定**:F8 说"若 /voice 页面 SSE 在线",简化实现为"是否有活跃的
  `/voice/tasks/stream` 订阅者"(`notify.is_online()`),单用户场景下足够准。
- **重启自愈范围扩大**:F11 原文只说"残留 running 任务标记为 failed",实现时
  把 `queued` 也一并处理——本项目的任务队列是纯内存(`executor._running` +
  数据库 `status='queued'` 行),重启后没有任何机制会把 queued 任务捡起来跑,
  不处理的话这些记录会永远卡在"排队中",变成看不见尽头的僵尸状态。
- **result_summary 的 markdown 清理**:后台任务跑的是不带语音人设的原始
  `stream_turn`(prompt 里没有 P0 那套"禁止 markdown"规则),模型自然会用
  `代码`/**加粗**这类格式;但 `result_summary` 最终要被 TTS 朗读,真机测试
  (`执行 sleep 20,统计 .py 文件数`)时发现 50 字以内的短结果会跳过 LLM 压缩、
  原样带着反引号/星号进播报——加了一个 `_MD_STRIP_RE` 正则,在 `_summarize()`
  入口先摘掉这些符号,长文本走 LLM 压缩那条路的 prompt 里也加了同样的禁止说明。
  这是设计文档没预料到的口径差异,不是 bug,但值得记录:凡是"最终会被朗读"
  的文本,都不能假设模型自己会遵守语音礼仪——只有 P0 那套语音人设的会话才有
  这条规则,别处产出的文本都要主动清洗一遍。
- **推送 URL 落不到 `/voice`**:F9 期望"点击(推送通知)打开 /voice",但
  `gateway/adapters/web_push.py` 的 `PushManager.notify()` 把 `url` 硬编码成
  `f"/?conv={conv}"`,且该文件不在本期接触点预算内(0 行)——`notify.py` 调用
  `PUSH.notify(conv="voice-task", ...)` 后,点击推送实际落在主界面
  `/?conv=voice-task`,不是 `/voice`。可接受(用户还能从主界面点 🎙️ 进语音页),
  但如果要精确修,得给 `web_push.notify()` 加一个可选 `url` 参数——这触碰了
  预算外文件,留给 P2 或单独的小改动去做。
- **后台任务不注入派活工具**:按 §4.2 要求,`executor._run()` 直接调
  `stream_turn(...)`(不传 `extra_mcp_servers`),避免任务里的模型再调
  `voice_dispatch_task` 递归派生任务。

## 手工验收(隔离测试服务器,真实模型,非 mock)

用与 P0 相同的隔离测试服务器(`run_voice_test_server.py`,独立端口,不影响
生产 `wazir.chjm.cc`)跑通:

- [x] 「帮我在后台跑...这个明确要用 voice_dispatch_task 派到后台」→ AI 秒回
      「好,我去办,好了叫你」,`GET /voice/tasks` 立即能看到该任务 `status=running`,
      `progress_note` 随工具调用实时更新(如"正在执行:cd /Users/wesley/...");
- [x] 任务跑完后 `status=done`、`result_summary`/`result_full` 落库;
- [x] 「刚才那个 hello 任务办得怎么样了?」→ AI 用 `voice_query_task` 查到并
      口语转述"已经跑完了,执行成功了,结果是 hello。",没有念字段名;
- [x] `/voice/tasks/stream` 常驻 SSE 在任务终态时收到
      `event: task_done`,payload 含 `announce_text` 和合成好的 `audio_b64`;
- [x] `GET /voice/tasks/{id}` 详情、`POST /voice/tasks/{id}/stop` 对已终态任务
      正确返回 `{"ok": false}`(状态机拒绝非法迁移)、对不存在 id 返回 404;
- [x] `uv run pytest` 全绿(204 通过,含 27 个新增 P1 测试);
- [ ] 「连派两个任务并行跑」——单测已覆盖(`test_dispatch_queues_beyond_
      concurrency_limit`),真机手工暂未复测,风险低(逻辑与单测一致);
- [ ] 手机 Safari 上任务抽屉 UI(📋 按钮、底部弹层、停止按钮)的触摸体验——
      **待用户在真机上验证**,目前只验证了桌面浏览器 DOM 结构和接口联调。
- [ ] 离线推送(F9)——本机未配 `VAPID_PUBLIC_KEY`/`VAPID_PRIVATE_KEY`,
      `notify.on_task_terminal()` 离线分支代码路径有单测覆盖(mock
      `PUSH.notify`),但**未做真实设备的端到端推送验证**。

## 移除清单复核(00-overview.md §2.4,本期新增 agent.py 那 7 行)

已做「移除演练」(`git stash` 式验证,未真删):

```bash
git stash push -u -- claude_hermes/voice/tasks.py claude_hermes/voice/executor.py \
  claude_hermes/voice/notify.py claude_hermes/voice/task_tools.py tests/test_voice_p1.py \
  claude_hermes/voice/prompts.py claude_hermes/voice/routes.py claude_hermes/voice/session.py \
  claude_hermes/voice/static/voice.html claude_hermes/config.py claude_hermes/core/agent.py \
  tests/test_voice_p0.py
uv run pytest -q   # 176 passed —— 精确回到 P0 完成时的基线,无残留依赖
git stash pop      # 验证完毕,原样恢复,再次 204 passed
```

真要彻底移除时(P0+P1 一起清):

1. `rm -rf claude_hermes/voice/ tests/test_voice_p0.py tests/test_voice_p1.py docs/design/voice-companion/`
2. 还原 `web.py` 那 4 行、`index.html` 那 5 行、`config.py` 那 12 行(P0 的 8 行 + P1 的 4 行)、
   `core/agent.py` 那 7 行(P1 新增,P0 未触碰)
3. `rm -rf data/voice/`
4. `.env` 删掉 `VOICE_` 前缀变量(`VOICE_ENABLED`、`VOICE_TTS_VOICE`、
   `VOICE_TASK_MAX_CONCURRENCY`、`VOICE_TASK_TIMEOUT_MIN`、`VOICE_ANNOUNCE`)
5. `pyproject.toml` / `uv.lock` 本期未新增依赖,无需改动

## 下一步

P2(体验进阶:流式识别/CosyVoice2 音色/开口即打断/思考音效/主动插话档/审批卡片)
依赖 P0+P1 已就绪。另外两个本期发现但刻意不做的小尾巴,建议留给 P2 或单独小改动:

- `web_push.py` 的 `notify()` 加一个可选 `url` 参数,让任务完成推送能精确落在
  `/voice` 而不是主界面(见上方"技术决策"一节)。
- 离线推送(F9)的真实设备端到端验证(当前只有 mock 覆盖)。
