"""POST /conv/delete：删除普通 Web 会话前必须等正在运行的 Agent 收尾。"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import session_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def delete_web_app(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    from vococo.core import worktree

    async def clean_worktree(_session_key):
        return {}

    async def remove_worktree(_session_key):
        return None

    monkeypatch.setattr(worktree, "worktree_dirty_summary", clean_worktree)
    monkeypatch.setattr(worktree, "remove_worktree", remove_worktree)
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([web.post("/conv/delete", adapter._handle_delete)])
    return app, adapter


@pytest.mark.anyio
async def test_delete_normal_web_session_waits_for_running_turn(delete_web_app):
    app, adapter = delete_web_app
    key = config.resolve_session_key("web", "running")
    session_store.start_turn(key, "仍在回复")
    calls = []

    async def cancel_and_wait(session_key):
        calls.append((session_key, len(session_store.load_history(session_key))))
        return True, True

    adapter.set_cancel_and_wait_callback(cancel_and_wait)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/conv/delete", json={"conv": "running"})
        assert response.status == 200
        assert await response.json() == {"ok": True}

    assert calls == [(key, 1)]
    assert session_store.load_history(key) == []


@pytest.mark.anyio
async def test_delete_refuses_while_running_turn_does_not_stop(delete_web_app):
    app, adapter = delete_web_app
    key = config.resolve_session_key("web", "stuck")
    session_store.start_turn(key, "仍在回复")

    async def cancel_and_wait(_session_key):
        return True, False

    adapter.set_cancel_and_wait_callback(cancel_and_wait)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/conv/delete", json={"conv": "stuck"})
        assert response.status == 409
        data = await response.json()
        assert data["ok"] is False
        assert "停止" in data["error"]

    assert len(session_store.load_history(key)) == 1
