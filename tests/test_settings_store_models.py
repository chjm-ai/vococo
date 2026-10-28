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
    err = settings_store.upsert_web_extra_model("claude-opus-5-5", "Opus 5.5（订阅）")
    assert err is None
    models = settings_store.list_web_extra_models()
    assert models == [{"id": "claude-opus-5-5", "label": "Opus 5.5（订阅）"}]


def test_extra_model_can_reuse_provider_and_preserves_metadata(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    settings_store.upsert_web_extra_model(
        "gpt-5.6-sol", "GPT-5.6 Sol（订阅）", group="codex", provider="codex-gpt"
    )
    settings_store.upsert_web_extra_model("gpt-5.6-sol", "GPT-5.6 Sol")
    assert settings_store.list_web_extra_models() == [{
        "id": "gpt-5.6-sol", "label": "GPT-5.6 Sol", "group": "codex", "provider": "codex-gpt",
    }]


def test_extra_model_order_and_explicit_provider_reset(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    assert settings_store.upsert_web_extra_model("claude-new", "自定义", provider="relay", order=-10) is None
    assert settings_store.upsert_web_extra_model("claude-new", "官方", provider="") is None
    assert settings_store.list_web_extra_models() == [
        {"id": "relay/claude-new", "label": "自定义", "provider": "relay", "order": -10},
        {"id": "claude-new", "label": "官方", "provider": ""},
    ]


def test_add_extra_model_no_label_falls_back_to_id(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    settings_store.upsert_web_extra_model("claude-opus-5-5", "")
    assert settings_store.list_web_extra_models()[0]["label"] == "claude-opus-5-5"


def test_add_extra_model_missing_id_errors(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    assert settings_store.upsert_web_extra_model("", "label") == "缺少模型 id"


def test_upsert_extra_model_same_id_overwrites_label(monkeypatch, tmp_path):
    """id 是键;第二次传同一个 id = 编辑(覆盖 label),不是报错。"""
    _point_to(monkeypatch, tmp_path)
    settings_store.upsert_web_extra_model("claude-opus-5-5", "Opus 5.5")
    err = settings_store.upsert_web_extra_model("claude-opus-5-5", "改过的名字")
    assert err is None
    assert settings_store.list_web_extra_models() == [{"id": "claude-opus-5-5", "label": "改过的名字"}]


def test_model_capabilities_validate_preserve_and_reset(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    assert settings_store.upsert_web_extra_model("claude-new", "旧", context_window=123, effort_levels=["off", "ultra"]) is None
    assert settings_store.upsert_web_extra_model("claude-new", "新") is None
    saved = settings_store.list_web_extra_models()[0]
    assert saved["context_window"] == 123 and saved["effort_levels"] == ["off", "ultra"]
    for kwargs in ({"context_window": True}, {"context_window": 0}, {"context_window": 1.5},
                   {"effort_levels": "high"}, {"effort_levels": ["bad"]},
                   {"effort_levels": [[]]}, {"effort_levels": ["low", "low"]}):
        assert settings_store.upsert_web_extra_model("claude-new", "错误", **kwargs)
        assert settings_store.list_web_extra_models()[0] == saved
    assert settings_store.upsert_web_extra_model("claude-new", "新", context_window=None, effort_levels=None) is None
    assert settings_store.list_web_extra_models()[0]["context_window"] is None
    assert settings_store.list_web_extra_models()[0]["effort_levels"] is None


def test_legacy_and_explicit_selection_records_do_not_lose_values(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    settings_store._PATH.write_text('''{"web_extra_models": [
        {"id": "claude-new", "provider": "relay", "context_window": 123},
        {"id": "relay/claude-new", "provider": "relay", "context_window": 456},
        {"id": "claude-new", "context_window": 789}
    ]}''', encoding="utf-8")
    records = {m["id"]: m for m in settings_store.list_web_extra_models()}
    assert records["relay/claude-new"]["context_window"] == 456
    assert records["claude-new"]["context_window"] == 789
    settings_store.upsert_web_extra_model("relay/claude-new", "编辑", effort_levels=[])
    records = {m["id"]: m for m in settings_store.list_web_extra_models()}
    assert records["relay/claude-new"]["context_window"] == 456
    assert records["relay/claude-new"]["effort_levels"] == []
    assert records["claude-new"]["context_window"] == 789


def test_remove_extra_model(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    settings_store.upsert_web_extra_model("claude-opus-5-5", "Opus 5.5")
    settings_store.remove_web_extra_model("claude-opus-5-5")
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
    settings_store.set_web_effort("turbo")   # 非法值 → 清空,不落库
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
    settings_store.set_web_effort("turbo", model="gpt-5.6-sol")
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


def test_legacy_disabled_builtin_models_migrates_to_all_models_list(monkeypatch, tmp_path):
    _point_to(monkeypatch, tmp_path)
    settings_store._PATH.write_text(
        '{"disabled_builtin_models": ["claude-opus-4-6"]}', encoding="utf-8"
    )
    assert settings_store.list_disabled_models() == ["claude-opus-4-6"]


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
