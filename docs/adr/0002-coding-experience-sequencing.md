# ADR 0002:复刻 Claude Code 编码体验的落地定序

- 状态:已接受
- 日期:2026-07-02
- 相关:[改造计划](../design/cc-replication-plan.md)

## 背景

要让 Hermes 从 Web 入口执行本机 repo 的中等编码任务,体验对齐 Claude Code。
读代码后确认:底座就是 claude-agent-sdk,编码能力一轮内本已具备;差距来自
harness 没「显示」结构化过程、也没「设闸」。可动的地方有三块,存在依赖与冲突关系。

## 决策

按此顺序落地,并**暂缓**其一:

1. **keystone:事件流补出工具入参**(先做)——没有它,diff/todo/审批全无数据。
2. **Web UI 结构化渲染**(diff / todo / 计划卡)。
3. **审批闸**(危险操作升级 + 修复没接进 SDK 的 danger hook)。
4. **暂缓「换原生长会话 session」(原方案 Phase 3)**。

## 理由

- keystone 是 1、3 的共同前置(单点解锁大半工作),必须最先。
- 「原生 session」和另一个在建的**工作目录功能**改的是同一块 session 模型,
  **现在动必产生冲突**;且其收益只在**多轮**编码对话体现,单个中等任务(一轮内跑完)现状已够。
- 因此用「每轮文本重建历史 + 每轮新 client」的现状先扛着,换取不与工作目录撞车。

## 代价 / 已知取舍

- 暂不换 session → **跨轮**编码对话仍丢连续性、每轮全量重发不走缓存(更慢、更耗周限额)。
  这是刻意接受的:等工作目录落地、session 模型稳定后再评估 Phase 3。
- 审批闸阻塞走 PreToolUse hook,理论与已跑通的 `ask_user` 同机制,但 hook 路径需真机验证。

## 反悔成本

中等。keystone / 渲染 / 审批都是增量,可独立回退(开关:`APPROVAL_GATE` / `DANGER_GUARD`)。
Phase 3 一旦启动会改 session 核心,届时另立 ADR。
