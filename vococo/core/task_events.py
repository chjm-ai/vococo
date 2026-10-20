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
            "kind": task.get("kind", "chat"),
            "source_session_key": task.get("source_session_key"),
            "conversation_id": tasks.conversation_id(task),
        },
    )
    if tasks.is_inline_research(task):
        from . import research

        _bridge_event({"conv": tasks.conversation_id(task), "type": "research_update",
                       "task_id": task["id"], "title": task["title"], "status": task["status"],
                       "progress_note": task["progress_note"],
                       "progress": research.progress(task["id"], task)})
        return
    if task["status"] != "running" or task["id"] in _started_tasks:
        return
    _started_tasks.add(task["id"])
    _bridge_event({"conv": tasks.session_key(task["id"]), "type": "start"})


def register_terminal_handler(
    handler: Callable[[str], Awaitable[None]],
) -> None:
    """注册一个任务终态处理器,重复注册同一函数时忽略。"""
    if handler not in _terminal_handlers:
        _terminal_handlers.append(handler)


def _deliver_research(task: dict) -> None:
    """另加一条完整消息，不结束来源会话正在运行的 turn。"""
    from ..memory import session_store

    source = task["source_session_key"]
    if task.get("research_delivered_at"):
        return
    if not source:
        tasks.mark_research_delivered(task["id"])
        return
    if task["status"] == "done":
        text = task["result_full"]
    elif task["status"] == "cancelled":
        text = f"研究「{task['title']}」已取消；已保存的证据和检查点保留。"
    else:
        text = f"研究「{task['title']}」失败：{task['result_summary'] or task['progress_note']}"
    turn_id = session_store.append_external(source, text, f"research:{task['id']}:{task['updated_at']}")
    session_store.set_pending_review(source, True)
    tasks.mark_research_delivered(task["id"])
    _bridge_event({"conv": tasks.conversation_id(task), "type": "research_result", "task_id": task["id"],
                   "title": task["title"], "status": task["status"], "text": text,
                   "turn_id": turn_id})


async def emit_terminal(task_id: str) -> None:
    """通知所有终态处理器,处理器失败不阻塞其他处理器。"""
    # 任务进入终态即解除 start 去重:同一任务被续接/追问重新激活(running)时,
    # on_task_activity 的 start 桥接要能再次触发,前端侧边栏才会补拉最新状态
    # (2026-08-17 修复:续接老任务后侧边栏/状态条停在"已完成"不刷新)。
    _started_tasks.discard(task_id)
    task = tasks.get(task_id)
    if task and tasks.is_inline_research(task) and task["status"] in tasks.TERMINAL_STATUSES:
        on_task_activity(task)
        try:
            _deliver_research(task)
        except Exception as error:
            print(f"[task_events] 研究 {task_id} 回传失败，重启后重试:{error}", flush=True)
    for handler in tuple(_terminal_handlers):
        try:
            await handler(task_id)
        except Exception:
            continue
