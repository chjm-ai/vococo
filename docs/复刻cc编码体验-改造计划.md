# 复刻 Claude Code 编码体验 · 改造计划与落地记录

> 目标(Wesley 定):让 Hermes 能从 **Web 入口**、在**本机 repo** 里执行**中等编码任务**,
> 体验对齐 Claude Code。定位从「个人助理」扩张为「个人助理 + 编码代理」。

## 关键判断(读代码后)

Hermes 底座 = `claude-agent-sdk` = Claude Code 内核。**编码能力一轮内本就具备**
(plan / TodoWrite / Subagent / 现场探索都是 SDK 自带)。用起来差,来自两个 harness 选择:

1. **事件流丢掉了工具入参**([core/agent.py](../claude_hermes/core/agent.py) 原来只抓 name/id)→ 渲染不出 diff/todo。
2. **全 bypassPermissions**,且 `pretool_danger_hook` **定义了却没接进 SDK**(死代码,守卫实际没生效)。

所以复刻 = 主要是「补入参 + 渲染 + 设审批闸」,不是造新能力。

## 已落地(本次)

| Phase | 做了什么 | 文件 |
|---|---|---|
| **0 keystone** | 事件流拼装并发出工具入参 `ToolInput`(累积 `input_json_delta` → `content_block_stop` 解析) | [core/agent.py](../claude_hermes/core/agent.py)、[gateway/core.py](../claude_hermes/gateway/core.py)、[adapters/web.py](../claude_hermes/gateway/adapters/web.py) |
| **1 结构化渲染** | Web UI:`TodoWrite`→勾选清单、`Edit/Write/MultiEdit`→红绿 diff、`ExitPlanMode`→计划卡、`Bash/Read`→折叠预览 | [web_static/index.html](../claude_hermes/gateway/adapters/web_static/index.html) |
| **2 审批闸** | `danger.py` 升级成 `classify()`(allow/escalate/block);**修复 hook 没接进 SDK 的 bug**;5 类危险操作在有交互通道时弹「允许一次/拒绝」(复用 clarify,拿锁前 resolve 防死锁);无通道则放行 | [tools/danger.py](../claude_hermes/tools/danger.py)、[core/agent.py](../claude_hermes/core/agent.py)、[config.py](../claude_hermes/config.py) |
| **评测台** | 同一编码任务 Hermes vs Claude Code 对比:git 隔离起点 + 采集验收/耗时/改动量 | [eval/](../eval/) |

**测试**:99 passed(新增 30:分类器 5 类、入参拼装、审批 round-trip)。

## 审批闸的 5 类危险操作(Wesley 确认)

| 操作 | 处置 |
|---|---|
| 写**工作目录外**的文件 | escalate(需 cwd,见下方「待接」) |
| `git push` / `git reset --hard` | escalate |
| `rm -rf`(任何目标;删整根仍是 block) | escalate |
| 包安装 `pip/npm/pnpm/yarn/brew/uv…` | escalate |
| `curl\|sh` 下载执行 | escalate |

安全默认:审批**超时或发送失败 → 视为拒绝**;无交互通道 → 放行(信任 CLI/eval/cron)。

## 待接 / 暂缓

- **工作目录 cwd**(另一个会话在开发):`danger.set_cwd(path)` 已留好钩子,工作目录功能开轮时调用它,
  「写 cwd 外文件」这条规则即生效(现在 cwd=None,该规则休眠,其余 4 条 Bash 规则已生效)。
- **Phase 3 原生 session(跨轮连续性 + 省缓存):暂缓**。与「工作目录」改同一块 session 模型,现在动必冲突;
  且收益只在多轮编码对话体现。等工作目录落地、session 稳定后再评估。见 [ADR 0001](adr/0001-coding-experience-sequencing.md)。

## 需要真机验证的点(订阅令牌 + 真流式)

1. 审批闸阻塞在 PreToolUse hook 里能否正常唤醒(理论同 `ask_user`,已跑通;但 hook 路径未真机验)。
2. `HookMatcher` 是否有默认超时会掐断长审批(已确认 SDK 支持 `hooks`,`timeout` 默认 None)。
3. 先跑一版**改造前 baseline**(bypass Hermes vs CC)当锚点,再跑改造后,证明差距缩小。
