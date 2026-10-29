"""Web 模型与思考深度接口：按模型返回档位、按模型持久化选择。"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo.gateway.adapters.web import WebAdapter
from vococo.gateway import settings_store


@pytest.fixture
def models_app():
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([
        web.get("/models", adapter._handle_models),
        web.post("/effort", adapter._handle_effort_switch),
    ])
    return app


@pytest.fixture
def settings_models_app():
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([
        web.get("/settings", adapter._handle_settings),
        web.get("/models", adapter._handle_models),
        web.post("/settings/model", adapter._handle_settings_model),
    ])
    return app


@pytest.fixture(autouse=True)
def _no_web_auth(monkeypatch):
    """测试不依赖本机 .env 是否配了 WEB_AUTH_TOKEN。"""
    from vococo import config

    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")


@pytest.mark.anyio
async def test_web_default_fallback_keeps_claude_alias(models_app, tmp_path, monkeypatch):
    from vococo import config
    monkeypatch.setattr(settings_store, "_PATH", tmp_path / "web_settings.json")
    upstream = "claude-sonnet-5-5"
    alias = f"cheapcoding-sonnet/{upstream}"
    monkeypatch.setattr(config, "MODEL", alias)
    settings_store.upsert_web_provider("cheapcoding-sonnet", {
        "base_url": "https://cheapcoding.cc", "api_key": "test", "model": upstream,
    })
    settings_store.set_web_effort("low", model=upstream)
    async with TestClient(TestServer(models_app)) as client:
        response = await client.get("/models")
        data = await response.json()
        assert data["default"] == alias
        response = await client.post("/effort", json={"effort": "max"})
        assert response.status == 200
        assert (await response.json())["model"] == alias
    assert settings_store.get_web_effort(alias) == "max"
    assert settings_store.get_web_effort(upstream) == "low"


@pytest.mark.anyio
async def test_settings_keep_same_name_claude_capabilities_separate(settings_models_app, tmp_path, monkeypatch):
    from vococo import providers
    from vococo.core.agent import configured_context_window

    monkeypatch.setattr(settings_store, "_PATH", tmp_path / "web_settings.json")
    model = "claude-sonnet-5-5"
    alias = f"relay/{model}"
    settings_store.upsert_web_provider("relay", {
        "base_url": "https://relay.example", "api_key": "test", "model": model,
    })
    # 模拟升级前的裸 ID 记录，编辑官方模型不能覆盖它。
    settings_store.upsert_web_extra_model(model, "旧中转", provider="relay", context_window=123, effort_levels=["low"])
    async with TestClient(TestServer(settings_models_app)) as client:
        for model_id, window, levels in [(model, 456, ["high"]), (alias, 789, ["max"])]:
            response = await client.post("/settings/model", json={
                "id": model_id, "label": model_id, "context_window": window, "effort_levels": levels,
            })
            assert response.status == 200, await response.text()
        response = await client.get("/settings")
        data = await response.json()
        choices = {m["id"]: m for m in data["models"]["choices"]}
        assert choices[model]["context_window"] == 456
        assert choices[alias]["context_window"] == 789
        assert choices[alias]["config_id"] == alias
        assert (await client.post("/settings/model", json={"action": "remove", "id": model})).status == 200
    assert configured_context_window(alias, model) == 789
    assert configured_context_window(model, model) is None
    assert providers.effort_levels_for_model(alias) == ("max",)
    assert settings_store.list_web_extra_models()[0]["id"] == alias


@pytest.fixture
def model_settings(monkeypatch):
    choices = [
        ("gpt-5.6-terra", "GPT-5.6 Terra（订阅）", "codex"),
        ("deepseek-v4-flash", "DeepSeek V4 Flash（API）", "api"),
    ]
    levels = {
        "gpt-5.6-terra": (("low", "low"), ("medium", "medium"), ("high", "high"),
                            ("xhigh", "xhigh"), ("max", "max")),
        "deepseek-v4-flash": (("off", "关闭"), ("high", "high"), ("max", "max")),
    }
    saved = {"gpt-5.6-terra": "xhigh", "deepseek-v4-flash": "max"}

    monkeypatch.setattr(
        "vococo.gateway.adapters.web.providers.available_models", lambda *a: choices
    )
    monkeypatch.setattr(
        "vococo.gateway.adapters.web.providers.resolve", lambda *a: ("gpt-5.6-terra", {})
    )
    monkeypatch.setattr(
        "vococo.gateway.adapters.web.providers.effort_choices_for_model", lambda model: levels[model]
    )
    monkeypatch.setattr(
        "vococo.gateway.adapters.web.providers.effort_levels_for_model",
        lambda model: tuple(level for level, _ in levels[model]),
    )
    monkeypatch.setattr(
        "vococo.gateway.adapters.web.settings_store.get_web_default_model", lambda: "gpt-5.6-terra"
    )
    monkeypatch.setattr(
        "vococo.gateway.adapters.web.settings_store.get_web_effort",
        lambda model="": saved.get(model, ""),
    )

    def set_effort(effort: str, *, model: str = "") -> None:
        saved[model] = effort

    monkeypatch.setattr(
        "vococo.gateway.adapters.web.settings_store.set_web_effort", set_effort
    )
    return saved


@pytest.mark.anyio
async def test_models_return_effort_levels_per_model(models_app, model_settings):
    async with TestClient(TestServer(models_app)) as client:
        resp = await client.get("/models", headers={"X-Auth-Token": ""})
        assert resp.status == 200
        data = await resp.json()

    assert data["effort"] == "xhigh"  # 保留给旧前端的当前默认模型字段
    assert data["efforts"]["gpt-5.6-terra"] == {
        "levels": [["low", "low"], ["medium", "medium"], ["high", "high"],
                   ["xhigh", "xhigh"], ["max", "max"]],
        "value": "xhigh",
    }
    assert data["efforts"]["deepseek-v4-flash"] == {
        "levels": [["off", "关闭"], ["high", "high"], ["max", "max"]],
        "value": "max",
    }


@pytest.mark.anyio
async def test_effort_switch_validates_model_specific_levels(models_app, model_settings):
    async with TestClient(TestServer(models_app)) as client:
        resp = await client.post(
            "/effort", headers={"X-Auth-Token": ""},
            json={"model": "gpt-5.6-terra", "effort": "low"},
        )
        assert resp.status == 200
        assert await resp.json() == {"ok": True, "model": "gpt-5.6-terra", "effort": "low"}

        unsupported = await client.post(
            "/effort", headers={"X-Auth-Token": ""},
            json={"model": "deepseek-v4-flash", "effort": "xhigh"},
        )
        assert unsupported.status == 400
        assert (await unsupported.json())["error"] == "deepseek-v4-flash 仅支持 off/high/max"

        # DeepSeek 独有的「关闭」档要能落库;GPT 侧没这档,传了应被拒
        off = await client.post(
            "/effort", headers={"X-Auth-Token": ""},
            json={"model": "deepseek-v4-flash", "effort": "off"},
        )
        assert off.status == 200
        assert await off.json() == {"ok": True, "model": "deepseek-v4-flash", "effort": "off"}

        not_for_gpt = await client.post(
            "/effort", headers={"X-Auth-Token": ""},
            json={"model": "gpt-5.6-terra", "effort": "off"},
        )
        assert not_for_gpt.status == 400

    assert model_settings["gpt-5.6-terra"] == "low"
    assert model_settings["deepseek-v4-flash"] == "off"


@pytest.mark.anyio
async def test_settings_models_match_panel_choices_and_can_hide_kimi(
    settings_models_app, monkeypatch, tmp_path,
):
    monkeypatch.setattr(settings_store, "_PATH", tmp_path / "web_settings.json")
    monkeypatch.setattr(settings_store, "list_skills", lambda: [])
    assert settings_store.upsert_web_provider(
        "kimi", {
            "base_url": "https://api.kimi.com/coding",
            "model": "kimi-k3",
            "api_key": "sk-kimi",
        },
    ) is None

    async with TestClient(TestServer(settings_models_app)) as client:
        before = await client.get("/settings", headers={"X-Auth-Token": ""})
        assert before.status == 200
        choices = (await before.json())["models"]["choices"]
        assert {item["id"]: item for item in choices}["kimi-k3"] == {
            "id": "kimi-k3", "label": "kimi-k3（订阅）", "group": "kimi", "disabled": False,
            "provider": "kimi", "order": 1000, "builtin": False, "config_id": "kimi-k3",
            "context_window": None, "effort_levels": None,
        }

        hidden = await client.post(
            "/settings/model", headers={"X-Auth-Token": ""},
            json={"action": "toggle", "id": "kimi-k3", "disabled": True},
        )
        assert hidden.status == 200

        after = await client.get("/settings", headers={"X-Auth-Token": ""})
        choices = (await after.json())["models"]["choices"]

    assert {item["id"]: item["disabled"] for item in choices}["kimi-k3"] is True


@pytest.mark.anyio
async def test_model_configuration_hot_reload_preserves_default(settings_models_app, monkeypatch, tmp_path):
    from vococo import providers
    monkeypatch.setattr(settings_store, "_PATH", tmp_path / "web_settings.json")
    monkeypatch.setattr(settings_store, "list_skills", lambda: [])
    settings_store.upsert_web_provider("codex-gpt", {
        "base_url": "http://127.0.0.1:8317", "api_key": "test", "model": "gpt-6.1-sol",
    })
    settings_store.set_web_default_model("gpt-6.1-sol")
    model = "gpt-new-test"
    async with TestClient(TestServer(settings_models_app)) as client:
        response = await client.post("/settings/model", json={
            "id": model, "label": "新 GPT", "provider": "codex-gpt", "order": 1,
            "context_window": 999999, "effort_levels": ["off", "high"],
        })
        assert response.status == 200
        data = await (await client.get("/models")).json()
        assert data["default"] == "gpt-6.1-sol"
        codex = [m for m in data["choices"] if m[2] == "codex"]
        assert codex[0][:2] == [model, "新 GPT"]
        assert providers.resolve(model, "claude-sonnet-5-5")[1]["ANTHROPIC_API_KEY"] == "test"
        configured = next(m for m in (await (await client.get("/settings")).json())["models"]["choices"] if m["id"] == model)
        assert configured["context_window"] == 999999
        assert configured["effort_levels"] == ["off", "high"]
        assert providers.effort_levels_for_model(model) == ("off", "high")
        response = await client.post("/settings/model", json={
            "id": "gpt-6.1-terra", "label": "Terra 自定义", "order": 0,
        })
        assert response.status == 200
        choices = (await (await client.get("/settings")).json())["models"]["choices"]
        terra = next(m for m in choices if m["id"] == "gpt-6.1-terra")
        assert terra == {"id": "gpt-6.1-terra", "label": "Terra 自定义", "group": "codex",
                         "provider": "codex-gpt", "order": 0, "builtin": True, "disabled": False,
                         "config_id": "gpt-6.1-terra", "context_window": None, "effort_levels": None}
        data = await (await client.get("/models")).json()
        assert next(m for m in data["choices"] if m[2] == "codex")[0] == "gpt-6.1-terra"
        for disabled in (True, False):
            response = await client.post("/settings/model", json={"action": "toggle", "id": model, "disabled": disabled})
            assert response.status == 200
            data = await (await client.get("/models")).json()
            assert (model in {m[0] for m in data["choices"]}) is not disabled
        response = await client.post("/settings/model", json={"action": "remove", "id": model})
        assert response.status == 200
        data = await (await client.get("/models")).json()
        assert model not in {m[0] for m in data["choices"]}
        assert data["default"] == "gpt-6.1-sol"


@pytest.mark.anyio
@pytest.mark.parametrize("body", [
    {"id": "gpt-new", "provider": "missing"},
    {"id": "gpt-new", "provider": ""},
    {"id": "gpt-new", "provider": []},
    {"id": "gpt-6.1-terra", "provider": "other"},
    {"id": "claude-sonnet-5-5", "provider": "codex-gpt"},
    {"id": "claude-new", "label": []},
    {"id": "claude-new", "order": True},
    {"id": "claude-new", "order": 1.5},
    {"id": "claude-new", "order": 1_000_001},
    {"id": "claude-new", "context_window": False},
    {"id": "claude-new", "effort_levels": ["invalid"]},
    {"id": []}, {"id": "claude-new", "action": "unknown"},
])
async def test_model_configuration_rejects_invalid_input(settings_models_app, monkeypatch, tmp_path, body):
    monkeypatch.setattr(settings_store, "_PATH", tmp_path / "web_settings.json")
    settings_store.upsert_web_provider("codex-gpt", {
        "base_url": "http://127.0.0.1:8317", "api_key": "test", "model": "gpt-6.1-sol",
    })
    async with TestClient(TestServer(settings_models_app)) as client:
        response = await client.post("/settings/model", json=body)
        assert response.status == 400
        assert (await response.json())["error"]
    assert settings_store.list_web_extra_models() == []


@pytest.mark.anyio
async def test_custom_model_can_change_provider_and_group(settings_models_app, monkeypatch, tmp_path):
    from vococo import providers
    monkeypatch.setattr(settings_store, "_PATH", tmp_path / "web_settings.json")
    for name, url in (("codex-gpt", "http://127.0.0.1:8317"), ("other", "https://api.example.com")):
        settings_store.upsert_web_provider(name, {"base_url": url, "api_key": name, "model": "configured-model"})
    async with TestClient(TestServer(settings_models_app)) as client:
        for provider, group in (("codex-gpt", "codex"), ("other", "api")):
            response = await client.post("/settings/model", json={"id": "gpt-custom", "provider": provider, "order": -1})
            assert response.status == 200
            assert providers.resolve("gpt-custom", "claude-sonnet-5-5")[1]["ANTHROPIC_API_KEY"] == provider
            choices = providers.available_models([])
            assert ("gpt-custom", "gpt-custom", group) in choices
