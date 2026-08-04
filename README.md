# vococo

基于 **Claude 订阅** 的个人 AI 助理,给自己用。单用户、常驻、语音优先、多渠道共用一个大脑。
架构参考 [Nous Research Hermes Agent](https://github.com/NousResearch/hermes-agent),锁定 Claude、单用户、精简。

> 完整需求见 [REQUIREMENTS.md](REQUIREMENTS.md)。

## 特性

- **多渠道,一个会话**:TUI / CLI / Telegram / Web(自建 PWA,手机浏览器直达)任选;默认全端共享同一主会话,TG 上问一半切网页接着聊。
- **Web 自建 UI**:多会话侧边栏、工具调用卡片(对齐 Claude Code 体验)、模型面板 + 订阅限额环、设置页(模型/供应商/skill/MCP/思考深度全可在线改)。
- **语音优先**:按住说话 → STT 转文字进对话;TTS 朗读回复;Omni 实时免提通话(可打断、带回声过滤)。
- **后台任务引擎**:语音派活 / cron 定时 / 普通会话里「开个独立新会话」三种触发共用一套引擎;每个任务跑在独立 git worktree + 分支,干完推送汇报,绝不碰主目录。
- **项目会话隔离**:Web 端把会话绑定到项目仓库后,每个会话自动开独立 worktree + 分支,并行会话互不抢分支。
- **长期记忆**:启动注入 `~/AI_BRAIN` 画像与记忆索引;对话中 `save_memory` 沉淀、`recall_past` 跨会话召回;记忆就是纯 Markdown 文件,Obsidian 可直接维护。
- **多供应商热切换**:官方订阅为主,DeepSeek / Kimi / 任意 Anthropic 兼容中转在设置页添加即生效,无需重启;`/model` 会话级临时切换。
- **安全模型**:灾难命令直接拦截(删根/格式化/fork 炸弹);写目录外、`git push`、装包等 5 类危险操作在手机弹按钮请你批准;启动时收敛 `.env` secret 出 env,工具输出自动打码。
- **Web Push 系统通知**:页面关了、锁屏了也收得到(回复完成 / 待审批 / 主动推送 / 出错四种场景)。
- **consent-first 主动化**:cron 定时任务 + 自动化建议——它发现你反复做同一件事时只提建议,你点头才开跑,绝不擅自建任务。
- **自我运维**:`vococo doctor` 一键自检;`restart_self` 工具让它改完自身代码后安全重启(遗书+还魂);事件循环假死看门狗自动自杀拉起。

## 架构

```
vococo/__main__.py     CLI 入口:tui / chat / serve / cron / doctor
core/                  agent 循环(claude-agent-sdk)· client 保温池 · prompt 组装
                       · 每会话 git worktree 隔离 · 后台任务引擎(task_runner)
gateway/               平台内核(命令注册表 / converse / 会话路由)
  ├─ adapters/telegram   TG bot(流式 + 白名单)
  ├─ adapters/web        自建 PWA(SSE 流式 / 多会话 / 设置页)
  ├─ adapters/web_push   VAPID Web Push 系统通知
  └─ settings_store.py   设置页存储(供应商/模型/skill/MCP/effort)
cron/                  cron / interval / once 调度 + 自动化建议(consent-first)
memory/                SQLite 会话库(state.db)+ 检索 + 图片/音频附件
tools/                 内置 MCP server(记忆/定时/发消息/自我重启…)
                       + danger.py(灾难拦截 + 审批闸 PreToolUse hook)
tui/                   rich/prompt_toolkit 流式 TUI(工具过程 + Markdown)
voice/                 语音:STT 转写 · TTS 朗读 · Omni 免提实时通话
plugin/                内置 skill 插件(只给 vococo 自己用,如 vococo-web-publish)
deploy/                run.sh 守护循环(崩溃自启)/ stop / restart / launchd
```

## 入口

```bash
vococo            # 默认进 TUI
vococo chat       # 纯文本对话(调试 fallback)
vococo serve      # 常驻:Web + Telegram + 调度器(heartbeat/主动推送)
vococo cron       # 列出定时任务
vococo doctor     # 自检:配置/认证/DB/AI_BRAIN/进程
```

Web 入口:`serve` 时设 `WEB_ENABLED=1` 即在同进程起网页服务(默认只听 `127.0.0.1:8848`),
配合 Cloudflare Tunnel / Tailscale 暴露到公网即得手机 PWA(走公网必设 `WEB_AUTH_TOKEN`)。

## 快捷命令(TUI/Telegram 通用)

`/new` 开新会话(旧史保留) · `/clear` 清屏+开新 · `/model [名]` 切模型 · `/history` 看历史 · `/status` 会话信息 · `/suggest` 看/接受自动化建议 · `/help`

## 多供应商切换

想用 **DeepSeek / Kimi** 或任意第三方 Anthropic 兼容中转:在 Web 设置页的「模型」
管理界面直接添加(base_url + key + model),**每轮自动生效,无需重启**;会话里
`/model <模型名>` 可临时覆盖。只用第三方时,`.env` 里的订阅 token 可留空。
模型名 / key / base_url 全在设置页管理,vococo 不硬编码任何供应商。

旧版 [cc-switch](https://github.com/farion1231/cc-switch) 桌面 App 的配置
(`~/.claude-hermes/config.yaml`,历史遗留路径名)可用一次性脚本导入设置页:
`python -m vococo.gateway.migrate_cc_switch`。

## 常驻(macOS)

> `deploy/*.sh` 目前假设 macOS + zsh。Linux 用户请照 `deploy/run.sh` 的思路自行写
> systemd unit;核心就是把 `uv run vococo serve` 跑成常驻进程。

```bash
bash deploy/run.sh     # 后台启动(登录 shell,推荐)
bash deploy/stop.sh    # 停止
tail -f data/logs/vococo.out.log   # 看日志
```

⚠️ **不要把项目放在 iCloud 同步的 `~/Desktop`/`~/Documents` 下**——常驻进程
`open()` 这些文件会被 File Provider 阻塞。放 `~/Repos` 等纯本地目录。

⚠️ **launchd 直接拉起在某些 Mac 上会卡死**:launchd 会话上下文不完整,
agent 派生的 `claude` 子进程会同步阻塞冻住事件循环。所以用 `deploy/run.sh`
(登录 shell)而非 `deploy/launchd.sh`。开机自启可把 `run.sh` 加为**登录项**
(系统设置 → 通用 → 登录项),它运行在完整 GUI 登录会话里,不受此限。

## 定时主动推送

编辑 `data/cron_jobs.json`(模板见 `deploy/cron_jobs.sample.json`),把任务 `enabled` 设 true,
`serve` 进程每 30s 检查到期任务,跑 agent 后推到 Telegram。支持 `cron` / `interval` / `once` 三种调度。
也可以直接对它说「每天早上 8 点给我发 XX」——它会用 `add_cron_job` 建好等你确认。

## 快速开始

**前置**:Python ≥ 3.11、[uv](https://docs.astral.sh/uv/)、一份 Claude Pro/Max 订阅
(或任意 Anthropic 兼容的第三方端点,见「多供应商切换」)。平台:macOS / Linux
已验证;Windows 未测(常驻脚本走 zsh + launchd,仅 macOS)。

```bash
# 1) 装依赖(uv 推荐;也可 python -m venv + pip install -e .)
uv sync --extra dev

# 2) 配认证(需 Claude Pro/Max 订阅)
claude setup-token            # 生成 sk-ant-oat01-... 令牌
cp .env.example .env          # 把令牌填进 .env 的 CLAUDE_CODE_OAUTH_TOKEN

# 3) 自检 + 开聊
uv run vococo doctor   # 检查配置 / 激活供应商
uv run vococo chat
```

## 配置

全部配置走项目根的 `.env`(从 `.env.example` 复制,已 gitignore)。最常用的几项:

| 变量 | 必填 | 说明 |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | 是* | `claude setup-token` 生成的订阅令牌(*只用第三方供应商时可空) |
| `VOCOCO_USER_NAME` | 否 | 助理如何称呼你,默认「主人」 |
| `VOCOCO_PERSONA_NAME` | 否 | 助理人格代号(UI/提示文案里露出),默认「Wazir」 |
| `AI_BRAIN_DIR` | 否 | 长期记忆目录,默认 `~/AI_BRAIN`;不存在则记忆功能自动跳过 |
| `AGENT_MODEL` | 否 | 默认 `claude-sonnet-5` |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALLOWED_CHAT_IDS` | 否 | 启用 Telegram 入口时填(@BotFather 拿 token) |
| `WEB_ENABLED` / `WEB_AUTH_TOKEN` | 否 | 启用手机浏览器 Web 入口;走公网必设口令 |

其余变量(安全闸、语音、Web Push、多供应商…)在 `.env.example` 里逐条有注释。

## 设计要点

- **只服务 Claude**:底层 `claude-agent-sdk`,认证用订阅令牌(`CLAUDE_CODE_OAUTH_TOKEN`),不花 API 按量费。
- **复用现有 skills**:claude-agent-sdk 底层即 Claude Code,可直接挂载已有 Claude skills 当工具;另有 `plugin/` 放 vococo 专属 skill。
- **不重造记忆**:接已有的 `~/AI_BRAIN`(USER.md 画像 + memory/ 知识),纯 Markdown,跨工具共享。
- **安全分层不是可选项**:bypassPermissions 的便利背后,是灾难拦截 + 审批闸 + secret 收敛 + 输出打码四层网(见 [SECURITY.md](SECURITY.md) 与 [CONTEXT.md](CONTEXT.md))。

> ⚠️ 订阅令牌只配个人自用;商用/对外必须改回 API key 按量计费。

## 许可

[MIT](LICENSE)。这是个人自用助理框架,fork 后请生成属于自己的凭据
(Claude / Telegram / VAPID),别把 `.env`、`data/` 提交进仓库(默认已 gitignore)。
