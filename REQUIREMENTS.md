# claude-hermes 需求文档

> 一个**基于 Claude 订阅**的个人 AI 助理(personal Hermes),给 Wesley 自己用。
> 架构参考 [Nous Research Hermes Agent](https://github.com/NousResearch/hermes-agent),但**锁定 Claude、单用户、精简**——只保留个人助理真正需要的部分。

- 版本:v0.1(草稿)
- 创建日期:2026-06-30
- 负责人:Wesley
- 状态:需求定义阶段

---

## 1. 背景与目标

### 1.1 为什么做
现有的 `intertrade-bot` 是**商用多租户**外贸 AI 平台,背着多租户隔离、计费钱包、vendor 隔离等大量"给客户用"的复杂度。Wesley 想要一个**纯个人自用**的助理:像 Hermes / OpenClaude 那样常驻在身边、有长期记忆、能跨设备对话、能调用自己已经攒下的一堆能力。

两个关键前提已验证(2026-06-30):
1. **Claude Max 订阅可驱动后台 agent**:`claude-agent-sdk` 用 `CLAUDE_CODE_OAUTH_TOKEN`(经 `claude setup-token` 生成)即可跑订阅,不花 API 按量费用。详见 [记忆:project_claude_subscription_via_sdk]。
2. **可原生复用现有 Claude skills**:`claude-agent-sdk` 底层就是 Claude Code,Wesley 已有的全部 skill(monthly-planner / things-assistant / audio-processor / customer-development …)能被直接加载当工具用。

### 1.2 目标(一句话)
> 一个常驻进程,我能从**飞书 / Telegram / 命令行**任意入口找它说话;它**记得我是谁、记得我们聊过什么**,并能**调用我已有的 skills** 帮我把事办了。

### 1.3 非目标(明确不做)
| 不做 | 原因 |
|---|---|
| 多租户 / 多用户隔离 | 纯个人自用,只有 Wesley 一个用户 |
| 计费钱包 / 成本扣费 | 订阅模式不按 token 扣费,计费层无意义 |
| 商用、对外服务 | 订阅令牌只能个人用,商用违反条款且撞周限额 |
| 多模型 provider 抽象 | Hermes 支持 40+ 提供商;本项目**只服务 Claude**,不做抽象,降复杂度 |
| RL 训练 / trajectory 压缩 | Hermes 的研究向能力,个人助理用不上 |

> ⚠️ **红线**:本项目用的订阅令牌只配个人自用。任何对外/商用场景必须回到 API key 按量计费。

---

## 2. 设计原则

1. **Claude-only,不做模型抽象**:底层固定 `claude-agent-sdk`,认证固定订阅令牌。砍掉 Hermes 的多 provider 适配层。
2. **参考 Hermes 的三大核心,其余精简**:统一网关 + 工具热注册 + 多层记忆——这三点借鉴;平台砍到 3 个,工具复用现有 skill,记忆接 AI_BRAIN。
3. **单进程常驻 + 多入口**:一个 gateway 进程同时监听飞书 / Telegram,外加 CLI 直连;跨入口共享同一份会话与记忆。
4. **记忆是灵魂**:与 Wesley 现有的 `~/AI_BRAIN` 记忆体系打通,而不是另起炉灶。
5. **先跑通再扩展**:MVP 只做"记忆对话 + 调 skill",入口先上 CLI 验证内核,再接飞书/TG。

---

## 3. 核心概念

| 概念 | 说明 | 参考 Hermes |
|---|---|---|
| **Gateway(网关)** | 常驻进程,统一接收各平台消息、分发给 agent、投递回复 | `gateway/run.py` |
| **Adapter(平台适配器)** | 每个入口一个适配器(飞书/Telegram/CLI),把平台消息归一成统一格式 | `gateway/platforms/*.py` |
| **Agent loop** | 单轮对话的核心循环:载入上下文 → 调 Claude → 工具调用 → 落库 → 回复 | `run_agent.py` |
| **Memory(记忆)** | 多层:会话历史(SQLite+FTS5)+ 用户画像(USER/SOUL)+ AI_BRAIN 知识 | `hermes_state.py` + `memory_manager.py` |
| **Skill/Tool** | 能力单元。直接复用 Wesley 已有的 Claude Code skills + 少量原生工具 | `tools/registry.py` |
| **Cron(定时)** | 定时任务 + 主动投递(早报/提醒),P1 阶段做 | `cron/scheduler.py` |

---

## 4. 功能需求

### 4.1 MVP(P0)——必须有

| 编号 | 需求 | 验收标准 |
|---|---|---|
| P0-1 | **Claude 订阅驱动 agent loop** | 设好 `CLAUDE_CODE_OAUTH_TOKEN`,能完成一轮带工具调用的对话,`is_error=False` |
| P0-2 | **长期记忆对话** | 关掉进程重启后,它仍记得之前会话的关键信息;能跨会话检索(FTS5) |
| P0-3 | **调用现有 skills** | 能加载并触发至少 1 个 Wesley 现有 skill(如 monthly-planner)完成任务 |
| P0-4 | **CLI 入口** | `python -m claude_hermes chat` 可直接对话,作为最快验证通道 |
| P0-5 | **接入 AI_BRAIN** | 启动时载入 `~/AI_BRAIN/USER.md`;能把新记忆按现有规范写回 `~/AI_BRAIN/memory/` |

### 4.2 P1——尽快有

| 编号 | 需求 | 说明 |
|---|---|---|
| P1-1 | **飞书入口** | 复用 intertrade-bot 已跑通的飞书 WS 接入经验,接成一个 adapter |
| P1-2 | **Telegram 入口** | Bot 模式,手机端随时找它 |
| P1-3 | **主动触达 / 定时汇报** | cron 定时任务,结果投递到飞书/TG(如每早工作简报) |
| P1-4 | **跨入口会话连续** | 飞书问一半,切到 CLI 能接着聊(共享 session) |

### 4.3 P2——以后再说

- Obsidian vault 读写(沉淀想法/查笔记)
- 语音输入(STT)
- 信息简报(RSS/邮件聚合)
- 可选向量记忆(语义召回)
- Web 仪表板(看会话/成本/cron)

---

## 5. 架构设计(参考 Hermes,精简版)

```
┌───────────────────────────────────────────────┐
│  入口层 Adapters                                │
│  ┌────────┐ ┌──────────┐ ┌─────┐               │
│  │ 飞书   │ │ Telegram │ │ CLI │   (各一适配器) │
│  └───┬────┘ └────┬─────┘ └──┬──┘               │
│      └───────────┼──────────┘                  │
│             归一为统一消息格式                   │
└──────────────────┼─────────────────────────────┘
                   ▼
┌───────────────────────────────────────────────┐
│  Gateway(常驻进程)                            │
│  - 路由消息到 agent                             │
│  - 管理 session                                 │
│  - cron tick(P1)                               │
└──────────────────┼─────────────────────────────┘
                   ▼
┌───────────────────────────────────────────────┐
│  Agent loop(core/agent.py)                    │
│  载入上下文 → claude-agent-sdk.query()          │
│           → 工具/skill 调用循环 → 落库 → 回复   │
│  认证:CLAUDE_CODE_OAUTH_TOKEN(订阅)           │
└─────┬───────────────────────┬───────────────────┘
      ▼                       ▼
┌──────────────┐      ┌────────────────────────┐
│ 记忆层        │      │ 工具/Skill 层          │
│ ① 会话 SQLite │      │ - 复用现有 Claude skill│
│   + FTS5      │      │ - 原生工具(记忆读写等)│
│ ② USER/SOUL   │      │ - 热注册(import 即用) │
│ ③ AI_BRAIN    │      └────────────────────────┘
└──────────────┘
```

### 5.1 关键技术决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 语言 | Python 3.13 | 和 intertrade-bot 一致,经验可迁 |
| Agent 内核 | `claude-agent-sdk`(pin 版本) | 已验证可跑订阅;原生支持 skill/MCP |
| 认证 | `CLAUDE_CODE_OAUTH_TOKEN` + 删 `ANTHROPIC_API_KEY` | 订阅唯一正路 |
| 默认模型 | `claude-opus-4-8`(可配) | SDK 默认给 Sonnet,个人用要好脑子就显式上 Opus(注意周限额) |
| 会话存储 | SQLite + FTS5 | 本地、零依赖、可全文检索,照搬 Hermes |
| 平台接入 | 自写轻量 adapter | 飞书复用 intertrade-bot 经验;TG 用官方 bot API |

### 5.2 与 Hermes 的差异(砍了什么)
- ❌ 多 provider 适配器 → ✅ 只留 Claude
- ❌ 29 个平台 → ✅ 飞书 + TG + CLI 三个
- ❌ 6 种终端后端(docker/ssh/modal…) → ✅ 本地直跑
- ❌ 命令审批 UI、RL 训练、credential pool → ✅ 不要
- ✅ 保留:统一网关思想、SQLite+FTS5 记忆、工具热注册、cron 主动投递

---

## 6. 认证与模型(已验证)

```bash
# .env(本文件 gitignore)
# 1) 不要设 ANTHROPIC_API_KEY —— 一旦存在就走 API 按量计费而非订阅
# 2) base_url 保持官方或不设
ANTHROPIC_BASE_URL=https://api.anthropic.com
# 3) 长期订阅令牌(claude setup-token 生成)
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
# 4) 模型
AGENT_MODEL=claude-opus-4-8
```

- 令牌生成:`claude setup-token`(需 Pro/Max 订阅,人工跑一次)。
- 令牌是**长期有效**的敏感凭据,只放 gitignore 的 `.env`,别进仓、别贴聊天。

---

## 7. 记忆系统设计(三层)

| 层 | 存储 | 内容 | 读写时机 |
|---|---|---|---|
| ① 会话记忆 | `data/state.db`(SQLite+FTS5) | 完整聊天历史、工具调用 | 每轮落库;检索时 FTS5 召回 |
| ② 用户画像 | `~/AI_BRAIN/USER.md` | 我是谁、长期偏好 | 启动时载入 system prompt |
| ③ 知识/经验 | `~/AI_BRAIN/memory/` + `MEMORY.md` 索引 | 沉淀的事实/坑/决策 | 按需读;新经验按现有规范写回 |

设计要点:
- **不重造记忆体系**——直接接 Wesley 已有的 `~/AI_BRAIN`(USER.md 自动注入、memory/ 文件制)。
- 会话层是新增的(SQLite),负责"对话连续性";AI_BRAIN 是已有的,负责"跨时间的我"。
- 写回 AI_BRAIN 遵循其既定 frontmatter 规范(name/description/type),并更新 `MEMORY.md` 索引。

---

## 8. 目录结构规划(初版)

```
claude-hermes/
├── REQUIREMENTS.md            # 本文档
├── README.md                  # 快速上手(待写)
├── .env.example               # 配置模板(待写)
├── .gitignore
├── pyproject.toml             # 依赖(待写)
│
├── claude_hermes/
│   ├── __main__.py            # CLI 入口(chat / gateway / cron)
│   ├── core/
│   │   ├── agent.py           # ★ agent loop(claude-agent-sdk)
│   │   └── prompt.py          # system prompt 组装(画像+记忆+技能)
│   ├── memory/
│   │   ├── session_store.py   # SQLite+FTS5 会话
│   │   └── brain.py           # AI_BRAIN 读写桥
│   ├── gateway/
│   │   ├── run.py             # 常驻网关
│   │   └── adapters/
│   │       ├── cli.py         # P0
│   │       ├── feishu.py      # P1
│   │       └── telegram.py    # P1
│   ├── tools/
│   │   ├── registry.py        # 热注册
│   │   └── builtin/           # 原生工具(记忆读写等)
│   └── cron/                  # P1 定时
│       └── scheduler.py
│
├── data/                      # 运行时(gitignore)
├── docs/                      # 设计细节 / ADR
└── tests/
```

---

## 9. 非功能需求

| 维度 | 要求 |
|---|---|
| 单用户 | 只服务 Wesley,无需鉴权/隔离 |
| 隐私 | 会话数据全本地(SQLite),不上云;令牌只在本地 .env |
| 可靠性 | 网关进程崩了能自启(systemd / launchd);会话落盘不丢 |
| 成本 | 订阅模式无按量费;但要尊重周限额,Opus 别滥用 |
| 可维护 | 单人维护,代码要小而清晰,优先简单方案 |
| 复用 | 飞书接入、session 思路尽量从 intertrade-bot 迁移已验证经验 |

---

## 10. 里程碑

| 里程碑 | 范围 | 验收 |
|---|---|---|
| **M0 内核打样** | P0-1/P0-4:订阅 agent loop + CLI 对话 | 命令行能跟它聊,走的是订阅 |
| **M1 记忆闭环** | P0-2/P0-3/P0-5:会话记忆 + 调 skill + 接 AI_BRAIN | 重启不失忆、能调一个 skill、能读写 AI_BRAIN |
| **M2 多入口** | P1-1/P1-2/P1-4:飞书 + TG + 跨入口连续 | 手机飞书/TG 能找它,会话跨入口连续 |
| **M3 主动化** | P1-3:cron + 定时投递 | 每早自动推一条简报到飞书 |

---

## 11. 待定问题(Open Questions)

1. ~~**CLI 内核 vs 直接上飞书**~~:✅ 已定 **M0 先做 CLI** 验证内核,飞书/TG 留到 M2。
2. **会话与 AI_BRAIN 的边界**:哪些进 SQLite 会话、哪些晋升为 AI_BRAIN 长期记忆?需要一条"晋升"规则。
3. **skill 加载范围**:全量挂载现有 skills,还是个人助理只挑一批白名单?(全量可能 tool schema 过大)
4. **是否复用 intertrade-bot 的飞书代码**:直接拷贝其 WS 接入 + token 管理,还是重写精简版?
5. **常驻方式**:macOS 本机 launchd,还是放到服务器 systemd?

---

## 附:参考资料
- Hermes Agent 蓝本:`/Users/wesley/Documents/Codex/2026-04-29/hermes-agent`
- 订阅验证记录 + 配方:AI_BRAIN 记忆 `project_claude_subscription_via_sdk`
- 框架经验来源:`intertrade-bot`(飞书接入 / session / skill 系统 / claude-agent-sdk 用法)
