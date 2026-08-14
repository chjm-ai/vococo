# vococo 需求文档

> 一个**基于 Claude 订阅**的个人 AI 助理,单用户自用。
> 架构参考 [Nous Research Hermes Agent](https://github.com/NousResearch/hermes-agent),但**锁定 Claude、单用户、精简**——只保留个人助理真正需要的部分。

- 版本:v0.1
- 状态:M0–M3 已落地(里程碑表见本文第 9 节)

---

## 1. 背景与目标

### 1.1 为什么做

想要一个**纯个人自用**的助理:像 Hermes / OpenClaude 那样常驻在身边、有长期记忆、
能跨设备对话、能调用已经攒下的一堆能力。不背多租户隔离、计费钱包、vendor 隔离
这类「给客户用」的复杂度。

两个关键前提(已验证):

1. **Claude 订阅可驱动后台 agent**:`claude-agent-sdk` 用 `CLAUDE_CODE_OAUTH_TOKEN`
   (经 `claude setup-token` 生成)即可跑订阅,不花 API 按量费用。
2. **可原生复用现有 Claude skills**:`claude-agent-sdk` 底层就是 Claude Code,
   已有的全部 skill 能被直接加载当工具用。

### 1.2 目标(一句话)

> 一个常驻进程,能从 **Web / 命令行**任意入口找它说话;它**记得我是谁、
> 记得我们聊过什么**,并能**调用我已有的 skills** 帮我把事办了。

### 1.3 非目标(明确不做)

| 不做 | 原因 |
|---|---|
| 多租户 / 多用户隔离 | 纯个人自用,只有一个用户 |
| 计费钱包 / 成本扣费 | 订阅模式不按 token 扣费 |
| 商用、对外服务 | 订阅令牌只能个人用,商用违反条款且撞周限额 |
| 多模型 provider 抽象 | 只服务 Claude,不做抽象,降复杂度 |
| RL 训练 / trajectory 压缩 | 研究向能力,个人助理用不上 |

> ⚠️ **红线**:订阅令牌只配个人自用。任何对外/商用场景必须回到 API key 按量计费。

---

## 2. 设计原则

1. **Claude-only,不做模型抽象**:底层固定 `claude-agent-sdk`,认证固定订阅令牌。
2. **参考 Hermes 三大核心,其余精简**:统一网关 + 工具热注册 + 多层记忆。
3. **单进程常驻 + 多入口**:一个 gateway 进程同时监听 Web,外加 CLI 直连;跨入口共享同一份会话与记忆。
4. **记忆是灵魂**:与现有 `~/AI_BRAIN` 记忆体系打通,而不是另起炉灶。
5. **先跑通再扩展**:MVP 只做「记忆对话 + 调 skill」,入口先上 CLI 验证内核。

---

## 3. 核心概念

术语的权威定义见 [CONTEXT.md](CONTEXT.md)。这里给功能层的速览:

| 概念 | 说明 |
|---|---|
| **Gateway(网关)** | 常驻进程,统一接收各平台消息、分发给 agent、投递回复 |
| **Adapter(平台适配器)** | 每个入口一个适配器(Web / CLI),把平台消息归一成统一格式 |
| **Agent loop** | 单轮对话的核心循环:载入上下文 → 调 Claude → 工具调用 → 落库 → 回复 |
| **Memory(记忆)** | 多层:会话历史(SQLite)+ 用户画像(USER.md)+ AI_BRAIN 知识 |
| **Skill/Tool** | 能力单元。复用现有 Claude Code skills + 少量原生工具 |
| **Cron(定时)** | 定时任务 + 主动投递(早报/提醒) |

---

## 4. 功能需求

### 4.1 MVP(P0)

| 编号 | 需求 | 验收标准 |
|---|---|---|
| P0-1 | **Claude 订阅驱动 agent loop** | 设好 `CLAUDE_CODE_OAUTH_TOKEN`,能完成一轮带工具调用的对话 |
| P0-2 | **长期记忆对话** | 重启后仍记得之前会话的关键信息;能跨会话检索(`recall_past`) |
| P0-3 | **调用现有 skills** | 能加载并触发至少 1 个现有 skill 完成任务 |
| P0-4 | **CLI 入口** | `vococo chat` 可直接对话 |
| P0-5 | **接入 AI_BRAIN** | 启动时载入 `~/AI_BRAIN/USER.md`;能把新记忆按规范写回 `~/AI_BRAIN/memory/` |

### 4.2 P1

| 编号 | 需求 | 说明 |
|---|---|---|
| P1-1 | **Web 入口** | 手机浏览器直连的自建 UI(SSE 流式 + 会话侧边栏) |
| P1-3 | **主动触达 / 定时汇报** | cron 定时任务,结果投递到 Web |
| P1-4 | **跨入口会话连续** | 一个入口问一半,切到另一个能接着聊(共享 session) |

### 4.3 P2(以后再说)

未立项的设想移到 [docs/roadmap.md](docs/roadmap.md);本文件只保留已验收范围。

---

## 5. 架构设计(精简版)

```
入口层 Adapters:  Web / CLI  (各一适配器,归一为统一消息格式)
        │
        ▼
Gateway(常驻进程):路由消息到 agent · 管理 session · cron tick
        │
        ▼
Agent loop(core/agent.py):载入上下文 → claude-agent-sdk.query()
        │                    → 工具/skill 调用循环 → 落库 → 回复
        │  认证:CLAUDE_CODE_OAUTH_TOKEN(订阅)
        ├──────────────────────┐
        ▼                      ▼
   记忆层                   工具/Skill 层
   ① 会话 SQLite            - 复用现有 Claude skill
   ② USER.md 画像           - 原生工具(记忆读写等)
   ③ AI_BRAIN 知识          - 热注册(import 即用)
```

实际的模块划分见 [AGENTS.md](AGENTS.md) 的「代码结构」。

### 5.1 关键技术决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 语言 | Python ≥ 3.11 | — |
| Agent 内核 | `claude-agent-sdk`(pin 版本) | 已验证可跑订阅;原生支持 skill/MCP |
| 认证 | `CLAUDE_CODE_OAUTH_TOKEN` + 删 `ANTHROPIC_API_KEY` | 订阅唯一正路 |
| 默认模型 | `claude-sonnet-5`(可配) | 日常速度+智能平衡;Opus 5 留给重活 |
| 会话存储 | SQLite(LIKE 子串检索) | 本地、零依赖;中文场景 LIKE 已够,不上 FTS5 |
| 平台接入 | 自写轻量 adapter | Web 自建 UI(SSE 流式);IM 平台适配器已移除 |

### 5.2 与原版 Hermes 的差异(砍了什么)

- ❌ 多 provider 适配器 → ✅ 只留 Claude(设置页可挂第三方 Anthropic 兼容端点)
- ❌ 29 个平台 → ✅ Web + CLI/TUI(早期的 TG adapter 已移除,手机端改走 Web PWA)
- ❌ 6 种终端后端(docker/ssh/modal…) → ✅ 本地直跑
- ❌ 命令审批 UI 重造、RL 训练、credential pool → ✅ 不要
- ✅ 保留:统一网关、SQLite 会话记忆、工具热注册、cron 主动投递

---

## 6. 认证与模型

- 令牌生成:`claude setup-token`(需 Pro/Max 订阅,人工跑一次)。
- 令牌是**长期有效**的敏感凭据,只放 gitignore 的 `.env`,别进仓、别贴聊天。
- 不设 `ANTHROPIC_API_KEY`——一旦存在就走 API 按量计费而非订阅(代码启动时会主动移除它)。
- 完整环境变量清单见 [.env.example](.env.example)。

---

## 7. 记忆系统设计(三层)

| 层 | 存储 | 内容 | 读写时机 |
|---|---|---|---|
| ① 会话记忆 | `data/state.db`(SQLite) | 完整聊天历史、工具调用 | 每轮落库;`recall_past` 用 LIKE 子串召回 |
| ② 用户画像 | `~/AI_BRAIN/USER.md` | 我是谁、长期偏好 | 启动时载入 system prompt |
| ③ 知识/经验 | `~/AI_BRAIN/memory/` + `MEMORY.md` 索引 | 沉淀的事实/坑/决策 | 按需读;新经验按规范写回 |

设计要点:

- **不重造记忆体系**——直接接现有 `~/AI_BRAIN`(可用 `AI_BRAIN_DIR` 配置;目录不存在则自动跳过)。
- 会话层负责「对话连续性」;AI_BRAIN 负责「跨时间的我」。
- 写回 AI_BRAIN 遵循其既定 frontmatter 规范(name/description/type),并更新 `MEMORY.md` 索引。

---

## 8. 非功能需求

| 维度 | 要求 |
|---|---|
| 单用户 | 只服务一个人,无需鉴权/隔离(Web 走访问口令只为防陌生人闯入) |
| 隐私 | 会话数据全本地(SQLite),不上云;令牌只在本地 `.env` |
| 可靠性 | 网关进程崩了能自启;会话落盘不丢 |
| 成本 | 订阅模式无按量费;但要尊重周限额,Opus 别滥用 |
| 可维护 | 单人维护,代码要小而清晰,优先简单方案 |
| 安全 | 危险分级三档闸 + fail-closed 边界,详见 [ADR-0003](docs/adr/0003-security-boundary-strategy.md) |

---

## 9. 里程碑

| 里程碑 | 范围 | 验收 |
|---|---|---|
| **M0 内核打样** | 订阅 agent loop + CLI 对话 | 命令行能聊,走的是订阅 |
| **M1 记忆闭环** | 会话记忆 + 调 skill + 接 AI_BRAIN | 重启不失忆、能调 skill、能读写 AI_BRAIN |
| **M2 多入口** | Web + CLI/TUI + 跨入口连续 | 手机能找它,会话跨入口连续 |
| **M3 主动化** | cron + 定时投递 | 每早自动推一条简报 |

后续设计与决策记录见 [docs/adr/](docs/adr/) 与 [docs/design/](docs/design/)。
