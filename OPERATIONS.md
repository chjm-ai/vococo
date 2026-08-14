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

判定逻辑在 [tools/danger.py](vococo/tools/danger.py),经 PreToolUse hook 生效。

## 运维坑(踩过的,别重犯)

- **worktree 会话**:Web 端每个项目会话跑在独立 git worktree + 分支,根治互相抢分支。改动提交后合回 main 生效。
- **合回 main 用 `zsh deploy/merge-main.sh`**(只合并,不重启)。worktree 里 `git checkout main` **永远**报 "main 已经被工作区使用"(exit 128,main 被主仓库工作区占用)——已有 7 个会话踩过,别再试;脚本自带预检(未提交/主仓库脏/不在 main)和冲突自动 abort。
- **重启 serve 只有两条正路**:vococo 会话内(改自身代码)用 `restart_self` 工具;外部会话/终端跑 `zsh deploy/restart.sh`。**禁止手搓 pgrep/kill 流程**——sandbox 里 pgrep 静默为空,曾诱发模型虚构"重启成功"(2026-07-06);汇报重启结果必须引用脚本/工具输出里真实出现的 PID。
  - **合并后要重启验证,走两步法**:先 `merge-main.sh`(不带任何参数)完成合并,再调 `restart_self` 工具。后者有"遗书+还魂"自动续聊验证闭环,重启完自动回到当前会话继续验证;`restart.sh` 是硬杀重启,没有还魂,会打断正在生成的回复,**AI 会话内禁用**。
  - `merge-main.sh --restart` **已于 2026-08 移除**(理由同上,它内部调的就是硬杀的 `restart.sh`)。现在传这个参数脚本会直接报错退出并提示两步法,见 [deploy/merge-main.sh](deploy/merge-main.sh) 开头。旧会话若照冻结的旧指引去传 `--restart`,请按报错改用两步法,**别把这个报错当 bug 去私改脚本**(2026-08-04 踩过)。
- **别误杀原版 hermes**:本项目配置在 `~/.vococo/`,**刻意独立于**原版 Hermes 的 `~/.hermes`,两者互不干扰。重启只动 vococo 自己的进程。
- **记忆唯一主库**:记忆实体文件只存在 AI_BRAIN 主库,`.claude` 项目侧全是软链;**禁止**在 Claude Code 项目记忆目录新建实体文件(会成孤本)。
- **editable 安装 + worktree**:`pip install -e` 会让 worktree 里的改动被主仓库的已安装包屏蔽,调试时用 `uv run` 从当前目录跑。
- **假死看门狗**([gateway/watchdog.py](vococo/gateway/watchdog.py)):run.sh 只兜"进程退出",兜不住"活着但事件循环卡死"(2026-07-21 假死 70 分钟,全靠系统超时运气恢复)。现在循环无响应 30s → 全线程堆栈写入 `data/logs/watchdog.log`(排查卡点看这里),180s → 自杀退出码 70 交 run.sh 拉起。阈值可用环境变量 `WATCHDOG_DUMP_SEC`/`WATCHDOG_EXIT_SEC` 调。

## 省上下文:每轮固定注入的三笔开销

system prompt 的 append 块 + skill 描述是每轮都进的固定成本,长会话里被 prompt cache 兜着不显眼,
但一直占着上下文窗口。2026-08-13 清过一轮,别再把这三笔加回来:

- **别重复注入 SDK 已经注了的东西**。`core/prompt.py` 现在会检测两处:①项目 `CLAUDE.md` 是
  `AGENTS.md` 的软链(本仓即如此)→ 跳过 `<project_guide>`,SDK 读 CLAUDE.md 时已注过;
  ② `~/.claude/projects/<slug>/memory/MEMORY.md` 软链到 AI_BRAIN 主库 → 跳过 `<memory_index>`
  全文,只留一句指针。两处合计曾每轮白烧约 6.5k token。判定都以 `realpath` 相同为准,
  不是同一份文件照旧注入,不会把索引弄丢。
- **skill 白名单可以按项目收敛**:`data/web_settings.json` 的 `skills_by_project`
  (`项目绝对路径 → skill 名列表`)优先级最高，按路径祖先匹配，主仓库配一条所有 worktree 自动继承。
  新配置可用 `skill_profiles` (`profile → skill 名列表`) + `project_profiles` (`项目路径 → profile`)
  表达复用 profile；只有用户明确选中的项目目录才会匹配 profile，普通聊天即使运行时回退到 vococo
  cwd 也仍走全局 `skills_enabled`。每个 skill 的 name+description 都逐字进 system prompt，
  目前没有设置页 UI，直接改 JSON；改完下一轮生效(设置每轮重读)。
- **外部 MCP 按任务加载**:外部 server 的 `enabled` 只是总开关；日常不挂。明确查询/操作
  Lemlist、DataForSEO 或 GA4 时才为该轮加载相应 server，短时「继续」可续接；`set_external_mcp`
  的手动开关只作用当前会话。禁止恢复旧的“关键词命中后持久化开启全部 MCP”逻辑。
- **`_INJECT_MAX_CHARS`(prompt.py)是截断保险丝,不是目标值**:MEMORY.md 长过它就静默截尾,
  最新沉淀的记忆刚存就从索引里消失。上调前先看看是不是又有重复注入可以砍。

## 排障:查会话

会话数据在 `data/state.db`(SQLite,`turns`/`session_meta`/`projects` 三张表,见 [session_store.py](vococo/memory/session_store.py))。别再手写 SQL 或翻 `~/.claude/projects/`——用 [scripts/inspect_sessions.py](scripts/inspect_sessions.py):

```bash
python3 scripts/inspect_sessions.py list                        # 列出所有会话(key/标题/轮数/最后活跃/模型/是否有worktree)
python3 scripts/inspect_sessions.py list --platform web --project <关键词>
python3 scripts/inspect_sessions.py show <session_key> -n 20     # 看某会话最近 N 轮(--all-history 含 /new 前的历史,--events 带工具调用时间线)
python3 scripts/inspect_sessions.py search "关键词1 关键词2"       # 跨会话关键词检索
python3 scripts/inspect_sessions.py projects                     # 项目 hash → 路径
python3 scripts/inspect_sessions.py sdk <session_key>            # 解析该会话对应的 claude CLI transcript(~/.claude/projects/.../*.jsonl)路径
```

⚠️ 在某个项目会话的 worktree 里跑本脚本,默认路径解出的是**这个 worktree 自己的空库**,不是真实数据——真实库在跑 `serve` 的那个主仓库下,用 `--db ~/Repos/vococo/data/state.db` 显式指定(或你实际部署的路径)。
