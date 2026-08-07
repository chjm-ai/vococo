"""Web 模型与思考深度接口：按模型返回档位、按模型持久化选择。"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo.gateway.adapters.web import WebAdapter


@pytest.fixture
def models_app():
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([
        web.get("/models", adapter._handle_models),
        web.post("/effort", adapter._handle_effort_switch),
    ])
    return app


@pytest.fixture(autouse=True)
def _no_web_auth(monkeypatch):
    """测试不依赖本机 .env 是否配了 WEB_AUTH_TOKEN。"""
    from vococo import config

    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")


@pytest.fixture
def model_settings(monkeypatch):
    choices = [
        ("gpt-5.6-terra", "GPT-5.6 Terra（订阅）", "codex"),
        ("deepseek-v4-flash", "DeepSeek V4 Flash（API）", "api"),
    ]
    levels = {
        "gpt-5.6-terra": (("low", "low"), ("medium", "medium"), ("high", "high"),
                            ("xhigh", "xhigh"), ("max", "max")),
        "deepseek-v4-flash": (("high", "high"), ("max", "max")),
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
        "levels": [["high", "high"], ["max", "max"]],
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
        assert (await unsupported.json())["error"] == "deepseek-v4-flash 仅支持 high/max"

    assert model_settings["gpt-5.6-terra"] == "low"
    assert model_settings["deepseek-v4-flash"] == "max"
