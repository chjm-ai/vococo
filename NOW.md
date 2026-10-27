# 当前工作

- 2026-10-27：[Issue #1](https://github.com/chjm-ai/vococo/issues/1) GPT 外置大脑已部署，固定入口 **https://mcp.chjm.cc/mcp**。代码已提交合并，生产已重启；正式 DNS、专用持久隧道、三个 launchd 服务、本机私有口令与桥凭据均已配置。
- 公网实测通过：OAuth 登录/授权/PKCE、5 个工具发现、真实任务 `0e428f10` 完成且标记一致、任务 `aeac8028` 实际取消、测试任务历史读取。无凭据公网 MCP/本机桥均 401。登录限流触发 429，等待后正确口令登录恢复 302。
- **待本人真机验收**：ChatGPT 自定义应用授权、文本工具调用、实时语音支持情况。官方 MCP 客户端公网验收不等于 ChatGPT 账号或语音已通过。
- 接入步骤见 [ChatGPT 接入说明](docs/chatgpt-mcp-connect.md)；技术与测试边界见 [RFC §10.4](docs/chatgpt-vococo-rfc.md#104-生产上线验收2026-10-27)。既有功能记录保留在 [PLAN.md](PLAN.md)。
