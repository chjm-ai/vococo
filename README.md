# vococo

基于 **Claude 订阅** 的个人 AI 助理,给自己用。单用户、常驻、语音优先、多渠道共用一个大脑。
架构参考 [Nous Research Hermes Agent](https://github.com/NousResearch/hermes-agent),锁定 Claude、单用户、精简。

> 完整需求见 [REQUIREMENTS.md](REQUIREMENTS.md)。

## 特性

- **多渠道,一个会话**:TUI / CLI / Web(自建 PWA,手机浏览器直达);默认全端共享同一主会话。
- **Web 自建 UI**:多会话侧边栏、工具调用卡片(对齐 Claude Code)、模型面板 + 限额环、设置页全在线改。
- **语音优先**:按住说话(STT)、TTS 朗读、Omni 实时免提通话(可打断)。
- **后台任务引擎**:语音派活 / cron / 独立新会话共用一套引擎,任务跑在独立 git worktree。
- **长期记忆**:启动注入 `~/AI_BRAIN`;`save_memory` 沉淀、`recall_past` 召回,纯 Markdown。
- **多供应商热切换**:DeepSeek / Kimi / 任意 Anthropic 兼容中转在设置页添加即生效(详见 REQUIREMENTS §6)。
- **安全模型 + 自我运维**:危险三档闸、手机审批;`vococo doctor` 自检、`restart_self` 安全重启、看门狗防假死。

需求矩阵与验收标准见 [REQUIREMENTS.md](REQUIREMENTS.md) §4,本节不重复维护。

## 架构

```
vococo/__main__.py     CLI 入口:tui / chat / serve / cron / doctor
core/                  agent 循环(claude-agent-sdk)· client 保温池 · prompt 组装
                       · 每会话 git worktree 隔离 · 后台任务引擎(task_runner)
gateway/               平台内核(命令注册表 / converse / 会话路由)
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
vococo serve      # 常驻:Web + 调度器(heartbeat/主动推送)
vococo cron       # 列出定时任务
vococo doctor     # 自检:配置/认证/DB/AI_BRAIN/进程
```

Web 入口:`serve` 时设 `WEB_ENABLED=1` 即在同进程起网页服务(默认只听 `127.0.0.1:8848`),
配合 Cloudflare Tunnel / Tailscale 暴露到公网即得手机 PWA(走公网必设 `WEB_AUTH_TOKEN`)。

## 快捷命令(TUI/Web 通用)

`/new` 开新会话(旧史保留) · `/clear` 清屏+开新 · `/model [名]` 切模型 · `/history` 看历史 · `/status` 会话信息 · `/suggest` 看/接受自动化建议 · `/help`

## 多供应商切换

想用 **DeepSeek / Kimi** 或任意第三方 Anthropic 兼容中转:在 Web 设置页的「模型」
管理界面直接添加(base_url + key + model),**每轮自动生效,无需重启**;会话里
`/model <模型名>` 可临时覆盖。只用第三方时,`.env` 里的订阅 token 可留空。
认证细节见 [REQUIREMENTS.md](REQUIREMENTS.md) §6。

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
`serve` 进程每 30s 检查到期任务,跑 agent 后推到 Web/系统推送。支持 `cron` / `interval` / `once` 三种调度。
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
| `WEB_ENABLED` / `WEB_AUTH_TOKEN` | 否 | 启用手机浏览器 Web 入口;走公网必设口令 |

其余变量(安全闸、语音、Web Push、多供应商…)在 `.env.example` 里逐条有注释。

## 设计要点

设计原则、非目标红线、与原版 Hermes 的差异对照见 [REQUIREMENTS.md](REQUIREMENTS.md)
§1–§5;术语与安全模型见 [CONTEXT.md](CONTEXT.md) / [SECURITY.md](SECURITY.md)。

> ⚠️ 订阅令牌只配个人自用;商用/对外必须改回 API key 按量计费。

## 许可

[MIT](LICENSE)。这是个人自用助理框架,fork 后请生成属于自己的凭据
(Claude / VAPID),别把 `.env`、`data/` 提交进仓库(默认已 gitignore)。
