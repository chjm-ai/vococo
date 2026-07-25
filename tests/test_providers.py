"""cc-switch 供应商集成:读取 ~/.hermes/config.yaml,解析激活供应商 + 注入 env。

用 monkeypatch 把 hermes_config_path 指向临时 yaml,不碰真实 ~/.hermes。
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from claude_hermes import providers
from claude_hermes.gateway import settings_store


def _write_config(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def _point_to(monkeypatch, path: Path) -> None:
    monkeypatch.setattr(providers, "hermes_config_path", lambda: path)


def _point_settings_to(monkeypatch, tmp_path: Path) -> None:
    """把设置页存储(web_extra_models/web_providers)也指向临时文件,不碰真实数据。"""
    monkeypatch.setattr(settings_store, "_PATH", tmp_path / "web_settings.json")


DEEPSEEK_CONFIG = """
    model:
      provider: deepseek
      base_url: https://api.deepseek.com/anthropic
      default: deepseek-chat
    custom_providers:
      - name: deepseek
        base_url: https://api.deepseek.com/anthropic
        api_key: sk-deepseek-xxx
        model: deepseek-chat
      - name: kimi
        base_url: https://api.moonshot.cn/anthropic
        api_key: sk-kimi-yyy
        model: kimi-k2-0711-preview
"""


def test_active_third_party(tmp_path, monkeypatch):
    _point_to(monkeypatch, _write_config(tmp_path, DEEPSEEK_CONFIG))
    active = providers.load_active()
    assert active is not None
    assert active.name == "deepseek"
    assert active.model == "deepseek-chat"
    assert active.is_official is False
    assert providers.has_active_third_party() is True


def test_resolve_active_injects_env(tmp_path, monkeypatch):
    _point_to(monkeypatch, _write_config(tmp_path, DEEPSEEK_CONFIG))
    model, env = providers.resolve(None, "claude-sonnet-5")
    assert model == "deepseek-chat"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-deepseek-xxx"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""  # 订阅 token 被清空,避免 401


def test_resolve_session_override_to_kimi(tmp_path, monkeypatch):
    # 全局激活 deepseek,但会话 /model 选了 kimi 的模型 → 应切到 kimi 的端点
    _point_to(monkeypatch, _write_config(tmp_path, DEEPSEEK_CONFIG))
    model, env = providers.resolve("kimi-k2-0711-preview", "claude-sonnet-5")
    assert model == "kimi-k2-0711-preview"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.moonshot.cn/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-kimi-yyy"


def test_resolve_session_override_to_official(tmp_path, monkeypatch):
    # 会话选官方 claude 模型 → 不注入第三方 env(走订阅),即使全局激活第三方
    _point_to(monkeypatch, _write_config(tmp_path, DEEPSEEK_CONFIG))
    model, env = providers.resolve("claude-opus-4-8", "claude-sonnet-5")
    assert model == "claude-opus-4-8"
    assert env == {}


def test_official_active_no_env(tmp_path, monkeypatch):
    _point_to(monkeypatch, _write_config(tmp_path, """
        model:
          provider: claude
          base_url: https://api.anthropic.com
          default: claude-opus-4-8
    """))
    active = providers.load_active()
    assert active is not None
    assert active.is_official is True
    assert providers.has_active_third_party() is False
    model, env = providers.resolve(None, "claude-sonnet-5")
    assert model == "claude-opus-4-8"
    assert env == {}


def test_missing_file_falls_back(tmp_path, monkeypatch):
    _point_to(monkeypatch, tmp_path / "does-not-exist.yaml")
    assert providers.load_active() is None
    assert providers.has_active_third_party() is False
    model, env = providers.resolve(None, "claude-sonnet-5")
    assert model == "claude-sonnet-5"
    assert env == {}


def test_empty_and_malformed_yaml(tmp_path, monkeypatch):
    _point_to(monkeypatch, _write_config(tmp_path, "   \n"))
    assert providers.load_active() is None
    _point_to(monkeypatch, _write_config(tmp_path, "model: [unclosed\n"))
    assert providers.load_active() is None  # 解析失败也不抛,回落 None


def test_available_models_lists_configured(tmp_path, monkeypatch):
    _point_to(monkeypatch, _write_config(tmp_path, DEEPSEEK_CONFIG))
    defaults = [("claude-opus-4-8", "Opus"), ("claude-sonnet-5", "Sonnet")]
    out = providers.available_models(defaults)
    by_id = {mid: (label, group) for mid, label, group in out}
    ids = list(by_id)
    assert ids[:2] == ["claude-opus-4-8", "claude-sonnet-5"]  # 官方档在前
    assert by_id["claude-opus-4-8"][1] == "anthropic"
    assert by_id["deepseek-chat"][1] == "api"
    # kimi 走 api.moonshot.cn(按量 API key),不是 api.kimi.com 订阅套餐 → 分组是 api 不是 kimi
    assert by_id["kimi-k2-0711-preview"][1] == "api"


# ── 设置页手动加的模型/服务商:合并进 providers.py 的既有逻辑 ──────────────
def test_available_models_includes_web_extra_model(tmp_path, monkeypatch):
    _point_to(monkeypatch, tmp_path / "does-not-exist.yaml")  # 没装 cc-switch
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_extra_model("claude-opus-5", "Opus 5（订阅）")
    defaults = [("claude-sonnet-5", "Sonnet 5（订阅）")]
    out = providers.available_models(defaults)
    by_id = {mid: (label, group) for mid, label, group in out}
    assert by_id["claude-opus-5"] == ("Opus 5（订阅）", "anthropic")


def test_available_models_dedups_web_extra_model_against_defaults(tmp_path, monkeypatch):
    _point_to(monkeypatch, tmp_path / "does-not-exist.yaml")
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_extra_model("claude-sonnet-5", "重复")
    defaults = [("claude-sonnet-5", "Sonnet 5（订阅）")]
    out = providers.available_models(defaults)
    assert [mid for mid, _, _ in out].count("claude-sonnet-5") == 1


def test_available_models_hides_disabled_builtin(tmp_path, monkeypatch):
    _point_to(monkeypatch, tmp_path / "does-not-exist.yaml")
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.set_builtin_model_disabled("claude-opus-4-6", True)
    defaults = [("claude-opus-4-6", "Opus 4.6（订阅）"), ("claude-sonnet-5", "Sonnet 5（订阅）")]
    out = providers.available_models(defaults)
    ids = [mid for mid, _, _ in out]
    assert "claude-opus-4-6" not in ids
    assert "claude-sonnet-5" in ids


def test_available_models_reenabled_builtin_comes_back(tmp_path, monkeypatch):
    _point_to(monkeypatch, tmp_path / "does-not-exist.yaml")
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.set_builtin_model_disabled("claude-opus-4-6", True)
    settings_store.set_builtin_model_disabled("claude-opus-4-6", False)
    defaults = [("claude-opus-4-6", "Opus 4.6（订阅）")]
    out = providers.available_models(defaults)
    assert [mid for mid, _, _ in out] == ["claude-opus-4-6"]


def test_available_models_includes_web_provider_without_cc_switch_config(tmp_path, monkeypatch):
    """回归:之前 config.yaml 缺失时 available_models 会提前 return,把 web_providers 漏掉。"""
    _point_to(monkeypatch, tmp_path / "does-not-exist.yaml")
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "mine", {"base_url": "https://api.example.com", "model": "my-model", "api_key": "sk-yyy"}
    )
    out = providers.available_models([])
    by_id = {mid: (label, group) for mid, label, group in out}
    assert by_id["my-model"] == ("my-model（API）", "api")


# ── list_cc_switch_providers:设置页只读展示 cc-switch 配置的供应商 ──────────
def test_list_cc_switch_providers_returns_deepseek_and_kimi(tmp_path, monkeypatch):
    _point_to(monkeypatch, _write_config(tmp_path, DEEPSEEK_CONFIG))
    out = providers.list_cc_switch_providers()
    by_name = {p["name"]: p for p in out}
    assert by_name["deepseek"] == {
        "name": "deepseek", "base_url": "https://api.deepseek.com/anthropic",
        "model": "deepseek-chat", "has_key": True,
    }
    assert by_name["kimi"]["model"] == "kimi-k2-0711-preview"


def test_list_cc_switch_providers_excludes_web_providers(tmp_path, monkeypatch):
    """web_providers 已经在设置页另有一节展示,cc-switch 只读列表不该重复列出。"""
    _point_to(monkeypatch, _write_config(tmp_path, DEEPSEEK_CONFIG))
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "mine", {"base_url": "https://api.example.com", "model": "my-model", "api_key": "sk-yyy"}
    )
    out = providers.list_cc_switch_providers()
    assert "mine" not in {p["name"] for p in out}
    assert "deepseek" in {p["name"] for p in out}  # cc-switch 自己的那些还在


def test_list_cc_switch_providers_empty_when_no_config(tmp_path, monkeypatch):
    _point_to(monkeypatch, tmp_path / "does-not-exist.yaml")
    assert providers.list_cc_switch_providers() == []


def test_resolve_picks_up_web_provider_via_explicit_model(tmp_path, monkeypatch):
    _point_to(monkeypatch, tmp_path / "does-not-exist.yaml")
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "mine", {"base_url": "https://api.example.com", "model": "my-model", "api_key": "sk-yyy"}
    )
    model, env = providers.resolve("my-model", "claude-sonnet-5")
    assert model == "my-model"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.example.com"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-yyy"


def test_web_provider_overrides_cc_switch_entry_with_same_name(tmp_path, monkeypatch):
    _point_to(monkeypatch, _write_config(tmp_path, DEEPSEEK_CONFIG))
    _point_settings_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "deepseek", {"base_url": "https://web-override.example.com", "model": "deepseek-chat", "api_key": "sk-web"}
    )
    model, env = providers.resolve("deepseek-chat", "claude-sonnet-5")
    assert model == "deepseek-chat"
    assert env["ANTHROPIC_BASE_URL"] == "https://web-override.example.com"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-web"


def test_sidecar_env_finds_named_provider(tmp_path, monkeypatch):
    # 标题总结兜底:按名取 deepseek 的 (model, env),大小写不敏感
    _point_to(monkeypatch, _write_config(tmp_path, DEEPSEEK_CONFIG))
    result = providers.sidecar_env("DeepSeek")
    assert result is not None
    model, env = result
    assert model == "deepseek-chat"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-deepseek-xxx"
    assert providers.sidecar_env("nonexistent") is None


def test_sidecar_env_prefers_exact_name(tmp_path, monkeypatch):
    # 同时配了 deepseek / deepseek-pro:按名找「deepseek」必须精确命中便宜档,不能撞到 pro
    _point_to(monkeypatch, _write_config(tmp_path, """
        custom_providers:
          - name: deepseek-pro
            base_url: https://api.deepseek.com/anthropic
            api_key: sk-pro
            model: deepseek-v4-pro
          - name: deepseek
            base_url: https://api.deepseek.com/anthropic
            api_key: sk-flash
            model: deepseek-v4-flash
    """))
    result = providers.sidecar_env("deepseek")
    assert result is not None
    assert result[0] == "deepseek-v4-flash"


def test_legacy_camelcase_fields(tmp_path, monkeypatch):
    # 老版 DeepLink 导入会留下 baseUrl / apiKey,应能兼容读取
    _point_to(monkeypatch, _write_config(tmp_path, """
        model:
          provider: relay
          default: some-model
        custom_providers:
          - name: relay
            baseUrl: https://relay.example.com/anthropic
            apiKey: sk-relay-zzz
            model: some-model
    """))
    model, env = providers.resolve(None, "claude-sonnet-5")
    assert model == "some-model"
    assert env["ANTHROPIC_BASE_URL"] == "https://relay.example.com/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-relay-zzz"


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
