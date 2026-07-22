"""cc-switch 供应商集成:读取 ~/.hermes/config.yaml,把当前激活的供应商
(base_url + api_key + model)注入 SDK,实现 DeepSeek / Kimi / 官方订阅 / 任意
第三方中转之间的热切换。

背景:cc-switch(https://github.com/farion1231/cc-switch)是个桌面 App,内建对
Hermes 的支持——它把供应商配置写进 `~/.hermes/config.yaml`。本模块负责读这个文件,
claude-hermes 不硬编码任何供应商:模型名 / api_key / base_url 全由 cc-switch 管理。

cc-switch 写入的格式(节选):

    model:
      provider: "deepseek"        # 当前激活的供应商名
      base_url: "https://api.deepseek.com/anthropic"
      default: "deepseek-chat"    # 当前模型
    custom_providers:
      - name: deepseek
        base_url: https://api.deepseek.com/anthropic
        api_key: sk-...
        model: deepseek-chat
      - name: kimi
        base_url: https://api.moonshot.cn/anthropic
        api_key: sk-...
        model: kimi-k2-0711-preview
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 兜底默认模型(cc-switch 未配置 / 文件缺失时);与 config.MODEL 的默认保持一致。
_FALLBACK_MODEL = "claude-sonnet-5"

# 官方 Anthropic 端点特征:命中即视为"走订阅",不注入第三方鉴权。
_OFFICIAL_HOSTS = ("api.anthropic.com",)
# cc-switch / 原版 hermes 里代表官方订阅的供应商名。
_OFFICIAL_NAMES = ("claude", "official", "anthropic", "claude-official")


def hermes_config_path() -> Path:
    """claude-hermes 的供应商配置路径(cc-switch 格式)。

    刻意独立于原版 Hermes 的 `~/.hermes`(那是另一个产品的活配置,不能共用):
      1. CLAUDE_HERMES_HOME 环境变量(非空)→ <该目录>/config.yaml
      2. 默认 ~/.claude-hermes/config.yaml

    想用 cc-switch 管理它:在 cc-switch 里把该 Hermes profile 的 hermes_config_dir
    指到 ~/.claude-hermes 即可(cc-switch 会直接写这个目录,不碰原版 ~/.hermes)。
    """
    raw = os.environ.get("CLAUDE_HERMES_HOME", "").strip()
    base = Path(os.path.expanduser(raw)) if raw else Path.home() / ".claude-hermes"
    return base / "config.yaml"


@dataclass(frozen=True)
class ActiveProvider:
    """一个供应商的可用配置:名字 + 端点 + 鉴权 + 模型。"""

    name: str
    base_url: str
    api_key: str
    model: str

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


def _load_yaml() -> dict | None:
    """读并解析 config.yaml;文件缺失/为空/解析失败/无 pyyaml 都返回 None(容错)。"""
    path = hermes_config_path()
    if not path.exists():
        return None
    try:
        import yaml  # 延迟 import:未装 pyyaml 时不至于拖垮整个进程
    except ImportError:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.strip():
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _provider_entries(config: dict) -> dict[str, dict]:
    """汇总所有供应商定义,键=name。

    cc-switch 写 `custom_providers:`(列表),原版 hermes 用 `providers:`(字典)。
    两者取并集,custom_providers 优先(与 cc-switch 的 dedup 顺序一致)。
    """
    out: dict[str, dict] = {}
    providers_dict = config.get("providers")
    if isinstance(providers_dict, dict):
        for name, entry in providers_dict.items():
            if isinstance(entry, dict):
                out[str(name)] = entry
    custom = config.get("custom_providers")
    if isinstance(custom, list):
        for entry in custom:
            if isinstance(entry, dict) and entry.get("name"):
                out[str(entry["name"])] = entry  # 列表覆盖字典
    return out


def _entry_field(entry: dict, *keys: str) -> str:
    """从供应商条目取字段,兼容 snake_case 与遗留 camelCase(baseUrl/apiKey)。"""
    for k in keys:
        v = entry.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def load_active() -> ActiveProvider | None:
    """当前激活的供应商;cc-switch 未配置 / 文件缺失 → None(调用方回落订阅)。"""
    config = _load_yaml()
    if not config:
        return None
    model_section = config.get("model")
    model_section = model_section if isinstance(model_section, dict) else {}
    active_name = _entry_field(model_section, "provider")
    entries = _provider_entries(config)
    entry = entries.get(active_name, {}) if active_name else {}

    base_url = _entry_field(model_section, "base_url") or _entry_field(
        entry, "base_url", "baseUrl"
    )
    api_key = _entry_field(entry, "api_key", "apiKey")
    model = (
        _entry_field(model_section, "default")
        or _entry_field(entry, "model")
        or _FALLBACK_MODEL
    )
    name = active_name or entry.get("name") or "claude"
    return ActiveProvider(name=str(name), base_url=base_url, api_key=api_key, model=model)


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
        "ANTHROPIC_AUTH_TOKEN": provider.api_key,
        # 清掉订阅 token,免得 CLI 拿它去打第三方端点导致 401
        "CLAUDE_CODE_OAUTH_TOKEN": "",
    }


def _find_by_model(config: dict, model: str) -> ActiveProvider | None:
    """按模型名反查它属于 cc-switch 里的哪个供应商(供 /model 会话覆盖用)。"""
    for name, entry in _provider_entries(config).items():
        if _entry_field(entry, "model") == model:
            return ActiveProvider(
                name=name,
                base_url=_entry_field(entry, "base_url", "baseUrl"),
                api_key=_entry_field(entry, "api_key", "apiKey"),
                model=model,
            )
    return None


def resolve(chosen_model: str | None, default_model: str) -> tuple[str, dict[str, str]]:
    """算出这一轮实际用的 (模型, 要注入的 env)。

    优先级:
      1. 会话用 /model 显式选了某模型 → 用它;若该模型属于 cc-switch 的第三方
         供应商,连带注入其 base_url + key,否则按官方(走订阅)。
      2. 没显式选 → 跟随 cc-switch 当前激活的供应商。
      3. 都没有 → default_model(= config.MODEL) + 订阅。
    """
    config = _load_yaml() or {}
    if chosen_model:
        found = _find_by_model(config, chosen_model)
        if found and not found.is_official:
            return chosen_model, _env_for(found)
        return chosen_model, {}
    active = load_active()
    if active is None:
        return default_model, {}
    model = active.model or default_model
    return model, _env_for(active)


def sidecar_env(name: str) -> tuple[str, dict[str, str]] | None:
    """按供应商名取 (model, 注入env),给标题总结这类轻量辅助调用做兜底。

    名字不区分大小写、允许子串匹配(配置里叫 deepseek / DeepSeek 都能命中);
    未配置 / 缺 key / 是官方端点(官方走订阅不用它兜底)→ None。
    """
    config = _load_yaml() or {}
    want = name.lower()
    entries = _provider_entries(config)
    # 先精确后子串:防「deepseek」误命中「deepseek-pro」这种带后缀的贵档
    ordered = sorted(entries.items(), key=lambda kv: kv[0].lower() != want)
    for pname, entry in ordered:
        if want not in pname.lower():
            continue
        provider = ActiveProvider(
            name=pname,
            base_url=_entry_field(entry, "base_url", "baseUrl"),
            api_key=_entry_field(entry, "api_key", "apiKey"),
            model=_entry_field(entry, "model"),
        )
        if provider.is_official or not provider.api_key or not provider.model:
            continue
        return provider.model, _env_for(provider)
    return None


def has_active_third_party() -> bool:
    """cc-switch 当前激活的是不是一个可用的第三方供应商(带 key)。"""
    active = load_active()
    return active is not None and not active.is_official and bool(active.api_key)


# Kimi(Moonshot)的订阅套餐固定走这个域名,其余第三方供应商(DeepSeek/Kimi 的常规
# API key 入口等)一律按量计费的 API 处理——这俩域名比在 config.yaml 塞自定义字段更抗
# cc-switch 覆写。
_SUBSCRIPTION_HOSTS = ("api.kimi.com",)  # Kimi Coding 订阅套餐


def _billing_kind(base_url: str) -> str:
    """按 base_url 的 host 判断是订阅还是按量 API;非已知订阅域名默认按 API。"""
    from urllib.parse import urlsplit

    host = (urlsplit(base_url).hostname or "").lower()
    return "订阅" if host in _SUBSCRIPTION_HOSTS else "API"


def available_models(default_choices: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """/model 无参时的候选:官方默认档 + cc-switch 里配好的各供应商模型。

    default_choices 是官方三档(claude-opus/sonnet/haiku),恒列在前。标签只留
    "模型名（订阅/API）",不带供应商名和 cc-switch 后缀,免得面板换行/信息过载。
    """
    out = list(default_choices)
    seen = {mid for mid, _ in out}
    config = _load_yaml()
    if not config:
        return out
    for name, entry in _provider_entries(config).items():
        if name.lower() in _OFFICIAL_NAMES:
            continue
        model = _entry_field(entry, "model")
        if not model or model in seen or not _entry_field(entry, "api_key", "apiKey"):
            continue
        seen.add(model)
        base_url = _entry_field(entry, "base_url", "baseUrl")
        kind = _billing_kind(base_url)
        out.append((model, f"{model}（{kind}）"))
    return out


def lookup_provider_by_model(model: str) -> dict | None:
    """按模型名查 cc-switch 供应商配置条目;未配置(官方模型)返回 None。"""
    config = _load_yaml()
    if not config:
        return None
    for name, entry in _provider_entries(config).items():
        if _entry_field(entry, "model") == model:
            return dict(entry)
    return None


def is_subscription_host(base_url: str) -> bool:
    """base_url 的 host 是否属于已知订阅套餐供应商(如 Kimi Coding)。"""
    from urllib.parse import urlsplit
    host = (urlsplit(base_url).hostname or "").lower()
    return host in _SUBSCRIPTION_HOSTS
