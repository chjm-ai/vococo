"""GPT 优先档：按模型保存、本地代理能力校验及 Web 接口。"""
from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config, providers
from vococo.gateway import settings_store
from vococo.gateway.adapters.web import WebAdapter


GPT = "gpt-6.1-sol"
OTHER_GPT = "gpt-6-astra"
CLAUDE = "claude-sonnet-5-5"
DEEPSEEK = "deepseek-flash"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def priority_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_store, "_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(config, "MODEL", GPT)
    settings_store.upsert_web_provider("codex-gpt", {
        "base_url": "http://127.0.0.1:8317", "api_key": "fake",
        "mgmt_key": "fake-management", "model": GPT,
    })


def test_store_default_model_isolation_and_clear():
    assert settings_store.get_web_service_tier(GPT) == ""
    settings_store.set_web_service_tier("priority", model=GPT)
    assert settings_store.get_web_service_tier(GPT) == "priority"
    assert settings_store.get_web_service_tier(OTHER_GPT) == ""
    settings_store.set_web_service_tier("priority", model=OTHER_GPT)
    settings_store.set_web_service_tier("", model=GPT)
    assert settings_store.get_web_service_tier(GPT) == ""
    assert settings_store.get_web_service_tier(OTHER_GPT) == "priority"


@pytest.mark.parametrize("tier", ["auto", "default", "flex", "PRIORITY", " priority ", [], {}, 1, True, None])
def test_store_rejects_invalid_tier_without_erasing_choice(tier):
    settings_store.set_web_service_tier("priority", model=GPT)
    with pytest.raises(ValueError):
        settings_store.set_web_service_tier(tier, model=GPT)
    assert settings_store.get_web_service_tier(GPT) == "priority"


@pytest.mark.parametrize("model", ["", " \t", [], {}, 1, True, None])
def test_store_rejects_empty_or_non_string_model(model):
    with pytest.raises(ValueError):
        settings_store.set_web_service_tier("priority", model=model)
    assert settings_store.get_web_service_tier(GPT) == ""


@pytest.mark.parametrize("value", [[], {}, 1, True, None, "flex"])
def test_corrupt_saved_tier_is_ignored(value):
    settings_store._PATH.write_text(json.dumps({"web_service_tiers": {GPT: value}}))
    assert settings_store.get_web_service_tier(GPT) == ""


@pytest.mark.parametrize("value", [[], "priority", 1, True, None])
def test_corrupt_saved_tier_container_is_ignored(value):
    settings_store._PATH.write_text(json.dumps({"web_service_tiers": value}))
    assert settings_store.get_web_service_tier(GPT) == ""


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
@pytest.mark.parametrize("model", ["gpt-5", "gpt-5.6-sol", GPT])
def test_current_gpt_on_local_codex_supports_priority(host, model):
    settings_store.upsert_web_provider("codex-gpt", {
        "base_url": f"http://{host}:8317", "api_key": "fake",
        "mgmt_key": "fake-management", "model": model,
    })
    assert providers.supports_priority_tier(model) is True


@pytest.mark.parametrize("url,key", [
    ("https://api.openai.com", "fake-management"),
    ("http://192.168.1.2:8317", "fake-management"),
    ("http://127.0.0.1.example.com:8317", "fake-management"),
    ("http://localhost.example.com:8317", "fake-management"),
    ("http://127.0.0.1:8317", ""),
])
def test_gpt_without_local_codex_management_does_not_support_priority(url, key):
    settings_store.upsert_web_provider("codex-gpt", {
        "base_url": url, "api_key": "fake", "mgmt_key": key, "model": GPT,
    })
    assert providers.supports_priority_tier(GPT) is False


@pytest.mark.parametrize("model", [CLAUDE, DEEPSEEK, "gpt-4o", "gpt-4.1", "gpt-oss-120b"])
def test_non_current_gpt_is_unsupported_even_on_local_proxy(model):
    settings_store.upsert_web_provider("local-test", {
        "base_url": "http://localhost:8318", "api_key": "fake",
        "mgmt_key": "fake-management", "model": model,
    })
    assert providers.supports_priority_tier(model) is False


def test_priority_state_reports_support_and_model_specific_value():
    assert providers.priority_state(GPT) == {"supported": True, "value": ""}
    settings_store.set_web_service_tier("priority", model=GPT)
    assert providers.priority_state(GPT) == {"supported": True, "value": "priority"}
    assert providers.priority_state(CLAUDE) == {"supported": False, "value": ""}


@pytest.mark.parametrize("summary,expected", [("detailed", "detailed"), ("none", None)])
def test_extra_body_merges_priority_with_existing_options(summary, expected):
    body = providers.extra_body_for_model(GPT, "ultra", "low", summary, "priority")
    assert json.loads(body) == {
        "output_config": {"effort": "ultra"}, "text": {"verbosity": "low"},
        "reasoning": {"summary": expected}, "service_tier": "priority",
    }
    assert providers.extra_body_for_model(GPT, "high", "", "", "") == ""
    assert providers.extra_body_for_model(GPT, "high", "", "") == ""


@pytest.mark.parametrize("model", [CLAUDE, DEEPSEEK, "gpt-4o"])
def test_extra_body_unsupported_model_never_receives_priority(model):
    assert providers.extra_body_for_model(model, "high", "", "", "priority") == ""
    assert json.loads(providers.extra_body_for_model(model, "off", "", "", "priority")) == {
        "thinking": {"type": "disabled"},
    }


def test_extra_body_checks_selected_route_not_only_resolved_gpt(monkeypatch):
    # 同一个上游 GPT 可以经不同供应商路由，不能按解析后的名字误开优先档。
    monkeypatch.setattr(providers, "resolve", lambda *a: (GPT, {}))
    monkeypatch.setattr(providers, "codex_mgmt_for_model", lambda model:
                        ("fake-management", "http://localhost:8317") if model == "local-alias" else None)
    assert providers.supports_priority_tier("local-alias") is True
    assert providers.supports_priority_tier("remote-alias") is False
    local = providers.extra_body_for_model(GPT, "", "", "", "priority", selection_model="local-alias")
    assert json.loads(local) == {"service_tier": "priority"}
    remote = providers.extra_body_for_model(GPT, "", "low", "auto", "priority", selection_model="remote-alias")
    assert json.loads(remote) == {"text": {"verbosity": "low"}, "reasoning": {"summary": "auto"}}


@pytest.mark.anyio
@pytest.mark.parametrize("selected", ["local-alias", ""])
async def test_agent_reads_tier_by_selection_model_and_passes_route(monkeypatch, selected):
    from vococo.core import agent

    selection_model = selected or config.MODEL
    settings_store.set_web_service_tier("priority", model=selection_model)
    monkeypatch.setattr(agent.agents, "runtime_for_session", lambda key: {})
    monkeypatch.setattr(providers, "resolve", lambda *a: ("gpt-5.6-sol", {}))
    monkeypatch.setattr(agent, "configured_context_window", lambda *a: None)

    class BodyCaptured(Exception):
        pass

    def capture_body(resolved_model, effort, verbosity, summary="", service_tier="", *, selection_model=""):
        assert resolved_model == "gpt-5.6-sol"
        assert service_tier == "priority"
        assert selection_model == (selected or config.MODEL)
        # 在 SDK 启动前结束，不启动子进程，也不发出模型请求。
        raise BodyCaptured

    monkeypatch.setattr(providers, "extra_body_for_model", capture_body)
    with pytest.raises(BodyCaptured):
        async for _ in agent.stream_turn([], "测试", model=selected):
            pass


@pytest.fixture
def priority_app(monkeypatch):
    choices = [(GPT, "GPT", "codex"), (CLAUDE, "Claude", "official"),
               (DEEPSEEK, "DeepSeek", "api")]
    monkeypatch.setattr(providers, "available_models", lambda *a, **kw: choices)
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([web.get("/models", adapter._handle_models),
                    web.post("/service-tier", adapter._handle_service_tier_switch)])
    return app


@pytest.mark.anyio
async def test_http_models_save_reload_and_reset(priority_app):
    async with TestClient(TestServer(priority_app)) as client:
        response = await client.get("/models")
        assert response.status == 200
        assert (await response.json())["service_tiers"] == {
            GPT: {"supported": True, "value": ""},
            CLAUDE: {"supported": False, "value": ""},
            DEEPSEEK: {"supported": False, "value": ""},
        }
        for tier in ("priority", ""):
            response = await client.post("/service-tier", json={"model": GPT, "tier": tier})
            assert response.status == 200, await response.text()
            assert await response.json() == {"ok": True, "model": GPT, "tier": tier}
            assert settings_store.get_web_service_tier(GPT) == tier
            response = await client.get("/models")
            assert (await response.json())["service_tiers"][GPT] == {"supported": True, "value": tier}


@pytest.mark.anyio
@pytest.mark.parametrize("body", [
    {}, {"model": GPT}, {"tier": "priority"},
    {"model": [], "tier": "priority"}, {"model": None, "tier": "priority"},
    {"model": 1, "tier": "priority"}, {"model": True, "tier": "priority"},
    {"model": "", "tier": "priority"}, {"model": " ", "tier": "priority"},
    {"model": GPT, "tier": []}, {"model": GPT, "tier": {}},
    {"model": GPT, "tier": None}, {"model": GPT, "tier": 1},
    {"model": GPT, "tier": True}, {"model": GPT, "tier": "flex"},
    {"model": "unknown", "tier": "priority"}, {"model": "unknown", "tier": ""},
    {"model": CLAUDE, "tier": "priority"}, {"model": CLAUDE, "tier": ""},
    {"model": DEEPSEEK, "tier": "priority"},
])
async def test_http_rejects_invalid_request_without_changing_saved_tier(priority_app, body):
    settings_store.set_web_service_tier("priority", model=GPT)
    async with TestClient(TestServer(priority_app)) as client:
        response = await client.post("/service-tier", json=body)
        assert response.status == 400
        assert (await response.json())["error"]
    assert settings_store.get_web_service_tier(GPT) == "priority"


@pytest.mark.anyio
async def test_http_rejects_available_but_remote_gpt(priority_app):
    settings_store.upsert_web_provider("codex-gpt", {
        "base_url": "https://remote.example.com", "api_key": "fake",
        "mgmt_key": "fake-management", "model": GPT,
    })
    async with TestClient(TestServer(priority_app)) as client:
        response = await client.post("/service-tier", json={"model": GPT, "tier": "priority"})
        assert response.status == 400
        assert (await response.json())["error"]
    assert settings_store.get_web_service_tier(GPT) == ""


def test_subagent_inherits_priority_and_rebuilds_on_model_switch():
    from claude_agent_sdk import ClaudeAgentOptions
    from vococo.core import subagent
    from vococo.tools import danger

    parent = ClaudeAgentOptions(
        model=GPT, hooks=danger.build_hooks(),
        env={"CLAUDE_CODE_EXTRA_BODY": '{"service_tier":"priority"}'},
    )
    inherited = subagent._child_options(parent, None)
    assert json.loads(inherited.env["CLAUDE_CODE_EXTRA_BODY"])["service_tier"] == "priority"
    settings_store.set_web_service_tier("priority", model=GPT)
    selected = subagent._child_options(parent, GPT)
    assert json.loads(selected.env["CLAUDE_CODE_EXTRA_BODY"])["service_tier"] == "priority"
    settings_store.set_web_service_tier("", model=GPT)
    disabled = subagent._child_options(parent, GPT)
    assert "service_tier" not in json.loads(disabled.env["CLAUDE_CODE_EXTRA_BODY"] or "{}")
    other = subagent._child_options(parent, CLAUDE)
    assert "service_tier" not in json.loads(other.env["CLAUDE_CODE_EXTRA_BODY"] or "{}")
