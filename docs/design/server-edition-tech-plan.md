# vococo 服务器版 技术方案(路径②:同一套代码 + 部署模式)

> 2026-08-05。配套文档:`docs/design/server-edition.md`(产品思路与路线决策)。
> 本文件是落地层设计:每个改动给出具体文件、函数、schema。行号以撰写时 main 分支为准。

---

## 1. 总体设计:三层配置 + 租户上下文

```
L1 进程层  config.py        现有,不动。模式判定只在这里发生一次。
L2 租户层  tenancy/(新增)   每个请求/任务解析出 tenant_id,ContextVar 全链路传递
L3 会话层  session_key      现有,server 模式加租户前缀
```

**铁律:`if MODE == "server"` 只允许出现在 config.py、tenancy/、各 store 的「后端选择」一处;业务代码只调 tenancy 抽象,不直接判模式。** 违反这条 = 模式分支散落 = 变相双份维护。

### 1.1 模式开关(config.py 新增)

```python
# config.py 顶部追加
MODE: str = os.environ.get("VOCOCO_MODE", "personal").strip() or "personal"
IS_SERVER: bool = MODE == "server"
```

server 模式派生默认(同文件,覆盖个人默认):

| 配置 | personal | server | 理由 |
|---|---|---|---|
| `WEB_HOST` | 127.0.0.1 | 0.0.0.0(容器内,前置 Caddy) | 对外服务 |
| `VOICE_ENABLED` | 按 .env | **强制 False** | 语音 v1 不做 |
| `UNIFY_SESSIONS` | True | **强制 False** | 没有「主人一个大脑」 |
| `WEB_ALLOW_STDIO_MCP` | False | **强制 False,且忽略 env** | 租户注册 stdio MCP = RCE |
| `APPROVAL_GATE` | True | 无效(见 §8,escalate 直接 block) | 客户不是审批人 |
| `CLIENT_POOL_IDLE_TTL` | 300 | 120(可 env 覆盖) | 多租户抢池,缩短回收 |

worktree/项目体系、selfops(restart_self/merge-main)在 server 模式不进入口(见 §8.3)。

---

## 2. 租户上下文(新包 `vococo/tenancy/`)

### 2.1 `tenancy/context.py`

```python
_tenant: ContextVar[str] = ContextVar("tenant", default="local")

def current() -> str:
    """personal 模式恒返回 'local';server 模式由中间件/调度器注入。
    server 模式下若仍为 'local' → 抛 TenantContextError(fail-closed,
    防止某条代码路径忘了注入而静默写进共享数据)。"""
```

配合 `set(tid)` / `reset(token)`。注入点只有三个(覆盖全部入口):

1. **Web 请求**:`gateway/adapters/web.py` 新增 `_auth_mw`(见 §4),每个请求解析 cookie → tenant_id → `tenancy.context.set()`,请求结束 reset。anyio 每个连接独立 task,ContextVar 天然 task 隔离;`anyio.to_thread` 会复制 context,线程池里也安全。
2. **cron 触发**:`cron/scheduler.py:_run_job()` 起任务前按 job 行的 tenant_id 注入。
3. **task_runner**:`core/task_runner.py:dispatch()` 的 task 记录带 tenant_id,执行线程入口注入。

### 2.2 `tenancy/paths.py`

```python
def data_dir() -> Path:      # personal → config.DATA_DIR
                             # server  → DATA_DIR/"tenants"/<tid>
def brain_dir() -> Path:     # personal → config.AI_BRAIN_DIR
                             # server  → data_dir()/"memory"
def workspace_dir() -> Path: # personal → None(维持 worktree/项目体系)
                             # server  → data_dir()/"workspace"(租户沙箱 cwd)
def settings_path() -> Path: # personal → DATA_DIR/"web_settings.json"
                             # server  → data_dir()/"settings.json"
```

这四个函数是全部「按租户换路径」的唯一出口。现有调用点逐个换源:

| 现有调用点 | 换成 |
|---|---|
| `memory/_db.py: conn()` 里 `config.DATA_DIR/"state.db"` | `tenancy.paths.data_dir()/"state.db"` |
| `core/prompt.py:86-110` 读 `AI_BRAIN_DIR` 的 USER.md/MEMORY.md | `tenancy.paths.brain_dir()` |
| `tools/builtin.py: save_memory / recall_past` | 同上(recall 走 `_db.conn()` 自动跟随) |
| `gateway/settings_store.py:_PATH` | `tenancy.paths.settings_path()` |
| `gateway/adapters/web.py:1751-1809` /file、/doc/preview 边界(HOME+AI_BRAIN+cwd) | server 模式 = workspace+brain 两目录 |
| `core/agent.py` 图片/音频落盘(IMAGES_DIR/AUDIO_DIR) | `data_dir()/"images"` 等同理 |

### 2.3 `tenancy/store.py`(platform.db,仅 server 模式)

单一平台库,行级 tenant_id。schema:

```sql
CREATE TABLE tenants(
  tenant_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',   -- active/suspended
  wallet_cny_balance REAL NOT NULL DEFAULT 0,
  monthly_quota_cny REAL,                  -- NULL=不限(P2 启用)
  markup REAL NOT NULL DEFAULT 5.0,
  created_at INTEGER NOT NULL
);
CREATE TABLE users(
  user_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,             -- argon2id
  role TEXT NOT NULL DEFAULT 'owner',      -- owner/member
  status TEXT NOT NULL DEFAULT 'active',
  created_at INTEGER NOT NULL
);
CREATE TABLE web_sessions(                 -- 登录态
  token TEXT PRIMARY KEY,                  -- 32B 随机 hex
  user_id TEXT NOT NULL REFERENCES users(user_id),
  expires_at INTEGER NOT NULL
);
CREATE TABLE tenant_agents(                -- P2 用,P0 先建表
  tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  quota_cny_override REAL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (tenant_id, agent_id)
);
CREATE TABLE cron_jobs(                    -- server 模式替代 cron_jobs.json
  job_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
  spec TEXT NOT NULL,                      -- 原 json 行的完整字段 JSON
  enabled INTEGER NOT NULL DEFAULT 1
);
```

迁移套路照 `memory/_db.py` 现有风格:`CREATE TABLE IF NOT EXISTS` + `PRAGMA table_info` 幂等加列,不引入迁移框架。

---

## 3. 会话存储改造(per-tenant state.db)

### 3.1 `memory/_db.py`:单例 → 按租户连接池

```python
_DBS: dict[str, sqlite3.Connection] = {}

def conn() -> sqlite3.Connection:
    tid = tenancy.context.current()           # personal 恒 'local'
    if tid not in _DBS:
        path = tenancy.paths.data_dir() / "state.db"
        # personal 模式 data_dir()=config.DATA_DIR → 路径与今天完全一致,老数据零迁移
        _DBS[tid] = _connect(path)            # 现有 _SCHEMA + ALTER 迁移逻辑原样搬进 _connect
    return _DBS[tid]
```

`reset()` 改遍历关闭。schema 本身**不动**(session_key 已是隔离键;server 模式 key 带租户前缀,见 §3.2,双保险)。加一条防御:server 模式下 conn() 拿到的 tid 若不带合法前缀直接抛错——物理隔离靠文件,逻辑防御靠前缀。

### 3.2 `config.resolve_session_key`:server 模式加租户前缀

```python
def resolve_session_key(platform: str, chat_id: object) -> str:
    key = _resolve_impl(platform, chat_id)   # 现有逻辑原样
    if IS_SERVER:
        return f"t:{tenancy.context.current()}:{key}"
    return key
```

影响面清查(都要兼容多出来的 `t:<tid>:` 前缀):

- `project_hash_from_key()`(config.py:345):server 模式没有「项目」概念(web 会话不绑本地仓库),直接返回 None → 下游 `project_root_for`/`project_cwd_for` 返回 None → cwd 走 §8.2 的租户沙箱。函数加 `if IS_SERVER: return None` 短路即可,个人模式逻辑不动。
- `_origin_from_session_key()`(gateway/core.py:469)等按前缀判断的函数:统一加一个 `strip_tenant_prefix(key)` helper,所有 startswith 判断前先剥。
- `memory/worktrees.py`、审批闸里认 worktree 的逻辑:server 模式不触发(cwd 不是 worktree)。

### 3.3 session_meta 增列(沿用 _db.py 迁移套路)

```sql
ALTER TABLE session_meta ADD COLUMN agent_id TEXT;   -- P2 多 agent 绑定
```

token 计量列(last_in/last_cache/last_out/total_tokens/model)**已存在**(_db.py:61-77),计费直接复用这个口径,见 §6。

---

## 4. 鉴权(web.py)

### 4.1 中间件链

现有 `_security_mw`(web.py:104,CSP/跨源写拦截)保留不动。新增 `_auth_mw` 挂在其后:

```
personal 模式:维持现状 —— _ok_token() 校验 WEB_AUTH_TOKEN(web.py:358-371)
server 模式:_auth_mw 解析 cookie → web_sessions 查 user → 校验租户 active
            → tenancy.context.set(tenant_id) + request["user"]
            未认证:GET / → 重定向 /login;API → 401 JSON
```

`_guard()` 双模式分支(这是「后端选择」允许判模式的位置之一)。

### 4.2 账号流程(v1 邀请制,不开放自助注册)

- 平台管理员在 /admin 创建租户 + 首个 owner 账号(邮箱 + 初始密码)。
- `POST /auth/login`(邮箱+密码,argon2id 校验)→ 种 httponly cookie(30 天);`POST /auth/logout`;`POST /auth/password`(改密)。
- 登录限频:per-IP 5 次/分钟,失败 10 次锁 15 分钟(内存计数即可)。
- `WEB_AUTH_TOKEN` 在 server 模式废弃(保留 personal 兼容)。

### 4.3 平台管理员

v1 抄 intertrade 做法:`ADMIN_USER`/`ADMIN_PASSWORD` env,`/admin/*` 路由独立 Basic Auth,与租户账号体系完全分离。

---

## 5. Web 路由与 UI 改造清单

| 路由/功能 | server 模式改动 |
|---|---|
| `GET /login`、`/auth/*` | 新增(仅 server) |
| `GET /`(index.html) | 未认证 → /login;会话列表数据源自动按租户隔离(conn 跟随 context) |
| 侧边栏「项目分组」 | server 模式隐藏(无项目概念),P2 换成「agent 分组」 |
| `/conversations`、`/history`、`/send`、`/events` | 零改动(session_key 前缀自动隔离;SSE `_clients` 按连接订阅会话,天然按 key 分) |
| `/settings/*` | 拆分:`/settings/providers`(供应商/key)server 模式 403,收归 .env + platform 配置;保留模型选择(白名单内,见 §7)、skill 开关(manifest 子集内,见 §7) |
| `/file/read|save`、`/doc/preview` | 边界改为 workspace+brain(§2.2) |
| `/cron/jobs/*` | 后端切 platform.db cron_jobs 表,按当前租户过滤 |
| `/pub/*` 发布 | server 模式 v1 关闭(共享发布目录是跨租户泄露面;vococo-web-publish 插件同理不挂) |
| Web Push | v1 关闭(`push_subs.json` 是全局文件),P2 租户化后开 |
| `GET /healthz` | 新增:返回 200 + db 连通检查,供 Caddy/uptime 监控 |
| `GET /usage` | 新增(P1):当前租户本月 billed 合计 + 余额(只读) |
| `/admin/*` | 新增(P1,Basic Auth):租户 CRUD、充值、流水、毛利 |

前端(index.html)改动集中在:登录页、隐藏项目分组、设置页按模式渲染。「新建会话先选 agent」属 P2。

---

## 6. 计费(新包 `vococo/billing/`,仅 server 模式激活)

移植 intertrade-bot `billing/` 四层骨架(wallet/pricing/ledger/cost_guard),代码重写、设计照抄。

### 6.1 schema(platform.db)

```sql
CREATE TABLE wallet_topups(
  id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL,
  amount_cny REAL NOT NULL, note TEXT DEFAULT '',
  operator TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE cost_ledger(
  id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL,
  session_key TEXT, agent_id TEXT,
  kind TEXT NOT NULL,             -- model / mcp / service
  vendor TEXT NOT NULL,           -- deepseek / kimi / anthropic / tavily ...
  model TEXT,
  in_tokens INTEGER DEFAULT 0, out_tokens INTEGER DEFAULT 0, cache_tokens INTEGER DEFAULT 0,
  vendor_cost_cny REAL NOT NULL,  -- 真实成本(运营视角)
  billed_cny REAL NOT NULL,       -- 客户售价 = vendor_cost × markup
  ts INTEGER NOT NULL
);
CREATE INDEX idx_ledger_tenant_ts ON cost_ledger(tenant_id, ts);
```

### 6.2 文件与函数

```
billing/rates.py    MODEL_RATES: {model: (in/m tokens ¥, cache/m ¥, out/m ¥)}
                    cost_for(model, in, cache, out) -> vendor_cost_cny
billing/wallet.py   deduct(tenant_id, billed, vendor_cost, meta) 余额不足→InsufficientBalanceError
                    topup(tenant_id, amount, operator, note)
                    balance(tenant_id) / precheck(tenant_id) -> bool
billing/ledger.py   insert(...)   双列记账
billing/guard.py    check_budget(tenant_id):余额 ≤ 0 → False
billing/meter.py    charge_turn(usage):回合成功后的统一计费入口
```

### 6.3 两个 hook 点(精确到行)

1. **预检**:`gateway/core.py:172 converse()` 入口、history load(line 203)之前:
   ```python
   if config.IS_SERVER and not billing.guard.check_budget(tenancy.context.current()):
       return Reply(text="账户余额不足,请联系客服充值后再使用。", is_error=True)
   ```
2. **实扣**:`gateway/core.py` 轮末(line 326 `set_last_error` 附近,回合成功分支):
   ```python
   if config.IS_SERVER and not reply.is_error:
       billing.meter.charge_turn(...)  # 从 session_meta 读本轮 last_in/last_cache/last_out/model
   ```
   计量口径复用现有 `get_context_usage()`(agent.py:563)写入 session_meta 的数据,**不在 agent.py 里新增计量逻辑**——这是「后端选择」式的单点 hook,个人模式零开销。

### 6.4 扣费规则

- `billed = vendor_cost × tenants.markup`(默认 5.0,可对租户单设)。
- 扣费时余额不足:允许扣成负数(透支兜底,抄 intertrade `record_overdraft` 思路),下回合被预检拦截;ledger 记 `kind='overdraft'` 备注。
- 外部 MCP/vendor(tavily 等):v1 平台统一 key 不计费(含在 markup 里),v2 走 `charge(kind='mcp')` 单独计价——预留 kind 列即为此。
- 对账:admin 后台周视图 `sum(vendor_cost) vs sum(billed)`,P1 验收标准。

---

## 7. 模型与多 agent(P2,此处定接口)

### 7.1 平台侧

- 供应商 key 全部回 .env(平台持有),server 模式 `/settings/providers` 关闭;`providers.py: resolve()` 逻辑不动。
- 模型白名单:`SERVER_ALLOWED_MODELS` env(如 `deepseek-v4-flash,deepseek-v4-pro,kimi-k3`),客户 `/model` 与设置页只能选白名单内——防客户切到贵模型打穿成本。
- **开放问题(重要)**:多客户共用平台 Claude 订阅(OAUTH_TOKEN)有条款与限额风险。v1 建议 server 模式默认模型走按量 API key(DeepSeek/Kimi),成本进 rates;Claude 订阅只给 personal 模式。

### 7.2 `agents_manifest.yaml`(仓根新增,进 git)

```yaml
agents:
  cs_bot:
    display_name: 客服助理
    persona_file: agents/cs_bot/PERSONA.md     # 仓内 agents/ 目录,进 git
    default_model: deepseek-v4-flash
    skills: [faq, order_query]                  # 指向仓内 agents/skills/
    monthly_quota_cny: 50
    welcome: {capabilities: [...], examples: [...]}
```

- skill 目录:server 模式 `_SKILLS_DIR`(settings_store.py:31)从 `~/.claude/skills` 换成**仓内 `agents/skills/`**(Docker 镜像内置,与主人个人 skill 库物理隔离)。`effective_skills()` server 模式 = manifest 声明 ∩ 目录存在。
- 外部 MCP:server 模式只允许 manifest 里平台声明的 http/sse server;stdio 永禁。
- 运行时:新会话选 agent → `session_meta.agent_id` 落库 → converse 时读出 → `core/prompt.py: build_system_prompt` 注入点从「preset + PERSONA + 记忆」变为「preset + **agent persona** + 租户记忆索引 + 边界声明(用户是客户,不是主人)」。personal 模式注入链不动。
- 月配额:每回合实扣后累计,超 `monthly_quota_cny`(或 tenant_agents 覆盖值)拒服提示。

---

## 8. 危险分级与沙箱(tools/danger.py)

### 8.1 server 模式策略表

| 档 | personal | server |
|---|---|---|
| block(灾难级) | 拦 | 拦,不变 |
| escalate(写 cwd 外/git push/reset --hard/rm -rf/装包/curl\|sh) | 弹审批 | **直接 block**,回复中说明「该操作超出沙箱权限」 |
| allow | 放行 | 放行 |
| _hard_guard 三条 | 拦 | 后台腰斩/记忆孤本两条保留;worktree 越界条不触发(无 worktree) |

实现:`classify()`(danger.py:223)返回 escalate 后,在 `pretool_guard_hook`(line 596)统一判断 `config.IS_SERVER → deny`。改动一处,不碰 classify 本体。

### 8.2 租户沙箱 cwd

- `gateway/core.py:210` 现在是 `cwd = config.project_cwd_for(session_key)`(None=进程默认目录=仓库根——server 模式绝不允许)。
- 改为:
  ```python
  cwd = config.project_cwd_for(session_key)
  if cwd is None and config.IS_SERVER:
      cwd = str(tenancy.paths.workspace_dir())   # 确保 mkdir -p
  ```
- 效果:「写 cwd 外」档的判定基准自动变成租户沙箱,危险分级逻辑零改动;agent 在沙箱内可自由读写文件(客户场景需要:做表格、写文档)。

### 8.3 入口裁剪

- `gateway/run.py: run_serve()`:server 模式不挂 Telegram adapter(v1)、不挂 voice 路由。
- 内置 MCP(tools/builtin.py:763 `build_mcp_servers`):server 模式摘除 `restart_self`、`add_mcp_server`、`set_external_mcp`;`dispatch_session` 保留但走 §9 配额;`suggest_automation` 保留(consent-first 同样适合客户)。
- selfops 遗书/还魂(run.py: `_resume_after_restart`)server 模式不启用(由容器重启策略管)。

---

## 9. cron 与任务引擎

- `cron/scheduler.py: load_jobs/save_jobs`(line 46-71):server 模式后端切 platform.db cron_jobs 表,接口签名不变,`create_job` 自动带当前 tenant_id。`_run_job` 入口注入 tenant context(§2.1)。
- `core/task_runner.py`:全局 `TASK_MAX_CONCURRENCY` 保留;server 模式加 per-tenant 上限 `SERVER_TENANT_TASK_MAX`(默认 2),dispatch 时按 tenant 计数,超了排队或拒绝。会话级锁(gateway/core.py 现有 per session_key)天然继承,同会话串行不用新做。

---

## 10. 部署(海外 VPS)

```
Dockerfile(仓根新增):
  FROM python:3.12-slim
  pip install uv && uv sync --locked      # 不带 dev extra
  ENV VOCOCO_MODE=server
  CMD ["uv","run","vococo","serve"]
.dockerignore: data/、.git、tests/
```

- 卷:`/app/data` 挂宿主机持久卷(含 platform.db + tenants/)。
- 反代:Caddy(`app.example.com { reverse_proxy 127.0.0.1:8848 }`),自动 TLS;可前置 CF 橙云(现有 cf-dns-token 复用)。
- 守护:docker `restart: always`(替代 deploy/run.sh 守护循环);watchdog 自杀机制保留(容器层拉起)。
- 备份:宿主机 cron 每日 `sqlite3 platform.db ".backup ..."` + tar tenants/ 到异地(抄 intertrade `deploy/backup_db.sh` 套路);部署脚本 deploy.sh:构建 → 备份 → 滚动重启。
- 监控:`/healthz` + 日志 + 告警 webhook;Sentry 留 P3。

---

## 11. 分阶段任务分解(PR 级)

**P0 租户地基(2-3 周)**
1. PR1:config.MODE/IS_SERVER + tenancy/{context,paths}.py + 全部路径调用点换源(§2.2 表);personal 回归测试全绿。
2. PR2:_db.py 按租户连接池 + resolve_session_key 前缀 + strip_tenant_prefix helper + session_meta.agent_id 列。
3. PR3:tenancy/store.py platform.db + users/web_sessions + /login + _auth_mw + _guard 双模式。
4. PR4:danger.py server 策略 + 沙箱 cwd(§8.2)+ 入口裁剪(§8.3)+ skill 目录切仓内 + 模型白名单。
5. PR5:Dockerfile + Caddy + 备份 + /healthz + 上 VPS,双测试租户冒烟。

**P1 计费(2 周)**
6. PR6:billing/ 四件 + rates + 两个 hook(§6.3)+ 透支兜底。
7. PR7:/admin/*(租户 CRUD/topup/ledger/毛利)+ /usage 客户页 + 对账视图。

**P2 多 agent(2 周)**
8. PR8:agents_manifest.yaml + agents/ 目录 + tenant_agents 绑定 + 新会话选 agent UI + persona 注入 + 月配额。
9. PR9:Web Push 租户化 + /pub 按租户重开(如需)+ Telegram 作为第二渠道(如需)。

每个 PR 独立可上线、独立可回滚;personal 模式全程可用(Wesley 的日用不受影响是硬约束)。

## 12. 测试与验收

- 单测(tests/ 新增 tenancy/、billing/ 目录):ContextVar 注入/泄漏(两个并发 task 不同 tid 互不串);rates 计算;wallet 扣负与预检;resolve_session_key 双模式。
- 集成验收(P0 出报告):双租户并发会话,互查对方 session_key/history 全 404;租户 A 的文件/记忆/cron 在租户 B 视角不存在;escalate 操作被 block;容器重启后会话可 resume。
- 计费对账(P1 验收):mock 一周流量,`sum(vendor_cost)` 与供应商账单误差 <5%,`sum(billed)` = 扣费合计。
- 回归纪律:每个 PR 必须 `uv run pytest` 全绿(personal 模式)才许合。

## 13. 开放问题(动手前要拍板的)

1. **平台模型 key 形态**:多客户共用一个 Claude 订阅有条款/限额风险 → v1 建议 server 模式只走按量 API(DeepSeek/Kimi),Claude 留给 personal。(倾向:是,把握中)
2. **保温池**:CLIENT_POOL_MAX=4 全局,多租户争抢;v1 缩短 TTL 观察,不行就 server 模式关保温池(冷启动+resume 老路径)。待实测。
3. **SDK transcript 位置**:CLI 的 sdk_session_id transcript 存进程 HOME,多租户共享进程无冲突(id 唯一),但删租户时要级联清理——列入租户删除 checklist。
4. **注册放开节奏**:v1 邀请制;自助注册+邮件验证列入 P3,与支付自动化同期。
