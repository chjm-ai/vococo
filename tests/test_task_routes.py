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


def _done_task(title: str, full: str) -> dict:
    t = tasks.create(title, "prompt", dispatch_chat_id="main", origin="chat")
    tasks.set_status(t["id"], "running")
    tasks.finish(t["id"], "done", full, "一句话摘要")
    return t


@pytest.mark.anyio
async def test_tasks_api_list_omits_full_text_but_flags_it(task_api_client):
    """列表不带 result_full(单条能几万字,拖慢进应用),只标 has_full;详情走 /tasks/{id}。"""
    long = _done_task("长结果", "完整结果" * 5000)
    queued = _create_task(origin="chat", dispatch_chat_id="main")

    response = await task_api_client.get(
        "/tasks?session_key=main", headers={"Accept-Encoding": "gzip"}
    )

    assert response.status == 200
    assert response.headers.get("Content-Encoding") == "gzip"
    rows = {row["id"]: row for row in await response.json()}
    assert "result_full" not in rows[long["id"]]
    assert rows[long["id"]]["has_full"] is True
    assert rows[long["id"]]["result_summary"] == "一句话摘要"  # 摘要照常给,列表要显示
    assert rows[queued["id"]]["has_full"] is False


@pytest.mark.anyio
async def test_tasks_api_detail_returns_full_text_unescaped(task_api_client):
    t = _done_task("详情", "完整结果")

    response = await task_api_client.get(f"/tasks/{t['id']}")

    assert response.status == 200
    assert "完整结果" in await response.text()  # 中文按原文输出,不是 \uXXXX
    assert (await response.json())["result_full"] == "完整结果"
