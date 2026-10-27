# ChatGPT 接入 vococo

上线日期：2026-10-27。固定地址，后续更新不更换：

**https://mcp.chjm.cc/mcp**

## 1. 本机查看登录口令

口令已在本机随机生成，保存在私有文件，不在聊天、Git 或 ChatGPT 配置里传递。Mac 终端执行：

```sh
open -a TextEdit ~/.local/share/vococo-mcp/login-password.txt
```

只在 `mcp.chjm.cc` 的浏览器登录页输入。不要把 `proxy.env`、`frontend.env`、`bridge.env` 或生产 `.env` 里的内部 token 填给 ChatGPT。

## 2. 在 ChatGPT 添加

不同账号菜单可能显示 Apps、应用、连接器或 Plugins，以实际界面为准：

1. 在设置中找到开发者模式/自定义 MCP 功能，创建自定义应用。没有入口时不能用更换 MCP 地址解决。
2. 名称填 `vococo`，服务器 URL 填上面的完整 `/mcp` 地址；使用公网 URL，不选临时 Tunnel。
3. 认证选 **OAuth**。服务支持动态客户端注册（DCR），不要填写内部桥 token；客户端 ID/密钥是否允许留空以当前 ChatGPT 界面为准，本站未替本人账号验收这些输入项。
4. 连接后，在跳转的 `mcp.chjm.cc` 登录页输入本机口令，再点击 **Authorize**。
5. 新建文本对话，在工具/应用选择器中启用 vococo，刷新后应有下列 5 个工具。

官方连接文档：https://developers.openai.com/apps-sdk/deploy/connect-chatgpt 。2026-10-27 获取的正文使用 Plugins → Add custom MCP server → Create as a plugin；不要假定所有账号与该菜单一致。

## 3. 先文本，再语音

先发：

> 让 vococo 新建一个测试任务：不调用工具、不读写文件，只回复“ChatGPT 接入成功”。给我任务 ID，然后查询直到完成。

再测：

> 列出 Vococo 开发最近的历史会话标题。

最后才切实时语音，测试同样的请求。若语音模式不提供自定义应用工具，属于 ChatGPT 客户端/账号能力门槛；文本接通不证明语音可用，也不承诺任务完成后 ChatGPT 自动插话。

| 工具 | 用途 |
|---|---|
| `vococo_create_task` | 派任务，需标题、完整要求、幂等键 |
| `vococo_get_task` | 查进度/结果，长结果分页读取 |
| `vococo_cancel_task` | 请求取消，仍须查询终态 |
| `vococo_list_sessions` | 列出「Vococo 开发」历史 |
| `vococo_read_session` | 按会话逐轮读取脱敏文本 |

当前只开放「Vococo 开发」。危险操作仍需在 vococo 审批；远程调用不能设置完全访问、再次派单或创建定时任务。历史文本会经 ChatGPT/OpenAI 处理，脱敏不保证识别所有自然语言秘密。

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
