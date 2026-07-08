"""侧边栏"语音任务"分组数据接口 /voice/sidebar 的测试(见 03-phase2-实现记录.md
存储统一改动一节)。只测这一个新接口 + /shared.css 静态路由,不重复测已有的
/conversations(那是抄它的模板)。
"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_hermes import config
from claude_hermes.gateway.adapters.web import WebAdapter
from claude_hermes.memory import session_store
from claude_hermes.voice import tasks


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def web_app(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(tasks, "_DB", None)
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes(
        [
            web.get("/voice/sidebar", adapter._handle_voice_sidebar),
            web.get("/shared.css", adapter._handle_shared_css),
        ]
    )
    return app


@pytest.mark.anyio
async def test_voice_sidebar_returns_main_pinned_first_and_task_rows(web_app):
    session_store.append("voice-chat:main", "你好", "你好呀")
    task = tasks.create("查天气", "帮我查一下今天天气")
    session_store.append(f"voice-task:{task['id']}", "帮我查一下今天天气", "晴天")

    async with TestClient(TestServer(web_app)) as client:
        resp = await client.get("/voice/sidebar")
        assert resp.status == 200
        data = await resp.json()

    assert data["main"]["conv"] == "voice-chat:main"
    assert data["main"]["title"] == "语音对话"
    assert data["main"]["pinned"] is True
    assert len(data["tasks"]) == 1
    row = data["tasks"][0]
    assert row["conv"] == f"voice-task:{task['id']}"
    assert row["task_status"] == "queued"
    assert row["title"] == "查天气"


@pytest.mark.anyio
async def test_voice_sidebar_task_row_survives_missing_task_row(web_app):
    """任务元数据(voice.db)万一被清过而对话还在 session_store 里,不该 500。"""
    session_store.append("voice-task:ghost123", "问", "答")

    async with TestClient(TestServer(web_app)) as client:
        resp = await client.get("/voice/sidebar")
        assert resp.status == 200
        data = await resp.json()

    assert len(data["tasks"]) == 1
    assert data["tasks"][0]["conv"] == "voice-task:ghost123"
    assert "task_status" not in data["tasks"][0]


@pytest.mark.anyio
async def test_shared_css_served_with_content(web_app):
    async with TestClient(TestServer(web_app)) as client:
        resp = await client.get("/shared.css")
        assert resp.status == 200
        text = await resp.text()
        assert "--accent" in text
        assert ".bubble" in text
