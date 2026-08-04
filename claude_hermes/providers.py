"""设置页管理的供应商集成:从 gateway.settings_store 读取 DeepSeek/Kimi/任意第三方中转
的配置,注入 SDK options.env,实现与官方订阅之间的热切换。

说明:此模块过去依赖 cc-switch 桌面 App 写入的 ~/.claude-hermes/config.yaml。
现在已经有了自己的设置页"模型"管理界面和独立存储(data/web_settings.json),所以
把核心语义改成从 settings_store 读取。cc-switch 配置文件不再被运行时读取,仅作为
一次性迁移脚本的数据来源(见 deploy/migrate-from-cc-switch.py)。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 兜底默认模型;与 config.MODEL 的默认保持一致。
_FALLBACK_MODEL = "claude-sonnet-5"

# 官方 Anthropic 端点特征:命中即视为"走订阅",不注入第三方鉴权。
_OFFICIAL_HOSTS = ("api.anthropic.com",)
# 代表官方订阅的供应商名(旧配置里或用户手填都可能出现,兼容保留)。
_OFFICIAL_NAMES = ("claude", "official", "anthropic", "claude-official")

# Kimi(Moonshot)的订阅套餐固定走这个域名,其余第三方供应商(DeepSeek/Kimi 的常规
# API key 入口等)一律按量计费的 API 处理。
_SUBSCRIPTION_HOSTS = ("api.kimi.com",)  # Kimi Coding 订阅套餐


def hermes_config_path() -> Path:
    """旧 cc-switch 配置文件路径。

    保留这个函数是给一次性迁移脚本用;运行时不再读取该文件。
    """
    raw = os.environ.get("CLAUDE_HERMES_HOME", "").strip()
    base = Path(os.path.expanduser(raw)) if raw else Path.home() / ".claude-hermes"
    return base / "config.yaml"


@dataclass(frozen=True)
class ActiveProvider:
    """一个供应商的可用配置:名字 + 端点 + 鉴权 + 模型。

    vision=True 表示该供应商的端点支持直传图片(如 Codex OAuth 代理背后的
    GPT 系模型),注入 env 时带上 ANTHROPIC_VISION_CAPABLE 标记,vision 判定
    (core/vision.py)据此跳过 qwen-vl 转文字旁路。
    """

    name: str
    base_url: str
    api_key: str
    model: str
    vision: bool = False

    @property
    def is_official(self) -> bool:
        if self.name.lower() in _OFFICIAL_NAMES:
            return True
        if not self.base_url:
            return True
        # 解析出 host 后【精确匹配】,而非子串包含——否则
        # https://api.anthropic.com.attacker.example/ 会因含子串被误判为官方(审计 M-1 / 2-6)。
        from urllib.parse import urlsplit

        host = urlsplit(self.base_url).hostname or ""
        return host.lower() in _OFFICIAL_HOSTS


def _entry_field(entry: dict, *keys: str) -> str:
    """从供应商条目取字段,兼容 snake_case 与遗留 camelCase(baseUrl/apiKey)。"""
    for k in keys:
        v = entry.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _entry_vision(entry: dict) -> bool:
    """条目是否声明支持视觉直传(设置页勾选,存 "1"/"";兼容 true/yes/on)。"""
    return _entry_field(entry, "vision").lower() in ("1", "true", "yes", "on")


def _all_web_providers() -> dict[str, dict]:
    """从 settings_store 取所有第三方服务商条目,键=name。"""
    from .gateway import settings_store

    return settings_store.web_providers_raw()


def _env_for(provider: ActiveProvider) -> dict[str, str]:
    """把第三方供应商注入 SDK options.env;官方订阅返回 {}(用进程默认认证)。"""
    if provider.is_official or not provider.api_key:
        return {}
    # base_url 会带着 key 打过去,注入前校验 scheme:非 http(s)(如 file:// / 缺 scheme)
    # 一律不注入,免得把 key 发到诡异端点(审计 M-1 / 2-6)。
    from urllib.parse import urlsplit

    scheme = urlsplit(provider.base_url).scheme.lower()
    if scheme not in ("http", "https"):
        print(f"⚠️ 供应商 {provider.name} 的 base_url 非 http(s),已拒绝注入:{provider.base_url}")
        return {}
    return {
        "ANTHROPIC_BASE_URL": provider.base_url,
        # Claude Code CLI / claude-agent-sdk 只认 ANTHROPIC_API_KEY 作为第三方
        # Anthropic-compatible 端点的鉴权,ANTHROPIC_AUTH_TOKEN 不被识别,会
        # 导致 CLI 报 "Not logged in · Please run /login"。
        "ANTHROPIC_API_KEY": provider.api_key,
        # 清掉订阅 token,免得 CLI 拿它去打第三方端点导致 401
        "CLAUDE_CODE_OAUTH_TOKEN": "",
        # 供应商声明支持视觉(设置页勾选,如 Codex OAuth 代理背后的 GPT 系) →
        # 带标记让 core/vision.py 跳过 qwen-vl 转文字、直传原图。CLI 不认这个
        # 自定义变量,原样透传无副作用(见 core/vision.py is_vision_capable)。
        "ANTHROPIC_VISION_CAPABLE": "1" if provider.vision else "",
    }


def _provider_for_model(model: str) -> ActiveProvider | None:
    """按模型名反查它属于 web_providers 里的哪个供应商(供 /model 会话覆盖用)。"""
    for name, entry in _all_web_providers().items():
        if _entry_field(entry, "model") == model:
            return ActiveProvider(
                name=name,
                base_url=_entry_field(entry, "base_url", "baseUrl"),
                api_key=_entry_field(entry, "api_key", "apiKey"),
                model=model,
                vision=_entry_vision(entry),
            )
    return None


def resolve(chosen_model: str | None, default_model: str) -> tuple[str, dict[str, str]]:
    """算出这一轮实际用的 (模型, 要注入的 env)。

    优先级:
      1. 会话用 /model 显式选了某模型 → 用它;若该模型属于设置页里的第三方供应商,
         连带注入其 base_url + key,否则按官方(走订阅)。
      2. 没显式选 → default_model(= config.MODEL) + 订阅。

    注意:不再支持 cc-switch 的"默认激活供应商"概念。当前只用 Claude 为主、第三方
    靠 /model 显式切;若以后需要"默认走第三方",得在 settings_store 里加 active_provider。
    """
    if chosen_model:
        found = _provider_for_model(chosen_model)
        if found and not found.is_official:
            return chosen_model, _env_for(found)
        return chosen_model, {}
    return default_model, {}


def sidecar_env(name: str) -> tuple[str, dict[str, str]] | None:
    """按供应商名取 (model, 注入env),给标题总结这类轻量辅助调用做兜底。

    名字不区分大小写、允许子串匹配(配置里叫 deepseek / DeepSeek 都能命中);
    传空串 = 匹配任意第一个可用第三方(后台任务没配 DeepSeek、想兜到
    Codex/GPT 代理这类供应商时用);
    未配置 / 缺 key / 是官方端点(官方走订阅不用它兜底)→ None。
    """
    want = name.lower()
    entries = _all_web_providers()
    # 先精确后子串:防「deepseek」误命中「deepseek-pro」这种带后缀的贵档;
    # 空串时所有条目等权,取第一条可用
    ordered = sorted(entries.items(), key=lambda kv: kv[0].lower() != want)
    for pname, entry in ordered:
        if want and want not in pname.lower():
            continue
        provider = ActiveProvider(
            name=pname,
            base_url=_entry_field(entry, "base_url", "baseUrl"),
            api_key=_entry_field(entry, "api_key", "apiKey"),
            model=_entry_field(entry, "model"),
            vision=_entry_vision(entry),
        )
        if provider.is_official or not provider.api_key or not provider.model:
            continue
        return provider.model, _env_for(provider)
    return None


def has_active_third_party() -> bool:
    """当前是否配置了可用的第三方供应商(带 key)。

    决定 config.py 启动时是否允许不填 CLAUDE_CODE_OAUTH_TOKEN(只有第三方可用时
    才免,否则缺订阅 token 直接起不来)。
    """
    for entry in _all_web_providers().values():
        if _entry_field(entry, "api_key"):
            provider = ActiveProvider(
                name="x",
                base_url=_entry_field(entry, "base_url", "baseUrl"),
                api_key=_entry_field(entry, "api_key", "apiKey"),
                model=_entry_field(entry, "model"),
                vision=_entry_vision(entry),
            )
            if not provider.is_official:
                return True
    return False


def load_active() -> ActiveProvider | None:
    """返回第一个可用的第三方供应商(历史兼容函数,保留给少数老调用点用)。

    旧语义是"cc-switch 当前激活的供应商";现在没有激活位的概念,改成返回任意一个
    可用的第三方供应商(展示/打码用,不影响实际模型选择)。
    """
    for name, entry in _all_web_providers().items():
        api_key = _entry_field(entry, "api_key", "apiKey")
        base_url = _entry_field(entry, "base_url", "baseUrl")
        model = _entry_field(entry, "model")
        provider = ActiveProvider(
            name=name, base_url=base_url, api_key=api_key, model=model,
            vision=_entry_vision(entry),
        )
        if not provider.is_official and api_key:
            return provider
    return None


def _billing_kind(base_url: str) -> str:
    """按 base_url 的 host 判断是订阅还是按量 API;非已知订阅域名默认按 API。"""
    from urllib.parse import urlsplit

    host = (urlsplit(base_url).hostname or "").lower()
    return "订阅" if host in _SUBSCRIPTION_HOSTS else "API"


def available_models(
    default_choices: list[tuple[str, str]],
) -> list[tuple[str, str, str]]:
    """/model 无参时的候选:官方默认档 + 设置页手动加的档位 + 设置页里的各供应商模型。

    default_choices 是代码里写死的官方档(claude-opus/sonnet/haiku),恒列在前;设置页
    可以把其中某几档隐藏掉(disabled_builtin_models,不动代码常量,只摘出选择器)。
    web_extra_models(设置页手动补录,见 gateway.settings_store)紧随其后——同样按
    "官方订阅"处理,不需要 key,用来填新模型发布但代码还没来得及加的空窗期。
    标签只留"模型名（订阅/API）",不带供应商名,免得面板换行/信息过载。
    第三个元素是分组:anthropic(官方订阅) / kimi(Kimi 订阅) / api(按量计费),
    供 WebUI 模型面板分组展示 + 决定要不要查订阅额度。
    """
    from .gateway import settings_store

    disabled = set(settings_store.list_disabled_builtin_models())
    extra = [
        (m["id"], m.get("label") or m["id"])
        for m in settings_store.list_web_extra_models()
        if m.get("id")
    ]
    out: list[tuple[str, str, str]] = [
        (mid, label, "anthropic") for mid, label in default_choices if mid not in disabled
    ]
    seen = {mid for mid, _, _ in out}
    for mid, label in extra:
        if mid in seen:
            continue
        seen.add(mid)
        out.append((mid, label, "anthropic"))
    for name, entry in _all_web_providers().items():
        if name.lower() in _OFFICIAL_NAMES:
            continue
        model = _entry_field(entry, "model")
        if not model or model in seen or not _entry_field(entry, "api_key", "apiKey"):
            continue
        seen.add(model)
        base_url = _entry_field(entry, "base_url", "baseUrl")
        kind = _billing_kind(base_url)
        # 配了 mgmt_key 的本地 Codex 代理 → 单独 codex 组(订阅额度可查,前端有圆环)
        if _entry_field(entry, "mgmt_key"):
            group = "codex"
        else:
            group = "kimi" if kind == "订阅" else "api"
        out.append((model, f"{model}（{kind}）", group))
    return out


def lookup_provider_by_model(model: str) -> dict | None:
    """按模型名查设置页里的供应商配置条目;未配置(官方模型)返回 None。"""
    for entry in _all_web_providers().values():
        if _entry_field(entry, "model") == model:
            return dict(entry)
    return None


def is_subscription_host(base_url: str) -> bool:
    """base_url 的 host 是否属于已知订阅套餐供应商(如 Kimi Coding)。"""
    from urllib.parse import urlsplit

    host = (urlsplit(base_url).hostname or "").lower()
    return host in _SUBSCRIPTION_HOSTS


def subscription_api_key_for_model(model: str) -> str | None:
    """model 对应的供应商若是已知订阅套餐(如 Kimi Coding)主机,返回其 api_key
    (可能是空串,表示配置了但没填 key);不是订阅供应商(未配置/按量计费 API)返回 None。
    """
    entry = lookup_provider_by_model(model)
    if entry is None:
        return None
    base_url = _entry_field(entry, "base_url", "baseUrl")
    if not base_url or not is_subscription_host(base_url):
        return None
    return _entry_field(entry, "api_key", "apiKey")


def codex_mgmt_for_model(model: str) -> tuple[str, str] | None:
    """model 对应的供应商若是本地 Codex OAuth 代理(条目配了 mgmt_key),返回
    (mgmt_key, base_url);否则 None。

    只认 127.0.0.1/localhost 的 base_url——mgmt_key 是本地代理的管理钥匙,
    绝不发给任意远程端点。
    """
    entry = lookup_provider_by_model(model)
    if entry is None:
        return None
    base_url = _entry_field(entry, "base_url", "baseUrl")
    mgmt_key = _entry_field(entry, "mgmt_key")
    if not base_url or not mgmt_key:
        return None
    from urllib.parse import urlsplit

    host = (urlsplit(base_url).hostname or "").lower()
    if host not in ("127.0.0.1", "localhost"):
        return None
    return mgmt_key, base_url.rstrip("/")


async def codex_usage(mgmt_key: str, base_url: str) -> dict:
    """查本地 Codex OAuth 代理背后的 GPT 订阅额度。

    链路:代理 Management API 的 auth-files 拿账号(auth_index + chatgpt_account_id),
    再经 api-call 把请求转发到 chatgpt.com/backend-api/wham/usage(代理持有的
    登录会话自带 Cloudflare 通行能力,直接 curl 会被挑战页拦)。Authorization 头
    用 $TOKEN$ 占位符,代理自动替换成账号的真实 token。

    返回统一成现有 five_hour 结构(utilization 0-1 / resets_at 秒级 unix),
    resetLabel/圆环前端零改动;plan_type/credits 放顶层给 tooltip 用。
    """
    import aiohttp

    async with aiohttp.ClientSession() as session:
        # 1) auth-files:拿第一个 oauth 账号的 auth_index + account_id
        async with session.get(
            f"{base_url}/v0/management/auth-files",
            headers={"X-Management-Key": mgmt_key},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                txt = await resp.text()
                return {"provider": "codex", "error": f"auth-files HTTP {resp.status}: {txt[:200]}"}
            files = (await resp.json()).get("files", [])
        auth_index = account_id = ""
        for f in files:
            if f.get("account_type") == "oauth" and f.get("auth_index"):
                auth_index = f["auth_index"]
                account_id = (f.get("id_token") or {}).get("chatgpt_account_id") or ""
                break
        if not auth_index:
            return {"provider": "codex", "error": "代理里没有可用的 Codex oauth 账号"}
        # 2) api-call 转发 wham/usage
        header = {
            "Authorization": "Bearer $TOKEN$",
            "Content-Type": "application/json",
            # 带完整浏览器 UA 才能过 Cloudflare 挑战
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
        }
        if account_id:
            header["Chatgpt-Account-Id"] = account_id
        async with session.post(
            f"{base_url}/v0/management/api-call",
            headers={"X-Management-Key": mgmt_key},
            json={
                "authIndex": auth_index,
                "method": "GET",
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "header": header,
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                return {"provider": "codex", "error": f"api-call HTTP {resp.status}"}
            data = await resp.json()
        status = data.get("status_code")
        if status != 200:
            return {"provider": "codex", "error": f"wham/usage HTTP {status}"}
        body = data.get("body") or {}
        # api-call 的 body 有时是 JSON 字符串而非对象(aiohttp 与 curl 表现不同),统一转对象
        if isinstance(body, str):
            import json as _json

            try:
                body = _json.loads(body)
            except Exception:
                return {"provider": "codex", "error": f"wham/usage body 解析失败: {body[:200]}"}

    rl = body.get("rate_limit") or {}
    primary = rl.get("primary_window") or {}
    used_pct = float(primary.get("used_percent") or 0)
    reached = bool(rl.get("limit_reached"))
    five_hour = {
        "utilization": min(1.0, used_pct / 100),
        "resets_at": primary.get("reset_at"),
        "status": "rejected" if reached else "allowed",
    }
    return {
        "provider": "codex",
        "plan_type": body.get("plan_type"),
        "credits": body.get("credits") or {},
        "limits": {"five_hour": five_hour},
    }


async def kimi_usage(api_key: str) -> dict:
    """调 Kimi Code 订阅的用量查询 API,返回 {"provider":"kimi","limits":{"five_hour":{...}}}。"""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://api.kimi.com/coding/v1/usages",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                txt = await resp.text()
                return {"provider": "kimi", "error": f"HTTP {resp.status}: {txt[:200]}"}
            data = await resp.json()

    # Kimi 返回:usage 是本月累计(前端用量面板暂不展示),limits 是各窗口明细(含 5h)
    five_hour = {}
    limits_raw = data.get("limits", [])
    for lim in limits_raw:
        if lim.get("window", {}).get("duration") == 300:
            detail = lim.get("detail", {})
            limit = int(detail.get("limit", 0) or 0)
            used = int(detail.get("used", 0) or 0)
            remaining = max(0, limit - used)
            pct = 0.0
            if limit > 0:
                pct = min(1.0, used / limit)
            five_hour = {
                "status": "rejected" if remaining <= 0 else "allowed_warning" if pct >= 0.8 else "allowed",
                "utilization": pct,
                "resets_at": detail.get("resetTime", ""),
                "limit": limit,
                "remaining": remaining,
            }
            break

    return {
        "provider": "kimi",
        "limits": {"five_hour": five_hour},
    }
