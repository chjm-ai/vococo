"""设置页手动加模型档位 / 第三方服务商的存储层:CRUD + 校验。

用 monkeypatch 把 settings_store._PATH 指向临时文件,不碰真实 data/web_settings.json。
"""
from __future__ import annotations

from pathlib import Path

from vococo.gateway import settings_store


def _point_to(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings_store, "_PATH", tmp_path / "web_settings.json")


def test_add_and_list_extra_model(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    err = settings_store.upsert_web_extra_model("claude-opus-5", "Opus 5（订阅）")
    assert err is None
    models = settings_store.list_web_extra_models()
    assert models == [{"id": "claude-opus-5", "label": "Opus 5（订阅）"}]


def test_extra_model_can_reuse_provider_and_preserves_metadata(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    settings_store.upsert_web_extra_model(
        "gpt-5.6-sol", "GPT-5.6 Sol（订阅）", group="codex", provider="codex-gpt"
    )
    settings_store.upsert_web_extra_model("gpt-5.6-sol", "GPT-5.6 Sol")
    assert settings_store.list_web_extra_models() == [{
        "id": "gpt-5.6-sol", "label": "GPT-5.6 Sol", "group": "codex", "provider": "codex-gpt",
    }]


def test_add_extra_model_no_label_falls_back_to_id(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    settings_store.upsert_web_extra_model("claude-opus-5", "")
    assert settings_store.list_web_extra_models()[0]["label"] == "claude-opus-5"


def test_add_extra_model_missing_id_errors(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    assert settings_store.upsert_web_extra_model("", "label") == "缺少模型 id"


def test_upsert_extra_model_same_id_overwrites_label(monkeypatch, tmp_path):
    """id 是键;第二次传同一个 id = 编辑(覆盖 label),不是报错。"""
    _point_to(monkeypatch, tmp_path)
    settings_store.upsert_web_extra_model("claude-opus-5", "Opus 5")
    err = settings_store.upsert_web_extra_model("claude-opus-5", "改过的名字")
    assert err is None
    assert settings_store.list_web_extra_models() == [{"id": "claude-opus-5", "label": "改过的名字"}]


def test_remove_extra_model(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    settings_store.upsert_web_extra_model("claude-opus-5", "Opus 5")
    settings_store.remove_web_extra_model("claude-opus-5")
    assert settings_store.list_web_extra_models() == []


def test_upsert_and_list_provider(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    err = settings_store.upsert_web_provider(
        "deepseek",
        {"base_url": "https://api.deepseek.com/anthropic", "model": "deepseek-chat",
         "api_key": "sk-xxx", "label": ""},
    )
    assert err is None
    out = settings_store.list_web_providers()
    assert out == [{
        "name": "deepseek", "base_url": "https://api.deepseek.com/anthropic",
        "api_key": "sk-xxx", "model": "deepseek-chat", "label": "",
        "vision": "", "mgmt_key": "",
    }]


def test_upsert_provider_vision_flag(monkeypatch, tmp_path):
    """vision 只接受 "1",其余落空串;存取往返。"""
    _point_to(monkeypatch, tmp_path)
    assert settings_store.upsert_web_provider(
        "codex-gpt",
        {"base_url": "http://127.0.0.1:8317", "model": "gpt-5.6",
         "api_key": "sk-proxy", "vision": "1"},
    ) is None
    assert settings_store.list_web_providers()[0]["vision"] == "1"
    # 非 "1" 的脏值一律落空串
    assert settings_store.upsert_web_provider(
        "x", {"base_url": "https://a.com", "model": "m", "vision": "on"}
    ) is None
    assert settings_store.list_web_providers()[1]["vision"] == ""


def test_upsert_provider_missing_fields_errors(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    assert settings_store.upsert_web_provider("x", {"model": "m"}) == "缺少 base_url"
    assert settings_store.upsert_web_provider("x", {"base_url": "https://a.com"}) == "缺少 model"
    assert settings_store.upsert_web_provider("", {"base_url": "https://a.com", "model": "m"}) == "缺少服务商名称"


def test_upsert_provider_rejects_non_http_scheme(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    err = settings_store.upsert_web_provider("x", {"base_url": "file:///etc/passwd", "model": "m"})
    assert err == "base_url 必须是 http(s) 协议"


def test_remove_provider(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider("deepseek", {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"})
    settings_store.remove_web_provider("deepseek")
    assert settings_store.list_web_providers() == []


def test_web_providers_raw_shape_matches_cc_switch_entry(monkeypatch, tmp_path):
    """providers.py 的 _provider_entries 直接拿这个字典当 cc-switch 条目用,字段名必须一致。"""
    _point_to(monkeypatch, tmp_path)
    settings_store.upsert_web_provider(
        "mine", {"base_url": "https://api.example.com", "model": "my-model", "api_key": "sk-yyy"}
    )
    raw = settings_store.web_providers_raw()
    assert raw == {"mine": {"base_url": "https://api.example.com", "api_key": "sk-yyy",
                             "model": "my-model", "label": "", "vision": "", "mgmt_key": ""}}


# ── web 端思考深度(effort)──────────────────────────────────────────────
def test_web_effort_default_empty(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    assert settings_store.get_web_effort() == ""


def test_set_and_get_web_effort(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    settings_store.set_web_effort("max")
    assert settings_store.get_web_effort() == "max"
    settings_store.set_web_effort("high")
    assert settings_store.get_web_effort() == "high"


def test_web_effort_rejects_invalid_and_clears(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    settings_store.set_web_effort("max")
    settings_store.set_web_effort("ultra")   # 非法值 → 清空,不落库
    assert settings_store.get_web_effort() == ""


def test_web_effort_is_kept_per_model_with_legacy_fallback(monkeypatch, tmp_path):
    """新选择只覆盖当前模型；已有全局 high/max 继续给未迁移模型兜底。"""
    _point_to(monkeypatch, tmp_path)
    settings_store.set_web_effort("max")
    settings_store.set_web_effort("low", model="gpt-5.6-sol")
    settings_store.set_web_effort("xhigh", model="gpt-5.6-luna")

    assert settings_store.get_web_effort("gpt-5.6-sol") == "low"
    assert settings_store.get_web_effort("gpt-5.6-luna") == "xhigh"
    assert settings_store.get_web_effort("deepseek-v4-flash") == "max"


def test_invalid_model_effort_reverts_to_legacy_fallback(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    settings_store.set_web_effort("high")
    settings_store.set_web_effort("max", model="gpt-5.6-sol")
    settings_store.set_web_effort("ultra", model="gpt-5.6-sol")
    assert settings_store.get_web_effort("gpt-5.6-sol") == "high"


# ── 设置页手动隐藏的内置模型档位 ──────────────────────────────────────────
def test_disable_and_list_builtin_model(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    assert settings_store.list_disabled_builtin_models() == []
    settings_store.set_builtin_model_disabled("claude-opus-4-6", True)
    assert settings_store.list_disabled_builtin_models() == ["claude-opus-4-6"]


def test_reenable_builtin_model(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    settings_store.set_builtin_model_disabled("claude-opus-4-6", True)
    settings_store.set_builtin_model_disabled("claude-opus-4-6", False)
    assert settings_store.list_disabled_builtin_models() == []


def test_disable_builtin_model_is_idempotent(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    settings_store.set_builtin_model_disabled("claude-opus-4-6", True)
    settings_store.set_builtin_model_disabled("claude-opus-4-6", True)
    assert settings_store.list_disabled_builtin_models() == ["claude-opus-4-6"]


def test_upsert_provider_mgmt_key(monkeypatch, tmp_path):
    """mgmt_key 存取往返;不填落空串。"""
    _point_to(monkeypatch, tmp_path)
    assert settings_store.upsert_web_provider(
        "codex-gpt", {"base_url": "http://127.0.0.1:8317", "model": "gpt-5.5",
                      "api_key": "sk", "mgmt_key": "mgmt-secret"}
    ) is None
    assert settings_store.list_web_providers()[0]["mgmt_key"] == "mgmt-secret"
    assert settings_store.upsert_web_provider(
        "plain", {"base_url": "https://a.com", "model": "m"}
    ) is None
    assert settings_store.list_web_providers()[1]["mgmt_key"] == ""
