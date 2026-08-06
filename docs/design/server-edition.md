# vococo 服务器版 产品思路

> ⚠️ **已废弃,仅存档**(2026-08-06)。双模式路线已推翻,改走「两套代码」路线,见 `docs/design/trade-edition-plan.md`(VocoTrade)。本文件及其技术方案对应的 P0 代码已整体移除,存档锚点 `git tag server-edition-p0`。
>
> 2026-08-05 调研产出。基于 vococo 与 intertrade-bot(`~/Desktop/Repos/intertrade-bot`)两仓库代码现状,非凭空设计。
> 目标:把 vococo 从「单用户本地个人助理」扩展为「多租户 SaaS」,作为跟客户交互的 AI 端口对外服务。

---

## 0. 现状一句话

- **vococo**:单用户假设写得很深——进程级全局配置(`config.py` 全部常量)、单份模型 key、单一 `WEB_AUTH_TOKEN`、单一 `state.db`、单一 `~/AI_BRAIN` 记忆目录;唯一隔离维度是 `session_key`。但核心资产(agent 循环、Web SSE UI、工具/skill 系统、危险分级、任务引擎)全部**无用户状态**,天然可复用。
- **intertrade-bot**:已是一套生产在跑的多租户 SaaS(飞书外贸 bot,`bot.speeedai.com`,1 个真实租户 uhome + 3 个 bot)。多租户(行级 `tenant_id` 隔离)、计费(钱包+markup+双列流水)、多 agent(双 manifest)三套机制完整,但消息入口/鉴权/业务表全部绑死飞书+外贸场景。

结论先行:** vococo 服务器版走「同一套代码 + 部署模式开关」路线,租户/计费/多 agent 三套机制从 intertrade-bot 移植骨架、剥离飞书与外贸语义。**

---

## 1. 两种改造路径对比

### 路径①:另起独立服务器版本(fork 或新仓)

| 利 | 弊 |
|---|---|
| 可以激进删减,代码不受本地版包袱 | skill/工具/agent 循环/prompt 三层堆叠全部**双份维护** |
| 安全模型可以推倒重写 | vococo 还在高速迭代(Web UI、工具卡片刚上线),fork 出去即开始 drift |
| 发布节奏独立 | Wesley 一个人维护两套 = 实际等于其中一套慢慢烂掉 |

**已有前车之鉴**:intertrade-bot 本质就是「为服务器场景另起的一套」,结果它的 skill 体系(`skills_manifest.yaml` + `skills/<name>/SKILL.md`)和 vococo 的 skill 体系(`~/.claude/skills` + `settings_store`)已经是两套、互不通用;它在 vococo 之前写的 agent 循环也没有回流。再来一次 fork,只是把这个错误复制第二遍。

### 路径②:同一套代码 + 部署模式(推荐)

`VOCOCO_MODE=personal|server`,模式判定集中在 `config.py` 一处,功能差异用**开关**表达而非散落的 if-else。

| 利 | 弊 | 缓解 |
|---|---|---|
| 能力单一来源:本地版每加一个 skill/工具,服务器版自动获得 | 代码里出现模式分支 | 分支只许出现在 config 层与少数 feature flag,业务代码只读 flag |
| bug 修一次两边生效;危险分级/审批闸/工具卡片这些重资产不重写 | 本地专属功能(语音/TUI)的代码进了服务器镜像 | 懒加载/optional import;server 模式不 import voice 模块 |
| 本地版继续当「开发 dogfood 环境」——服务器版每个功能先在本地跑顺 | 单进程架构的租户爆炸半径 | per-tenant 并发闸 + 回合超时(见 §6 风险) |

### 推荐:路径②(把握:高)

核心理由正是任务书里的考量:**skill/工具/能力共用,避免双份维护**。vococo 的配置体系已经是「进程级常量(`config.py`)+ 运行时覆盖层(`settings_store.py`)」两层结构,多租户化本质是**在中间插入第三层「租户层」**,不是重写。路径①的所有优点(激进删减、安全重写)在路径②里都能用「server 模式默认值」实现,而路径①的缺点是不可逆的。

---

## 2. 功能取舍:砍什么、留什么

原则:** vococo 的 UI 能力、对话能力、agent 能力是产品本体,完整保留;砍的是「只有主人才有意义」的本地特权功能。**

### 2.1 必须完整保留(对外交互端口)

| 模块 | 位置 | 说明 |
|---|---|---|
| Web UI 全套 | `gateway/adapters/web.py` | SSE 流式 + 断线补发、工具卡片、多会话侧边栏、历史——这是对客户的门面,一个不能少 |
| agent 循环 | `core/agent.py: stream_turn` | 流式事件、保温池(`client_pool.py`)、上下文预算管理,无用户状态,直接用 |
| 内置 MCP 工具 | `tools/builtin.py` | 记忆/cron/发消息等,租户隔离后保留;`restart_self` 等自我运维类在 server 模式摘除 |
| skill 系统 | `settings_store.py` + `~/.claude/skills` | 保留机制,管理权收回平台(见 §4.3) |
| 危险分级 | `tools/danger.py` | 保留三档框架,**server 模式改默认策略**(见 §4.5) |
| cron + 后台任务引擎 | `cron/scheduler.py` + `core/task_runner.py` | 客户场景同样有价值(定时报表/监控),加租户隔离与配额 |
| 多供应商切换 | `providers.py` | 保留,但 key 管理收归平台侧 |
| Web Push | `gateway/adapters/web_push.py` | PWA 通知对客户同样是留存利器 |

### 2.2 砍掉 / server 模式关闭

| 模块 | 理由 |
|---|---|
| TUI / `vococo chat` CLI | 服务器无 TTY;管理员排障用 ssh + 日志 |
| 语音全套(Omni WebRTC / STT / TTS, `voice/`) | 全局单份阿里云 key、成本高、客户场景非必需;v1 直接关,`VOICE_ENABLED` 默认 off,后续按需开 |
| git worktree 隔离(`core/worktree.py`)+ `merge-main.sh` + `restart_self` | 这套是「让 agent 安全地改 vococo 自己的代码」,客户不是来改 vococo 的;改为**租户沙箱工作目录**(`data/tenants/<tid>/workspace/`),危险分级的「写 cwd 外 escalate」逻辑原样生效 |
| 自我运维部署脚本(`deploy/run.sh`、`launchd.sh`) | 服务器版由 Docker/systemd + 部署管线管,不走 AI 自杀还魂那套 |
| 全局 `~/AI_BRAIN` 记忆 | 机制保留,路径改 per-tenant:`data/tenants/<tid>/memory/`(客户的助理只记该客户的事) |
| `UNIFY_SESSIONS` 跨入口合并主会话 | 服务器上没有「主人全端共享一个大脑」这回事,每个客户会话天然独立 |
| 设置页的供应商/key 管理 | 收归平台;客户侧设置页只留:选 agent、选模型(套餐内)、skill 开关(套餐内)、改密码 |

### 2.3 改造后可保留为「可选渠道」

Telegram adapter(`adapters/base.py` 的 Protocol 设计本来就支持多平台)——server 模式下可以作为客户的可选接入渠道,但 v1 不做,Web 先行。

---

## 3. intertrade-bot 借鉴清单

按「直接抄骨架 / 改造后用 / 明确不抄」三档:

### 3.1 直接抄骨架(高价值、与外贸无关)

1. **计费四层结构**(`billing/`,整套):
   - `wallet.py` 单一钱包 + `wallet_topups` 充值流水 + `InsufficientBalanceError`;
   - `pricing.py` `GLOBAL_MARKUP`(默认 5 倍)+ 按 vendor 单独 markup,`billed = vendor_cost × markup`;
   - `ledger.py` **双列记账**(`vendor_cost_cny` 真实成本 / `billed_cny` 客户售价)——运营毛利一眼可见;
   - `cost_guard.py` 回合前 `check_budget` 预检、回合后 `record_cost` 实扣。
   这套与业务完全无关,是 server 版计费的地基。

2. **双 manifest 模式**:`bots_manifest.yaml`(产品目录:名称/人设/默认配额/skills)+ `skills_manifest.yaml`(部署侧:requires_mcp / allowed_tools_override / 工具中文标签)。「agent 产品定义」与「租户实例配置」分离,避免每租户一份 config 漂移——正好解决 vococo 多 agent 化的问题。

3. **`services/base.py` 的 ServiceContext + 装饰器模式**:`@service_call` 统一做 vendor 错误归一化、计费入口、租户上下文注入。vococo 的外部 MCP/vendor 调用将来要计费,走同一个 `charge()` 闸门,不会漏扣。

4. **chat 级串行闸**(`core/bot.py: _spawn_gated`,同 chat 1 in-flight + 0 queued):既是防并发烧钱,也是天然的 per-tenant 限流参考。

5. **结构化日志 ContextVar**(`core/obs.py`:`current_{tenant,bot,run_id}` 全链路):多租户排查问题的必需品,vococo 现在没有,server 版必须补。

### 3.2 改造后用

| intertrade 做法 | vococo server 版怎么用 |
|---|---|
| 行级 `tenant_id` 隔离(所有 SQL 第一参数是 tenant_id) | **改升级:per-tenant DB 文件**(见 §4.1,理由详述) |
| 租户凭据 `clients/<tenant>/.env` | 改为 platform.db 加密存储(Fernet,`auth/crypto.py` 现成) |
| admin dashboard(Basic Auth + 钱包/成本/巡检页) | 功能清单照抄,界面用 vococo Web 技术栈重写 |
| SQLite + `CREATE TABLE IF NOT EXISTS` 自迁移 | v1 沿用;`ensure_columns` 幂等加列套路照抄 |

### 3.3 明确不抄

- **飞书绑定**:消息入口、OAuth、用户身份、文档/表存储全在飞书上——vococo server 版入口是自己的 Web UI,这套用不上;要借鉴的只有「token 三元组 PK」这类隔离设计思想。
- **外贸业务 schema / 8 个外贸岗位 bot**:产品定义重写,骨架(双 manifest)留下。
- **8 个 systemd service + N 个 timer 的重部署**:vococo server 版起步单进程单容器,worker 拆分等规模到了再说。
- **vendor 全家桶**(SaleSmartly/RunningHub/Xpoz):与 vococo 客户场景无关。

---

## 4. 落地方案

### 4.1 多租户隔离

**核心动作:在「进程」和「会话」之间插入租户层。**

```
config.py(进程级,不变)
   └─ tenant context(新增:每个请求/任务解析出 tenant_id,ContextVar 传递)
        └─ session_key(现有,加租户前缀 t:<tid>:web:<conv>)
```

| 维度 | 方案 | 理由 |
|---|---|---|
| 平台元数据(租户/钱包/流水/agent 绑定/账号) | 单一 `data/platform.db`,行级 `tenant_id` | 跨租户统计、扣费对账都要全表扫,必须单库 |
| 客户会话数据(turns/session_meta) | **per-tenant 文件**:`data/tenants/<tid>/state.db` | vococo 的 `memory/_db.py` 是单例连接,改成「按租户取连接」比给所有表现有加列侵入小得多;天然物理隔离;删租户=删目录(GDPR 友好);单文件锁竞争消失 |
| 记忆 | `data/tenants/<tid>/memory/`(替代全局 `~/AI_BRAIN`) | `save_memory`/`recall_past` 工具按当前租户上下文路由 |
| 文件/沙箱 | `data/tenants/<tid>/workspace/` 作为该租户 agent 的 cwd | 替代 worktree;危险分级「写 cwd 外 escalate」原样生效 |
| 凭据 | platform.db 加密字段(Fernet,抄 intertrade `auth/crypto.py`) | 不再用 `.env` 文件散落 |
| cron 任务 | `platform.db` 的 `cron_jobs` 表带 `tenant_id`(替代全局 `cron_jobs.json`) | 调度器 tick 时按租户分发 |

**租户解析**:HTTP 请求 → 鉴权中间件 → `tenant_id` 写入 ContextVar → 后续所有存储/计费/记忆调用自动携带。借鉴 intertrade `core/obs.py` 的 ContextVar 套路,一处注入全链路可用。

### 4.2 计费扣费

模型:**预充值钱包 + 按实际 vendor 成本 × markup 扣费 + 双列流水**(intertrade 已验证的模式,不做订阅套餐——客户量和定价模型都没验证前,订阅制是给自己挖坑)。

```
回合开始:cost_guard.check_budget(tenant_id)  余额 ≤ 阈值 → 直接拒,提示充值
回合结束:SDK 回传 token usage → 按模型单价表算出 vendor_cost
        → billed = vendor_cost × markup(默认 4-5x,对齐 intertrade 的 5x)
        → wallet.deduct() 同步扣余额 + ledger.insert() 双列记账
外部 vendor(MCP/搜索/生图):走统一的 services 层 charge() 闸门,同上
```

- 单价表:`vendor_rates.py` 按模型维护(DeepSeek V4 / Kimi / Claude 各自 input/output 单价),涨价改表不改码。
- 充值:v1 手动(客户微信/支付宝转账,admin 后台 `topup()`);v2 再接自动支付(见 §6 风险)。
- 超支兜底:`record_overdraft()` 允许小额透支(vendor 调用已发生但余额不足),抄 intertrade。
- 套餐层(P2 再做):钱包之上加「月配额」字段,超配额即降速/拒服,为将来的定价分层留口。

### 4.3 多 agent 配置

```
agents_manifest.yaml(平台产品目录,进 git)
  agent_id: cs_bot
    display_name: 客服助理
    persona_file: agents/cs_bot/PERSONA.md      # 人设 system prompt
    default_model: deepseek-v4-flash
    skills: [faq, order_query]
    allowed_tools: [...]
    monthly_quota_cny: 50

platform.db: tenant_agents 表(租户实例)
  (tenant_id, agent_id, status, quota_override, created_at)
```

- 运行时:客户在 Web UI 选 agent 建会话 → 会话绑定 `agent_id` → prompt 三层堆叠变为「preset + **agent persona** + 租户记忆」——恰好把 vococo 现有的单一人格 `PERSONA_NAME` 泛化成 per-agent。
- skill 管理权:平台在 manifest 里定义每个 agent 可用 skill;租户只能在已开通 agent 的 skill 集合内开关,不能自由挂外部 MCP(安全 + 计费双重需要)。
- vococo 现有的 vococo-internal 插件机制(`core/agent.py: _PLUGIN_SKILLS`)保留,给平台自用能力用。

### 4.4 部署形态(海外主机)

- **单 VPS 起步**(现有 Vultr/Kimmy 经验直接复用),Debian + Docker 单容器(`uv sync` 构建,`vococo serve` 入口,server 模式)。
- 进程形态:**单进程**(web + cron + task runner 同事件循环,vococo 现状如此),规模到 ~50 租户再拆 worker。
- 反代/TLS:Caddy(自动 HTTPS)或 nginx;前置 Cloudflare 橙云(DNS token 已有,`cf-api-access` 记忆可复用)。
- 数据:`data/` 挂卷;每日 sqlite 快照备份(抄 intertrade `deploy/backup_db.sh` 套路,部署前自动备份)。
- 监控:v1 = 结构化日志 + `/healthz` + 告警 webhook(飞书/TG 发给 Wesley);v2 上 Sentry。
- 域名建议:`app.<品牌域>.com`,与国内资产隔离。

### 4.5 安全与鉴权

**这是架构哲学变化最大的一处:vococo 现有模型是「聊天对象=主人=可信审批者」,server 版是「聊天对象=客户=不可信输入源」。**

| 项 | 方案 |
|---|---|
| 账号体系 | v1:邮箱+密码(argon2)+ session cookie;admin 走独立 Basic Auth(抄 intertrade)。v2:Google/微信 OAuth |
| 请求鉴权 | 中间件统一解析,替代现有单份 `WEB_AUTH_TOKEN`;`_guard()` 按路由要求登录/管理员 |
| 危险分级 server 策略 | **escalate 档对客户默认 block**(客户没有审批权,弹审批是平台给自己找事);cwd=租户沙箱,沙箱内读写/执行 allow;`block` 档不变;`_hard_guard` 三条防线不变 |
| 密钥管理 | 模型/vendor key 全部平台持有,注入 SDK 子进程 env(现有 `_turn_env()` 机制),客户永不可见;`_scrub_env_secrets()` 保留 |
| 限流 | per-tenant 并发回合上限 + 单回合 token 上限 + chat 级串行闸(防单一客户薅光平台 key 额度) |
| 注入防护 | 客户输入即不可信:系统提示明确「用户是指令来源但不是主人」;记忆/文件工具严格限定租户目录;外部 MCP 不允许租户自定义 stdio(现有 `WEB_ALLOW_STDIO_MCP` 默认禁,server 模式永禁) |
| Web 安全基线 | 现有 `_security_mw`(CSP/nosniff/跨源写拦截)保留,加 per-IP 登录限频 |

---

## 5. 分阶段实施路线

| 阶段 | 周期(估) | 内容 | 验收标准 |
|---|---|---|---|
| **P0 租户地基** | 2-3 周 | `VOCOCO_MODE` 开关;tenant 上下文(ContextVar);账号鉴权中间件;per-tenant 数据目录与 state.db;server 模式功能裁剪(语音/TUI/worktree/自我运维全关);危险分级 server 策略;Docker 单容器上 VPS | 两个测试租户同时在线,会话/记忆/文件互不可见;危险操作在 server 模式被 block |
| **P1 计费** | 2 周 | platform.db(tenants/wallets/ledger/topups);token 计量 + 单价表 + markup;cost_guard 预检/实扣;admin 后台 v1(余额/充值/流水/毛利) | 跑一周对账:ledger 的 vendor_cost 合计 ≈ 供应商账单,billed 合计 = 扣费合计 |
| **P2 多 agent** | 2 周 | agents_manifest.yaml + tenant_agents 表;agent 选择 UI;persona 注入;skill 权限收敛到 manifest;月配额 | 同一租户开 2 个 agent,人设/工具集/skill 互不影响;超配额正确拒服 |
| **P3 打磨** | 持续 | 自动支付;租户自助后台(看用量);PG 迁移(>50 租户或单库 >5GB 再议);可观测(Sentry+用量看板);语音按客户需求重启;Telegram 作为第二渠道 | — |

每个阶段结束都可独立上线:P0 结束即可邀请 seed 客户免费试用,P1 结束即可开始收钱。

---

## 6. 风险点(按严重度)

1. **单进程爆炸半径(高)**:所有租户共享一个 Python 进程/事件循环,一个租户的 agent 死循环/超大上下文可能拖垮全部。对策:per-tenant 并发闸 + 回合硬超时 + 保温池 LRU 上限 + 看门狗(vococo 现有 `watchdog.py` 假死自杀机制正好用上);中期拆 worker 进程。
2. **安全模型翻转不彻底(高)**:任何一处残留「信任聊天对象」的假设(如 `/doc/preview` 按 HOME 读文件、设置页暴露 key)都是租户间数据泄露。对策:P0 做一次「以客户视角」的权限审计,逐路由过;`tenant_id` 注入走 ContextVar 强制,不允许业务代码自行传参。
3. **计费精度(中)**:Claude Agent SDK 的 token usage 是否完整覆盖子代理/压缩/MCP 调用需要实测;对策:ledger 双列设计与供应商账单周对账(P1 验收标准已含),发现口径差就补 `record_service_cost`。
4. **支付与合规(中)**:国内主体收款的通道问题(微信/支付宝手动充值起步,Stripe 需海外主体);面向国内客户还涉及生成式 AI 备案(参见 `llm-beian-gbt45654` 记忆)——先小范围 seed 客户、手动收款,量起来再解决主体与备案。
5. **SQLite 扩展性(低-中)**:per-tenant 文件已大幅缓解;platform.db 是写入热点但量级小(扣费流水);PG 迁移留作 P3 选项,所有 SQL 走 store 层不散落,迁移成本可控。
6. **双模式维护纪律(低-中)**:模式分支散落会重蹈双份维护覆辙。对策:立规矩——分支只许在 config/feature-flag 层,业务代码只读 flag;review 时专项检查。

---

## 7. 结论

**同一套代码、`VOCOCO_MODE` 模式切换;P0 先做租户地基,从 intertrade-bot 移植计费四层与双 manifest 骨架,单 VPS Docker 起步。** 这套路线让 vococo 的每一项新能力自动成为服务器版的能力,也让 intertrade-bot 蹚过的多租户/计费坑不白蹚。
