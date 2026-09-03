"""设置页管理的供应商集成:从 gateway.settings_store 读取 DeepSeek/Kimi/任意第三方中转
的配置,注入 SDK options.env,实现与官方订阅之间的热切换。

说明:此模块过去依赖 cc-switch 桌面 App 写入的 ~/.claude-hermes/config.yaml。
现在已经有了自己的设置页"模型"管理界面和独立存储(data/web_settings.json),所以
把核心语义改成从 settings_store 读取。cc-switch 配置文件不再被读取,一次性迁移
脚本(gateway/migrate_cc_switch.py)已于 2026-08 随死代码清理删除(git 历史可查)。
"""
from __future__ import annotations

from dataclasses import dataclass

# 兜底默认模型;与 config.MODEL 的默认保持一致。
_FALLBACK_MODEL = "claude-sonnet-5"

# 官方 Anthropic 端点特征:命中即视为"走订阅",不注入第三方鉴权。
_OFFICIAL_HOSTS = ("api.anthropic.com",)
# 代表官方订阅的供应商名(旧配置里或用户手填都可能出现,兼容保留)。
_OFFICIAL_NAMES = ("claude", "official", "anthropic", "claude-official")

# Kimi(Moonshot)的订阅套餐固定走这个域名,其余第三方供应商(DeepSeek/Kimi 的常规
# API key 入口等)一律按量计费的 API 处理。
_SUBSCRIPTION_HOSTS = ("api.kimi.com",)  # Kimi Coding 订阅套餐

# 订阅令牌探活用的模型:挑最便宜的 haiku,配 max_tokens=1,一次开销可忽略。
PROBE_MODEL = "claude-haiku-4-5-20251001"


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


def _provider_entry_by_name(entries: dict[str, dict], name: str) -> tuple[str, dict] | None:
    """按名称取供应商条目;名称不区分大小写,兼容手填配置。"""
    for entry_name, entry in entries.items():
        if entry_name.lower() == name.lower():
            return entry_name, entry
    return None


def _provider_entry_for_model(model: str) -> tuple[str, dict] | None:
    """按模型反查供应商;额外模型可通过 provider 复用一个代理配置。"""
    from .gateway import settings_store

    entries = _all_web_providers()
    for name, entry in entries.items():
        if _entry_field(entry, "model") == model:
            return name, entry
    for extra in settings_store.list_web_extra_models():
        if extra.get("id") != model:
            continue
        provider_name = extra.get("provider")
        if isinstance(provider_name, str) and provider_name.strip():
            return _provider_entry_by_name(entries, provider_name.strip())
    return None


def _provider_for_model(model: str) -> ActiveProvider | None:
    """按模型名反查供应商,供 /model 会话覆盖和额外模型路由使用。"""
    found = _provider_entry_for_model(model)
    if found is None:
        return None
    name, entry = found
    return ActiveProvider(
        name=name,
        base_url=_entry_field(entry, "base_url", "baseUrl"),
        api_key=_entry_field(entry, "api_key", "apiKey"),
        model=model,
        vision=_entry_vision(entry),
    )


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
    # 默认模型同样反查:若属于第三方供应商(如 .env 的 AGENT_MODEL 直接配
    # deepseek-v4-flash),也注入其 base_url + key,否则按官方(走订阅)。
    # 2026-08-04:Claude 官方订阅账号被封后,主服务默认模型切第三方必需此分支。
    found = _provider_for_model(default_model)
    if found and not found.is_official:
        return default_model, _env_for(found)
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


def probe_subscription_token(token: str, *, timeout: float = 10) -> tuple[str, str]:
    """探活订阅令牌:发一个 max_tokens=1 的最小请求,看官方认不认。

    返回 (state, detail),state 三态:
      - "ok"      认证通过,官方模型可用
      - "bad"     明确被拒(401/403),令牌失效/被吊销,detail 带服务端原话
      - "unknown" 没结论(网络不通、限流、服务端 5xx),不足以判定令牌坏了

    三态是刻意的:网络抖动跟令牌被吊销必须分开,否则 doctor 一断网就报假 ❌。429 也
    归 unknown——限流说明认证其实已经过了。

    刻意用 stdlib urllib 而不是本模块其余地方的 aiohttp:唯一调用方 doctor 是同步的,
    为一次性探活起个事件循环不划算。

    2026-08-16 加。在此之前 doctor 只判断 token 非空就报 ✅「已配置(走订阅)」,
    从不验活。.env 里的令牌被吊销("OAuth access token has been revoked")后整整五天
    没人发现:官方模型是唯一踩这个令牌的路径,日常对话默认走第三方端点,后台任务在
    core/task_runner.py 里还会自动回落 DeepSeek,失效全程静默。
    """
    import json
    import urllib.error
    import urllib.request

    if not token.strip():
        return "bad", "未配置"
    body = json.dumps(
        {
            "model": PROBE_MODEL,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {token.strip()}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return ("ok", f"HTTP {resp.status}") if resp.status == 200 else (
                "unknown", f"HTTP {resp.status}"
            )
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001 —— 拿不到详情就只报状态码
            pass
        suffix = f" · {detail}" if detail else ""
        if e.code in (401, 403):
            return "bad", f"HTTP {e.code}{suffix}"
        return "unknown", f"HTTP {e.code}{suffix}"
    except Exception as e:  # noqa: BLE001 —— 网络层失败一律 unknown,不冤枉令牌
        return "unknown", f"{type(e).__name__}: {e}"


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
    web_extra_models(设置页手动补录,见 gateway.settings_store)紧随其后——未指定
    provider 时按官方订阅处理;指定 provider 时复用对应第三方的连接配置。
    标签只留"模型名（订阅/API）",不带供应商名,免得面板换行/信息过载。
    第三个元素是分组:anthropic(官方订阅) / kimi(Kimi 订阅) / api(按量计费),
    供 WebUI 模型面板分组展示 + 决定要不要查订阅额度。
    """
    from .gateway import settings_store

    disabled = set(settings_store.list_disabled_builtin_models())
    entries = _all_web_providers()
    out: list[tuple[str, str, str]] = [
        (mid, label, "anthropic") for mid, label in default_choices if mid not in disabled
    ]
    seen = {mid for mid, _, _ in out}
    for extra in settings_store.list_web_extra_models():
        mid = extra.get("id")
        if not mid or mid in seen:
            continue
        provider_name = extra.get("provider")
        if provider_name:
            if not isinstance(provider_name, str):
                continue
            found = _provider_entry_by_name(entries, provider_name)
            if found is None or not _entry_field(found[1], "api_key", "apiKey"):
                continue
        seen.add(mid)
        out.append((mid, extra.get("label") or mid, extra.get("group") or "anthropic"))
    for name, entry in entries.items():
        if name.lower() in _OFFICIAL_NAMES:
            continue
        model = _entry_field(entry, "model")
        if not model or model in seen or not _entry_field(entry, "api_key", "apiKey"):
            continue
        seen.add(model)
        base_url = _entry_field(entry, "base_url", "baseUrl")
        kind = _billing_kind(base_url)
        # 配了 mgmt_key 的本地 Codex 代理 → 单独 codex 组(订阅额度可查,前端有圆环),
        # 标签按订阅展示——host 是 127.0.0.1 猜不出计费模式,但 mgmt_key 声明了是订阅上游
        if _entry_field(entry, "mgmt_key"):
            group = "codex"
            kind = "订阅"
        else:
            group = "kimi" if kind == "订阅" else "api"
        out.append((model, f"{model}（{kind}）", group))
    # 组序 + 组内档位统一排序:GPT-5.6 系列按能力降序(Sol 旗舰 > Terra 均衡 >
    # Luna 轻量)。sol/luna 是设置页手加的 extra、terra 是 provider 主模型,不排
    # 就成了 sol→luna→terra,弱档 Luna 插到中间;未知 GPT 模型档位取 99 沉底,
    # 非 codex 组 key 全 0 靠稳定排序保持原相对顺序。
    _GROUP_ORDER = {"anthropic": 0, "kimi": 1, "codex": 2, "api": 3}
    _GPT_TIERS = {"gpt-5.6-sol": 0, "gpt-5.6-terra": 1, "gpt-5.6-luna": 2}

    out.sort(
        key=lambda item: (
            _GROUP_ORDER.get(item[2], 9),
            _GPT_TIERS.get(item[0].lower(), 99) if item[2] == "codex" else 0,
        )
    )
    return out


def normalize_model_text(text: str) -> str:
    """自然语言里的模型名归一化:小写、去掉空白与 .-_/· 等常见分隔符。

    "claude opus 4.6" / "Opus-4.6" / "opus4.6" 归一后都是 "claudeopus46",
    供 switch_model 这类自然语言入口与候选模型 id 做全等/包含匹配。
    纯文本工具,不做语义判断;空输入返回空串。
    """
    import re

    return re.sub(r"[\s\-_./·（）()]+", "", (text or "").strip().lower())


def match_models_by_text(
    text: str, candidates: list[tuple[str, str, str]]
) -> list[tuple[str, str, str]]:
    """按用户口语在候选模型里找命中的 (id, label, group),保持候选原顺序。

    text 是用户原话/别名(如 "opus 4.6"、"kimi k3"、"deepseek"),candidates 形如
    available_models() 的返回值。规则:输入归一化后与某候选 id 归一化形式【全等】
    的排最前(用户直接说了规范 id 的场景);其余按"候选归一化串包含输入归一化串"
    过滤(唯一子串也算命中),零命中返回 []。歧义(多个候选都包含)不在这里消解,
    由调用方把列表交回给用户澄清——绝不擅自替用户猜。
    """
    want = normalize_model_text(text)
    if not want:
        return []
    normed = [
        (mid, label, group, normalize_model_text(mid))
        for mid, label, group in candidates
    ]
    exact = [c[:3] for c in normed if c[3] == want]
    if exact:
        return exact
    return [c[:3] for c in normed if want in c[3]]


def lookup_provider_by_model(model: str) -> dict | None:
    """按模型名查供应商配置;额外模型会返回其复用服务商的连接信息。"""
    found = _provider_entry_for_model(model)
    if found is None:
        return None
    _, entry = found
    return {**entry, "model": model}


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


# Claude Code SDK 支持的完整档位。DeepSeek / Kimi 等 Anthropic-compatible 端点
# 当前只确认兼容 high/max；不能因为本地 GPT 代理支持五档就盲目把未知参数传过去。
# 显示名与档位 id 一致(英文),与 GPT 侧档位命名对齐。
_FULL_EFFORT_CHOICES: tuple[tuple[str, str], ...] = (
    ("low", "low"),
    ("medium", "medium"),
    ("high", "high"),
    ("xhigh", "xhigh"),
    ("max", "max"),
)
_COMPAT_EFFORT_CHOICES: tuple[tuple[str, str], ...] = (
    ("high", "high"),
    ("max", "max"),
)


def effort_choices_for_model(model: str) -> tuple[tuple[str, str], ...]:
    """返回模型实际可在 Web 端选择的思考深度(id, 显示名, 现与 id 一致)。

    官方 Claude 与本地 Codex/GPT 代理走 Claude Code 的完整五档。普通第三方
    Anthropic-compatible 端点则保守沿用已经验证的 high/max，避免 Kimi、DeepSeek
    等服务商收到其未声明支持的 low/medium/xhigh 参数。
    """
    provider = _provider_for_model(model)
    if (
        provider is None
        or provider.is_official
        or model.lower().startswith("gpt-")
        or codex_mgmt_for_model(model) is not None
    ):
        return _FULL_EFFORT_CHOICES
    return _COMPAT_EFFORT_CHOICES


def effort_levels_for_model(model: str) -> tuple[str, ...]:
    """只返回可用档位 id，供运行时校验已持久化的选择。"""
    return tuple(level for level, _ in effort_choices_for_model(model))


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


# ─── 用量查询(/api/usage 的数据源)─────────────────────────────────────
async def usage_for_model(model: str | None) -> tuple[dict, int]:
    """按模型查订阅配额,返回 (payload, http_status);Web 层只做 json_response。

    Claude 订阅:SDK 流式回复的 RateLimitEvent 缓存(官方精确值),utilization 缺失
    时合并本地日志估算兜底,确保始终有具体百分比。Kimi 订阅:主动调
    api.kimi.com/coding/v1/usages。Codex OAuth 代理(本地,GPT 订阅):经代理
    management api-call 转发 chatgpt.com/backend-api/wham/usage。
    API 按量计费(DeepSeek 等):{"provider":"api","type":"api"}。
    """
    from . import config

    model = (model or "").strip()
    if not model:
        model = resolve(None, config.MODEL)[0]

    # Kimi 订阅(api.kimi.com);web_providers 条目的 snake_case/camelCase 归一化
    # 已在 subscription_api_key_for_model 内做完,这里拿到的就是规整好的 api_key。
    api_key = subscription_api_key_for_model(model)
    if api_key is not None:
        if api_key:
            try:
                return await kimi_usage(api_key), 200
            except Exception as ex:
                return {"provider": "kimi", "error": str(ex)}, 502
        return {"provider": "kimi", "type": "api"}, 200

    # Codex OAuth 代理(本地,GPT 订阅):经代理 management api 转发查额度
    mgmt = codex_mgmt_for_model(model)
    if mgmt is not None:
        try:
            return await codex_usage(*mgmt), 200
        except Exception as ex:
            return {"provider": "codex", "error": str(ex)}, 502

    # Claude 官方订阅:优先用 SDK 缓存的 RateLimitEvent(官方精确值),
    # 若 utilization 缺失则合并本地日志估算值作兜底。
    p_entry = lookup_provider_by_model(model)
    if not p_entry or p_entry.get("name", "").lower() in _OFFICIAL_NAMES:
        # 懒加载:core.agent 依赖本模块,顶层 import 会成环
        from .core.agent import get_rate_limits
        from .gateway.adapters.usage_local import get_local_claude_usage

        official = get_rate_limits()
        local = await get_local_claude_usage()

        five_off = (official.get("five_hour") or {}) if isinstance(official, dict) else {}
        five_loc = (local.get("limits", {}).get("five_hour") or {}) if local else {}

        # 官方有利用率就用官方的;否则退回本地估算,但要明确标注来源
        source = "official"
        if five_off.get("utilization") is not None:
            merged = dict(five_off)
        elif local:
            merged = dict(five_loc)
            source = "local_estimate"
        else:
            merged = dict(five_off)

        # resets_at 两边可能都有,取官方优先
        if not merged.get("resets_at") and five_off.get("resets_at"):
            merged["resets_at"] = five_off["resets_at"]
        if not merged.get("resets_at") and five_loc.get("resets_at"):
            merged["resets_at"] = five_loc["resets_at"]

        # 7d 窗口同样合并
        seven_off = (official.get("seven_day") or {}) if isinstance(official, dict) else {}
        seven_loc = (local.get("limits", {}).get("seven_day") or {}) if local else {}
        if seven_off.get("utilization") is not None:
            merged_seven = dict(seven_off)
            seven_source = "official"
        elif local:
            merged_seven = dict(seven_loc)
            seven_source = "local_estimate"
        else:
            merged_seven = dict(seven_off)
            seven_source = None

        payload: dict = {
            "provider": "claude",
            "source": source,
            "limits": {"five_hour": merged, "seven_day": merged_seven},
        }
        if local:
            # 本地估算详情给前端 hover 卡片用
            payload["local"] = local.get("local")
            payload["forecast"] = local.get("forecast")
            payload["pace"] = local.get("pace")
            payload["local_history"] = local.get("local_history")
            payload["confidence"] = local.get("confidence")

        # 标注 7d 数据来源(如果存在)
        if merged_seven:
            merged_seven["source"] = seven_source
        merged["source"] = source

        return payload, 200

    # 其他(DeepSeek/Moonshot API 等):按量计费,无配额
    return {"provider": "api", "type": "api"}, 200


async def sidecar_chat(prompt: str, *, timeout: float = 30) -> str | None:
    """轻量一次性纯文本补全:没有工具、没有系统提示包,不是完整 Agent 会话,只是
    "把一段文字丢给已配置的 DeepSeek 拿一句总结/润色"这类场景用(如 cron 脚本
    任务模式的结果总结,见 cron/scheduler.py)——一次完整 Agent 会话哪怕文本很短
    也要重付系统提示+工具定义的打包成本,这种场景不值得。

    走 DeepSeek 原生 OpenAI 兼容端点(跟 memory/people_profiles.py._chat_json
    同一账号同一 key,只是这里不要求 JSON 输出)。没配置 DeepSeek 或调用失败都
    返回 None,调用方自己决定怎么兜底(通常是回退到原始文本,不阻塞主流程)。
    """
    fallback = sidecar_env("deepseek")
    if fallback is None:
        return None
    _, env = fallback
    api_key = env.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    import aiohttp

    from . import config

    payload = {
        "model": config.PEOPLE_PROFILES_MODEL,
        "temperature": 0.3,
        "messages": [{"role": "user", "content": prompt}],
    }
    url = f"{config.PEOPLE_PROFILES_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as sess:
            async with sess.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    return None
                body = await resp.json()
        text = (body["choices"][0]["message"]["content"] or "").strip()
        return text or None
    except (aiohttp.ClientError, TimeoutError, KeyError, IndexError, ValueError):
        return None
