"""执行终态的语音展示：广播状态与可选音频，不负责系统推送。"""
from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable

from .. import config
from ..core import task_events, task_words, tasks
from . import tts

# 兼容旧测试和旧路由的模块级名称;实际状态由 core.task_events 持有。
_subscribers = task_events._subscribers
_started_tasks = task_events._started_tasks


def register_main_event_bridge(fn: Callable[[dict], None] | None) -> None:
    task_events.register_main_event_bridge(fn)


def subscribe() -> asyncio.Queue:
    return task_events.subscribe()


def unsubscribe(q: asyncio.Queue) -> None:
    task_events.unsubscribe(q)


def is_online() -> bool:
    return task_events.is_online()


def _broadcast(event: str, payload: dict) -> None:
    task_events._broadcast(event, payload)


def _bridge_event(payload: dict) -> None:
    task_events._bridge_event(payload)


def on_task_activity(task: dict) -> None:
    task_events.on_task_activity(task)


def _announce_text(task: dict) -> str:
    task_words.flag_if_reversed_direction(task["result_summary"], "notify._announce_text")
    if task["status"] == "done":
        return f"对了,「{task['title']}」办完了,{task['result_summary'] or '结果已经出来了'}。"
    if task["status"] == "cancelled":
        return f"「{task['title']}」这个任务已经取消了。"
    return f"「{task['title']}」这个任务没办成:{task['result_summary'] or task['progress_note']}。"


async def on_task_terminal(task_id: str) -> None:
    """所有来源刷新状态；仅语音派发执行在通话中插播。"""
    task = tasks.get(task_id)
    if task is None:
        return
    from ..core import dispatch

    own = dispatch.current(task["session_key"]) if dispatch.enabled() else None
    if (own and own["state"] == "waiting") or (task.get("relation") == "child" and task["status"] == "done"):
        task_events.on_task_activity(task)
        return
    announce_text = _announce_text(task)
    payload = {
        "id": task["id"], "task_id": task["id"], "job_id": task.get("job_id"),
        "conversation_id": tasks.conversation_id(task), "title": task["title"],
        "status": task["status"], "result_summary": task["result_summary"],
        "announce_text": announce_text, "updated_at": task["updated_at"],
        "dispatch_chat_id": task.get("dispatch_chat_id"), "origin": task.get("origin") or "voice",
    }
    if payload["origin"] == "voice" and is_online():
        # 合成可能很慢，不能阻塞状态刷新和统一完成通知。
        asyncio.create_task(_patch_audio_after(payload, announce_text))
    _broadcast("task_done", payload)


async def _patch_audio_after(payload: dict, announce_text: str) -> None:
    """音频补发与完成事件分离，挂断 Omni 后仍能通过 Web Audio 播放。"""
    try:
        audio = await tts.synthesize(announce_text, config.VOICE_TTS_VOICE)
        if audio:
            patch = dict(payload)
            patch["audio_b64"] = base64.b64encode(audio).decode("ascii")
            patch["audio_patch"] = True
            _broadcast("task_done", patch)
    except Exception:
        pass


async def _terminal_event_handler(task_id: str) -> None:
    await on_task_terminal(task_id)


task_events.register_terminal_handler(_terminal_event_handler)
