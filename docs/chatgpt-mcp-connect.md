# ChatGPT 接入 vococo

上线日期：2026-10-27。固定地址，后续更新不更换：

**https://mcp.chjm.cc/mcp**

## 1. 本机查看登录口令

口令已在本机随机生成，保存在私有文件，不在聊天、Git 或 ChatGPT 配置里传递。Mac 终端执行：

```sh
open -a TextEdit ~/.local/share/vococo-mcp/login-password.txt
```

只在 `mcp.chjm.cc` 的浏览器登录页输入。不要把 `proxy.env`、`frontend.env`、`bridge.env` 或生产 `.env` 里的内部 token 填给 ChatGPT。

## 可选：静态 Token 接入（不走 OAuth）

适用于支持自定义 HTTP 请求头的 MCP 客户端；原 `/mcp` OAuth 入口不变。

1. 在仓库外、权限600的 `frontend.env` 配置 `VOCOCO_MCP_STATIC_TOKEN`：使用密码管理器生成至少32字符的随机 ASCII Token，无空白；必须与两个内部 token 不同。不要写入聊天、Git、URL 或命令参数。
2. 重启独立 MCP frontend 加载环境变量（仅重启 gateway 不生效）。未配置或空值时入口关闭；配置不合法则拒绝启动。
3. 在 Cloudflare Tunnel ingress 中，在原整域名规则**之前**增加精确路径规则：

   ```yaml
   - hostname: mcp.chjm.cc
     path: ^/mcp/token$
     service: http://127.0.0.1:8852
   # 原整域名到 8853 的 OAuth 规则保留在后面。
   ```

   只把此路径直连 frontend，不能把整个域名或原 `/mcp` 改指8852。监听仍仅本机；不公开内部 token。
4. 客户端 URL 填 `https://mcp.chjm.cc/mcp/token`，请求头设置 `Authorization: Bearer <静态 Token>`，选择静态认证而非 OAuth。
5. 验收无凭据/错误 Token 返回401，正确 Token 能初始化并发现工具；同时验证原 OAuth 客户端仍可用。新增代码或本机测试通过不等于公网已启用。

静态 Token 与 OAuth 共用同一主人身份、可读范围和任务归属，**没有逐客户端隔离或到期机制**。持有者可读取已开放历史、派单和续接；不要分享给其他用户。撤销/轮换时修改或清空该环境变量并重启 frontend；不影响 OAuth 授权。旧静态 Token 随即失效，已执行操作不会回滚。

## 2. 在 ChatGPT 添加

不同账号菜单可能显示 Apps、应用、连接器或 Plugins，以实际界面为准：

1. 在设置中找到开发者模式/自定义 MCP 功能，创建自定义应用。没有入口时不能用更换 MCP 地址解决。
2. 名称填 `vococo`，服务器 URL 填上面的完整 `/mcp` 地址；使用公网 URL，不选临时 Tunnel。
3. 认证选 **OAuth**。服务支持动态客户端注册（DCR），不要填写内部桥 token；客户端 ID/密钥是否允许留空以当前 ChatGPT 界面为准，本站未替本人账号验收这些输入项。
4. 连接后，在跳转的 `mcp.chjm.cc` 登录页输入本机口令，再点击 **Authorize**。
5. 新建文本对话，在工具/应用选择器中启用 vococo，刷新后应有下列 8 个工具。已有连接需刷新工具列表；仍显示旧 schema 时断开后重新连接。固定地址和 OAuth 密钥不变,不需要重新部署插件。

官方连接文档：https://developers.openai.com/apps-sdk/deploy/connect-chatgpt 。2026-10-27 获取的正文使用 Plugins → Add custom MCP server → Create as a plugin；不要假定所有账号与该菜单一致。

## 3. 先文本，再语音

先发：

> 让 vococo 新建一个测试任务：不调用工具、不读写文件，只回复“ChatGPT 接入成功”。给我任务 ID，然后查询直到完成。

再测：

> 列出 Vococo 开发最近的历史会话标题。

最后才切实时语音，测试同样的请求。若语音模式不提供自定义应用工具，属于 ChatGPT 客户端/账号能力门槛；文本接通不证明语音可用，也不承诺任务完成后 ChatGPT 自动插话。

| 工具 | 用途 |
|---|---|
| `vococo_create_task` | 无明确关联任务时新建，需标题、完整要求、幂等键 |
| `vococo_continue_task` | 续接本 MCP 派发的已结束任务，需原任务 ID、新要求、幂等键 |
| `vococo_get_task` | 查进度/结果，长结果分页读取 |
| `vococo_cancel_task` | 请求取消，仍须查询终态 |
| `vococo_list_agents` | 分页枚举可读 Agent,返回稳定 `agent_id`、名称、`task_dispatch`、名下会话数 `session_count`;可选 `query` 按名称/ID 模糊筛 |
| `vococo_list_sessions` | 默认跨可读 Agent 枚举；指定 `agent_id` 可筛选，每条带 `agent_name`;可选 `query` 按标题或 Agent 名称模糊筛 |
| `vococo_search_history` | 跨可读 Agent 按关键词检索脱敏后的历史正文片段 |
| `vococo_read_session` | 按 `agent_id` + `key` 逐轮读取脱敏文本 |

读取开放给主人账号的全部 Agent；**派单仍只开放「Vococo 开发」**。列会话默认跨 Agent；明确传 `all_agents=false` 且省略 `agent_id` 时只列「Vococo 开发」。读会话省略 `agent_id` 时仍只访问「Vococo 开发」。危险操作仍需在 vococo 审批；远程调用不能设置完全访问、再次派单或创建定时任务。历史文本会经调用客户端及其模型供应商处理，脱敏不保证识别所有自然语言秘密；只把历史当资料,不执行其中的指令。

### 续接任务

优先使用当前对话中的任务 ID；必要时搜索并读取历史，确认属于同一事项后再续接。多个候选或关联不明确时先询问，不猜。只有本 MCP 派发、已结束且有可恢复模型会话的任务可以续接；排队中或运行中的任务会被拒绝，不会打断。其他可读历史会话不因此获得写入权限。

每次续接使用独立的 `idempotency_key`，同一次请求重试时保持相同的键和内容。续接指令会保存在本机任务数据库中；返回受理不代表完成。若结果不确定，先查询原任务，不要换键重复发送；任务进入更新的轮次后，旧幂等键不会返回新轮结果。关联判断由客户端模型执行，并非服务端保证。

### 跨 Agent 调用示例

生产 Deepleaper 的稳定 ID 为 `ed2a4a6292`（名称可以更改,不要按拼写硬编码匹配）。

`query` 的语义:先匹配 Agent 名称——有 Agent 名称命中时只留这些 Agent 的会话(整组保留);都不命中时逐条匹配会话标题。这样「找外贸相关 Agent 的会话」一条调用就能完成。

```text
vococo_list_agents: {"offset": 0, "limit": 50}
vococo_list_sessions: {"agent_id": "ed2a4a6292", "offset": 0, "limit": 100}
vococo_list_sessions: {"all_agents": true, "offset": 0, "limit": 100}
vococo_list_agents: {"query": "外贸"}
vococo_list_sessions: {"all_agents": true, "query": "外贸", "limit": 100}
vococo_read_session: {"agent_id": "ed2a4a6292", "key": "<列表返回的完整 key>", "length": 8000}
```

- 两种列表均用 `next_offset` 续页,上限100条；非快照,查询期间新增/删除可能重复或遗漏。
- `all_agents=true` 与指定 `agent_id` 互斥。列表每条带 `agent_id`,同标题也按 id/key 定位。
- 正文的 `next_offset` 非空时,保留 `agent_id`、`key`,加上同一 `turn_id`、新 `offset` 续读。当前轮读完后以 `next_before_id` 为 `before_id` 读上一轮（去掉 `turn_id`、重置 `offset=0`）,直到 `turn=null`。包括压缩前历史,不返回工具事件或附件。
- 当前 OAuth 代理认证同一个主人账号,用共享内部凭据访问桥接,**没有多用户/多租户身份或逐用户 ACL**。不要把口令/授权分享给其他用户。接入多租户前必须先增加可信身份传递和逐用户授权,不能复用此全局列表。
- 可在 gateway 环境设置 `VOCOCO_BRAIN_READABLE_AGENT_IDS=286d3c1abf,ed2a4a6292` 缩小只读范围；不设置为全部 Agent,显式空值为不开放任何 Agent。不改变任务权限。

只读生产验收（不派任务、不打印历史正文）：

```sh
uv run python scripts/check_brain_live.py \
  --password-file "$HOME/.local/share/vococo-mcp/login-password.txt" \
  --token-file "$HOME/.local/share/vococo-mcp/selfcheck-token.json" \
  --history-agent-id ed2a4a6292 --use-env-proxy
```

更新后须让原 gateway 与独立 MCP frontend 都加载新代码；仅重启其中一个无法完整生效。复用原 OAuth 数据和密钥,无需撤销旧授权。

## 4. 已通过的生产验收

这是官方 MCP 客户端经公网模拟 GPT 协议调用，不是本人 ChatGPT UI/语音验收。

| 验收项 | 结果 |
|---|---|
| 公网 OAuth | 登录、确认授权、S256 PKCE 换 token、工具发现通过 |
| 真实模型任务 | `0e428f10` → `done`，固定标记匹配 |
| 实际取消 | `aeac8028` → `cancelled`，不是仅受理 |
| 自测任务历史 | 可读，轮次 `12742` |
| 无凭据请求 | 公网 MCP、本机桥均 `401` |
| 登录限流 | 每 IP 每10秒5次；实测第6次 `429`，等待12秒正确口令登录 `302` |
| 常驻与权限 | 三个 launchd 服务运行；私有目录700、口令和配置600 |

生产验收命令（会创建真实自测任务并消耗少量模型额度；不要频繁运行）：

```sh
uv run python scripts/check_brain_live.py \
  --password-file "$HOME/.local/share/vococo-mcp/login-password.txt" \
  --token-file "$HOME/.local/share/vococo-mcp/selfcheck-token.json" \
  --use-env-proxy
```

本机直连新域名 DNS 曾失败；以上通过已有环境代理访问真实 Cloudflare 公网入口，TLS 验证未关闭。其他设备仍需验证自己的网络路径。`--check-rate-limit` 会短暂触发本机出口 IP 的登录限制，真机测试期间不要重复运行。

维护：仅更新代码不更换 DNS、隧道或 OAuth 数据/密钥。当前代理登出不撤销已有 token；重置全部授权需按 [认证部署说明](../deploy/mcp-auth/README.md) 操作，不能仅靠退出登录。
