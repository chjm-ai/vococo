# vococo

个人 AI 助理(单用户),常驻进程 + 多入口(Web / Telegram / CLI),跨入口共享会话与记忆。近期扩张为也能从 Web 入口、在选定项目里执行中等编码任务(个人助理 + 编码代理,同一套统一会话,不分模式)。本文件是领域术语表,只定义概念,不含实现细节。

## Language

**Project(项目)**:
用户在自己电脑上选定的一个文件夹,作为 agent 的工作根目录(cwd)。进入该项目下的会话时,agent 在这个文件夹里读写文件、执行命令、加载该目录的配置。一个项目下可以有多个会话。
**身份即路径**:项目由其文件夹的规范化绝对路径唯一标识(同文件夹即同项目,天然去重);无自定义名,显示名取文件夹名;文件夹改名/移动即视为项目丢失。会话 key 中以路径的**短哈希**编码(`web:p<hash>:<conv_id>`),另存一张 `哈希→路径` 小映射表供 UI 显示与排序。
_Avoid_: 工作区、workspace、目录(单说「目录」易与磁盘路径混淆)

**Session(会话)**:
一条对话线程,归属于且仅归属于一个项目(严格层级:项目 → 会话)。承载连续的聊天历史与上下文。当前由字符串 key 标识。
_Avoid_: 对话(conversation)、聊天记录

**Default Project(默认项目)**:
兜底项目,收纳所有未显式归属的会话。其 cwd 即进程默认工作目录。老会话迁移到这里;新用户初始也在这里。
_Avoid_: 未分类

**说明:项目是 Web 端概念。** TG/CLI 及跨入口的 main 线不属于任何项目,永远跑在进程默认目录下,不受 Web 端项目操作影响。

**Risk Tier(危险分级)**:
每次工具调用被 `classify()`(`tools/danger.py`)判成三档 —— `allow`(放行)/ `escalate`(请用户批准)/ `block`(直接拦)。灾难级(删整根、格式化磁盘、覆写裸盘、fork 炸弹)→ `block`;5 类危险操作(写**项目 cwd 外**的文件、`git push`/`git reset --hard`、`rm -rf`、包安装、`curl|sh`)→ `escalate`;其余 `allow`。`escalate`/`block` 分别受 `APPROVAL_GATE`/`DANGER_GUARD` 开关控制。
_Avoid_: 白名单/黑名单(这是三档而非二元)

**Hard Guard(常开正确性防线)**:
跟 Risk Tier 并列、不属于三档模型的另一套机制(`tools/danger.py:_hard_guard`):后台任务腰斩(`run_in_background`)、记忆孤本(在 Claude Code 项目记忆目录新建实体文件)、worktree 越界(worktree 会话写共享主仓库)。三项永远 `deny`,不受 `DANGER_GUARD`/`APPROVAL_GATE` 开关影响——它们修的是「不这么做程序行为就是错的」正确性 bug,不是「操作有多危险」,关掉安全开关不该连带关掉这几条。
_Avoid_: 把这三项当成 Risk Tier 的第四档

**Approval Gate(审批闸)**:
对 `escalate` 的操作,在**有交互通道时**(Web/TG)弹「允许一次 / 拒绝」请用户拍板;**无通道时**(CLI/eval/cron)放行(信任该通道);超时或发送失败视为拒绝。这是「远程编码:动手前对齐」的安全阀。
_Avoid_: 权限系统(它只管 escalate 这一档,不是全量权限模型)

**Tool Card(工具卡片)**:
Web 上把一次工具调用渲染成结构化 UI —— 待办清单(TodoWrite)、红绿 diff(Edit/Write/MultiEdit)、计划卡(ExitPlanMode)、命令预览(Bash/Read)。复刻 Claude Code「看得见过程」的体验。
_Avoid_: 工具日志(它是结构化交互,不是纯文本流水)

**Suggestion(建议)**:
一个待用户一键接受才生效的定时任务提议(consent-first)。
_Avoid_: 自动触发/自主任务

**Voice Call(语音通话)**:
Web 通话视图里像打电话一样与助理对话。免提唯一管线是 **Omni 管线**(浏览器
WebRTC 直连阿里云 Qwen-Omni-Realtime 当"耳朵+嘴巴":识别/断句/打断/朗读;大脑
仍是 Claude)。兜底输入是**按住说话**(录音→服务端转写→回复轮)。自建全双工
管线(P2)已于 2026-07-12 退休,见 ADR 0004。
_Avoid_: 语音模式(易与聊天页的 🎤 语音输入混淆——那只是输入法,不是通话)

**Reply Turn(回复轮)**:
一次「用户话语 → Claude 一轮(带派活工具)→ 按句切分 → 逐句下发」的完整回合,
入口是 `/voice/send`(SSE)。Omni 管线与按住说话共用这一个回复轮。
_Avoid_: 对话轮(会话历史意义上的 turn 与此不同层)

**Voice Task(语音任务)**:
通话中派给后台独立会话执行的长活。派发即返回任务 ID,进度可查,终态主动播报
(在线 SSE 念出来 / 离线推送)。每个任务在独立 git worktree 分支里跑。
_Avoid_: 后台任务(过泛,cron 任务也叫后台任务)
