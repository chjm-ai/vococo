# MCP OAuth 代理：安全部署准备

本目录提供准备脚本的模板和上线清单。**未安装生产服务、配置 DNS 或创建隧道。** 不读写 vococo 生产 DB，不自动读取 HOME 配置，不添加依赖。另已将官方发行二进制核验后用于临时回环 OAuth 测试，测试服务已回收，不等于生产上线。

## 固定架构

```text
https://mcp.chjm.cc/mcp
  → Cloudflare 独立命名隧道（未来配置，TLS 在 Cloudflare 终止）
  → http://127.0.0.1:8853/mcp（sigbit/mcp-auth-proxy v2.10.2，darwin-amd64）
  → http://127.0.0.1:8852/mcp（已有 MCP frontend）
```

由成熟代理负责 OAuth 和口令登录，不自行实现 OAuth。按 v2.10.2 源码核实：

- `EXTERNAL_URL=https://mcp.chjm.cc`：必须是源站 URL，**不能带 `/mcp`**，否则代理拒绝启动。
- 代理目标参数为 `http://127.0.0.1:8852`：Go 的透明反向代理会保留传入 `/mcp`；再次附加会变成 `/mcp/mcp`。
- `NO_AUTO_TLS=true`、`LISTEN=127.0.0.1:8853`，禁止公网直连本地监听。
- `PASSWORD_HASH` 为 bcrypt cost 12；没有明文 `PASSWORD`。
- `PROXY_BEARER_TOKEN` 是 frontend 使用的**已有共享 token**，不是 OAuth token。脚本交互输入，不扫描/修改上游配置；上线前人工确认两端一致。
- `PROXY_FORWARD_AUTHORIZATION=false`：OAuth token 不透传上游，以共享 token 替代。
- `DATA_PATH=<私有目录>/data`、`REPOSITORY_BACKEND=local`：代理首次启动自行创建持久 OAuth 数据、`secret` 和 `private_key.pem`。脚本不生成 OAuth 密钥，不设置覆盖文件密钥的 `AUTH_HMAC_SECRET`/`JWT_PRIVATE_KEY`。

上游源码依据（均固定 v2.10.2）：
[配置](https://github.com/sigbit/mcp-auth-proxy/blob/v2.10.2/docs/docs/configuration.md)、
[启动/持久化](https://github.com/sigbit/mcp-auth-proxy/blob/v2.10.2/pkg/mcp-proxy/main.go)、
[路径拼接](https://github.com/sigbit/mcp-auth-proxy/blob/v2.10.2/pkg/backend/transparent.go)、
[口令登录](https://github.com/sigbit/mcp-auth-proxy/blob/v2.10.2/pkg/auth/auth.go)。

## 只准备（用户本地终端）

以下命令仅供用户之后操作，本次不执行。选择**仓库外**新目录，父目录必须已存在、属于当前用户且其他用户不可写。路径不能经过软链接（macOS `/tmp` 等路径需使用真实路径）。没有默认写入 HOME 的位置。

```sh
# 按实际情况替换；不要把凭据目录放入 Git 或同步盘。
PRIVATE_DIR='/绝对路径/私有父目录/mcp-auth'
.venv/bin/python scripts/setup_mcp_auth.py prepare --private-dir "$PRIVATE_DIR"
```

口令由 `getpass` 隐藏输入并重复确认，UTF-8 长度 16–72 字节。优先使用环境已有 bcrypt；否则使用 macOS `/usr/sbin/htpasswd -niBC 12`，明文仅经 stdin 传入，不放 argv、日志或仓库。没有两者则明确失败，不自动装包。token 也隐藏输入，要求至少32字符的 bearer token 字符集。不要录制终端或用 shell 调试输出凭据。

生成文件：`proxy.env`、`run-proxy.sh`、`cc.chjm.mcp-auth.plist`、`prepared.json`，均为600；目录与 `data/` 为700。plist 不内嵌 secret，使用 `env -i` 清除继承环境，shell 启动器仅加载私有 env。plist/启动器设置 umask 077，代理新建的数据文件不会暴露给其他系统用户。标准输出/错误默认丢弃，避免代理访问日志泄漏授权链接、查询串或认证信息；诊断不能直接在共享终端开启完整请求日志。

重复准备会校验权限、文件摘要、模板与路径，不重新提示、不重写凭据、不改持久数据。同名 launchd 服务已加载则拒绝操作。已有陌生/半完成目录、被手动修改的配置、软链接/硬链接或错误权限均失败，不自动修复/覆盖。失败中途可能留下私有半成品；应由用户检查后决定如何清理，脚本不会删除已有目录。未加载但已存在 HOME 下的服务文件不会被扫描，也不会被本脚本覆盖；之后人工安装仍需排查同名配置。

## 可选：只下载和核验（不执行）

本阶段需要用户环境已有 `gh` 且能访问公开 GitHub API。`gh` 使用其自身认证环境；脚本不打开其 HOME 配置文件。

```sh
.venv/bin/python scripts/setup_mcp_auth.py download --private-dir "$PRIVATE_DIR"
```

只接受 GitHub 官方固定 tag `v2.10.2` 的唯一 `mcp-auth-proxy-darwin-amd64` asset，以及官方 `digest=sha256:<64位十六进制>`。先取元数据，再流式下载，逐字节计算 SHA-256 比较。**缺少 digest、版本/URL 不符、摘要不符、空文件或超过128MiB均失败，不能称已核验。** 这是 GitHub 发布摘要的完整性核验，不是独立签名/构建来源证明。

核验通过才以不覆盖方式保存 `<私有目录>/mcp-auth-proxy`，权限保持600，不执行、不 `chmod +x`。重复下载会重新获取官方 digest 并校验已有文件；摘要不同绝不覆盖。已由用户改成700的同摘要二进制也可校验。下载中断不会保存为正式文件，普通异常清理临时文件；强制终止留下的 `.download-*` 仅可由用户检查后删除。

## 外网开放的硬前提：Cloudflare 登录限流

v2.10.2 的登录 handler 与路由初始化中**未发现内置登录尝试限流**。不能仅靠 bcrypt/长口令抵抗尝试，也不能让隧道绕过边缘规则。

在启动隧道/添加公网 DNS 之前，必须在 Cloudflare 建立并启用限流规则：

```text
(http.host eq "mcp.chjm.cc" and
 http.request.uri.path eq "/.auth/login" and
 http.request.method eq "POST")
```

建议起点：按真实客户端 IP 每60秒最多5次，超过后阻断至少10分钟；套餐不支持该周期/动作时，选择可用的同等或更严格阈值，或升级/换用有成熟限流的入口，**不可无防护上线**。这会限制所有登录提交（并非仅失败），共享出口可能误伤，需实测调整。另评估注册和 token 端点的滥用规则，避免阻断正常 OAuth/MCP 流量。

必须实际测试：低于阈值能登录，超过阈值被边缘拒绝，恢复后可登录；检查 Cloudflare 安全事件，并确认没有其他域名/路由直通8853。脚本只告警，不会创建规则，也不会把人工确认当作已验证。不要把 Cloudflare Access 交互登录加到整个 MCP 域名，避免破坏 OAuth 客户端发现和回调。

## MCP 前端与原 gateway 的凭据（生产未配置）

真实前端入口为 `python -m vococo.gateway.brain_mcp`。两个随机凭据必须不同，且至少32字符：

| 配置位置 | 内容 |
|---|---|
| 代理 `proxy.env` 的 `PROXY_BEARER_TOKEN` | 上游共享 token A |
| 前端私有 `frontend.env` 的 `VOCOCO_MCP_UPSTREAM_TOKEN` | 同一 token A |
| 前端私有 `frontend.env` 的 `VOCOCO_BRAIN_BRIDGE_TOKEN` | 独立桥 token B |
| 原 vococo 进程环境的 `VOCOCO_BRAIN_BRIDGE_TOKEN` | 同一 token B |

token 不写聊天、命令参数或 Git。`frontend.env` 位于仓库外私有目录、权限600；不要让代理加载桥 token B。`run-frontend.sh.in` 是前端启动模板：人工用经过 shell 引号转义的绝对路径替换 `@FRONTEND_ENV@`、`@PYTHON@`，后者必须指向**已部署主代码**的 Python，不是会被清理的 worktree。该模板不由准备脚本自动生成；同样采用清空继承环境、私有 umask 的独立 launchd 配置，不能简单以 `source` 加载整个生产 `.env`。

原 gateway 重启后才会加载新桥代码/环境。缺少安全配置时返回503，不回退使用全权 Web token。不能把8848直接代理到新域名；固定域名只接8853。生产启动顺序为原 gateway → 前端 → 认证代理 → 已有登录限流的独立隧道，逐层测试后再启用公网。

## 后续人工安装/验收（本次未执行）

1. 检查 frontend 仅监听 `127.0.0.1:8852` 且必须校验相同共享 token；确认8853未占用。
2. 检查当前用户 launchd 是否已有 `cc.chjm.mcp-auth`；人工检查拟使用的 LaunchAgents 目标，已有则停下，禁止覆盖。本脚本只输出 plist 到私有目录，不写 LaunchAgents。
3. 经用户批准后才能把核验的二进制改为700、加载输出的 plist。可直接针对私有目录中的 plist 人工 `launchctl bootstrap`，不必复制任何 secret 到 plist；这一步会实际执行代理，超出准备脚本职责。
4. 首次启动后检查 `data/`、密钥与 OAuth 数据文件为700/600；保留整个私有目录，不能放在 worktree、临时盘或每次重建的目录。
5. 边缘限流配置完成并验证后，才将独立命名隧道的 **整个 `mcp.chjm.cc` 域名** 接到 `http://127.0.0.1:8853`，其余 ingress 返回404。不能仅转发 `/mcp`，还需要 OAuth 发现、登录、注册与授权端点。隧道凭据需独立私有保存，不能放仓库。脚本不创建隧道/DNS，也不修改现有隧道配置。
6. 真实客户端验收完整授权码+PKCE登录、MCP initialize/tools/list、错误口令、过期凭据、断线重连、重启后持久化、边缘限流及共享 token 不匹配。**准备阶段隔离测试不等于通过 OAuth 2.1 互操作/安全审计。**

## 更新、备份与撤销边界

- 准备脚本固定版本，没有自动升级/覆盖/密钥轮换功能；更新失败应停下，不通过删 `data/` 解决。二进制更新需另行审核新版本、停服务并备份，在同一个 DATA_PATH 上保留已有 `secret`、`private_key.pem` 和 OAuth 数据；不要重跑初始化或随意切换到空数据目录。
- 完整停服务备份整个私有目录，备份也必须私有且按凭据保管；恢复必须保留路径一致或人工审核所有引用。脚本不读取或备份生产 DB。
- **没有标准单客户端 token revocation，改口令/登出不等于撤销已发 token。** 回环真实代理测试确认登出后 access token 仍有效。紧急隔离先停隧道/代理，再私有备份**整个** DATA_PATH；需全部撤销时，经确认使用全新空 DATA_PATH 启动，让代理重建签名/HMAC密钥和 OAuth库，并保持固定外部 URL。测试确认此时旧 access 与 refresh 均被拒绝，所有客户端必须重新连接。禁止只删某个数据库文件或仅改密码冒充撤销；恢复旧备份可能恢复旧授权，须按密钥保管。生产路径变更/启动器及 prepared.json 摘要更新需人工一致审核，准备脚本不会覆盖已存在配置，也未实测生产重置。

## 隔离测试

```sh
.venv/bin/python -m pytest tests/test_mcp_auth_setup.py -q
```

测试只使用临时目录、固定公开测试口令和模拟网络；系统 htpasswd 测试只哈希公开测试字符串。没有真实下载，没有执行下载物，没有加载 launchd、访问生产 DB 或 HOME 配置。
