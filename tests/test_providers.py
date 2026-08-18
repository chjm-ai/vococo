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

    assert providers.effort_levels_for_model("gpt-5.6-terra") == (
        "low", "medium", "high", "xhigh", "max"
    )
    assert providers.effort_levels_for_model("gpt-5.6-sol") == (
        "low", "medium", "high", "xhigh", "max"
    )
    assert providers.effort_choices_for_model("claude-sonnet-5") == (
        ("low", "low"), ("medium", "medium"), ("high", "high"), ("xhigh", "xhigh"), ("max", "max"),
    )
    assert providers.effort_choices_for_model("deepseek-v4-flash") == (
        ("high", "high"), ("max", "max"),
    )


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
