<p align="center">
  <img src="docs/images/logo.png" width="120" alt="vococo logo">
</p>

<h1 align="center">vococo</h1>

<p align="center">
  基于 <b>Claude 订阅</b> 的个人 AI 助理，单用户、常驻、语音优先、多渠道共用一个大脑。
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-≥3.11-blue" alt="Python">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey" alt="Platform">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
</p>

---

## 截图

| 聊天 · 工具调用 | 工作台 |
|:---:|:---:|
| ![chat](docs/images/chat.jpg) | ![workbench](docs/images/workbench.jpg) |
| **语音对话** | **定时任务** |
| ![voice](docs/images/voice.jpg) | ![cron](docs/images/cron.jpg) |

---

> 完整需求与验收标准见 [REQUIREMENTS.md](REQUIREMENTS.md)。

## 特性

- **多渠道，一个会话** — TUI / CLI / Web（自建 PWA，手机浏览器直达）；全端共享同一主会话
- **Web 自建 UI** — 多会话侧边栏、工具调用卡片、模型面板 + 限额环、设置页全在线改
- **语音优先** — 按住说话（STT）、TTS 朗读、Omni 实时免提通话（可打断）
- **后台任务引擎** — 语音派活 / cron / 独立新会话共用一套引擎，代码任务跑在独立 git worktree
- **原生深度研究** — 规划、搜集、复核、报告四阶段；只读网页工具、来源证据账本、有限预算与阶段恢复，报告在任务会话中查看
- **长期记忆** — 启动注入 `~/AI_BRAIN`；`save_memory` 沉淀、`recall_past` 召回，纯 Markdown
- **多供应商热切换** — DeepSeek / Kimi / 任意 Anthropic 兼容中转，设置页添加即生效
- **运行数据面板** — 设置页「数据」:工作强度日历、各模型用量与花费、缓存命中/工具成功率，逐个会话可下钻
- **安全模型 + 自我运维** — 危险三档闸、手机审批；`vococo doctor` 自检、`restart_self` 安全重启、看门狗防假死

## 架构

```
vococo/__main__.py     CLI 入口:tui / chat / serve / cron / doctor
core/                  agent 循环(claude-agent-sdk)· client 保温池 · prompt 组装
                       · 每会话 git worktree 隔离 · 后台任务引擎(task_runner)
gateway/               平台内核(命令注册表 / converse / 会话路由)
  ├─ adapters/web        自建 PWA(SSE 流式 / 多会话 / 设置页)
  ├─ adapters/web_push   VAPID Web Push 系统通知
  ├─ settings_store.py   设置页存储(供应商/模型/skill/MCP/effort)
  └─ stats.py            运行数据面板:日志增量 ETL(stats.db)+ 用量/花费/会话聚合
cron/                  cron / interval / once 调度 + 自动化建议(consent-first)
memory/                SQLite 会话库(state.db)+ 检索 + 图片/音频/视频附件
tools/                 内置 MCP server(记忆/定时/发消息/自我重启…)
                       + danger.py(灾难拦截 + 审批闸 PreToolUse hook)
tui/                   rich/prompt_toolkit 流式 TUI(工具过程 + Markdown)
voice/                 语音:STT 转写 · TTS 朗读 · Omni 免提实时通话
plugin/                内置 skill 插件(只给 vococo 自己用,如 vococo-web-publish)
deploy/                run.sh 守护循环(崩溃自启)/ stop / restart / launchd
```

## 入口

```bash
vococo            # 默认进 TUI
vococo chat       # 纯文本对话(调试 fallback)
vococo serve      # 常驻:Web + 调度器(heartbeat/主动推送)
vococo cron       # 列出定时任务
vococo doctor     # 自检:配置/认证/DB/AI_BRAIN/进程
```

Web 入口:`serve` 时设 `WEB_ENABLED=1` 即在同进程起网页服务(默认只听 `127.0.0.1:8848`),
配合 Cloudflare Tunnel / Tailscale 暴露到公网即得手机 PWA(走公网必设 `WEB_AUTH_TOKEN`)。

## 快捷命令(TUI/Web 通用)

`/new` 开新会话(旧史保留) · `/clear` 清屏+开新 · `/model [名]` 切模型 · `/history` 看历史 · `/status` 会话信息 · `/suggest` 看/接受自动化建议 · `/help`

## 多供应商切换

想用 **DeepSeek / Kimi** 或任意第三方 Anthropic 兼容中转:在 Web 设置页的「模型」
管理界面直接添加(base_url + key + model),**每轮自动生效,无需重启**;会话里
`/model <模型名>` 可临时覆盖。只用第三方时,`.env` 里的订阅 token 可留空。
认证细节见 [REQUIREMENTS.md](REQUIREMENTS.md) §6。

## 历史研究

原生深度研究已移除，不再提供助手研究工具，也不在重启后恢复未完成研究。
后续调研使用普通搜索与子代理，不新增自动流程。历史任务、报告、来源证据和检查点保留。

以下接口仍受现有 Web 登录认证保护：

| 接口 | 用途 |
|---|---|
| `POST /research` | 返回 410：原生深度研究已移除 |
| `GET /tasks/<id>/research` | 读取历史阶段、预算、来源证据和检查点 |
| `GET /tasks/<id>/research/progress` | 读取历史操作、来源与已提交产出的轻量快照 |
| `POST /tasks/<id>/resume` | 返回 410：不再恢复研究 |
| `GET /tasks/<id>/report` | 下载历史 Markdown 报告，未完成返回 409 |
| `POST /tasks/<id>/stop` | 保留取消入口 |

## 常驻(macOS)

> `deploy/*.sh` 目前假设 macOS + zsh。Linux 用户请照 `deploy/run.sh` 的思路自行写
> systemd unit;核心就是把 `uv run vococo serve` 跑成常驻进程。

```bash
bash deploy/run.sh     # 后台启动(登录 shell,推荐)
bash deploy/stop.sh    # 停止
tail -f data/logs/vococo.out.log   # 看日志
```

⚠️ **不要把项目放在 iCloud 同步的 `~/Desktop`/`~/Documents` 下**——常驻进程
`open()` 这些文件会被 File Provider 阻塞。放 `~/Repos` 等纯本地目录。

⚠️ **launchd 直接拉起在某些 Mac 上会卡死**:launchd 会话上下文不完整,
agent 派生的 `claude` 子进程会同步阻塞冻住事件循环。所以用 `deploy/run.sh`
(登录 shell)而非 `deploy/launchd.sh`。开机自启可把 `run.sh` 加为**登录项**
(系统设置 → 通用 → 登录项),它运行在完整 GUI 登录会话里,不受此限。

## 定时主动推送

编辑 `data/cron_jobs.json`(模板见 `deploy/cron_jobs.sample.json`),把任务 `enabled` 设 true,
`serve` 进程每 30s 检查到期任务并推送结果。支持 `cron` / `interval` / `once` 三种调度。
默认是需要分析/决策的 Agent 任务;固定脚本可设 `mode: "script"` 和 `command`,直接执行、不进
Agent。脚本末行输出 `##CRON_SIGNAL:0##` 时原样展示结果、零 LLM 调用;输出
`##CRON_SIGNAL:1##` 时才按可选 `summarize_prompt` 做一次轻量总结。
也可以直接对它说「每天早上 8 点给我发 XX」——它会用 `add_cron_job` 建好等你确认。

## 快速开始

**前置**:Python ≥ 3.11、[uv](https://docs.astral.sh/uv/)、一份 Claude Pro/Max 订阅
(或任意 Anthropic 兼容的第三方端点,见「多供应商切换」)。平台:macOS / Linux
已验证;Windows 未测(常驻脚本走 zsh + launchd,仅 macOS)。

```bash
# 1) 装依赖(uv 推荐;也可 python -m venv + pip install -e .)
uv sync --extra dev

# 2) 配认证(需 Claude Pro/Max 订阅)
claude setup-token            # 生成 sk-ant-oat01-... 令牌
cp .env.example .env          # 把令牌填进 .env 的 CLAUDE_CODE_OAUTH_TOKEN

# 3) 自检 + 开聊
uv run vococo doctor   # 检查配置 / 激活供应商
uv run vococo chat
```

## 配置

全部配置走项目根的 `.env`(从 `.env.example` 复制,已 gitignore)。最常用的几项:

| 变量 | 必填 | 说明 |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | 是* | `claude setup-token` 生成的订阅令牌(*只用第三方供应商时可空) |
| `VOCOCO_USER_NAME` | 否 | 助理如何称呼你,默认「主人」 |
| `VOCOCO_PERSONA_NAME` | 否 | 助理人格代号(UI/提示文案里露出),默认「vococo」 |
| `AI_BRAIN_DIR` | 否 | 长期记忆目录,默认 `~/AI_BRAIN`;不存在则记忆功能自动跳过 |
| `AGENT_MODEL` | 否 | 默认 `claude-sonnet-5-5` |
| `WEB_ENABLED` / `WEB_AUTH_TOKEN` | 否 | 启用手机浏览器 Web 入口;走公网必设口令 |

其余变量(安全闸、语音、Web Push、多供应商…)在 `.env.example` 里逐条有注释。

## 设计要点

设计原则、非目标红线、与原版 Hermes 的差异对照见 [REQUIREMENTS.md](REQUIREMENTS.md)
§1–§5;术语与安全模型见 [CONTEXT.md](CONTEXT.md) / [SECURITY.md](SECURITY.md)。

> ⚠️ 订阅令牌只配个人自用;商用/对外必须改回 API key 按量计费。

## 许可

[MIT](LICENSE)。这是个人自用助理框架,fork 后请生成属于自己的凭据
(Claude / VAPID),别把 `.env`、`data/` 提交进仓库(默认已 gitignore)。

### GPT 输出详细程度

在 **设置 → 模型 → 输出详细程度** 为每个 GPT 模型选择「模型默认 / 低 / 中 / 高」。
保存后下一条消息生效，思考强度不受影响。「模型默认」不传参数，保留原有表达提示。
明确选择中/高时会替换原有的简洁提醒；所有档位均以本轮用户明确要求为准。

vococo 使用 Claude Agent SDK，请求通过 Anthropic 兼容端点进入代理。
`CLAUDE_CODE_EXTRA_BODY` 携带 `text.verbosity`，但 CLIProxyAPI 原版的
Claude→Codex / OpenAI 转换会丢弃该字段。仓库提供经过回归测试的兼容补丁：

```bash
bash deploy/patch-codex-verbosity.sh /path/to/CLIProxyAPI
```

脚本只修改代理源码并运行测试，不替换二进制或重启服务；重复执行不会重复打补丁。
补丁基于 CLIProxyAPI `20c9095cac089c6a8e40554debe4dd8a19d4c14f`，版本不匹配时停止。
按代理原有流程构建、部署后，GPT 请求才会收到原生详细程度参数；部署前仍有当轮表达提示生效。
它控制回答详略，不保证固定字数，也不使用输出 token 上限截断回答。

验证（不需要真实模型密钥）：

```bash
uv run pytest tests/test_verbosity.py tests/test_verbosity_wire.py tests/test_client_pool.py -q
node tests/verbosity_settings.cjs
```

`test_verbosity_wire.py` 使用真实 SDK/CLI 向本地 HTTP 服务发请求，验证最终请求参数，
不访问真实 GPT。代理补丁自带 Go 测试，验证最终 Codex Responses 的 `text.verbosity`
和 OpenAI Chat Completions 的顶层 `verbosity`，不向其他模型透传。
