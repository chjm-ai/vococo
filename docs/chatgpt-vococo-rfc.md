# ChatGPT 调用 vococo：需求与实现评估

- 日期：2026-10-26
- 状态：主人已确认先开发只读连接探针；完整任务桥仍是 RFC 草案，未获实施授权。
- 来源：[Issue #1](https://github.com/chjm-ai/vococo/issues/1)
- 范围：个人自用，由 ChatGPT 派单、vococo 在本机执行开发或业务任务。

## 1. 结论与关键修正

| 判断 | 把握 | 依据 / 限制 |
|---|---|---|
| 通过远程 MCP 让 ChatGPT 文本会话派单，技术路径成立 | 高 | OpenAI Apps SDK 官方文档支持远程 MCP；尚未在主人的账号实测 |
| 不必重造 Agent 和任务执行器 | 高 | 现有 dispatch、任务库、worktree、取消、回执均可复用 |
| 官方实时语音可直接调用自定义 MCP | 不承诺 | 官方帮助页搜索摘要写明 Voice mode currently does not support apps；正文抓取 403，账号/客户端需实测 |
| 官方 App 可以在任务完成后自动插话播报 | 未验证 | MCP 传输可发送消息，不等于 ChatGPT 会主动开启模型轮次或语音播放 |
| 任务跨 gateway 重启自动继续 | 当前不支持 | heal_after_restart 将遗留 queued/running 标记失败；结果和回执持久化不等于执行恢复 |

**建议：先验证官方 ChatGPT 文本入口，再做异步任务桥。实时语音是独立的产品门槛，不与文本 MCP 成功混算。**

如果只有“官方 App 实时语音派活”才有价值，应先验证这条门槛；不支持就暂停，而不是先完成网关再发现入口不可用。

## 2. 用户目标与边界

### 目标场景

1. 用户在 ChatGPT 中说或输入“让 Vococo 开发 Agent 检查这个仓库的问题”。
2. ChatGPT 调用受限工具，vococo 落库后立即返回任务 ID，执行放到后台。
3. 用户继续交流，或再派第二个任务，不占着一个长时间工具请求。
4. 用户询问进展或结果；危险操作到 vococo 可信界面审批。
5. 执行过程、结果和可继续追问的会话留在 vococo，不只存在 ChatGPT 里。

### 首期非目标

- 不把 vococo 全部内部工具、Bash、任意文件读取或 Web 管理接口直接暴露出去。
- 不共享订阅凭据，不做多用户服务、公共应用上架或商业转售。
- 不复制完整 ChatGPT 历史；ChatGPT 必须在派单文本中明确必要背景。
- 不允许客户端传入任意 cwd、内部 session key、权限档位、模型凭据或审批结果。
- 不承诺自动续跑、事务回滚、自动合并发布、ChatGPT 后台唤醒或实时语音主动播报。
- 不为本次需求重写现有 Omni 通话。若选择自建 OpenAI Realtime 入口，另行立项、计费和评审。

## 3. 平台可行性门槛

| 项目 | 当前证据 | 必须补的验证 |
|---|---|---|
| MCP 服务 | 官方推荐 SDK + Streamable HTTP，常用 /mcp | 固定协议/SDK 版本，Inspector 实测 |
| 账号权限 | 自定义 MCP 的套餐、工作区管理及客户端开放条件可能变化 | 在本人账号记录套餐、开发者模式、可创建 App 的入口及日期，不根据旧套餐表推断 |
| 文本调用 | 官方文档有连接自定义 MCP 的流程 | 实际完成发现工具、认证、ping 和写工具确认 |
| 语音派单 | 帮助页搜索摘要称暂不支持 Apps；不是本账号实测 | 实时通话中实际调用同一 ping；语音转文字后发送不算实时通话成功 |
| 主动播报 | 无已验证依据 | 无用户新发言时任务完成，观察是否主动开启语音输出；没有证据就保持“不支持/未验证” |
| 断开连接 | 本机异步任务可独立于 HTTP 请求存活 | ChatGPT 断线后仍可在 vococo 查同一任务 |

P0 不需要开放执行权限：仅最小远程 MCP 的 ping，最多配合读取本次测试任务。未通过文本门槛，不推进写工具。

## 4. 方案选择

| 方案 | 价值 | 代价 / 缺点 | 建议 |
|---|---|---|---|
| ChatGPT 自定义 MCP App | 官方文本入口直接派单，执行仍在本机 | 需公网可达或官方支持的私网连接；账号条件与语音限制；认证不能省 | 推荐首期 |
| GPT Actions / 另一套 HTTP 接口 | 某些账号可能提供替代入口 | 额外维护 schema 和调用路径；不证明能解决实时语音或主动播报 | 首期不并做 |
| 自建 OpenAI Realtime 语音入口 | 可自行控制函数调用、完成事件与播报时机 | 不再是官方 ChatGPT App；新增前端/音频生命周期，API 单独计费 | 仅在语音是硬需求时另行确认 |
| 保持现有 vococo 通话 | 已有派活、任务事件和本机控制 | 不是 ChatGPT 官方语音体验 | 零新增接入成本的基准方案 |

## 5. 推荐架构

```text
ChatGPT 自定义 MCP App
  ↓ HTTPS + 专用授权
远程 MCP 接入层（只暴露任务工具）
  ↓ 参数/身份/归属/幂等/限流/授权范围检查
外部任务服务（绑定允许的 Agent 和来源）
  ↓ 普通任务执行器及权限上下文
现有 task_runner → Agent / skills / worktree / 审批闸
  ↓
现有任务库、进度、结果、回执、vococo Web/Push
```

- 远程 MCP 是新的“入站服务”；现有内部 MCP 是 vococo 调工具的“内部接口”，不能直接改成全量公开。
- 优先同进程生命周期，避免多一个常驻任务执行器；根据 MCP SDK 与 aiohttp 的兼容试验决定是否同端口挂载。若不兼容，可用独立 MCP HTTP 适配进程和受控本地桥接，仍只能由原进程调度执行任务，不能启动第二份 runner。
- OAuth 优先使用成熟授权服务，避免自行实现完整登录系统。ChatGPT 连接使用 OAuth 2.1、PKCE、资源元数据发现及受众绑定；鉴权在每次工具执行时校验。
- 普通 Cloudflare Access 网页登录不能假定为 ChatGPT MCP OAuth；必须验证机器请求可达。隧道只负责网络，不负责工具权限。
- 客户端提供的 conversation_ref 只是关联标签，不能成为身份或访问权限依据。

## 6. 首期工具契约（拟定）

统一工具名和返回字段在 P0 后冻结；以下是需求约束，不表示已实现。

| 工具 | 输入 | 返回 / 行为 |
|---|---|---|
| ping | 无 | 最小服务标识、接口版本；不返回路径、密钥或设置 |
| list_agents | 无 | 仅主人配置允许远程调用的 Agent ID/名称/用途 |
| create_task | agent_id、title、prompt、idempotency_key | 落库后返回 task_id、status、created_at；不等待模型完成 |
| list_tasks | cursor?、limit? | 仅本授权主体创建的任务，摘要分页；默认不含全文 |
| get_task_status | task_id | 状态、更新时间、有限进度、needs_approval、cancel_requested、执行版本 |
| get_task_result | task_id、cursor? | 当前轮次结果、摘要和有限分页；未完成时明确返回未就绪 |
| cancel_task | task_id | 返回取消请求是否受理和当前状态，不虚报已停止 |

### 执行与结果

- 正式状态沿用 queued/running/done/failed/cancelled；审批、取消请求是补充字段，不伪造现有状态。
- needs_approval 必须来自真实待处理审批记录，不靠模型文字猜测；任务结束后旧审批记录要标明是否已超时/失效。补批是重试一步，不是恢复持久化调用栈。
- done 仅表示该轮执行结束，不代表用户所有目标完成。ChatGPT 必须展示结果中的失败、跳过与未验证项。
- 开发任务由所选 Agent 解析到服务端绑定的项目，按现有策略创建 worktree；不接受公网任意路径参数。
- 首期在 vococo 原会话继续追问，不提供公网 append：当前 append 在 running 时会打断原任务，不能包装成“无副作用留言”。后续续聊必须明确轮次和并发冲突。
- 创建/取消标注为写工具；只读查询标注为只读，但工具 annotations 不是服务端授权机制。ChatGPT 的调用确认不能替代 vococo 对 push、安装或发布的操作级审批。
- 默认仅返回用户请求范围内的摘要；全文查询属于显式向 OpenAI 外发数据，不默认读取全库、自动附带原始日志、任意文件或附件。

### 幂等与配额（建议初值，待确认）

- 幂等按授权主体 + idempotency_key 唯一绑定，并记录规范化参数摘要；同键同参数返回原任务，不同参数返回冲突。
- 幂等记录和任务创建要原子化或可对账，防止落库成功但响应断线后重试创建第二份任务。去重不是“外部副作用恰好一次”保证。
- 同主体同时 2 个运行、10 个排队，且不超过全局 runner 配额；超额明确拒绝或排队，不另开绕过全局限制的执行器。
- prompt 暂沿用内部委托上限 60,000 字符；title 100 字符；列表每页不超过 50 条；结果每页不超过 8,000 字符。
- 速率初值：创建/取消合计 10 次/分钟，查询 60 次/分钟；返回 retry_after，不让 ChatGPT 高频轮询。
- 建议 create_task 在本机正常负载下 2 秒内返回；队列等待和任务运行时间另计。以上均是目标，不是已测性能。

## 7. 权限和安全硬要求

1. 专用远程授权与 Web 管理口令分离；凭据可撤销，token 有有效期和用途范围。不在 URL、源码、工具描述或日志输出密钥。
2. 服务端校验 token 签名、issuer、audience、有效期和 scopes；身份不从 prompt/agent_id/session 参数推断。
3. 每个查询、取消、结果请求均检查“授权主体拥有该远程任务”；猜到其他任务 ID 也不能访问，返回一致的 not_found。
4. 任务只能选择预先配置的允许 Agent；远程来源固定标准档，不继承目标 full 模式，不允许修改安全设置/Agent 配置/注册 MCP/创建脚本 cron 来扩权。审批防线关闭时，写入口失败关闭。
5. 首期远程来源不使用免批永久规则、后台本地危险操作自动放行或任务全允许来扩大授权；凡 classify=escalate 的操作必须走可信审批或拒绝。这是对当前后台策略的新增约束，必须单独覆盖测试。
6. 提示词、返回结果、远程工具描述均不能授予权限。远程接口不提供 approve、切换权限或配置安全规则的工具。
7. 使用现有后台 Web 审批/推送/铃铛；没有可用审批通道、免打扰或超时则拒绝相应操作。补批重试可能再次产生副作用，必须显示原操作与当前任务轮次。
8. SDK 子代理、再次派单等替代入口都必须继承远程来源约束，不能只在 MCP handler 检查一次。
9. 校验 Host、Origin（存在时）、内容类型、请求大小和协议会话绑定；服务端无 Origin 的请求按机器客户端协议处理，不能误用浏览器 CORS 代替认证。
10. 工具审计记录主体、工具、任务、请求关联 ID、结果码与耗时；参数只记摘要/哈希。进度及结果字段限量、脱敏，不返回 SDK 日志、凭据与内部数据行。
11. 撤销凭据立即阻止后续调用；已受理任务是否取消由主人本地决定，不能把 token 撤销误报成任务回滚。

## 8. 源码复用与实现工作包

| 现有位置 | 已核实语义 | 实现需要 |
|---|---|---|
| vococo/core/task_runner.py:745 | dispatch 落库后调度，立即返回 | 复用调度；外部来源必须先绑定安全上下文，不能无上下文裸调 |
| vococo/core/task_runner.py:792 | append 可打断 running 任务并重跑 | 首期不对外暴露 |
| vococo/core/task_runner.py:838 | cancel 发取消请求，运行结束后才落终态 | 明确受理与停止的差别 |
| vococo/core/task_runner.py:884 | 重启将普通遗留任务标失败，补终态回执 | 不许宣传自动续跑；中断原因需对外可读 |
| vococo/core/tasks.py:61、200 | 状态迁移、来源权限捕获 | 复用状态和来源约束；增加远程主体/幂等关联 |
| vococo/core/delegation.py:77 | 委托要求内部来源 Agent 和真实活动用户轮次 | 不能伪造内部轮次调用 delegate；需明确外部来源适配 |
| vococo/gateway/web_auth.py:16 | X-Auth-Token 全局 Web 口令 | 不复用为远程权限体系 |
| vococo/gateway/task_routes.py:60、82 | 详情按任务 ID 返回、全局 SSE | 不直接公开；新入口做归属与字段过滤 |
| vococo/tools/danger.py；gateway/notice_actions.py | 现有审批与铃铛补批 | 增加远程来源严格策略及待审批投影，不新造远程批准通道 |

拟新增 remote_mcp 协议适配、外部任务服务和远程授权/关联表；具体文件名在代码设计时确定。普通 task 仍是执行真相来源，关联表不能另维护一套运行状态机。

## 9. 阶段与验收

### P0：平台与协议验证

- 最小工具、临时测试数据、明确授权；不开生产任务写入口。
- Inspector 验证协议、缺 token 拒绝和认证发现。
- 本账号分别记录 Web 文本、手机、实时语音是否发现/调用同一个 ping；区分输入法语音和实时通话。
- 固定 OAuth/连接路径与 MCP SDK 挂载方式，确认文本方案是否仍值得继续。
- 语音不支持则记录阻塞，不把文本成功当作语音验收，也不自动切到 API 自建方案。

### P1：异步派单与查询

- 连续派两个任务，立即返回不同 ID；关掉 ChatGPT 后仍在 vococo 执行。
- 同键并发重试只生成一项任务；同键不同参数拒绝；模拟“提交成功、响应断开”验证不重复派单。
- 缺失/过期/错误受众 token 拒绝；查询、取消、结果无法访问其他主体或原有 Web 任务。
- 允许 Agent 之外、任意路径、伪造 session/权限参数拒绝；目标 full 不扩大远程权限。
- 用假模型验证普通读写/测试可执行；push、安装、发布、危险本地命令必须审批，不可经子代理/再派单绕过。
- 排队取消得到 cancelled；运行中取消只先报受理，最终状态另查；副作用不回滚。
- gateway 重启后原运行任务标失败，仍可查历史结果，绝不自动重做有副作用的动作。
- 真实开发用独立测试仓库：修改、测试、查看 diff；真实业务用无敏感素材的报告任务。生产 vococo 自修改不作为首次冒烟测试。
- 审批仅在 vococo 完成；超时/离线明确拒绝，结果如实列跳过项。
- 输出分页无遗漏、上限生效；不泄漏密钥、原始内部任务行或任意附件。

### P2：通知增强（可选）

先复用 vococo Web/Push 告知完成；ChatGPT 只能主动查询时就明确说“查询式”。只有实际证明官方客户端可接收并播报，才新增相应承诺。自建 Realtime 分支另写方案，不默认为 P1 必需。

## 10. 工作量与待拍板

工作量为初评、非工期承诺（把握：中）：P0 小；P1 中到大，主要工作在认证、来源权限、幂等和恢复测试，不在添加工具函数；自建实时语音属于独立大项。P0 后再给文件级计划与工期，避免把公网执行入口估成一个下午的路由改动。

主人已确认先做探针，再开发真实派单及历史读取，首期限定「Vococo 开发」，危险审批回 vococo；固定域名和 OAuth 口令方案已确认，不再作为开工待拍板项。真实客户端语音行为仍需本人账号实测。

初评之后的探针与任务桥实现、隔离验收边界见下节；生产部署与公网验收现已完成（§10.4），Issue 尚未远程改写。

### 10.1 已实现的连接探针 MVP

- 入口：`vococo/gateway/mcp_probe.py`，独立进程，固定监听回环地址，不启动现有 gateway、Agent、任务库或记忆。
- 初版工具为 `vococo_ping(challenge)`；挑战词为 1–64 位字母、数字、下划线或短横线；每次返回新的随机 receipt 和 UTC 时间。第二轮增加的只读测试工具见 §10.2。
- 使用官方 MCP 1.28.1 的 Streamable HTTP、无状态 JSON 响应；工具只读，无需登录，仅用于短期链路测试。不能在此服务上直接追加真实执行能力；执行入口必须另做 OAuth/授权。
- Host/Origin 仍严格检查；可通过 `--public-origin` 允许一个指定 HTTPS 域名，不关闭 DNS rebinding 防护。公网反代若固定上游 Host，则使用回环 Host；Origin 存在时仍需匹配。
- 服务端日志记录工具名、回执、时间和有限测试指标，不记录挑战词或长正文；用于核对是否真的发生工具调用。它没有真实任务进度、文件访问或开发能力。
- 自动验证：`uv run pytest tests/test_mcp_probe.py -q`；本地真实 HTTP 与官方客户端完成发现、调用、拒绝非法参数/未知工具、限制 Host/Origin、确认管理路由不存在。

本机启动（不重启 vococo）：

```sh
uv run python -m vococo.gateway.mcp_probe --port 8852
# 固定公网域名反代时，加：--public-origin https://实际域名
```

实测操作：

1. 经单独 HTTPS 隧道只暴露 8852，不转发现有 8848 Web 管理服务；外网开放前确认，短期使用后关闭。此探针没有生产级限流，不适合永久公开。
2. 在 ChatGPT 自定义 App/MCP 配置中填写实际 HTTPS 地址加 `/mcp`，认证选择“不需要认证”；若账号无该入口，记录账号限制，不伪装完成。
3. 文本先测：明确要求调用 vococo_ping，challenge=`text_test_01`，返回完整 receipt；在服务日志核对。
4. 再开启真正实时语音，要求调用同一工具，challenge=`voice_test_01`；必须出现一条新的服务端 receipt，并与工具结果一致。仅念出工具名称、复述旧回执或语音输入法提交文字均不算通过。
5. 若语音看不到 App 或不发起调用，保留文字成功/语音失败两份结果，停止扩大开发；不自动转做自建语音。

验收状态：本地 10 项测试通过、独立代码评审通过。主人批准临时开放后，已通过单独 Cloudflare Quick Tunnel 完成公网 HTTPS → 官方 MCP 客户端初始化/工具发现/vococo_ping → 本机日志回执匹配（2026-10-26 14:13 UTC）。测试服务独立于生产 vococo，最多运行一小时，自动关闭；临时地址不作为长期配置。

本机默认代理访问该公网地址出现 ConnectError，客户端 trust_env=False 直连后通过；不是已证明 ChatGPT 可达或语音成功。ChatGPT 账号文本及真正实时语音仍需主人客户端操作验证。

### 10.2 耗时与长回复测试扩展（2026-10-26）

主人反馈连接成功，要求继续测速度和长回复。该反馈仅记录为“连接成功”，不推断已通过官方实时语音调用。

| 工具 | 输入约束 | 返回与目的 |
|---|---|---|
| vococo_ping | challenge 1–64位 | 原有回执，用于短请求延迟 |
| vococo_delay | delay_ms 为整数，0–5000，默认1000 | 异步等待，返回实际服务端等待时间；同时最多两次，多余调用明确报忙 |
| vococo_long_reply | characters 为整数，256–32768，默认4096 | 生成精确字符数中英混合正文，含 BEGIN/END 回执标记、UTF-8字节数、SHA256及服务端生成耗时 |

这些仍是合成只读测试数据，不是调用模型写长文，不代表 Agent 工作完成。JSON 响应不是模型 token 流；UTF-8字节数只指正文，不是完整网络报文大小。

可重复运行的验收脚本（每轮最多10次ping，不是负载测试）：

```sh
uv run python scripts/check_mcp_probe.py http://127.0.0.1:8852/mcp
uv run python scripts/check_mcp_probe.py https://实际测试域名/mcp
# 默认不继承环境代理；确实需要时加 --use-env-proxy
```

脚本校验三档长回复的字符/字节/哈希/头尾，并在延迟请求期间发一次ping，观察是否被阻塞。结果默认输出JSON；测试产物在忽略目录 `data/mcp-probe-test/`，不提交临时公网地址或正文。

本轮实测（2026-10-26 14:34 UTC）：

| 项目 | 本地客户端 → 回环地址 | 本机客户端 → 公网HTTPS隧道 → 本机 |
|---|---:|---:|
| 初始化 | 54.07ms | 1883.43ms |
| ping中位数（各5次） | 6.74ms | 372.44ms |
| ping最小–最大 | 6.66–7.37ms | 365.34–565.44ms |
| 1024字符 / 1664正文UTF-8字节 | 10.30ms | 385.99ms |
| 8192字符 / 13504正文UTF-8字节 | 11.15ms | 867.93ms |
| 32768字符 / 54130正文UTF-8字节 | 12.55ms | 1141.42ms |
| 指定等待2000ms的端到端耗时 | 2010.29ms | 2456.89ms |
| 等待请求未完成时ping已返回 | 通过 | 通过 |

所有长回复完整性和服务端回执匹配通过；16项自动测试通过，独立代码评审未发现需修问题。测试也发现未参数化 `dict` 返回注解不会让 SDK 产生 structuredContent，已改为明确值类型的字典并通过真实客户端回归。

这是小样本链路测量：长回复每档只测一次，不能推断稳定吞吐、P95或真实ChatGPT延迟；不含OpenAI工具选择、模型生成、语音播放。最慢长正文服务端生成仅0.681ms，而公网耗时1141.42ms，说明本次样本主要耗时在客户端/协议/网络而非正文构造，不能据此定位具体网络节点。

更新探针需重启独立测试会话，本轮Quick Tunnel地址已变化；请更新ChatGPT App地址并刷新工具列表。原定2026-10-26 15:12:23 UTC自动关闭时间不延长，生产vococo未重启。ChatGPT侧建议测试：先调用 delay_ms=2000，再请求8192字符，仅汇报字符数、首尾回执、末尾标记和摘要；模型宣称“已核验哈希”不能代替客户端本地计算验证。

### 10.3 真实任务桥与历史读取（2026-10-27）

**代码及隔离验收完成；后续生产部署与公网验收已完成，见§10.4。** 本节保留实现和隔离测试边界，不替代本人 ChatGPT 账号/语音验收。

```text
固定 https://mcp.chjm.cc/mcp
  → 独立命名隧道（已部署，不改已有共享隧道）
  → mcp-auth-proxy v2.10.2，127.0.0.1:8853（成熟 OAuth 口令登录）
  → brain_mcp，127.0.0.1:8852（独立上游共享凭据）
  → 原 gateway，127.0.0.1:8848/internal/brain/{operation}（独立桥凭据）
  → 原 task_runner / session_store
```

前端启动入口 `uv run python -m vococo.gateway.brain_mcp`；必须预先安全配置不同的 `VOCOCO_MCP_UPSTREAM_TOKEN` 和 `VOCOCO_BRAIN_BRIDGE_TOKEN`，均至少32字符；原 gateway 也须配置同一桥 token。凭据不能放命令参数或 Git。成熟代理仅往前端注入上游共享凭据，不把用户 OAuth token 传到原 gateway。

| 对外工具 | 实际能力与边界 |
|---|---|
| `vococo_create_task(title,prompt,idempotency_key)` | 立即返回排队任务；同键同内容返回原任务，同键不同内容409；固定 Agent `286d3c1abf` 和其执行目录 |
| `vococo_get_task(id,offset,length)` | 仅本连接账本登记任务；最多8000字符结果片段，返回 next_offset |
| `vococo_cancel_task(id)` | 仅本连接任务；cancel_requested 不代表已停，不能回滚既有动作 |
| `vococo_list_agents(offset,limit)` | 主人账号可读全部 Agent 的稳定 id/名称/是否可派单；最多100条 |
| `vococo_list_sessions(offset,limit,agent_id,all_agents)` | 默认固定开发 Agent；可指定 Agent 或跨 Agent 枚举；每条带归属,最多100条，非快照分页 |
| `vococo_read_session(key,before_id,turn_id,offset,length,agent_id)` | 默认固定开发 Agent,显式 id 时校验 id/key 归属；每次读一轮纯文本，含压缩前历史；最多8000字符，两角色合计，next_offset 续本轮，next_before_id 读更早轮次 |

2026-10-27 扩展只读范围：用户明确授权读取全部 Agent。OAuth 仍是单主人账号,共享凭据不携带逐用户/租户身份,不得部署为多用户数据服务。gateway 可用 `VOCOCO_BRAIN_READABLE_AGENT_IDS` 限制可读 id；未设置表示全部,空值表示全部拒绝,每次分页重新校验。派单/取消/任务账本边界不变。连接刷新与完整示例见 [接入说明](chatgpt-mcp-connect.md)。

内部桥写操作不接受调用方指定 Agent/目录/来源/权限；拒绝公网 peer、代理/Cloudflare/Origin 头以及未知或重复字段。MCP 进程只通过受限 HTTP 调用，不加载生产任务库，不启动第二个 runner。原 gateway 管理接口没有原样开放。

远程 source_access 固定 standard 并标记 remote_brain；危险操作在完全访问/永久规则之前进入原有后台审批，没有可信通道或检测失败则拒绝。禁再次派单、子代理、cron、改权限/配置及全局 recall_past。独立评审用临时数据复现 recall_past 跨 Agent 检索，已关闭该工具并复核。限制不是针对恶意编码任务的完整操作系统隔离，不能把 Agent 的通用编码能力宣传为不可绕过的多租户沙箱。

历史和结果先脱敏再分页，不返回事件、原始 DB 行、附件票据；保守移除链接、明显密钥字段及疑似编码。任意自然语言里的秘密仍可能漏检，用户须接受所读文本经 OpenAI 处理。返回历史是资料，不是执行新指令。

验收命令（默认测试不下载二进制；真实 OAuth 用已核验官方固定发行物）：

```sh
VOCOCO_MCP_AUTH_TEST_BINARY="$PWD/data/mcp-auth-verify/mcp-auth-proxy-darwin-amd64" \
  uv run pytest tests/test_brain_mcp.py tests/test_brain_routes.py tests/test_brain_oauth.py \
  tests/test_mcp_auth_setup.py tests/test_mcp_probe.py tests/test_task_routes.py \
  tests/test_danger.py tests/test_danger_inert_text.py tests/test_permissions.py tests/test_delegation.py -q
```

结果：**348 passed**。任务/历史测试用临时库与假调度，不执行真实模型；官方 MCP 客户端通过真实 HTTP 桥验证派单幂等、取消、文本分页和来源权限。真实认证代理测试只在回环运行，以测试传输映射 HTTPS origin，不是公网 TLS/Cloudflare 验收：验证 discovery、DCR、错误口令、登录与授权确认、S256 PKCE（含错误 verifier）、MCP 调用、refresh、重启后 token/客户端持久化。登出后旧 token 仍有效；停代理后切换全新 OAuth 数据和密钥目录，旧 access/refresh 被拒绝，证明的是**全部授权重置**，不是标准单客户端撤销。测试结束回收全部自建进程。

准备脚本与上线清单见 [认证部署说明](../deploy/mcp-auth/README.md)。部署后的状态见下一节，不再更换 Quick Tunnel 地址。

### 10.4 生产上线验收（2026-10-27）

固定入口 **https://mcp.chjm.cc/mcp** 已启用。生产已提交合并并重启；正式 CNAME 指向专用命名隧道，认证代理/前端/隧道三个 launchd 服务运行。口令在本机随机生成，私有目录700、配置和口令600，不在 Git 或聊天中输出。原 gateway 继续承担唯一任务执行器。

公网 OAuth 登录、授权确认、S256 PKCE 和官方 MCP 客户端发现5工具通过。真实无工具/无文件写入模型任务 `0e428f10` 达到 `done` 并匹配 `MCP_LIVE_OK_87b69a155853`；另一任务 `aeac8028` 达到 `cancelled`；自测任务历史轮次 `12742` 可读。创建/完成/取消回执分别为 `37bff3bc53af448885177593f5e58491`、`ef492694762846dbb9303664a8385fa4`、`d0b6a3855a9b41d78df028ea139f5213`。

公网 MCP 无 token 401，本机桥无 token 401。Cloudflare 登录 POST 限流每IP每10秒5次，实际5次错误400后429；等待12秒正确口令POST恢复302。验收脚本 `scripts/check_brain_live.py` 已独立评审修复异常清理和POST恢复验证，4项失败路径回归通过。

本机直连新域名 DNS 曾失败，公网验收通过既有环境代理进行，TLS 校验保持开启。这不是所有网络路径可达的保证。以上是官方 MCP 客户端真实公网协议验收，**不是本人 ChatGPT UI 或实时语音验收**；后两项仍待本人操作。接入步骤见 [ChatGPT 接入说明](chatgpt-mcp-connect.md)。

## 11. 资料与证据边界

核查日期：2026-10-26。官方能力随客户端/套餐更新，以本人账号实测为准。

- [OpenAI：Build your MCP server](https://developers.openai.com/apps-sdk/build/mcp-server)：正文抓取成功；SDK、Streamable HTTP、连接路径。
- [OpenAI：Authentication](https://developers.openai.com/apps-sdk/build/auth)：正文抓取成功；OAuth 2.1、PKCE、资源发现与 token 校验。
- [OpenAI：Apps in ChatGPT](https://help.openai.com/en/articles/11487775-connectors-in-chatgpt)：正文抓取 403；搜索摘要称语音暂不支持 Apps，作为风险证据，不冒充本账号实测。
- [OpenAI：Developer mode and MCP apps](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt)：正文抓取 403；套餐/客户端开放条件待账号核实，不据搜索摘要编造具体支持矩阵。
- 本文源码行号基于本次 worktree 阅读，仅定位参考；早期只读探针及测速以§10.1–10.2为准；真实任务桥实现见§10.3，生产公网验收见§10.4，不与本人 ChatGPT 真机验收混算。
