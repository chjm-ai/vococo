# claude-hermes

基于 **Claude 订阅** 的个人 AI 助理(personal Hermes),给自己用。
架构参考 [Nous Research Hermes Agent](https://github.com/NousResearch/hermes-agent),锁定 Claude、单用户、精简。

> 完整需求见 [REQUIREMENTS.md](REQUIREMENTS.md)。

## 现状

| 里程碑 | 状态 |
|---|---|
| **M0** CLI 内核(订阅 agent loop + 命令行对话 + 注入 AI_BRAIN 画像) | ✅ |
| **M0+** rich/prompt_toolkit TUI(slash 补全 + spinner + Markdown 渲染) | ✅ |
| M1 记忆闭环(SQLite 会话 + 调 skill + AI_BRAIN 读写) | ⬜ |
| **M2** 多入口 · Telegram | 🚧 代码完成,待 bot token 实测 |
| M2 多入口 · 飞书 | ⬜ |
| M3 主动化(cron + 定时投递) | ⬜ |

## 入口

```bash
claude-hermes            # 默认进 TUI(推荐)
claude-hermes chat       # 纯文本对话(调试 fallback)
claude-hermes telegram   # 启动 Telegram bot(需先配 TELEGRAM_BOT_TOKEN)
```

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
