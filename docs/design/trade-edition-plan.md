# VocoTrade(外贸版)改进计划

> 2026-08-06 定稿。产品名:**VocoTrade**,域名:**vocotrade.com**(可注册,待注册)。
> 配套存档:server-edition.md / server-edition-tech-plan.md(**已废弃**,同代码双模式路线,仅作参考;P0 代码存档锚点 `git tag server-edition-p0`)。

## 1. 背景与路线转变
- 现状:vococo 个人版快速迭代;服务器版曾走「同一套代码 + VOCOCO_MODE=server」路线,P0 已上线 Lobster(ai.chjm.cc)。
- 问题:P0 仍是 Coder 逻辑——客户拿到的是「翻版 Wazir」(单一人格、开发者形态 UI、通用聊天框),不是数字员工产品。产品形态差异不是开关能表达的,双模式越往后每加一个功能都要写两套分支。
- 新路线:**两套代码**。vococo 个人版继续;VocoTrade 独立仓库,产品逻辑彻底重构。个人版新能力通过「同步区」机制单向搬运,避免双份维护。

## 2. 产品定位
**面向无代码/低办公技能客户的多 Agent 数字员工平台:用户不配置、只选择。**
- 左栏:员工花名册(按场景分类)→ 选一个员工,直接用
- 右栏:员工工作台(聊天 + 常用任务卡片 + 数据面板 + 员工简介)
- 全局管理:我的员工 / 我的数据(连接器授权)/ 用量余额
- 员工模板 = 人设 + 技能包 + 数据源绑定 + 工作台预设;技能/模型/供应商配置全归平台,客户不可见

## 3. 命名与域名(已拍板)
| 候选 | 域名 | 状态 |
|---|---|---|
| **VocoTrade** ✅ | vocotrade.com | 可注册(待注册) |
| VocalWaimao(备选) | vocalwaimao.com | 可注册 |
| VocalTrade | vocal.trade | 可注册(.trade 冷门) |
| —(被占) | voctrade.com / vocaltrade.com | ❌ 已注册 |

- 仓库名与产品名一致:**vocotrade**;部署:海外 VPS + Docker + Caddy,独立于 vococo 与 intertrade-bot
- Local Trade 不推荐——语义为「本地贸易」,与定位相反
- 国内客户涉及 AI 备案(见 llm-beian-gbt45654 记忆),上线前确认

## 4. 两套代码边界
核心:两套代码 ≠ 两套核心。共享资产抽成「同步区」,VocoTrade 把 vococo 当 upstream 单向同步。

| 区域 | 内容 | 机制 |
|---|---|---|
| 同步区(vococo → VocoTrade) | core/agent.py、tools/danger.py、cron/ + task_runner、providers.py、memory/_db.py、web SSE 聊天组件 | git subtree pull;同步区改动先提交 vococo main,VocoTrade 只拉不改 |
| 分叉区(VocoTrade 自研) | agents/(员工模板)、租户层(重写)、工作台 UI、billing/(移植 intertrade 四件套) | 独立开发 |
| 个人专属(不搬) | prompt 人格层、AI_BRAIN、TUI、voice、selfops、worktree 项目体系 | — |

判断标准:代码出现 USER_NAME / AI_BRAIN / PERSONA 个人语义的,不进同步区。

## 5. 改进计划(分阶段)

### 第 0 步:移除 P0 双模式遗产(主仓,已完成 2026-08-06)
理由:双模式代码是错误路线产物,18+ 文件挂 tenancy/IS_SERVER 分支;留着 → 每次改动处理两套分支,越拖越难清。先移除,提交记录 + tag 存档,恢复随时可参考。
- [x] 移除前打存档锚点:git tag server-edition-p0
- [x] 删除 vococo/tenancy/(context/paths/store)
- [x] config.py:去掉 MODE/IS_SERVER 及 server 派生默认值
- [x] 还原引用文件 server 分支,恢复单用户直连:
  __main__.py / core/prompt.py / core/task_runner.py / core/tasks.py / cron/scheduler.py /
  gateway/adapters/web.py / gateway/core.py / gateway/run.py / gateway/settings_store.py /
  memory/_db.py / memory/audio.py / memory/images.py / providers.py / tools/builtin.py / tools/danger.py
  (整体还原至 P0 前提交 97a271b,零手工 diff)
- [x] 删除部署资产:Dockerfile、docker-compose.yml、.dockerignore、deploy/server/
- [x] 删除 agents/ 空壳
- [x] 设计文档标废弃:server-edition*.md 头部加「已废弃,仅存档」
- [x] 验收:grep tenancy|IS_SERVER|VOCOCO_MODE 零命中(存档文档除外);uv run pytest 全绿;vococo doctor 通过

### 第 1 步:建 VocoTrade 仓库
- [ ] fork 当前 main(已无 server 代码,干净起点),仓库名 vocotrade
- [ ] 注册 git remote upstream → vococo;README 写明同步区清单与规则
- [ ] 删除个人专属;保留同步区六块
- [ ] 产出:可跑的空壳服务端

### 第 2 步:产品重构(数字员工矩阵)
- [ ] agents_manifest.yaml + agents/<name>/ 员工模板(人设/技能包/工作台预设/数据源绑定)
- [ ] 租户层按新产品语义重写:每租户 × 每员工一套数据与记忆(不复用旧 tenancy)
- [ ] Web UI 重写:登录 → 员工选择 → 工作台(左栏花名册/右栏工位/全局管理)
- [ ] 客户侧设置收敛:只留「选员工、看用量」

### 第 3 步:计费(移植 intertrade-bot)
- [ ] billing/ 四件套:wallet/pricing/ledger/cost_guard(抄已验证骨架)
- [ ] admin 后台:租户 CRUD/充值/流水/毛利对账

### 第 4 步:部署与替换
- [ ] 海外 VPS + Docker + Caddy(复用现有经验)
- [ ] 注册 vocotrade.com 域名
- [ ] 替换 Lobster 现 P0 部署;新域名上线

## 6. 风险
1. 同步区漂移(中):core/agent.py 是 vococo 最活跃文件,冲突会频繁——规则:同步区改动一律先提交 vococo main,VocoTrade 只拉不改。
2. 现网替换窗口(低):现 P0 部署无计费无多 agent,只是 demo;成熟前保持不动。
3. 命名不可逆(低):vocotrade.com 注册前确认;国内客户涉及 AI 备案(见 llm-beian-gbt45654 记忆)。
