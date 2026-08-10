"""通用后台任务事件总线测试。"""
from __future__ import annotations

import pytest

from vococo.core import task_events


@pytest.fixture
def clean_task_events():
    old_bridge = task_events._main_event_bridge
    old_handlers = list(task_events._terminal_handlers)
    task_events._subscribers.clear()
    task_events._started_tasks.clear()
    task_events._terminal_handlers.clear()
    task_events.register_main_event_bridge(None)
    yield
    task_events._subscribers.clear()
    task_events._started_tasks.clear()
    task_events._terminal_handlers[:] = old_handlers
    task_events.register_main_event_bridge(old_bridge)


def test_task_activity_broadcasts_and_bridges_only_first_start(clean_task_events):
    events = []
    queue = task_events.subscribe()
    task_events.register_main_event_bridge(events.append)
    task = {
        "id": "task-1",
        "title": "测试任务",
        "status": "running",
        "progress_note": "正在执行",
        "created_at": 1.0,
        "updated_at": 2.0,
        "dispatch_chat_id": "main",
        "origin": "chat",
    }

    task_events.on_task_activity(task)
    task_events.on_task_activity(task)

    assert queue.qsize() == 2
    assert events == [{"conv": "task:task-1", "type": "start"}]
    task_events.unsubscribe(queue)


@pytest.mark.anyio
async def test_emit_terminal_calls_all_handlers_even_if_one_fails(clean_task_events):
    calls = []

    async def failed_handler(_task_id: str) -> None:
        calls.append("failed")
        raise RuntimeError("test failure")

    async def successful_handler(task_id: str) -> None:
        calls.append(task_id)

    task_events.register_terminal_handler(failed_handler)
    task_events.register_terminal_handler(successful_handler)

    await task_events.emit_terminal("task-2")

    assert calls == ["failed", "task-2"]
