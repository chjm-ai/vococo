# AGENTS.md — claude-hermes 开发指南

> 本文件是「在本仓库里怎么干活」的操作手册。
> 背景性文档各有分工,别在这里重复:
> - [README.md](README.md) — 项目现状 / 里程碑 / 入口命令
> - [CONTEXT.md](CONTEXT.md) — 领域术语表(项目 / 会话 / 危险分级 / 审批闸…)
> - [REQUIREMENTS.md](REQUIREMENTS.md) — 完整需求

## 这是什么

基于 **Claude 订阅**的个人 AI 助理(单用户),常驻进程 + 多入口(Web / Telegram / CLI),跨入口共享会话与记忆。也能从 Web 入口在选定项目里执行中等编码任务。基于 `claude-agent-sdk`。

## 环境 / 命令

```bash
uv sync --extra dev          # 装依赖(含 pytest)
uv run pytest                # 跑全部测试
uv run pytest tests/test_danger.py -q   # 单文件

uv run claude-hermes         # TUI
uv run claude-hermes chat    # 纯文本对话(调试 fallback)
uv run claude-hermes serve   # 常驻:Web + Telegram + 调度器
uv run claude-hermes doctor  # 自检:配置 / 激活供应商
```

- Python **≥ 3.11**,包管理用 **uv**,依赖锁在 `uv.lock`。
- 测试框架 pytest,测试放 `tests/`,命名 `test_*.py`。

## 代码结构

```
claude_hermes/
  core/        agent loop(agent.py)、system prompt(prompt.py)、
               会话存储、worktree、危险分级(danger)
  gateway/     core.py 平台无关内核(命令注册 + converse 事件流)
               adapters/  各入口薄层:web / telegram(将来 feishu)
               run.py     GatewayRunner:跑所有 adapter + 调度器
  cron/        scheduler.py 心跳 + cron/interval/once 定时任务
  tools/       内置 MCP 工具(记忆 / 定时 / 发消息…)
  tui/         rich/prompt_toolkit 流式终端界面
  config.py    路径 / 常量 / AI_BRAIN 目录
  providers.py cc-switch 多供应商(DeepSeek/Kimi)每轮注入
```

## 约定

- **注释和文档一律中文**,风格贴合现有代码(信息密度高、点到即止)。
- 改代码前先读懂现有实现,保持原有风格;优先简单方案,函数小而清晰。
- 系统提示是三层堆叠:`claude_code` preset + Hermes 人设(PERSONA)+ 动态记忆(USER.md / MEMORY.md),见 [prompt.py](claude_hermes/core/prompt.py)。

## 安全模型(动手前必读)

工具调用分三档(细节见 CONTEXT.md「危险分级 / 审批闸」):

| 档位 | 触发 | 行为 |
|------|------|------|
| `block` | 删整根 / 格式化磁盘 / fork 炸弹 | 直接拦 |
| `escalate` | 写项目 cwd 外的文件、`git push`、`git reset --hard`、`rm -rf`、装包、`curl\|sh` | 有交互通道时请用户批准 |
| `allow` | 其余 | 放行 |

判定逻辑在 [tools/danger.py](claude_hermes/tools/danger.py),经 PreToolUse hook 生效。

## 运维坑(踩过的,别重犯)

- **worktree 会话**:Web 端每个项目会话跑在独立 git worktree + 分支,根治互相抢分支。改动提交后合回 main 生效。
- **合回 main 用 `zsh deploy/merge-main.sh`**(可加 `--restart` 顺带重启)。worktree 里 `git checkout main` **永远**报 "main 已经被工作区使用"(exit 128,main 被主仓库工作区占用)——已有 7 个会话踩过,别再试;脚本自带预检(未提交/主仓库脏/不在 main)和冲突自动 abort。
- **重启 serve 只有两条正路**:Wazir 会话内(改自身代码)用 `restart_self` 工具;外部会话/终端跑 `zsh deploy/restart.sh`。**禁止手搓 pgrep/kill 流程**——sandbox 里 pgrep 静默为空,曾诱发模型虚构"重启成功"(2026-07-06);汇报重启结果必须引用脚本/工具输出里真实出现的 PID。
- **别误杀原版 hermes**:本项目配置在 `~/.claude-hermes/`,**刻意独立于**原版 Hermes 的 `~/.hermes`,两者互不干扰。重启只动 claude-hermes 自己的进程。
- **记忆唯一主库**:记忆实体文件只存在 AI_BRAIN 主库,`.claude` 项目侧全是软链;**禁止**在 Claude Code 项目记忆目录新建实体文件(会成孤本)。
- **editable 安装 + worktree**:`pip install -e` 会让 worktree 里的改动被主仓库的已安装包屏蔽,调试时用 `uv run` 从当前目录跑。

## 排障:查会话

会话数据在 `data/state.db`(SQLite,`turns`/`session_meta`/`projects` 三张表,见 [session_store.py](claude_hermes/memory/session_store.py))。别再手写 SQL 或翻 `~/.claude/projects/`——用 [scripts/inspect_sessions.py](scripts/inspect_sessions.py):

```bash
python3 scripts/inspect_sessions.py list                        # 列出所有会话(key/标题/轮数/最后活跃/模型/是否有worktree)
python3 scripts/inspect_sessions.py list --platform web --project <关键词>
python3 scripts/inspect_sessions.py show <session_key> -n 20     # 看某会话最近 N 轮(--all-history 含 /new 前的历史,--events 带工具调用时间线)
python3 scripts/inspect_sessions.py search "关键词1 关键词2"       # 跨会话关键词检索
python3 scripts/inspect_sessions.py projects                     # 项目 hash → 路径
python3 scripts/inspect_sessions.py sdk <session_key>            # 解析该会话对应的 claude CLI transcript(~/.claude/projects/.../*.jsonl)路径
```

⚠️ 在某个项目会话的 worktree 里跑本脚本,默认路径解出的是**这个 worktree 自己的空库**,不是真实数据——真实库在跑 `serve` 的那个主仓库下,用 `--db ~/Repos/claude-hermes/data/state.db` 显式指定(或你实际部署的路径)。
