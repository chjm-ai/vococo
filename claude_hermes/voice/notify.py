"""任务终态分发(F8/F9):/voice 页面在线 → SSE 推 task_done 事件;
离线 → 走现有 gateway/adapters/web_push.py 发系统推送。

"在线"简化判定:是否有活跃的 /voice/tasks/stream 订阅者(单用户场景下足够准确,
不做更复杂的"该用户是否正看着这条任务"判断)。
"""
from __future__ import annotations

import asyncio
import base64

from .. import config
from . import tasks, tts

_subscribers: set[asyncio.Queue] = set()


def subscribe() -> asyncio.Queue:
    """/voice/tasks/stream 建连时调用,返回一个只属于该连接的事件队列。"""
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def is_online() -> bool:
    return bool(_subscribers)


def _broadcast(event: str, payload: dict) -> None:
    for q in list(_subscribers):
        q.put_nowait((event, payload))


def _announce_text(task: dict) -> str:
    if task["status"] == "done":
        return f"对了,「{task['title']}」办完了,{task['result_summary'] or '结果已经出来了'}。"
    if task["status"] == "cancelled":
        return f"「{task['title']}」这个任务已经取消了。"
    return f"「{task['title']}」这个任务没办成:{task['result_summary'] or task['progress_note']}。"


async def on_task_terminal(task_id: str) -> None:
    """任务进入终态时调用一次:在线走 SSE 事件,离线走 web_push。"""
    task = tasks.get(task_id)
    if task is None:
        return
    announce_text = _announce_text(task)
    payload = {
        "id": task["id"],
        "title": task["title"],
        "status": task["status"],
        "result_summary": task["result_summary"],
        "announce_text": announce_text,
    }
    if is_online():
        # 顺手合成播报音频,前端拿到事件直接排进播放队列,不用再回头请求一次。
        audio = await tts.synthesize(announce_text, config.VOICE_TTS_VOICE)
        payload["audio_b64"] = base64.b64encode(audio).decode("ascii") if audio else None
        _broadcast("task_done", payload)
        return
    from ..gateway.adapters.web_push import PUSH

    body = task["result_summary"] or task["progress_note"] or "任务已结束"
    await PUSH.notify(
        title=f"任务完成:{task['title']}",
        body=body,
        conv="voice-task",
        kind="done",
        tag=f"voice-task-{task['id']}",
    )
