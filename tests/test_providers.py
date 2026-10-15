"""供应商集成测试:以 gateway.settings_store.web_providers 为单一数据源。

旧数据源 cc-switch 的 ~/.claude-hermes/config.yaml 已废弃,providers.py 运行时不再
读取它;这些测试只 mock settings_store,不碰真实文件。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from vococo import providers
from vococo.gateway import settings_store


def _point_settings_to(monkeypatch, tmp_path: Path) -> None:
    """把设置页存储指向临时文件,不碰真实 data/web_settings.json。"""
    monkeypatch.setattr(settings_store, "_PATH", tmp_path / "web_settings.json")


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"])
def test_claude_provider_alias_is_independent(tmp_path, monkeypatch, model):
    _point_settings_to(monkeypatch, tmp_path)
    name = "cheapcoding-" + model.split("-")[1]
    alias = f"{name}/{model}"
    settings_store.upsert_web_provider(name, {
        "base_url": "https://cheapcoding.cc/anthropic", "model": model, "api_key": "test-key",
    })
    choices = providers.available_models([(model, "官方")])
    assert (model, "官方", "anthropic") in choices
    assert any(mid == alias and "Cheapcoding" in label and group == "api" for mid, label, group in choices)
    assert providers.resolve(model, model) == (model, {})
    assert providers.lookup_provider_by_model(model) is None
    upstream, env = providers.resolve(alias, model)
    assert upstream == model
    assert env["DISABLE_PROMPT_CACHING"] == "1"
    assert providers.resolve(None, alias) == (upstream, env)
    assert providers._provider_for_model(alias).model == model
    assert providers.lookup_provider_by_model(alias)["model"] == model
    assert providers.sidecar_env(name) == (model, env)
    assert providers.sidecar_env(name, selection_id=True) == (alias, env)
    settings_store.set_web_effort("low", model=model)
    settings_store.set_web_effort("max", model=alias)
    assert settings_store.get_web_effort(model) == "low"
    assert settings_store.get_web_effort(alias) == "max"
    assert "low" in providers.effort_levels_for_model(model)
    assert "low" not in providers.effort_levels_for_model(alias)
    settings_store.set_model_disabled(alias, True)
    assert [mid for mid, _, _ in providers.available_models([(model, "官方")])] == [model]
    assert alias in {mid for mid, _, _ in providers.available_models([(model, "官方")], True)}
    settings_store.set_model_disabled(alias, False)
    settings_store.set_model_disabled(model, True)
    assert [mid for mid, _, _ in providers.available_models([(model, "官方")])] == [alias]


def test_extra_claude_model_is_namespaced_and_routes_upstream(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    sonnet, opus = "claude-sonnet-5-5", "claude-opus-5-5"
    settings_store.upsert_web_provider("relay", {
        "base_url": "https://cheapcoding.cc", "api_key": "test", "model": sonnet,
    })
    settings_store.upsert_web_extra_model(opus, "额外 Opus", group="api", provider="relay")
    defaults = [(sonnet, "官方 Sonnet"), (opus, "官方 Opus")]
    choices = providers.available_models(defaults)
    assert (opus, "官方 Opus", "anthropic") in choices
    assert any(mid == f"relay/{opus}" and "relay" in label and group == "api" for mid, label, group in choices)
    assert providers.resolve(opus, sonnet) == (opus, {})
    upstream, env = providers.resolve(f"relay/{opus}", sonnet)
    assert upstream == opus and env["DISABLE_PROMPT_CACHING"] == "1"
    assert providers.lookup_provider_by_model(f"relay/{opus}")["model"] == opus
    settings_store.set_model_disabled(opus, True)
    assert f"relay/{opus}" in {mid for mid, _, _ in providers.available_models(defaults)}
    settings_store.remove_web_extra_model(opus)
    with pytest.raises(ValueError, match="供应商"):
        providers.resolve(f"relay/{opus}", sonnet)


def test_provider_name_starting_claude_routes_generated_alias(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    model = "claude-sonnet-5-5"
    alias = f"claude-relay/{model}"
    settings_store.upsert_web_provider("claude-relay", {
        "base_url": "https://cheapcoding.cc", "api_key": "test", "model": model,
    })
    assert alias in {mid for mid, _, _ in providers.available_models([])}
    resolved, env = providers.resolve(alias, model)
    assert resolved == model and env["ANTHROPIC_API_KEY"] == "test"
    assert providers.resolve(model, model) == (model, {})


@pytest.mark.parametrize("extra", [False, True])
@pytest.mark.parametrize("upstream", ["anthropic/claude-sonnet-5-5", "claude-vendor/claude-sonnet-5-5"])
def test_real_slash_claude_model_keeps_existing_route(tmp_path, monkeypatch, extra, upstream):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider("openrouter", {
        "base_url": "https://openrouter.example", "api_key": "test",
        "model": "ordinary-model" if extra else upstream,
    })
    if extra:
        settings_store.upsert_web_extra_model(upstream, "OpenRouter Claude", provider="openrouter", group="api")
    resolved, env = providers.resolve(upstream, "claude-sonnet-5-5")
    assert resolved == upstream
    assert env["ANTHROPIC_BASE_URL"] == "https://openrouter.example"
    assert providers.lookup_provider_by_model(upstream)["model"] == upstream
    assert upstream in {mid for mid, _, _ in providers.available_models([])}
    assert "DISABLE_PROMPT_CACHING" not in env


@pytest.mark.parametrize("name,model,label,expected", [
    ("yesvg-opus", "claude-opus-5-5", "", "Opus 5.5 · YesVG"),
    ("cheapcoding-sonnet", "claude-sonnet-5-5", "", "Sonnet 5.5 · Cheapcoding"),
    ("cheapcoding-fable", "claude-fable-5-1", "", "Fable 5.1 · Cheapcoding"),
    ("my-relay", "claude-opus-5-5", "自定义档位（API）", "自定义档位 · my-relay"),
    ("my-relay", "claude-new-model", "", "claude-new-model · my-relay"),
])
def test_third_party_claude_labels_are_short(tmp_path, monkeypatch, name, model, label, expected):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(name, {
        "base_url": "https://relay.example", "api_key": "test", "model": model,
    })
    if label:
        settings_store.upsert_web_extra_model(model, label, provider=name, group="api")
    choices = {mid: (text, group) for mid, text, group in providers.available_models([])}
    assert choices[f"{name}/{model}"] == (expected, "api")
    assert providers.resolve(f"{name}/{model}", model)[0] == model


def test_three_claude_tiers_coexist_with_official(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    models = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"]
    aliases = []
    for model in models:
        name = "cheapcoding-" + model.split("-")[1]
        settings_store.upsert_web_provider(name, {
            "base_url": "https://cheapcoding.cc", "api_key": "test", "model": model,
        })
        aliases.append(f"{name}/{model}")
    choices = providers.available_models([(model, model) for model in models])
    assert {mid for mid, _, group in choices if group == "anthropic"} == set(models)
    assert {mid for mid, _, group in choices if group == "api"} == set(aliases)
    for model, alias in zip(models, aliases):
        assert providers.resolve(model, model) == (model, {})
        assert providers.resolve(alias, model)[0] == model


@pytest.mark.parametrize("url,disabled", [
    ("https://cheapcoding.cc:443/anthropic", True),
    ("https://cheapcoding.cc.evil.example", False),
    ("https://other.example/cheapcoding.cc", False),
    ("https://api.anthropic.com", False),
])
def test_cache_policy_depends_on_exact_host(url, disabled):
    env = providers._env_for(providers.ActiveProvider("cheapcoding", url, "test", "claude-sonnet-5-5"))
    assert (env.get("DISABLE_PROMPT_CACHING") == "1") is disabled


def test_missing_claude_alias_fails_without_breaking_slash_models(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="供应商"):
        providers.resolve("missing/claude-sonnet-5-5", "claude-sonnet-5-5")
    settings_store.upsert_web_provider("other", {
        "base_url": "https://other.example", "api_key": "test", "model": "org/ordinary-model",
    })
    assert providers.resolve("org/ordinary-model", "claude-sonnet-5-5")[0] == "org/ordinary-model"
    assert providers.resolve("org/unknown-model", "claude-sonnet-5-5") == ("org/unknown-model", {})


@pytest.mark.parametrize("entry", [
    {"model": "claude-opus-5-5", "api_key": "test", "base_url": "https://cheapcoding.cc"},
    {"model": "claude-sonnet-5-5", "api_key": "", "base_url": "https://cheapcoding.cc"},
    {"model": "claude-sonnet-5-5", "api_key": "test", "base_url": "file:///tmp"},
])
def test_invalid_claude_alias_never_falls_back_to_official(tmp_path, monkeypatch, entry):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider("cheapcoding-sonnet", entry)
    with pytest.raises(ValueError, match="供应商"):
        providers.resolve("cheapcoding-sonnet/claude-sonnet-5-5", "claude-sonnet-5-5")


@pytest.mark.anyio
async def test_usage_default_keeps_provider_alias(tmp_path, monkeypatch):
    from vococo import config
    _point_settings_to(monkeypatch, tmp_path)
    model = "claude-sonnet-5-5"
    settings_store.upsert_web_provider("cheapcoding-sonnet", {
        "base_url": "https://cheapcoding.cc", "api_key": "test", "model": model,
    })
    monkeypatch.setattr(config, "MODEL", f"cheapcoding-sonnet/{model}")
    assert await providers.usage_for_model(None) == ({"provider": "api", "type": "api"}, 200)


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
    model, env = providers.resolve("claude-opus-5-5", "claude-sonnet-5")
    assert model == "claude-opus-5-5"
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
    defaults = [("claude-opus-5-5", "Opus 5.5"), ("claude-sonnet-5", "Sonnet 5")]
    out = providers.available_models(defaults)
    by_id = {mid: (label, group) for mid, label, group in out}
    ids = list(by_id)
    assert ids[:2] == ["claude-opus-5-5", "claude-sonnet-5"]  # 官方档在前
    assert by_id["deepseek-chat"] == ("deepseek-chat（API）", "api")


def test_available_models_kimi_subscription_group(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "kimi", {"base_url": "https://api.kimi.com/coding", "model": "kimi-k3", "api_key": "sk-xxx"}
    )
    out = providers.available_models([])
    by_id = {mid: (label, group) for mid, label, group in out}
    assert by_id["kimi-k3"] == ("kimi-k3（订阅）", "kimi")


def test_available_models_hides_kimi_subscription_but_lists_it_for_settings(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "kimi", {"base_url": "https://api.kimi.com/coding", "model": "kimi-k3", "api_key": "sk-xxx"}
    )
    settings_store.set_model_disabled("kimi-k3", True)

    assert "kimi-k3" not in {mid for mid, _, _ in providers.available_models([])}
    assert "kimi-k3" in {mid for mid, _, _ in providers.available_models([], True)}


def test_available_models_includes_web_extra_model(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_extra_model("claude-opus-5-5", "Opus 5.5（订阅）")
    defaults = [("claude-sonnet-5", "Sonnet 5（订阅）")]
    out = providers.available_models(defaults)
    by_id = {mid: (label, group) for mid, label, group in out}
    assert by_id["claude-opus-5-5"] == ("Opus 5.5（订阅）", "anthropic")


def test_extra_model_reuses_declared_provider(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "codex-gpt", {
            "base_url": "http://127.0.0.1:8317", "model": "gpt-5.6-terra",
            "api_key": "sk-proxy", "vision": "1", "mgmt_key": "mgmt-secret",
        },
    )
    settings_store.upsert_web_extra_model(
        "gpt-5.6-sol", "GPT-5.6 Sol（订阅）", group="codex", provider="codex-gpt"
    )
    settings_store.upsert_web_extra_model(
        "gpt-5.6-luna", "GPT-5.6 Luna（订阅）", group="codex", provider="codex-gpt"
    )

    by_id = {mid: (label, group) for mid, label, group in providers.available_models([])}
    assert by_id["gpt-5.6-terra"] == ("gpt-5.6-terra（订阅）", "codex")
    assert by_id["gpt-5.6-sol"] == ("GPT-5.6 Sol（订阅）", "codex")
    assert by_id["gpt-5.6-luna"] == ("GPT-5.6 Luna（订阅）", "codex")

    for model in ("gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna"):
        resolved, env = providers.resolve(model, "claude-sonnet-5")
        assert resolved == model
        assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8317"
        assert env["ANTHROPIC_VISION_CAPABLE"] == "1"
    assert providers.codex_mgmt_for_model("gpt-5.6-luna") == (
        "mgmt-secret", "http://127.0.0.1:8317"
    )


def test_gpt6_models_are_available_through_configured_codex_proxy(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "codex-gpt", {
            "base_url": "http://127.0.0.1:8317", "model": "gpt-5.6-terra",
            "api_key": "sk-proxy", "mgmt_key": "mgmt-secret",
        },
    )

    choices = providers.available_models([])
    by_id = {mid: (label, group) for mid, label, group in choices}
    assert by_id["gpt-6-astra"] == ("GPT-6 Astra（订阅）", "codex")
    assert by_id["gpt-6.1-sol"] == ("GPT-6.1 Sol（订阅）", "codex")
    assert by_id["gpt-6-luna"] == ("GPT-6 Luna（订阅）", "codex")
    codex_ids = [mid for mid, _, group in choices if group == "codex"]
    assert codex_ids[:3] == ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"]

    expected_env = {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
        "ANTHROPIC_API_KEY": "sk-proxy",
        "CLAUDE_CODE_OAUTH_TOKEN": "",
        "ANTHROPIC_VISION_CAPABLE": "",
    }
    for model in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"):
        assert providers.resolve(model, "claude-sonnet-5") == (model, expected_env)
        assert providers.codex_mgmt_for_model(model) == (
            "mgmt-secret", "http://127.0.0.1:8317"
        )


def test_gpt6_models_do_not_require_management_key(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "codex-gpt", {
            "base_url": "http://127.0.0.1:8317", "model": "gpt-5.6-terra",
            "api_key": "sk-proxy",
        },
    )

    ids = {mid for mid, _, _ in providers.available_models([])}
    assert {"gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"} <= ids
    resolved, env = providers.resolve("gpt-6.1-sol", "claude-sonnet-5")
    assert resolved == "gpt-6.1-sol"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8317"
    assert providers.codex_mgmt_for_model("gpt-6.1-sol") is None


def test_codex_builtin_overrides_duplicate_manual_model(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "codex-gpt", {
            "base_url": "http://127.0.0.1:8317", "model": "gpt-5.6-terra",
            "api_key": "sk-proxy",
        },
    )
    settings_store.upsert_web_provider(
        "openai-api", {
            "base_url": "https://api.example.com", "model": "gpt-6.1-sol",
            "api_key": "sk-api",
        },
    )
    settings_store.upsert_web_extra_model("gpt-6.1-sol", "错误分组", group="anthropic")

    choices = providers.available_models([])
    matches = [(label, group) for mid, label, group in choices if mid == "gpt-6.1-sol"]
    assert matches == [("GPT-6.1 Sol（订阅）", "codex")]
    assert providers.resolve("gpt-6.1-sol", "claude-sonnet-5")[1]["ANTHROPIC_BASE_URL"] == (
        "http://127.0.0.1:8317"
    )


def test_gpt6_extra_model_falls_back_to_declared_provider_without_codex(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "openai-api", {
            "base_url": "https://api.example.com", "model": "gpt-5.6-terra",
            "api_key": "sk-api",
        },
    )
    settings_store.upsert_web_extra_model(
        "gpt-6.1-sol", "GPT-6.1 Sol（API）", group="api", provider="openai-api"
    )

    assert providers.resolve("gpt-6.1-sol", "claude-sonnet-5")[1]["ANTHROPIC_BASE_URL"] == (
        "https://api.example.com"
    )


def test_effort_choices_follow_model_provider_capability(tmp_path, monkeypatch):
    """Codex GPT / 官方 Claude 为五档；普通第三方端点保守保留 high/max。"""
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "codex-gpt", {
            "base_url": "http://127.0.0.1:8317", "model": "gpt-5.6-terra",
            "api_key": "sk-proxy", "mgmt_key": "mgmt-secret",
        },
    )
    settings_store.upsert_web_extra_model(
        "gpt-5.6-sol", "GPT-5.6 Sol（订阅）", group="codex", provider="codex-gpt"
    )
    settings_store.upsert_web_provider(
        "deepseek", {
            "base_url": "https://api.deepseek.com/anthropic", "model": "deepseek-v4-flash",
            "api_key": "sk-deepseek",
        },
    )
    settings_store.upsert_web_provider(
        "kimi", {
            "base_url": "https://api.moonshot.cn/anthropic", "model": "kimi-k3",
            "api_key": "sk-kimi",
        },
    )

    assert providers.effort_levels_for_model("gpt-5.6-terra") == (
        "low", "medium", "high", "xhigh", "max"
    )
    assert providers.effort_levels_for_model("gpt-5.6-sol") == (
        "low", "medium", "high", "xhigh", "max"
    )
    assert providers.effort_choices_for_model("claude-sonnet-5") == (
        ("low", "low"), ("medium", "medium"), ("high", "high"), ("xhigh", "xhigh"), ("max", "max"),
    )
    # DeepSeek 实测支持完整六档 + 关思考开关;其它第三方兼容端点没验过,不跟着加
    assert providers.effort_choices_for_model("deepseek-v4-flash") == (
        ("off", "关闭"), ("low", "low"), ("medium", "medium"), ("high", "high"),
        ("xhigh", "xhigh"), ("ultra", "ultra"), ("max", "max"),
    )
    assert providers.effort_choices_for_model("kimi-k3") == (
        ("high", "high"), ("max", "max"),
    )
    # CLI 的 --effort 发不出去的档位才要额外注入请求体,其余的返回空串
    assert providers.extra_body_for_effort("off") == '{"thinking": {"type": "disabled"}}'
    assert providers.extra_body_for_effort("ultra") == '{"output_config": {"effort": "ultra"}}'
    assert providers.extra_body_for_effort("max") == ""


def test_available_models_hides_extra_when_declared_provider_missing(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_extra_model(
        "gpt-5.6-luna", "GPT-5.6 Luna（订阅）", group="codex", provider="codex-gpt"
    )
    assert "gpt-5.6-luna" not in {mid for mid, _, _ in providers.available_models([])}


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


# ── sidecar_chat:cron 脚本任务模式的轻量一次性总结 ───────────────────
# 真实 LLM 调用不在这测(要 key+花钱,跟 people_profiles.py._chat_json 同一约定),
# 只测不用发网络请求就能确定结果的分支;调用方(cron/scheduler.py)自己的测试
# 直接 mock 这个函数,不深入到 aiohttp 层。
@pytest.mark.anyio
async def test_sidecar_chat_returns_none_when_no_provider(tmp_path, monkeypatch):
    _point_settings_to(monkeypatch, tmp_path)
    assert await providers.sidecar_chat("总结一下") is None


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


# ── probe_subscription_token:订阅令牌探活的三态 ─────────────────────
def _fake_urlopen(monkeypatch, *, status=None, http_error=None, exc=None):
    """替换 providers 内部 urllib.request.urlopen,不发真实请求。"""
    import urllib.request

    class _Resp:
        def __init__(self, code):
            self.status = code

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake(req, timeout=None):
        if exc is not None:
            raise exc
        if http_error is not None:
            raise http_error
        return _Resp(status)

    monkeypatch.setattr(urllib.request, "urlopen", _fake)


def _http_error(code: int, message: str):
    import io
    import json
    import urllib.error

    body = json.dumps({"type": "error", "error": {"type": "x", "message": message}}).encode()
    return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(body))


def test_probe_token_empty_is_bad():
    assert providers.probe_subscription_token("  ") == ("bad", "未配置")


def test_probe_token_ok(monkeypatch):
    _fake_urlopen(monkeypatch, status=200)
    state, _ = providers.probe_subscription_token("sk-ant-oat01-x")
    assert state == "ok"


def test_probe_token_revoked_is_bad(monkeypatch):
    """401 = 明确被拒,detail 里要带上服务端原话,便于分辨吊销还是别的。"""
    _fake_urlopen(monkeypatch, http_error=_http_error(401, "OAuth access token has been revoked."))
    state, detail = providers.probe_subscription_token("sk-ant-oat01-x")
    assert state == "bad"
    assert "401" in detail and "revoked" in detail


def test_probe_token_rate_limited_is_unknown(monkeypatch):
    """429 说明认证其实过了,不能当成令牌失效。"""
    _fake_urlopen(monkeypatch, http_error=_http_error(429, "rate limited"))
    state, _ = providers.probe_subscription_token("sk-ant-oat01-x")
    assert state == "unknown"


def test_probe_token_network_failure_is_unknown(monkeypatch):
    """断网不能报假 ❌。"""
    _fake_urlopen(monkeypatch, exc=OSError("no route to host"))
    state, detail = providers.probe_subscription_token("sk-ant-oat01-x")
    assert state == "unknown"
    assert "OSError" in detail


def test_model_choices_use_fable51():
    from vococo.gateway.core import MODEL_CHOICES

    assert ("claude-fable-5-1", "Fable 5.1（订阅）") in MODEL_CHOICES
    assert not any(model == "claude-fable" for model, _ in MODEL_CHOICES)


# ── normalize_model_text / match_models_by_text(switch_model 的模糊匹配)──────
# 候选样例与 available_models() 同构:(id, label, group)
_CAND = [
    ("claude-fable-5-1", "Fable 5.1(订阅)", "anthropic"),
    ("claude-opus-5-5", "Opus 5.5(订阅)", "anthropic"),
    ("claude-opus-4-6", "Opus 4.6(订阅)", "anthropic"),
    ("claude-sonnet-5-5", "Sonnet 5.5(订阅)", "anthropic"),
    ("claude-haiku-4-5", "Haiku 4.5(订阅)", "anthropic"),
    ("kimi-k3", "kimi-k3(API)", "api"),
    ("K2.7 Code", "K2.7 Code(API)", "api"),
]


def test_normalize_model_text():
    assert providers.normalize_model_text("claude opus 4.6") == "claudeopus46"
    assert providers.normalize_model_text("Opus-4.6") == "opus46"
    assert providers.normalize_model_text("  K2.7·Code  ") == "k27code"
    assert providers.normalize_model_text("") == ""
    assert providers.normalize_model_text(None) == ""


def test_match_exact_id_gets_full_name_first():
    # 用户直接说规范 id(带横杠)也能归一化全等命中
    hits = providers.match_models_by_text("claude-opus-4-6", _CAND)
    assert [h[0] for h in hits] == ["claude-opus-4-6"]


def test_match_spoken_name_unique():
    hits = providers.match_models_by_text("opus 4.6", _CAND)
    assert [h[0] for h in hits] == ["claude-opus-4-6"]


def test_match_fable5_uses_fable51():
    hits = providers.match_models_by_text("fable 5", _CAND)
    assert [h[0] for h in hits] == ["claude-fable-5-1"]


def test_match_sonnet_uses_sonnet55():
    for text in ("sonnet", "sonnet 5.5", "Sonnet5.5"):
        hits = providers.match_models_by_text(text, _CAND)
        assert [h[0] for h in hits] == ["claude-sonnet-5-5"]


def test_match_short_series_name_ambiguous_keeps_order():
    # "opus" 同时含 opus-5-5 与 opus-4-6:返回全部候选让上层澄清,不擅自挑
    hits = providers.match_models_by_text("opus", _CAND)
    assert [h[0] for h in hits] == ["claude-opus-5-5", "claude-opus-4-6"]


def test_match_multi_word_provider_model():
    # 第三方供应商的自由文本模型名(带空格)同样能按口语命中
    hits = providers.match_models_by_text("k2.7", _CAND)
    assert [h[0] for h in hits] == ["K2.7 Code"]
    hits = providers.match_models_by_text("kimi k3", _CAND)
    assert [h[0] for h in hits] == ["kimi-k3"]


def test_match_miss_and_empty():
    assert providers.match_models_by_text("gpt-7", _CAND) == []
    assert providers.match_models_by_text("", _CAND) == []
    assert providers.match_models_by_text(None, _CAND) == []
