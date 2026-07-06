# Hermes 优化方案 —— 借鉴社区个人助理实践

> 调研了 OpenClaw、原版 Hermes、mem0/Redis 记忆工程等实践,对照 claude-hermes 现状落地。
> **主动性方向经对比后调整**:放弃 OpenClaw 式自主 heartbeat,改为**全面采用原版 Hermes 的
> consent-first(人在环内)建议系统**——见「优化 2」。多模型路由、向量检索、技能市场不适合我们,见文末。

---

## 总览

| # | 优化点 | 解决的问题 | 改动文件 | 默认 |
|---|--------|-----------|---------|------|
| 1 | 同会话串行化 + 跨会话并发 | 并发写库/串消息竞态;慢会话卡住同平台其他会话 | `gateway/run.py` | 直接生效 |
| 2 | **建议(suggestion)系统** | 助理只会被动回应,且不该自作主张打扰你 | `cron/suggestions.py` 等 | 直接生效(consent-first) |
| 3 | 记忆加固(索引注入 + 字数上限) | 记忆「存了却没被读」;摘要过长污染索引 | `core/prompt.py`、`tools/builtin.py` | 直接生效 |

---

## 优化 1:同会话串行化 + 跨会话并发

**问题**:`gateway/run.py` 的 `_serve()` 逐条 `await _dispatch`,导致①慢会话卡住同平台**所有**
其他会话;②私聊 UNIFY 共享 `main`、cron 推送等并发写 `state.db`/编辑同一条 TG 消息,可能错乱。

**改法**:每个 `session_key` 一把 `anyio.Lock` + 并发派发——`_serve` 用 `start_soon` 派发(不同会话
并发),`_dispatch` 内 `async with self._lock_for(...)`(同会话串行),整体 try 兜底防炸 nursery。

**验证**:同会话时间线 `start→end→start→end`(串行)✓;跨会话 `start→start→end→end`(并发)✓。

---

## 优化 2:建议系统(采用原版 Hermes 方案,放弃 OpenClaw heartbeat)

### 为什么不用 OpenClaw 的 heartbeat

| | OpenClaw heartbeat | 原版 Hermes 方案(采用) |
|---|---|---|
| 谁做决定 | agent **自主**判断要不要找你 | 系统**提议**,你**一键接受**才生效 |
| 代价 | 每次都 burn 一次 LLM、可能瞎打扰、不可控 | 稳、省、可控 |
| 哲学 | agent 自主 | **consent-first(人在环内)** |

对订阅额度有限、单人自用、最怕被瞎打扰的场景,consent-first 更合适。

### 机制

**建议(suggestion)= 一个待命的 cron 任务;Hermes 提议 → 你 `/suggest` 一键接受(才真正建任务)
或忽略(按 `dedup_key` 记住,永不再提)**。接受 = 调用 `create_job`,不搞第二套引擎。

| 组件 | 文件 | 作用 |
|------|------|------|
| 建议内核 | `cron/suggestions.py` | 存 `data/suggestions.json`;登记/去重/接受/忽略;`MAX_PENDING=5` 防提醒墙 |
| 起步目录 | `cron/suggestion_catalog.py` | 内置晨间简报、每周复盘;启动时播种(dedup 幂等) |
| 建任务 | `cron/scheduler.py: create_job` | 接受建议时创建真正的 cron 任务 |
| agent 提议 | `tools/builtin.py: suggest_automation` | 发现你反复做的事 → 提一条 `usage` 建议(校验 cron 合法) |
| 复盘接入 | `cron/scheduler.py: _reflect` | 定时复盘时也会提议自动化 |
| 交互 UI | `gateway/core.py: /suggest` | 列出待定 → Choice 按钮接受/忽略(TG/Web/CLI/TUI 通用) |

**用法**:`/suggest` 看待定;`/suggest accept <序号/id>` 接受;`/suggest dismiss <序号/id>` 忽略。

**验证**:登记/去重/忽略latch/接受建任务/目录幂等/工具/命令 UI 共 11 项测试通过。

---

## 优化 3:记忆加固

**A. 记忆「存了却没被读」**:system prompt 原本只拼 PERSONA+USER.md,`MEMORY.md` 索引没注入 →
agent 不知道有哪些记忆。新增 `_load_memory_index()` 把索引注入(几十行,成本极低)。

**B. 摘要过长污染索引**:`save_memory` 的 `summary` 加 `_SUMMARY_MAX=120` 上限,超长打回让 agent 压成一句话。

**验证**:MEMORY.md 注入✓、缺失不崩✓、超长摘要拒绝✓。

---

## 不做的事(及原因)

| 社区做法 | 为什么不抄 |
|---------|--------------|
| OpenClaw 自主 heartbeat | 不可控 + 吃额度 + 可能瞎打扰,不如 consent-first 建议 |
| 多模型 provider 路由 | 我们是 Claude 订阅令牌,无 API 计费,省钱无意义 |
| 向量/语义检索记忆 | LIKE 子串匹配是刻意选择:对中文友好、避免 FTS5 分词错乱 |
| 原版 blueprint 蓝图系统(713 行) | 重、可选;当前 catalog + suggest_automation 已够用 |
| ClawHub 技能市场 | 已复用 Claude Code `~/.claude/skills/` 生态 |
