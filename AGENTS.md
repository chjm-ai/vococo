# AGENTS.md — claude-hermes 开发指南

索引:README(现状/命令)、CONTEXT(术语/危险分级)、REQUIREMENTS(需求)、OPERATIONS(运维坑+排障脚本)。

## 环境
uv sync --extra dev;uv run pytest;入口命令见 README(tui/chat/serve/doctor)。

## 结构
core/ agent+prompt+会话+worktree+danger;gateway/ 内核+adapters;
cron/ 调度;tools/ MCP工具;tui/ 终端UI;config.py;providers.py

## 约定
文档/注释中文;先读懂现状再改,优先简单方案。
系统提示三层:preset+PERSONA+动态记忆,见 core/prompt.py

## 安全
工具分 block/escalate/allow 三档,判定见 tools/danger.py,详情见 CONTEXT.md

## 运维/排障
坑清单、重启姿势、查会话脚本 → 见 OPERATIONS.md
