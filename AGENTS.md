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

## 按需加载资料
默认只根据当前对话和本文件行动,不要为「可能有用」预读整个资料库或遍历项目。先判断
问题属于哪类,再只读对应的最小资料链:

| 用户问题 | 读取链路 |
|---|---|
| 某人是谁、与主人的关系、近况、互动 | 先读 `~/AI_BRAIN/memory/people-network.md` 按名字和「别名」找标准名;命中后只读 `~/AI_BRAIN/memory/people/<标准名>.md`。不得只搜索当前项目代码后就断言没有此人。 |
| 长期偏好、历史决策、过去聊过的事 | 先看已注入的 `MEMORY.md` 索引;需要细节时用 `recall_past` 或只读索引指向的单个文件。 |
| 项目现状、运行命令、架构 | 先读 `README.md` 或本文件指向的单个文档;运维/排障才读 `OPERATIONS.md`,术语/安全判定才读 `CONTEXT.md`。 |

人物资料以画像库为准,不要把称谓或同音别名当作另一个人。回答只引用本轮实际读到的资料,
不要补造细节。

### AI_BRAIN 读不到时:是 TCC 权限,不是 iCloud 抖动(别重试)
`~/AI_BRAIN` 软链到 iCloud。读它报 `Operation not permitted` 或 `Interrupted system
call`(EINTR)时,**两种报错是同一个病的两副面孔:macOS TCC 权限。重试无效,重试 100 次
也一样**——iCloud Drive 目录只能靠「完全磁盘访问」放行,不弹窗直接拒(EPERM);其余受保护
目录(Desktop 等)系统想弹授权框,但 vococo 是 launchd 后台进程、没有 GUI 会话应答,超时
被打断(EINTR)。不得据此推断画像不存在,要如实说「AI_BRAIN 读不到,是 TCC 权限问题」。

修法:系统设置 → 隐私与安全性 → 完全磁盘访问权限,加 `/bin/zsh`(plist 的
ProgramArguments[0],TCC 认的 responsible process),然后**必须整栈重启**才生效。
⚠️ `restart_self` 修不好这个:它只换 python 那一层,而 launchd 直接拉起的
zsh(run.sh:127 的 while 循环)会一直活着并带着授权前的旧 TCC 上下文,子进程全部继承。
整栈重启只能靠 `launchctl kickstart -k gui/$(id -u)/com.vococo`。
它**不会**被 danger.py 拦(_PROCESS_CONTROL_COMMANDS 只认 kill/pkill/killall),会话内
能跑通,但代价是:遗书/还魂只在 restart_self 路径上写,kickstart 会把当前会话拦腰斩断且
回不来。所以会话内要跑,先跟主人讲清楚这一点,让他自己决定在终端跑还是授权你跑。

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
