"""供应商集成测试:以 gateway.settings_store.web_providers 为单一数据源。

旧数据源 cc-switch 的 ~/.claude-hermes/config.yaml 已废弃,providers.py 运行时不再
读取它;这些测试只 mock settings_store,不碰真实文件。
"""
from __future__ import annotations

from pathlib import Path

from claude_hermes import providers
from claude_hermes.gateway import settings_store


def _point_settings_to(monkeypatch, tmp_path: Path) -> None:
    """把设置页存储指向临时文件,不碰真实 data/web_settings.json。"""
    monkeypatch.setattr(settings_store, "_PATH", tmp_path / "web_settings.json")


# ── 基础 resolve / load_active / has_active_third_party ──────────────
def test_resolve_default_model_when_no_providers(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    model, env = providers.resolve(None, "claude-sonnet-5")
    assert model == "claude-sonnet-5"
    assert env == {}


def test_resolve_explicit_official_model_no_env(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "deepseek", {"base_url": "https://api.deepseek.com", "model": "deepseek-chat", "api_key": "sk-xxx"}
    )
    model, env = providers.resolve("claude-opus-5", "claude-sonnet-5")
    assert model == "claude-opus-5"
    assert env == {}


def test_resolve_explicit_third_party_model_injects_env(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "deepseek", {"base_url": "https://api.deepseek.com/anthropic", "model": "deepseek-chat", "api_key": "sk-xxx"}
    )
    model, env = providers.resolve("deepseek-chat", "claude-sonnet-5")
    assert model == "deepseek-chat"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert env["ANTHROPIC_API_KEY"] == "sk-xxx"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""


def test_load_active_returns_first_usable_third_party(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "deepseek", {"base_url": "https://api.deepseek.com", "model": "deepseek-chat", "api_key": "sk-xxx"}
    )
    active = providers.load_active()
    assert active is not None
    assert active.name == "deepseek"
    assert active.model == "deepseek-chat"
    assert active.is_official is False


def test_has_active_third_party_true(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "deepseek", {"base_url": "https://api.deepseek.com", "model": "deepseek-chat", "api_key": "sk-xxx"}
    )
    assert providers.has_active_third_party() is True


def test_has_active_third_party_false_when_no_key(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "deepseek", {"base_url": "https://api.deepseek.com", "model": "deepseek-chat", "api_key": ""}
    )
    assert providers.has_active_third_party() is False


def test_has_active_third_party_false_when_official_host(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "claude", {"base_url": "https://api.anthropic.com", "model": "claude-sonnet-5", "api_key": "sk-xxx"}
    )
    assert providers.has_active_third_party() is False


# ── available_models:官方 + 自定义档位 + web_providers ───────────────
def test_available_models_lists_builtin_and_web_providers(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "deepseek", {"base_url": "https://api.deepseek.com/anthropic", "model": "deepseek-chat", "api_key": "sk-xxx"}
    )
    defaults = [("claude-opus-5", "Opus 5"), ("claude-sonnet-5", "Sonnet 5")]
    out = providers.available_models(defaults)
    by_id = {mid: (label, group) for mid, label, group in out}
    ids = list(by_id)
    assert ids[:2] == ["claude-opus-5", "claude-sonnet-5"]  # 官方档在前
    assert by_id["deepseek-chat"] == ("deepseek-chat（API）", "api")


def test_available_models_kimi_subscription_group(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "kimi", {"base_url": "https://api.kimi.com/coding", "model": "kimi-k3", "api_key": "sk-xxx"}
    )
    out = providers.available_models([])
    by_id = {mid: (label, group) for mid, label, group in out}
    assert by_id["kimi-k3"] == ("kimi-k3（订阅）", "kimi")


def test_available_models_includes_web_extra_model(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_extra_model("claude-opus-5", "Opus 5（订阅）")
    defaults = [("claude-sonnet-5", "Sonnet 5（订阅）")]
    out = providers.available_models(defaults)
    by_id = {mid: (label, group) for mid, label, group in out}
    assert by_id["claude-opus-5"] == ("Opus 5（订阅）", "anthropic")


def test_available_models_dedups_web_extra_model_against_defaults(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_extra_model("claude-sonnet-5", "重复")
    defaults = [("claude-sonnet-5", "Sonnet 5（订阅）")]
    out = providers.available_models(defaults)
    assert [mid for mid, _, _ in out].count("claude-sonnet-5") == 1


def test_available_models_hides_disabled_builtin(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.set_builtin_model_disabled("claude-opus-4-6", True)
    defaults = [("claude-opus-4-6", "Opus 4.6"), ("claude-sonnet-5", "Sonnet 5")]
    out = providers.available_models(defaults)
    ids = [mid for mid, _, _ in out]
    assert "claude-opus-4-6" not in ids
    assert "claude-sonnet-5" in ids


# ── sidecar_env:标题总结兜底 ────────────────────────────────────────
def test_sidecar_env_finds_named_provider(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "deepseek", {"base_url": "https://api.deepseek.com/anthropic", "model": "deepseek-chat", "api_key": "sk-xxx"}
    )
    result = providers.sidecar_env("DeepSeek")
    assert result is not None
    model, env = result
    assert model == "deepseek-chat"
    assert env["ANTHROPIC_API_KEY"] == "sk-xxx"
    assert providers.sidecar_env("nonexistent") is None


def test_sidecar_env_prefers_exact_name(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "deepseek-pro", {"base_url": "https://api.deepseek.com", "model": "deepseek-v4-pro", "api_key": "sk-pro"}
    )
    settings_store.upsert_web_provider(
        "deepseek", {"base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash", "api_key": "sk-flash"}
    )
    result = providers.sidecar_env("deepseek")
    assert result is not None
    assert result[0] == "deepseek-v4-flash"


def test_sidecar_env_empty_name_returns_any_third_party(tmp_path, monkeypatch):
    """空串=任意第一个可用第三方(后台任务没配 DeepSeek 时兜到 Codex/GPT 代理)。"""
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "codex-gpt", {"base_url": "http://127.0.0.1:8317", "model": "gpt-5.6", "api_key": "sk-proxy"}
    )
    result = providers.sidecar_env("")
    assert result is not None
    model, env = result
    assert model == "gpt-5.6"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8317"


def test_sidecar_env_empty_name_none_when_no_provider(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    assert providers.sidecar_env("") is None


# ── vision 声明:Codex/GPT 代理直传图片 ───────────────────────────────
def test_resolve_vision_provider_injects_capable_flag(tmp_path, monkeypatch):
    """勾了「支持视觉直传」的供应商 → env 带 ANTHROPIC_VISION_CAPABLE=1。"""
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "codex-gpt", {"base_url": "http://127.0.0.1:8317", "model": "gpt-5.6", "api_key": "sk-proxy", "vision": "1"}
    )
    model, env = providers.resolve("gpt-5.6", "claude-sonnet-5")
    assert model == "gpt-5.6"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8317"
    assert env["ANTHROPIC_API_KEY"] == "sk-proxy"
    assert env["ANTHROPIC_VISION_CAPABLE"] == "1"


def test_resolve_non_vision_provider_flag_empty(tmp_path, monkeypatch):
    """没勾视觉的供应商(DeepSeek)→ 标记为空串,仍走转文字旁路。"""
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "deepseek", {"base_url": "https://api.deepseek.com/anthropic", "model": "deepseek-chat", "api_key": "sk-xxx"}
    )
    _, env = providers.resolve("deepseek-chat", "claude-sonnet-5")
    assert env["ANTHROPIC_VISION_CAPABLE"] == ""


# ── lookup_provider_by_model / subscription_api_key_for_model ────────
def test_lookup_provider_by_model_returns_web_provider(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "kimi", {"base_url": "https://api.kimi.com/coding", "model": "kimi-k3", "api_key": "sk-xxx"}
    )
    entry = providers.lookup_provider_by_model("kimi-k3")
    assert entry is not None
    assert entry["model"] == "kimi-k3"


def test_subscription_api_key_for_model_kimi(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "kimi", {"base_url": "https://api.kimi.com/coding", "model": "kimi-k3", "api_key": "sk-kimi"}
    )
    assert providers.subscription_api_key_for_model("kimi-k3") == "sk-kimi"


def test_subscription_api_key_for_model_deepseek_is_none(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "deepseek", {"base_url": "https://api.deepseek.com", "model": "deepseek-chat", "api_key": "sk-xxx"}
    )
    assert providers.subscription_api_key_for_model("deepseek-chat") is None


# ── 兼容性与路径 ─────────────────────────────────────────────────────
def test_legacy_camelcase_fields_in_migrate_script(tmp_path, monkeypatch):
    """老 cc-switch 配置会残留 baseUrl/apiKey,迁移脚本导入前应转成 snake_case。"""
    from claude_hermes.gateway import migrate_cc_switch

    _point_settings_to(monkeypatch, tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
custom_providers:
  - name: relay
    baseUrl: https://relay.example.com/anthropic
    apiKey: sk-relay-zzz
    model: some-model
""", encoding="utf-8")
    monkeypatch.setattr(providers, "hermes_config_path", lambda: cfg)

    entries = migrate_cc_switch._extract_providers(migrate_cc_switch._load_yaml())
    assert entries["relay"]["base_url"] == "https://relay.example.com/anthropic"
    assert entries["relay"]["api_key"] == "sk-relay-zzz"
    assert entries["relay"]["model"] == "some-model"


def test_hermes_home_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HERMES_HOME", str(tmp_path))
    assert providers.hermes_config_path() == tmp_path / "config.yaml"
    monkeypatch.delenv("CLAUDE_HERMES_HOME", raising=False)


def test_default_path_is_independent_of_real_hermes(monkeypatch):
    # 默认路径必须是 ~/.claude-hermes,绝不能落到原版 Hermes 的 ~/.hermes
    monkeypatch.delenv("CLAUDE_HERMES_HOME", raising=False)
    p = providers.hermes_config_path()
    assert p.name == "config.yaml"
    assert p.parent.name == ".claude-hermes"
    assert ".hermes/config.yaml" not in str(p)


# ── 旧 cc-switch 迁移脚本:只读展示 / 导入 ───────────────────────────
def test_migrate_script_extracts_cc_switch_providers(tmp_path, monkeypatch):
    """迁移脚本能正确解析 cc-switch 的 providers 字典和 custom_providers 列表。"""
    from claude_hermes.gateway import migrate_cc_switch

    _point_settings_to(monkeypatch, tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
model:
  provider: deepseek
  default: deepseek-chat
custom_providers:
  - name: deepseek
    base_url: https://api.deepseek.com/anthropic
    api_key: sk-deepseek-xxx
    model: deepseek-chat
providers:
  kimi:
    base_url: https://api.moonshot.cn/anthropic
    api_key: sk-kimi-yyy
    model: kimi-k2.7-code
""", encoding="utf-8")
    monkeypatch.setattr(providers, "hermes_config_path", lambda: cfg)

    entries = migrate_cc_switch._extract_providers(migrate_cc_switch._load_yaml())
    assert "deepseek" in entries
    assert "kimi" in entries
    assert entries["deepseek"]["model"] == "deepseek-chat"
    assert entries["kimi"]["api_key"] == "sk-kimi-yyy"


def test_migrate_script_skips_official_names(tmp_path, monkeypatch):
    from claude_hermes.gateway import migrate_cc_switch

    _point_settings_to(monkeypatch, tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("custom_providers:\n  - name: claude\n    base_url: https://api.anthropic.com\n    api_key: x\n    model: claude-sonnet-5\n", encoding="utf-8")
    monkeypatch.setattr(providers, "hermes_config_path", lambda: cfg)
    entries = migrate_cc_switch._extract_providers(migrate_cc_switch._load_yaml())
    assert "claude" not in entries


# ── codex_mgmt_for_model:本地 Codex 代理的额度查询钥匙 ────────────────
def test_codex_mgmt_for_model_returns_key_for_local_proxy(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "codex-gpt", {"base_url": "http://127.0.0.1:8317", "model": "gpt-5.5",
                      "api_key": "sk-proxy", "mgmt_key": "mgmt-secret"}
    )
    assert providers.codex_mgmt_for_model("gpt-5.5") == ("mgmt-secret", "http://127.0.0.1:8317")


def test_codex_mgmt_for_model_rejects_remote_host(tmp_path, monkeypatch):
    """mgmt_key 是本地代理的管理钥匙,远程 base_url 一律拒绝,防外发。"""
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "evil", {"base_url": "http://attacker.example:8317", "model": "gpt-5.5",
                 "api_key": "sk-proxy", "mgmt_key": "mgmt-secret"}
    )
    assert providers.codex_mgmt_for_model("gpt-5.5") is None


def test_codex_mgmt_for_model_none_without_key(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "codex-gpt", {"base_url": "http://127.0.0.1:8317", "model": "gpt-5.5", "api_key": "sk-proxy"}
    )
    assert providers.codex_mgmt_for_model("gpt-5.5") is None


def test_available_models_codex_group(tmp_path, monkeypatch):
    """配了 mgmt_key 的本地代理 → codex 分组(订阅额度可查)。"""
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "codex-gpt", {"base_url": "http://127.0.0.1:8317", "model": "gpt-5.5",
                      "api_key": "sk-proxy", "mgmt_key": "m"}
    )
    choices = providers.available_models([])
    gpt = [c for c in choices if c[0] == "gpt-5.5"]
    assert gpt and gpt[0][2] == "codex"
