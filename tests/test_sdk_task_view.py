"""SDK 任务清单的独立视图:只展示,绝不进入后台执行器。"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.core import tasks
from vococo.core.timeline import Timeline
from vococo.tools import sdk_task_hooks
from vococo.voice import notify
from vococo.voice import routes


@pytest.fixture
def sdk_task_db(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(tasks, "_DB", None)
    notify._subscribers.clear()
    yield
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None


def test_sdk_task_view_keeps_sdk_number_and_latest_fields(sdk_task_db):
    tasks.upsert_sdk_task(
        chat_id="chat-a", number=4, title="缓存迁移", description="迁移 Service Worker", status="pending"
    )
    tasks.upsert_sdk_task(
        chat_id="chat-a", number=4, title="完成缓存迁移", description="正在修复", status="in_progress"
    )

    rows = tasks.list_sdk_tasks("chat-a")
    assert rows == [
        {
            "id": "sdk:chat-a:4", "number": 4, "title": "完成缓存迁移",
            "description": "正在修复", "status": "in_progress", "origin": "sdk_task",
            "dispatch_chat_id": "chat-a", "deleted": False,
            "created_at": pytest.approx(rows[0]["created_at"]),
            "updated_at": pytest.approx(rows[0]["updated_at"]),
        }
    ]


@pytest.mark.anyio
async def test_task_hooks_sync_create_update_and_delete(sdk_task_db, monkeypatch):
    monkeypatch.setattr(sdk_task_hooks, "_chat_id", lambda: "chat-a")
    seen = []
    monkeypatch.setattr(notify, "on_sdk_task_activity", lambda task: seen.append(task))

    await sdk_task_hooks.posttool_sdk_task_sync_hook(
        {
            "tool_name": "TaskCreate",
            "tool_input": {"subject": "缓存迁移", "description": "迁移 Service Worker"},
            "tool_response": "Task #4 created successfully: 缓存迁移",
        },
        None,
        {},
    )
    await sdk_task_hooks.posttool_sdk_task_sync_hook(
        {
            "tool_name": "TaskUpdate",
            "tool_input": {"task_id": "4", "subject": "完成缓存迁移", "status": "in_progress"},
            "tool_response": "Updated task #4 status",
        },
        None,
        {},
    )
    await sdk_task_hooks.posttool_sdk_task_sync_hook(
        {
            "tool_name": "TaskDelete",
            "tool_input": {"task_id": "Task #4"},
            "tool_response": "Task #4 deleted",
        },
        None,
        {},
    )

    assert tasks.list_sdk_tasks("chat-a") == []
    assert [task["status"] for task in seen] == ["pending", "in_progress", "deleted"]


def test_task_tools_do_not_pollute_process_timeline():
    tl = Timeline()
    tl.tool_started("TaskCreate", "t1", None)
    tl.tool_input("t1", {"subject": "缓存迁移"}, None)
    tl.tool_finished("TaskCreate", True, "Task #4 created", "t1", "Task #4 created", None)
    tl.tool_started("Bash", "t2", None)

    assert [block["name"] for block in tl.blocks] == ["Bash"]


@pytest.mark.anyio
async def test_sdk_tasks_endpoint_returns_current_conversation_in_number_order(sdk_task_db):
    tasks.upsert_sdk_task(
        chat_id="chat-a", number=5, title="回归测试", description="", status="pending"
    )
    tasks.upsert_sdk_task(
        chat_id="chat-a", number=4, title="缓存迁移", description="", status="in_progress"
    )
    app = web.Application()
    app.add_routes([web.get("/voice/sdk-tasks", routes._handle_sdk_tasks_list)])

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/voice/sdk-tasks?conv=chat-a")
        assert resp.status == 200
        rows = await resp.json()

    assert [(row["number"], row["status"]) for row in rows] == [(4, "in_progress"), (5, "pending")]
