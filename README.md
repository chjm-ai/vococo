# claude-hermes

基于 **Claude 订阅** 的个人 AI 助理(personal Hermes),给自己用。
架构参考 [Nous Research Hermes Agent](https://github.com/NousResearch/hermes-agent),锁定 Claude、单用户、精简。

> 完整需求见 [REQUIREMENTS.md](REQUIREMENTS.md)。

## 现状

| 里程碑 | 状态 |
|---|---|
| **M0** CLI 内核(订阅 agent loop + 注入 AI_BRAIN 画像) | ✅ |
| **M0+** rich/prompt_toolkit 流式 TUI(思考 + 工具过程 + Markdown) | ✅ |
| **M1** 记忆闭环(SQLite 会话持久化 + 接通 ~110 个 skill) | ✅ |
| **M2** Telegram(@Claude077bot,流式 + 白名单) | ✅ |
| **M3** 主动化(gateway + cron 定时推送 + 心跳) | ✅ |
| M2 飞书入口 | ⬜(gateway 已就绪,只差 FeishuAdapter) |

## 架构(gateway)

```
gateway/core.py    平台无关:命令注册表 + converse(消费事件流)+ 会话
gateway/adapters/  各平台薄层:telegram(收发+流式Sink),将来 feishu
gateway/run.py     GatewayRunner:跑所有 adapter + 调度器,主动推送经 adapter
cron/scheduler.py  心跳 + cron/interval/once 定时任务 → 推送
```

## 入口

```bash
claude-hermes            # 默认进 TUI
claude-hermes chat       # 纯文本对话(调试 fallback)
claude-hermes serve      # 常驻:Telegram 收发 + 调度器(heartbeat/主动推送)
claude-hermes cron       # 列出定时任务
```

## 快捷命令(TUI/Telegram 通用)

`/new` 开新会话(旧史保留) · `/clear` 清屏+开新 · `/model [名]` 切模型 · `/history` 看历史 · `/status` 会话信息 · `/help`

## 常驻(macOS launchd)

```bash
bash deploy/launchd.sh install     # 开机自启 + 崩溃自愈
bash deploy/launchd.sh status      # 看状态
bash deploy/launchd.sh logs        # 看日志
bash deploy/launchd.sh uninstall   # 卸载
```

## 定时主动推送

编辑 `data/cron_jobs.json`(模板见 `deploy/cron_jobs.sample.json`),把任务 `enabled` 设 true,
`serve` 进程每 30s 检查到期任务,跑 agent 后推到 Telegram。支持 `cron` / `interval` / `once` 三种调度。

## 快速开始(M0)

```bash
# 1) 装依赖
python3 -m venv .venv && . .venv/bin/activate
pip install -e .

# 2) 配认证(需 Claude Pro/Max 订阅)
claude setup-token            # 生成 sk-ant-oat01-... 令牌
cp .env.example .env          # 把令牌填进 .env 的 CLAUDE_CODE_OAUTH_TOKEN

# 3) 开聊
python -m claude_hermes chat
```

## 设计要点

- **只服务 Claude**:底层 `claude-agent-sdk`,认证用订阅令牌(`CLAUDE_CODE_OAUTH_TOKEN`),不花 API 按量费。
- **复用现有 skills**:claude-agent-sdk 底层即 Claude Code,可直接挂载已有 Claude skills 当工具(M1)。
- **不重造记忆**:接已有的 `~/AI_BRAIN`(USER.md 画像 + memory/ 知识)。

> ⚠️ 订阅令牌只配个人自用;商用/对外必须改回 API key 按量计费。
