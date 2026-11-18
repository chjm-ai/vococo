"""POST /conv/delete：删除普通 Web 会话前必须等正在运行的 Agent 收尾。"""
from __future__ import annotations

import asyncio

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

    async def remove_worktree(_session_key, **_kwargs):
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
        assert await response.json() == {"ok": True, "cleanup": []}

    assert calls == [(key, 1)]
    assert session_store.load_history(key) == []


@pytest.mark.anyio
async def test_delete_refuses_when_repository_lease_is_active(delete_web_app, monkeypatch):
    from vococo.core.repository_locks import lease, session_digest
    from vococo.memory import agents

    app, _ = delete_web_app
    key = config.resolve_session_key("web", "busy")
    session_store.start_turn(key, "待保留")
    monkeypatch.setattr(agents, "agent_for_session", lambda _: {"id": "test-owner"})
    with lease("test-owner", session_digest(key)):
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/conv/delete", json={"conv": "busy"})
            assert response.status == 409
    assert session_store.load_history(key)


@pytest.mark.anyio
@pytest.mark.parametrize("owner", [None, {"id": "test-owner"}])
async def test_delete_blocks_new_and_queued_messages(delete_web_app, monkeypatch, owner):
    from vococo.core import worktree
    from vococo.gateway.adapters.base import Incoming
    from vococo.gateway.adapters.web import SessionClosingError
    from vococo.gateway.run import GatewayRunner
    from vococo.memory import agents

    app, adapter = delete_web_app
    key = config.resolve_session_key("web", "race")
    session_store.start_turn(key, "原消息")
    monkeypatch.setattr(agents, "agent_for_session", lambda _: owner)
    entered, release = asyncio.Event(), asyncio.Event()

    async def preview(_key, **_kwargs):
        entered.set()
        await release.wait()
        return []

    monkeypatch.setattr(worktree, "remove_worktree", preview)
    runner = GatewayRunner([adapter])
    adapter.set_cancel_and_wait_callback(runner.cancel_and_wait_turn)
    async with TestClient(TestServer(app)) as client:
        deleting = asyncio.create_task(client.post("/conv/delete", json={"conv": "race"}))
        await entered.wait()
        with pytest.raises(SessionClosingError):
            await adapter._ingest("race", "并发消息")
        queued = asyncio.create_task(runner._dispatch_one(adapter, Incoming("web", "race", "排队消息")))
        await asyncio.sleep(0)
        assert not queued.done()
        release.set()
        assert (await deleting).status == 200
        await queued
        await runner._dispatch_one(adapter, Incoming("web", "race", "迟到消息"))
        assert WebAdapter().session_closing(key)
    assert not session_store.load_history(key)


@pytest.mark.anyio
async def test_queued_message_continues_after_delete_fails(delete_web_app, monkeypatch):
    from vococo.core import worktree
    from vococo.gateway.adapters.base import Incoming
    from vococo.gateway.run import GatewayRunner

    app, adapter = delete_web_app
    key = config.resolve_session_key("web", "retained")
    entered, release = asyncio.Event(), asyncio.Event()
    handled = []

    async def preview(_key, **_kwargs):
        entered.set()
        await release.wait()
        raise BlockingIOError()

    runner = GatewayRunner([adapter])
    async def handle(_adapter, incoming):
        handled.append(incoming.text)
    monkeypatch.setattr(runner, "_handle", handle)
    monkeypatch.setattr(worktree, "remove_worktree", preview)
    async with TestClient(TestServer(app)) as client:
        deleting = asyncio.create_task(client.post("/conv/delete", json={"conv": "retained"}))
        await entered.wait()
        queued = asyncio.create_task(runner._dispatch_one(adapter, Incoming("web", "retained", "排队消息")))
        await asyncio.sleep(0)
        release.set()
        assert (await deleting).status == 409
        await queued
    assert handled == ["排队消息"]
    assert not adapter.session_closing(key)


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
