# OPERATIONS.md — 运维坑 & 排障速查

> 从 AGENTS.md 拆出来的详细版:AGENTS.md 只放"做什么/别做什么"的一句话索引,
> 这里放完整背景、踩坑细节、命令示例。改代码前先看 AGENTS.md,遇到具体问题再来这里查。

## 安全模型细节

工具调用分三档(概念定义见 CONTEXT.md「危险分级 / 审批闸」):

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
