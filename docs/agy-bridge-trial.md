# Antigravity 本机 API 验收

链路：HTTP 请求 → 已登录的官方 `agy` → Gemini 3.8 Flash → HTTP JSON / SSE。不经 CLIProxyAPI，也不回退到按量 API。

这是限时试用，不是通用模型供应商。Google 的[官方 FAQ](https://antigravity.google/docs/faq)说明个人套餐不支持封装进第三方应用，账号可能被暂停；不要用这条路线替代生产 API，也不要发送敏感资料。`agy` 仍使用本机登录状态，可能按自己的设置保存会话。

## 在 vococo 试用

在本机实例的 `.env` 设 `VOCOCO_AGY_TRIAL_UNTIL=YYYY-MM-DD` 并重启。以服务器本地时间为准，当天零点停止；留空默认关闭。仅已配置网页口令、且 `agy` 没有全局钩子、插件或 MCP 等自定义配置的实例可启用。新建普通 Web 会话，在「Antigravity · 网页纯文本」中选模型；目录来自官方 `agy models`，不会改变别的会话或全局默认模型。

模型选择器会通过官方 `agy --print /usage --output-format json` 查询各模型组的剩余额度和重置时间；没有返回的窗口不显示。不同聊天可同时请求，实际并发及额度仍由上游决定。

首轮成功才锁定原生会话，之后可在同组切模型，但不能切到其他供应商。`agy` 按自己的会话 ID 恢复历史；vococo 每轮只发送手动输入的文字，不同步已有历史、附件、项目文件、记忆、Skill 或工具。后台、语音和自动续跑不走此通道，不支持重新生成；到期后保留回看，若需继续用其他供应商，请新建会话。Web 试用复用现有 SSE，不需要启动下方独立端口，也不影响「Antigravity CLI」终端交接。

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
