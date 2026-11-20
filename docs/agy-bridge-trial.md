# Antigravity 本机 API 验收

链路：HTTP 请求 → 已登录的官方 `agy` → Gemini 3.8 Flash → HTTP JSON / SSE。不经 CLIProxyAPI，也不回退到按量 API。

这是限时试用，不是通用模型供应商。Google 的[官方 FAQ](https://antigravity.google/docs/faq)说明个人套餐不支持封装进第三方应用，账号可能被暂停；不要用这条路线替代生产 API，也不要发送敏感资料。`agy` 仍使用本机登录状态，可能按自己的设置保存会话。

## 在 vococo 使用

已取消试用截止日；只要本机配置了网页口令、`agy` 可执行，且没有全局钩子、插件或 MCP 等自定义配置即可使用。新建普通 Web 会话，在模型面板的「Antigravity」组中选模型；目录来自官方 ACP `configOptions`，不会改变别的会话或全局默认模型。

模型按「族」展示（Gemini 4 Neon / Argon / Hydrogen、Orchestrator、Claude Opus/Sonnet 5.5），思考深度不再拆成 High/Medium/Low 行，改用输入框左侧的「思考档位」；GPT 相关模型隐藏。旧会话里的 Gemini 3.8 / Claude 4.6 / GPT 选择会自动迁移到当前第一族，并保留原来的档位。上游升级灰度期间新旧模型列表会随机返回，vococo 会重试并保留最近一次新清单。Antigravity CLI 入口暂时从模型面板隐藏，额度环挂在「Antigravity」分组标题右侧。

网页会话通过官方 `agy acp --stdio` 运行，并在独立的交接工作区里按轮刷新：

- 当前 Agent 的项目规则、Agent 职责、README 和两份记忆索引（全局分区 + Agent 自己的索引）；
- 当前 Agent 允许的文本 Skill 清单，模型用只读 MCP `read_skill` 按需读取正文；
- 5 个只读 MCP 工具：`read_agent_context`、`read_agent_memory`、`read_context_file`、`list_skills`、`read_skill`。
- 本机 `vococo-local` MCP（若 `~/.local/share/vococo-mcp/static-token.txt` 存在且前端可达）：
  - 只读：`vococo_list_agents`、`vococo_list_sessions`、`vococo_search_history`、`vococo_read_session`、`vococo_get_task`；
  - 写操作：`vococo_create_task`、`vococo_continue_task`、`vococo_cancel_task`，每次都会在网页弹出「允许一次 / 拒绝」，未点允许不会执行；超过 4000 字或疑似含凭据的指令直接拒绝，不弹窗。

文件读写、编辑、命令和联网已完全放开：`SETTINGS_ACP` 使用 `always-proceed` 并允许工作区外访问，命令在本机以当前系统用户身份直接执行；三个 vococo 写任务仍由 `permissions.ask` 转成网页「允许一次 / 拒绝」，其余工具不再拦截。`vococo_create_task` 目前按 MCP 连接默认派给 Vococo 开发；聊天历史来自本机 MCP，不会写进交接目录。静态 token 只在进程内传给 `agy`，不落到工作区。模型选择器仍通过官方 `agy --print /usage --output-format json` 查询额度。

首轮成功才锁定原生会话，之后可在同组切模型，但不能切到其他供应商。`agy` 按自己的会话 ID 恢复历史；旧版纯文本会话会自动尝试用 ACP 载入。后台、语音和自动续跑不走此通道，不支持重新生成。Web 通道复用现有 SSE，不需要启动下方独立端口，也不影响「Antigravity CLI」终端交接。

## 独立 HTTP 验收

在包含 `scripts/agy_bridge.py` 的 vococo 会话副本根目录运行。依赖当前 Mac 已登录的 `~/.local/bin/agy`、Clash 的 `127.0.0.1:7890` 和 vococo 现有的 Python 环境。无需重新登录或改终端代理设置。

```zsh
export AGY_BRIDGE_TOKEN="$(/Users/wesley/Repos/vococo/.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
/Users/wesley/Repos/vococo/.venv/bin/python -m scripts.agy_bridge &
```

在同一终端发请求（`-N` 关闭 curl 的输出缓冲）：

```zsh
printf 'header = "Authorization: Bearer %s"\n' "$AGY_BRIDGE_TOKEN" | \
  curl --noproxy '*' -NsS --config - http://127.0.0.1:8768/v1/generate \
    -H 'Content-Type: application/json' \
    --data-binary '{"prompt":"不用工具，逐行写出 25 条月球冷知识","stream":true}'
```

流式支持 `delta`（正文增量）和 `done`（本轮成功）。流式 HTTP 200 只表示连接建立；遇到 `error` 或未收到 `done` 就断开，都算失败。短回答可能只有一块。把 `stream` 改为 `false` 可验收普通 JSON，成功时返回 `{ "model": "…", "text": "…" }`。不填 `model` 默认 `gemini-3.8-flash-medium`，也允许 `low` 和 `high` 完整型号 ID。

验收完在该终端用 `fg` 将服务切回前台，再按 `Ctrl+C` 停止。不要把端口映射出去，不要把令牌放进前端代码。原型一次只跑一个请求，不支持多轮、文件或工具调用，也不兼容 OpenAI / Anthropic 的聊天 API。
