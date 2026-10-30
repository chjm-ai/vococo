# MCP 任务完成通知(MCP Events)

上线代码日期:2026-10-27。协议按 https://developers.openai.com/plugins/build/mcp-events (2026-07-28 版)。

一句话:GPT 用 `vococo_create_task` 派了活,不用一直轮询 `vococo_get_task`——任务**进入终态那一刻**,vococo 会向它订阅的 webhook 发一条带签名的事件,GPT 收到后查完整结果并向你汇报。

## 1. 服务端做了什么

| 方法 | 作用 |
|---|---|
| `server/discover` | 宣告 `supportedVersions: ["2026-07-28"]`、`capabilities.events` |
| `events/list` | 列出事件 `vococo.task.finished`(参数只有可选 `task_id`,投递方式 `webhook`) |
| `events/subscribe` | 校验并保存订阅;先向回调地址发 `verification` 挑战,回显正确才生效 |
| `events/unsubscribe` | 按 `url`(+可选 `task_id`)退订,幂等 |

- 事件体只有 `task_id / status / title / summary`(脱敏、≤1500 字),**完整结果仍然走 `vococo_get_task`**。
- 终态 `done / failed / cancelled` 只通知一次;`queued → cancelled` 这种不经过执行器的取消也会补发。
- 投递失败按指数退避重试(最长约 5 分钟后放弃),**同一事件的 `eventId` 始终不变**,客户端按 `webhook-id` 去重即可。
- 订阅持久化在 SQLite,服务重启不丢;`ttlMs` 默认 7 天,最少 5 分钟,最多 30 天,过期自动清理;回执里 `refreshBefore` 之前重新订阅一次即可续期。
- 刚跑完才订阅:订阅回执会带 `cursor`,并立刻补发 24 小时内、尚未通知过的终态。
- 只通知「本连接派出去的」任务:远程桥登记过的任务才会收到事件,本机手搓的任务不发。

安全边界:

- 回调地址必须 `https` + 443 端口,解析出的**每个** IP 都必须是公网地址;直连校验过的 IP(TLS 仍校验原域名),不跟随重定向,防 DNS rebinding 与跳内网。
- 签名走 Standard Webhooks:头 `webhook-id` / `webhook-timestamp` / `webhook-signature: v1,<base64>` / `X-MCP-Subscription-Id`;签名内容 `{msg_id}.{timestamp}.{body}` 用 `whsec_` 后的 base64 密钥做 HMAC-SHA256。
- 不扩大原有远程权限:仍然只有那 5 个工具、只绑「Vococo 开发」、危险操作照旧走 vococo 审批、桥 token 与 OAuth 数据不动。

## 2. 客户端要满足的接口

`POST` 你的回调地址,`Content-Type: application/json`:

1. 订阅时收到 `{"type":"verification","challenge":"..."}` → 用任意 2xx 返回 `{"challenge":"..."}`(官方 JSON 格式);兼容旧接收端的纯文本回显。响应正文最多 4096 字节,支持分段与 chunked 响应,回调 URL 的查询参数完整保留。
2. 之后收到事件:

```json
{"eventId":"…","name":"vococo.task.finished","timestamp":"2026-10-27T02:00:00Z",
 "data":{"task_id":"abc12345","status":"done","title":"查资料","summary":"已整理完 3 份资料"},"cursor":null}
```

回 2xx 仅确认 webhook 已接收,不代表 ChatGPT 已处理、回复或播报;回 410 表示这个订阅永久失效(vococo 会删掉订阅);413 不重试,其余非 2xx 按失败重试。

## 3. 怎么让 GPT 用上

**官方列明的支持界面**:ChatGPT 网页版 Work、桌面版 Work(Cloud)、dots。用户需明确要求监控事件及触发后做什么,ChatGPT 才会调用 `events/subscribe`,并自动提供回调地址和签名密钥;调用 `vococo_create_task` 本身不等于建立订阅。事件能力已通过官方要求的 `server/discover` 声明,不应因旧 `initialize` 的能力字段推断客户端不支持。

按官方验收步骤,在插件页面重新扫描 MCP,确认事件与工具一起出现,再在 Work 聊天发起订阅。**普通聊天、实时语音及第三方客户端是否支持,需分别实测**,官方页面未说明语音支持情况,不能直接断言支持或不支持。没有有效订阅,服务端不能自行构造 ChatGPT 的回调地址或保证唤醒/播报。

在 ChatGPT 里可以直接这样要求(订阅话术):

> 用 vococo 派一个任务,并通过 MCP events 订阅 `vococo.task.finished`。当这个任务完成、失败或取消时,立刻用 `vococo_get_task` 读出结果再告诉我。会话结束前不要取消订阅。

## 4. 验收状态

| 项 | 状态 |
|---|---|
| 协议层(discover / subscribe / 官方 JSON 挑战验证 / 签名投递 / 退订 / 过滤 / 去重重试 / 持久化 / 补发 / 脱敏日志) | 自动化测试覆盖(`tests/test_mcp_events.py`);不等于真实 ChatGPT 验收 |
| 生产 `mcp.chjm.cc/mcp` 上线 | 需重启事件前端进程后生效;未做真机 ChatGPT 验证 |
| GPT 收到事件后自动汇报 | 依赖客户端,未验证 |
| 语音播报 | 依赖客户端,不支持/未验证,服务端不承诺 |

**部署前置(必做)**:事件箱、订阅和桥登记表(任务库)要在同一个库,否则 `record_terminal` 查不到登记表、一条事件都不会发。`frontend.env` 和原 gateway 进程的环境里都要加:

```sh
VOCOCO_MCP_EVENTS_DB=<仓库绝对路径>/data/voice/voice.db
```

失败事件按指数退避重发(约 4s 起翻倍,单次最长 5 分钟,累计 6 次后放弃)。加完后按顺序:重启原 gateway → 重启 MCP 前端(`~/.local/share/vococo-mcp/run-frontend.sh`,即 `launchctl kickstart -k gui/$(id -u)/cc.chjm.mcp-frontend`)。代理/OAuth/隧道都不用动。仅前端协议/投递代码更新,无需重启 gateway;合并后先确认再重启前端,不打断执行中的任务。

## 5. 诊断日志

前端启动后写入 `config.DATA_DIR/logs/mcp-events.log`,单文件 2 MiB,保留 3 个轮转备份,不依赖 launchd 标准输出配置。只记录方法名、连接摘要、订阅 ID、事件 ID、HTTP 状态、尝试次数和错误类别;不记回调 URL、challenge、密钥或任务正文。

- 有 `server/discover` / `events/list`,没有 `events/subscribe`:客户端尚未发起订阅,不能归因于投递失败。
- `mcp_subscription code=-32015`:已收到订阅请求但回调验证失败,看 `reason`。
- `mcp_callback_verified`:回调验证成功;`mcp_subscription code=0` 表示订阅/退订成功。
- `mcp_event_delivery outcome=delivered http_status=2xx`:仅证明接收端接受该事件。`error` / `permanent_error` / `gone` 区分重试、永久失败与退订。
- 无日志只能说明缺少证据,不能排除请求尚未到达本前端。投递完成后事件箱记录会删除,以事件 ID 查询轮转日志核验历史。

2026-10-30 修复:旧实现只接受 HTTP 200 + 纯文本 challenge,与官方 JSON + 任意 2xx 不兼容;原测试也使用纯文本,未能发现问题。另修复响应头先到时丢失正文、回调查询参数丢失、网络异常时状态类型错误及最后一次重试成功被误记永久失败。未声称这些缺陷是任务 27366f26 未建订阅的已证实原因,真实 ChatGPT 订阅/响应仍待验收。
