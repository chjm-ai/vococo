"""侧边栏"语音任务"分组数据接口 /voice/sidebar 的测试(见 03-phase2-实现记录.md
存储统一改动一节)。只测这一个新接口,不重复测已有的 /conversations(那是抄它的模板)。
"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.core import tasks
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import session_store


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
        ]
    )
    return app


@pytest.mark.anyio
async def test_voice_sidebar_returns_main_pinned_first_and_task_rows(web_app):
    session_store.append("voice-chat:main", "你好", "你好呀")
    task = tasks.create("查天气", "帮我查一下今天天气")
    session_store.append(f"task:{task['id']}", "帮我查一下今天天气", "晴天")

    async with TestClient(TestServer(web_app)) as client:
        resp = await client.get("/voice/sidebar")
        assert resp.status == 200
        data = await resp.json()

    assert data["main"]["conv"] == "voice-chat:main"
    assert data["main"]["title"] == "语音通话"
    assert data["main"]["pinned"] is True
    assert len(data["tasks"]) == 1
    row = data["tasks"][0]
    assert row["conv"] == f"task:{task['id']}"
    assert row["task_status"] == "queued"
    assert row["title"] == "查天气"
    assert row["task_updated_at"] > 0


@pytest.mark.anyio
async def test_voice_sidebar_task_row_carries_done_timestamp(web_app):
    """2026-08-04:任务行透传完成时间 task_updated_at(终态落库时更新),前端据此
    做「终态任务显示满 10 分钟自动隐藏」(voiceTaskHidden)——新完成的 10 分钟内
    要正常显示,满 10 分钟才不再出现,隐藏不是删除。"""
    task = tasks.create("查天气", "帮我查一下今天天气")
    session_store.append(f"task:{task['id']}", "帮我查一下今天天气", "晴天")
    assert tasks.set_status(task["id"], "running")
    assert tasks.finish(task["id"], "done", "晴天", "晴天")

    async with TestClient(TestServer(web_app)) as client:
        resp = await client.get("/voice/sidebar")
        data = await resp.json()

    row = data["tasks"][0]
    assert row["task_status"] == "done"
    assert row["task_updated_at"] >= tasks.get(task["id"])["updated_at"]


@pytest.mark.anyio
async def test_voice_sidebar_task_row_carries_pending_review(web_app):
    """2026-07-29:语音任务行统一接上 pending_review(跟 web/cron-task 一样的完成态
    未读标记),不再是 ab77594 当时"终态不挂点"的特例。"""
    task = tasks.create("查天气", "帮我查一下今天天气")
    session_store.append(f"task:{task['id']}", "帮我查一下今天天气", "晴天")
    session_store.set_pending_review(f"task:{task['id']}", True)

    async with TestClient(TestServer(web_app)) as client:
        resp = await client.get("/voice/sidebar")
        data = await resp.json()

    assert data["tasks"][0]["pending_review"] is True


@pytest.mark.anyio
async def test_voice_sidebar_task_row_survives_missing_task_row(web_app):
    """任务元数据(voice.db)万一被清过而对话还在 session_store 里,不该 500。"""
    session_store.append("task:ghost123", "问", "答")

    async with TestClient(TestServer(web_app)) as client:
        resp = await client.get("/voice/sidebar")
        assert resp.status == 200
        data = await resp.json()

    assert len(data["tasks"]) == 1
    assert data["tasks"][0]["conv"] == "task:ghost123"
    assert "task_status" not in data["tasks"][0]
