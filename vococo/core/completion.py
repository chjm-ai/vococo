"""执行完成的唯一通知入口；会话展示、语音播报和调度统计不自行推送。"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import uuid4

import anyio

from .. import config
from ..memory import session_store


@dataclass
class Completion:
    session_key: str
    status: str
    text: str
    turn_id: int | None = None
    title: str = ""
    run_id: str = ""
    job_id: str | None = None
    metrics: dict | None = None
    silent: bool = False
    copies: list[tuple[str, str, str]] = field(default_factory=list)
    job: dict | None = None
    id: str = field(default_factory=lambda: uuid4().hex)


_policy: Callable[[Completion], None] | None = None
_platform_push: Callable[[str, str, str], Awaitable[None]] | None = None


def register_policy(fn: Callable[[Completion], None] | None) -> None:
    """调度器只准备统计、静默与结果副本，不负责发送完成通知。"""
    global _policy
    _policy = fn


def register_platform_push(fn: Callable[[str, str, str], Awaitable[None]] | None) -> None:
    global _platform_push
    _platform_push = fn


def from_task(task: dict, turn_id: int | None = None) -> Completion:
    from . import tasks
    from .task_words import status_emoji
    from ..memory import agents

    event = Completion(
        session_key=tasks.conversation_id(task), status=task["status"],
        text=task["result_summary"] or task["progress_note"] or "任务已结束",
        turn_id=turn_id, title=task["title"],
        job_id=task.get("job_id") if task.get("origin") == "cron" else None,
        metrics=agents.metrics_from_task(task),
    )
    platform, chat_id = task.get("dispatch_platform"), task.get("dispatch_chat_id")
    if platform and chat_id and not (tasks.is_inline_research(task) and platform == "web"):
        text = task["result_full"] if tasks.is_inline_research(task) and task["status"] == "done" else (
            f"{status_emoji(task['status'])} 任务「{task['title']}」{task['status']} — {event.text}"
        )
        event.copies.append((platform, str(chat_id), text))
    return event


async def publish(event: Completion) -> bool:
    """落库后调用一次；取消不打未读点，通知失败不破坏执行结果。"""
    with anyio.CancelScope(shield=True):
        try:
            if event.job_id and _policy is not None:
                _policy(event)
            if event.silent:
                return False
            if event.status != "cancelled":
                try:
                    session_store.set_pending_review(event.session_key, True)
                except Exception as error:
                    print(f"[completion] {event.session_key} 未读标记失败:{error}", flush=True)
            return await _deliver(event)
        except Exception as error:
            print(f"[completion] {event.session_key} 完成通知失败:{error}", flush=True)
            return False


async def _deliver(event: Completion) -> bool:
    receipts = []
    try:
        if config.PUSH_ON_DONE:
            receipts.append(bool(await _send_push(event)))
    except Exception as error:
        print(f"[completion] {event.session_key} 系统推送失败:{error}", flush=True)
        receipts.append(False)
    for platform, chat_id, text in dict.fromkeys(event.copies):
        if platform == "web" and config.resolve_session_key("web", chat_id) == event.session_key:
            continue  # 原会话已保存并展示结果，副本不能再生成一个相同消息。
        try:
            if _platform_push is None:
                raise RuntimeError("结果副本通道未注册")
            await _platform_push(platform, chat_id, text)
            receipts.append(True)
        except Exception as error:
            print(f"[completion] {event.session_key} 结果副本失败:{error}", flush=True)
            receipts.append(False)
    return bool(receipts) and all(receipts)


async def _send_push(event: Completion) -> int:
    from ..gateway.adapters.web_push import PUSH

    conv = "main" if event.session_key == config.SESSION_KEY else config.conversation_id_for_key(event.session_key)
    title = session_store.get_title(event.session_key) or event.title or config.PERSONA_NAME
    prefix = {"done": "完成", "failed": "失败", "cancelled": "已取消"}[event.status]
    return await PUSH.notify(
        title=f"{prefix}:{title}", body=event.text or prefix, conv=conv,
        kind={"done": "done", "failed": "error", "cancelled": "cancelled"}[event.status],
        tag=f"completion-{event.session_key}-{event.turn_id or event.run_id or event.id}",
    )
