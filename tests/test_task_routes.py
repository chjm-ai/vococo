"""通用后台任务 API 测试。"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.core import tasks
from vococo.gateway import task_routes


@pytest.fixture
def task_api_db(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(tasks, "_DB", None)
    yield
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None


@pytest.fixture
async def task_api_client(task_api_db):
    app = web.Application()
    task_routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        yield client


def _create_task(*, origin: str, dispatch_chat_id: str | None) -> dict:
    return tasks.create(
        f"{origin} task",
        "prompt",
        origin=origin,
        dispatch_chat_id=dispatch_chat_id,
    )


@pytest.mark.anyio
async def test_tasks_api_lists_voice_and_chat_tasks_by_conversation(
    task_api_client,
):
    _create_task(origin="voice", dispatch_chat_id="main")
    _create_task(origin="chat", dispatch_chat_id="main")
    _create_task(origin="cron", dispatch_chat_id=None)

    response = await task_api_client.get("/tasks?session_key=main")

    assert response.status == 200
    rows = await response.json()
    assert {row["origin"] for row in rows} == {"voice", "chat"}


@pytest.mark.anyio
async def test_tasks_api_rejects_unknown_source(task_api_client):
    response = await task_api_client.get("/tasks?source=voice")

    assert response.status == 400
    assert (await response.json())["error"] == "source 不支持"


@pytest.mark.anyio
async def test_tasks_api_keeps_sdk_tasks_separate(task_api_client):
    tasks.upsert_sdk_task(
        chat_id="main",
        number=1,
        title="待办",
        description="不是后台任务",
        status="pending",
    )

    response = await task_api_client.get("/sdk-tasks?session_key=main")

    assert response.status == 200
    rows = await response.json()
    assert rows[0]["origin"] == "sdk_task"
    assert tasks.list_recent_for_source(dispatch_chat_id="main") == []
