# AGENTS.md — vococo 开发指南

索引:README(现状/命令)、CONTEXT(术语/危险分级)、REQUIREMENTS(需求)、OPERATIONS(运维坑+排障脚本)、DESIGN(前端设计令牌规范)。

改前端样式前必读 DESIGN.md:颜色/字号/间距/圆角/过渡全部走 CSS 变量,禁止手写数值。

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
改完代码直接提交并合并回 main(`zsh deploy/merge-main.sh`);若改动涉及后端(core/gateway/cron/tools 等非纯前端代码),提交合并后要询问主人是否顺便重启(`restart_self`)。

**大改动合并后顺手推 GitHub**(`git push origin main`,远端 `chjm-ai/vococo`):
- 算「大改动」:新增文件/模块、新接口或数据结构、跨多个文件的重构、依赖变更、
  影响别人怎么用的行为改动。凡是值得单独写一条 feat/refactor 的,基本都算。
- 不算:改错别字、调文案、单行修 bug、纯注释——这类攒着,下次大改动一起带上去。
- push 属 escalate 档,有交互通道时会弹审批,批了才走;后台任务(无交互通道)会被拦,
  这时如实说「已合并 main、未 push」,别当成推成功了。
- 推之前先 `git log origin/main..main --oneline` 看一眼要推什么,推完把结果一句话带过。

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

### AI_BRAIN 读不到时:先做对照实验,别急着加权限、别重启 vococo
`~/AI_BRAIN` 软链到 iCloud。读它报 `Interrupted system call`(EINTR)或
`Operation not permitted` 时,**重试无效,重试 100 次也一样**;也不得据此推断画像不存在,
要如实说「AI_BRAIN 读不到」。

**第一步永远是这条对照命令**(2026-08-18 实测定案,别跳过):
```
ls ~/Downloads     # 受 TCC 保护,但不走 iCloud —— 一步分开两种病因
```
| Downloads | AI_BRAIN | 结论 |
|---|---|---|
| ✅ 可读 | ❌ 失败 | 真的是 iCloud/文件提供者问题 |
| ❌ 也失败 | ❌ 失败 | **TCC 子系统卡死,与 iCloud 无关 → 唯一解是重启 macOS** |

TCC 卡死的判据:`ps -o lstart -p $(pgrep tccd)` 显示 tccd 自开机起就没换过,且
`~/Movies` 这类目录直接挂起数分钟不返回(系统在等一个永远弹不出来的授权框)。
`tccd` 受 SIP 保护,`killall tccd` 杀不动,所以**除了重启系统没有别的办法**。
此时加「完全磁盘访问」、重启 vococo 全是白费——2026-08-18 就是这么白折腾了三轮。

⚠️ 反面教材,别重蹈:当时拿 `~/Desktop` 当对照组,但它开了「桌面与文稿」iCloud 同步、
本身就是 iCloud 托管,于是推出「不是 iCloud 特有问题」的错误结论,又绕去查
fileproviderd。`~/Downloads` 才是干净对照组。判断前先确认对照组真的干净。

顺带备查(与上面无关,是另一件事):整栈重启 vococo 只能靠
`launchctl kickstart -k gui/$(id -u)/com.vococo`,`restart_self` 只换 python 那一层,
launchd 拉起的 zsh(run.sh:127 的 while 循环)会一直活着。kickstart 不会被 danger.py 拦
(_PROCESS_CONTROL_COMMANDS 只认 kill/pkill/killall),但它不写遗书,会把当前会话拦腰
斩断且回不来——会话内要跑,先跟主人讲清楚。

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
