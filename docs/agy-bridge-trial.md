# Antigravity 本机 API 验收

链路：HTTP 请求 → 已登录的官方 `agy` → Gemini 3.8 Flash → HTTP JSON / SSE。不经 CLIProxyAPI，也不回退到按量 API。

这是限时试用，不是通用模型供应商。Google 的[官方 FAQ](https://antigravity.google/docs/faq)说明个人套餐不支持封装进第三方应用，账号可能被暂停；不要用这条路线替代生产 API，也不要发送敏感资料。`agy` 仍使用本机登录状态，可能按自己的设置保存会话。

## 在 vococo 试用

在本机实例的 `.env` 设 `VOCOCO_AGY_TRIAL_UNTIL=YYYY-MM-DD` 并重启。日期为服务器本地日期，当天零点停止；留空默认关闭。仅已配置网页口令、且 `agy` 没有全局钩子、插件或 MCP 等自定义配置的实例可启用。打开 Web 会话的模型选择器，在「Antigravity 试用 · 仅当前文字」里选 Gemini 3.8 Flash（Low、Medium、High）或 Claude Opus 4.6 Thinking。不会改变别的会话及全局默认模型。

试用每轮只转发你手动送出的当前文字，不带历史、附件、项目文件、vococo 记忆和工具。后台、语音和自动续跑不走此通道；试用会话不支持「重新生成」，手动重新输入即可；任何错误都不自动切换到其他供应商。到期后，在会话里重新选普通模型。Web 试用复用现有 SSE，不需要启用下面的独立验收端口。

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
