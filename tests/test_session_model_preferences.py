"""会话模型、思考深度和优先档不能串到其他会话。"""
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config, providers
from vococo.gateway import settings_store
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import session_store

GPT = "gpt-6.1-sol"
OTHER = "gpt-6.1-terra"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def preference_app(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(config, "MODEL", GPT)
    monkeypatch.setattr(settings_store, "_PATH", isolated / "settings.json")
    settings_store.upsert_web_provider("codex-gpt", {
        "base_url": "http://127.0.0.1:8317", "api_key": "fake",
        "mgmt_key": "fake", "model": GPT,
    })
    monkeypatch.setattr(providers, "available_models", lambda *a: [
        (GPT, "Sol", "codex"), (OTHER, "Terra", "codex"),
    ])
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([
        web.get("/models", adapter._handle_models),
        web.post("/model", adapter._handle_model_switch),
        web.post("/effort", adapter._handle_effort_switch),
        web.post("/service-tier", adapter._handle_service_tier_switch),
    ])
    return app


@pytest.mark.anyio
async def test_sessions_keep_model_effort_and_priority_independent(preference_app):
    async with TestClient(TestServer(preference_app)) as client:
        for conv in ("session:a", "session:b"):
            await client.post("/model", json={"conv": conv, "model": GPT})
            assert (await client.get("/models", params={"conv": conv})).status == 200
        for path, body in [
            ("/effort", {"effort": "high"}),
            ("/service-tier", {"tier": "priority"}),
        ]:
            response = await client.post(path, json={"conv": "session:a", "model": GPT, **body})
            assert response.status == 200
        a = await (await client.get("/models?conv=session:a")).json()
        b = await (await client.get("/models?conv=session:b")).json()
        assert a["efforts"][GPT]["value"] == "high"
        assert a["service_tiers"][GPT]["value"] == "priority"
        assert b["efforts"][GPT]["value"] == ""
        assert b["service_tiers"][GPT]["value"] == ""
        assert settings_store.get_web_effort(GPT) == ""
        assert settings_store.get_web_service_tier(GPT) == ""
        await client.post("/model", json={"conv": "session:b", "model": OTHER})
        a = await (await client.get("/models?conv=session:a")).json()
        b = await (await client.get("/models?conv=session:b")).json()
        assert a["chosen_model"] == GPT
        assert b["chosen_model"] == OTHER
        assert session_store.get_chosen_model("session:a") == GPT
        from vococo.memory import _db
        _db.reset()
        a = await (await client.get("/models?conv=session:a")).json()
        assert a["efforts"][GPT]["value"] == "high"
        assert a["service_tiers"][GPT]["value"] == "priority"


@pytest.mark.anyio
@pytest.mark.parametrize("conv", [None, [], {}, 42, True, "", " "])
async def test_invalid_conversation_cannot_change_global_defaults(preference_app, conv):
    async with TestClient(TestServer(preference_app)) as client:
        for path, body in [("/effort", {"effort": "high"}),
                           ("/service-tier", {"tier": "priority"})]:
            response = await client.post(path, json={"conv": conv, "model": GPT, **body})
            assert response.status == 400
    assert settings_store.get_web_effort(GPT) == ""
    assert settings_store.get_web_service_tier(GPT) == ""


def test_defaults_inherited_once_and_explicit_priority_off_survives(preference_app):
    from vococo.core.model_preferences import for_model

    settings_store.set_web_effort("high", model=GPT)
    settings_store.set_web_service_tier("priority", model=GPT)
    assert for_model("session:a", GPT) == {"effort": "high", "service_tier": "priority"}
    session_store.set_model_preferences("session:a", GPT, {"service_tier": ""})
    settings_store.set_web_effort("low", model=GPT)
    assert for_model("session:a", GPT) == {"effort": "high", "service_tier": ""}
    assert for_model("session:b", GPT) == {"effort": "low", "service_tier": "priority"}
    assert for_model("session:a", OTHER) == {"effort": "", "service_tier": ""}


@pytest.mark.anyio
@pytest.mark.parametrize("tier", ["", "priority"])
async def test_actual_turn_uses_session_options_not_global_defaults(preference_app, monkeypatch, tier):
    from vococo.core import agent

    settings_store.set_web_effort("low", model=GPT)
    settings_store.set_web_service_tier("priority" if not tier else "", model=GPT)
    session_store.set_model_preferences("session:a", GPT, {"effort": "high", "service_tier": tier})
    monkeypatch.setattr(agent.agents, "runtime_for_session", lambda key: {})
    monkeypatch.setattr(agent, "configured_context_window", lambda *a: None)

    class Captured(Exception):
        pass

    def capture(model, effort, verbosity, summary, service_tier, **kwargs):
        assert effort == "high"
        assert service_tier == tier
        raise Captured

    monkeypatch.setattr(providers, "extra_body_for_model", capture)
    with pytest.raises(Captured):
        async for _ in agent.stream_turn([], "测试", model=GPT, session_key="session:a"):
            pass
