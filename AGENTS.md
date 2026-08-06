# AGENTS.md — vococo 开发指南

索引:README(现状/命令)、CONTEXT(术语/危险分级)、REQUIREMENTS(需求)、OPERATIONS(运维坑+排障脚本)。

## 环境 / 命令
uv sync --extra dev 装依赖;uv run pytest 跑测试。
入口:vococo tui|chat|serve|doctor(TUI/纯文本/常驻/自检)。
Python ≥ 3.11,包管理 uv,依赖锁 uv.lock。

## 代码结构
core/ agent循环+prompt+会话存储+worktree+危险分级
gateway/ core.py平台内核 + adapters(web) + run.py
cron/ 定时调度;tools/ 内置MCP工具;tui/ 终端界面
config.py 路径/常量;providers.py 多供应商切换

## 约定
文档/注释一律中文;改代码前先读懂现状,保持原风格,优先简单方案。
系统提示三层堆叠:claude_code preset + PERSONA + 动态记忆,见 core/prompt.py。

## 安全模型(动手前必读)
工具调用分三档,判定在 tools/danger.py(经 PreToolUse hook 生效):

| 档位 | 触发 | 行为 |
|---|---|---|
| block | 删根/格式化/fork炸弹 | 直接拦 |
| escalate | 写cwd外文件、git push/reset硬重置、装包、危险管道命令 | 有交互通道时请批准 |
| allow | 其余 | 放行 |

细节见 CONTEXT.md「危险分级/审批闸」。

## 运维 / 排障
合并 worktree 改动回 main:`zsh deploy/merge-main.sh`(只合并不重启);worktree 里禁止切回 main 分支(必报错)。
合并后要重启验证:**先 merge-main.sh,再调 restart_self 工具**——它带"遗书+还魂",重启完自动回当前会话继续验证。
deploy/restart.sh 是硬杀重启(无还魂,会打断正在生成的回复),仅供终端/外部场景,AI 会话内禁用。
禁手搓进程查杀。
完整坑清单 + 查会话脚本用法 → 见 OPERATIONS.md。
