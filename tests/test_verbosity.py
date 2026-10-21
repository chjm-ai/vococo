"""输出详细程度：保存、接口校验与供应商请求参数。"""
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config, providers
from vococo.core.prompt import reply_style_reminder
from vococo.gateway import settings_store
from vococo.gateway.adapters.web import WebAdapter


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def verbosity_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_store, "_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(settings_store, "list_skills", lambda: [])
    settings_store.upsert_web_provider("codex-gpt", {
        "base_url": "http://127.0.0.1:8317", "api_key": "fake",
        "model": "gpt-6.1-sol",
    })


def test_values_persist_per_model_and_default_clears(verbosity_settings):
    assert settings_store.get_web_verbosity("gpt-6.1-sol") == ""
    settings_store.set_web_verbosity("low", model="gpt-6.1-sol")
    settings_store.set_web_verbosity("high", model="gpt-6-astra")
    assert settings_store.get_web_verbosity("gpt-6.1-sol") == "low"
    assert settings_store.get_web_verbosity("gpt-6-astra") == "high"
    settings_store.set_web_verbosity("", model="gpt-6.1-sol")
    assert settings_store.get_web_verbosity("gpt-6.1-sol") == ""
    assert settings_store.get_web_verbosity("gpt-6-astra") == "high"


@pytest.mark.parametrize("value", ["invalid", [], {}, 3, None])
def test_store_rejects_bad_values_without_erasing_choice(verbosity_settings, value):
    settings_store.set_web_verbosity("low", model="gpt-6.1-sol")
    with pytest.raises(ValueError):
        settings_store.set_web_verbosity(value, model="gpt-6.1-sol")
    assert settings_store.get_web_verbosity("gpt-6.1-sol") == "low"


def test_corrupt_saved_values_are_ignored(verbosity_settings):
    settings_store._PATH.write_text(json.dumps({"web_verbosities": {"gpt-6.1-sol": []}}))
    assert settings_store.get_web_verbosity("gpt-6.1-sol") == ""
    settings_store._PATH.write_text('{"web_verbosities": []}')
    assert settings_store.get_web_verbosity("gpt-6.1-sol") == ""


@pytest.mark.anyio
async def test_http_save_reload_reset_and_invalid_requests(verbosity_settings):
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([web.get("/settings", adapter._handle_settings),
                    web.post("/verbosity", adapter._handle_verbosity_switch)])
    async with TestClient(TestServer(app)) as client:
        for value in ("low", "medium", "high", ""):
            response = await client.post("/verbosity", json={"model": "gpt-6.1-sol", "verbosity": value})
            assert response.status == 200
            response = await client.get("/settings")
            state = (await response.json())["models"]["verbosities"]
            assert state["gpt-6.1-sol"]["value"] == value
            assert state["gpt-6.1-sol"]["levels"][1] == ["low", "低"]
            assert "claude-sonnet-5-5" not in state
        for body in ({"model": "unknown", "verbosity": "low"},
                     {"model": "claude-sonnet-5-5", "verbosity": "low"},
                     {"model": "gpt-6.1-sol", "verbosity": []},
                     {"model": [], "verbosity": "low"},
                     {"model": "gpt-6.1-sol", "verbosity": "ultra"},
                     {"model": "gpt-6.1-sol"}, {}):
            assert (await client.post("/verbosity", json=body)).status == 400
        monkey_token = config.WEB_AUTH_TOKEN
        config.WEB_AUTH_TOKEN = "test-auth"
        try:
            assert (await client.post("/verbosity", json={"model": "gpt-6.1-sol", "verbosity": "low"})).status == 401
        finally:
            config.WEB_AUTH_TOKEN = monkey_token


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "deepseek-v4-flash", "gemini-pro", "gpt-4o", "gpt-4.1", "gpt-oss-120b"])
def test_unsupported_models_never_receive_verbosity(model):
    assert providers.verbosity_choices_for_model(model) == ()
    assert providers.extra_body_for_model(model, "", "low") == ""


def test_verbosity_merges_with_effort_without_disabling_it():
    assert json.loads(providers.extra_body_for_model("gpt-6.1-sol", "ultra", "low")) == {
        "output_config": {"effort": "ultra"}, "text": {"verbosity": "low"},
    }
    assert providers.extra_body_for_model("gpt-6.1-sol", "high", "") == ""
    assert json.loads(providers.extra_body_for_model("deepseek-v4-flash", "off", "low")) == {
        "thinking": {"type": "disabled"},
    }


def test_selected_detail_does_not_keep_concise_instruction():
    assert "表达上请克制" in reply_style_reminder("gpt-6.1-sol", "low")
    assert "表达上请克制" not in reply_style_reminder("gpt-6.1-sol", "high")
    assert "详细" in reply_style_reminder("gpt-6.1-sol", "high")
    assert "兼顾" in reply_style_reminder("gpt-6.1-sol", "medium")
    assert reply_style_reminder("claude-sonnet-5-5", "high") == ""
