"""后台任务事件总线。

任务执行器只负责产生任务事件,不依赖具体的语音、网页或推送展示层。
不同适配器可以订阅同一组事件,决定如何展示或通知用户。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from . import tasks

_subscribers: set[asyncio.Queue] = set()
_main_event_bridge: Callable[[dict], None] | None = None
_started_tasks: set[str] = set()
_terminal_handlers: list[Callable[[str], Awaitable[None]]] = []


def register_main_event_bridge(fn: Callable[[dict], None] | None) -> None:
    """注册主会话事件桥接,供 Web SSE 显示任务状态。"""
    global _main_event_bridge
    _main_event_bridge = fn


def _bridge_event(payload: dict) -> None:
    if _main_event_bridge is None:
        return
    try:
        _main_event_bridge(payload)
    except Exception:
        pass


def subscribe() -> asyncio.Queue:
    """订阅任务状态事件,返回当前连接专属的队列。"""
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers.add(queue)
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    _subscribers.discard(queue)


def is_online() -> bool:
    return bool(_subscribers)


def _broadcast(event: str, payload: dict) -> None:
    for queue in list(_subscribers):
        queue.put_nowait((event, payload))


def on_task_activity(task: dict) -> None:
    """广播任务非终态变化,并桥接任务会话的起跑/进度状态。"""
    _broadcast(
        "task_update",
        {
            "id": task["id"],
            "title": task["title"],
            "status": task["status"],
            "progress_note": task["progress_note"],
            "created_at": task["created_at"],
            "updated_at": task["updated_at"],
            "dispatch_chat_id": task.get("dispatch_chat_id"),
            "origin": task.get("origin"),
        },
    )
    if task["status"] != "running" or task["id"] in _started_tasks:
        return
    _started_tasks.add(task["id"])
    _bridge_event({"conv": tasks.session_key(task["id"]), "type": "start"})


def on_sdk_task_activity(task: dict) -> None:
    """广播 SDK 待办投影变化,不触发后台任务通知。"""
    _broadcast("sdk_task_update", task)


def register_terminal_handler(
    handler: Callable[[str], Awaitable[None]],
) -> None:
    """注册一个任务终态处理器,重复注册同一函数时忽略。"""
    if handler not in _terminal_handlers:
        _terminal_handlers.append(handler)


async def emit_terminal(task_id: str) -> None:
    """通知所有终态处理器,处理器失败不阻塞其他处理器。"""
    for handler in tuple(_terminal_handlers):
        try:
            await handler(task_id)
        except Exception:
            continue
