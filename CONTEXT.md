# CONTEXT · 术语表

> 只放**领域词汇**的规范定义,不放实现细节。实现见 `docs/`。

## 定位

- **个人助理 + 编码代理**:Hermes 原是「个人助理」;在此扩张为也能从 Web 入口、在本机 repo 里
  执行中等编码任务。二者是**同一套统一会话**,不分「编码模式 / 助理模式」(Wesley 已定)。

## 会话与目录

- **会话(Session)**:一条对话线,跨入口(CLI/TUI/TG/Web)可连续。持久化在 SQLite。
- **工作目录(cwd)**:一个会话绑定的 repo 根,是编码操作的**边界**。由「工作目录功能」提供
  (另一会话在建);`danger.set_cwd()` 是它与审批闸的接点。

## 安全

- **危险分级**:每次工具调用判成三档 —— `allow`(放行)/ `escalate`(请用户批准)/ `block`(直接拦)。
- **灾难级(catastrophic)**:删整根/家目录、格式化磁盘、覆写裸盘、fork 炸弹等 → `block`。
- **危险操作(5 类)**:写工作目录外的文件、`git push`/`git reset --hard`、`rm -rf`、包安装、
  `curl|sh` → `escalate`。
- **审批闸(Approval Gate)**:对 `escalate` 操作,在**有交互通道时**弹「允许一次 / 拒绝」请用户拍板;
  **无通道**(CLI/eval/cron)则放行(信任该通道);超时/失败视为拒绝。

## 交互

- **工具卡片(Tool Card)**:Web 上把一次工具调用渲染成结构化 UI —— 待办清单(TodoWrite)、
  红绿 diff(Edit/Write/MultiEdit)、计划卡(ExitPlanMode)、命令预览(Bash/Read)。
- **建议(Suggestion)**:一个待用户一键接受才生效的定时任务提议(consent-first,已有机制)。
