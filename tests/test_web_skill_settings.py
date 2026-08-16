"""Web Skill 设置接口：通用与 Git 编程两套名单。"""
from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo.gateway import settings_store
from vococo.gateway.adapters.web import WebAdapter


@pytest.fixture(autouse=True)
def _skill_settings(monkeypatch, tmp_path):
    from vococo import config

    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(settings_store, "_PATH", tmp_path / "web_settings.json")
    monkeypatch.setattr(settings_store, "_scan_skills", lambda: [
        {"name": "assistant", "description": ""}, {"name": "code", "description": ""},
    ])
    (tmp_path / "web_settings.json").write_text(json.dumps({
        "skills_mode": "custom", "skills_enabled": ["assistant"],
    }), encoding="utf-8")


@pytest.fixture
def skill_app():
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([
        web.post("/settings/skill", adapter._handle_settings_skill),
        web.post("/settings/skills/coding/reset", adapter._handle_settings_coding_skills_reset),
    ])
    return app


@pytest.mark.anyio
async def test_coding_scope_updates_only_coding_profile(skill_app):
    async with TestClient(TestServer(skill_app)) as client:
        response = await client.post("/settings/skill", json={
            "name": "code", "enabled": True, "scope": "coding",
        })
        assert response.status == 200
        assert (await response.json())["coding_mode"] == "custom"

    items = {item["name"]: item for item in settings_store.list_skills()}
    assert items["assistant"]["enabled"] is True
    assert items["code"]["enabled"] is False
    assert items["assistant"]["coding_enabled"] is True
    assert items["code"]["coding_enabled"] is True


@pytest.mark.anyio
async def test_coding_scope_reset_returns_to_inheritance(skill_app):
    settings_store.set_skill("code", enabled=True, scope="coding")

    async with TestClient(TestServer(skill_app)) as client:
        response = await client.post("/settings/skills/coding/reset")
        assert response.status == 200

    assert settings_store.coding_skills_mode() == "inherit"


@pytest.mark.anyio
async def test_skill_endpoint_rejects_unknown_scope(skill_app):
    async with TestClient(TestServer(skill_app)) as client:
        response = await client.post("/settings/skill", json={
            "name": "code", "enabled": True, "scope": "unknown",
        })
        assert response.status == 400
        assert (await response.json())["error"] == "scope 只能是 general 或 coding"
