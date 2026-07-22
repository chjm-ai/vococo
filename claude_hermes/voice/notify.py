"""任务终态分发(F8/F9):/voice 页面在线 → SSE 推 task_done 事件;
离线 → 走现有 gateway/adapters/web_push.py 发系统推送(Web Push / VAPID)。

当 Web Push 不可配(无密钥)或用户持有平台订阅(TG/Web)时,走
register_platform_push 注册的回调,由 GatewayRunner.push 把通知送到
用户派发任务的那个入口。

F10:排队中取消(CancelQueued)以前只推 SSE 不通知离线用户,2026-07-14
改为也会走 on_task_terminal,接入全套离线通知。
"""
from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable

from .. import config
from . import tasks, tts

_subscribers: set[asyncio.Queue] = set()

# 状态对应的 emoji 前缀,用于平台推送(Telegram / Web 文字消息)
_status_emoji = {"done": "✅", "failed": "❌", "cancelled": "🚫"}

# 注册的平台推送回调:async (platform, chat_id, text) -> None
_platform_push: Callable[[str, str, str], Awaitable[None]] | None = None

# ── 主 SSE 桥接(voice-task 侧栏小红点) ──────────────────────────────────
_main_event_bridge: Callable[[dict], None] | None = None
# 已发 start 的任务 id 集合,防止进度更新重复发(仅首次起跑发一次)。
_started_tasks: set[str] = set()


def register_main_event_bridge(fn: Callable[[dict], None] | None) -> None:
    """注册/注销主 SSE 桥接回调,把语音任务状态变化(起跑/终态)以 start/done
    事件推给主 SSE 通道,让 voice-task 侧栏行的小红点能像普通会话行一样闪烁。

    由 WebAdapter 在初始化时注册,传入 self._emit。
    传入 None 可注销(主要用于测试清理)。"""
    global _main_event_bridge
    _main_event_bridge = fn


def _bridge_event(payload: dict) -> None:
    if _main_event_bridge is not None:
        try:
            _main_event_bridge(payload)
        except Exception:
            pass


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


def on_task_activity(task: dict) -> None:
    """非终态变化(派发/起跑/进度更新/排队中取消)时调用:仅在线 SSE 推 task_update,
    给通话视图的任务状态条实时刷新用;离线不推送——中间态没到打扰用户的程度,
    回来打开页面拉一次 /voice/tasks 自然能看到。"""
    _broadcast(
        "task_update",
        {
            "id": task["id"],
            "title": task["title"],
            "status": task["status"],
            "progress_note": task["progress_note"],
            "created_at": task["created_at"],
        },
    )
    # 桥接到主 SSE:任务起跑 → 前端侧栏显示小红点。_started_tasks 防重复:
    # 进度更新(工具调用等)也走本函数,但只有首次起跑的 running 才需要发 start。
    if task["status"] == "running" and task["id"] not in _started_tasks:
        _started_tasks.add(task["id"])
        _bridge_event({"conv": f"voice-task:{task['id']}", "type": "start"})


def register_platform_push(
    fn: Callable[[str, str, str], Awaitable[None]] | None,
) -> None:
    """注册一个平台推送回调,在 Web Push 不可达时作为备用通路发给任务的派发者。

    fn(platform, chat_id, text) — platform 和 chat_id 来自任务记录的
    dispatch_platform/dispatch_chat_id 字段,由 task_tools.py 在派发时从
    clarify.current() 捕获。不传 fn 或传 None = 清除(主要用于测试清理)。
    """
    global _platform_push
    _platform_push = fn


def _announce_text(task: dict) -> str:
    if task["status"] == "done":
        return f"对了,「{task['title']}」办完了,{task['result_summary'] or '结果已经出来了'}。"
    if task["status"] == "cancelled":
        return f"「{task['title']}」这个任务已经取消了。"
    return f"「{task['title']}」这个任务没办成:{task['result_summary'] or task['progress_note']}。"


def _platform_text(task: dict) -> str:
    """平台推送用的文字消息（含 emoji 状态标记）,比语音播报更紧凑。"""
    emoji = _status_emoji.get(task["status"], "📋")
    summary = task["result_summary"] or task["progress_note"] or ""
    sep = " — " if summary else ""
    return f"{emoji} 任务「{task['title']}」{task['status']}{sep}{summary}"


async def on_task_terminal(task_id: str) -> None:
    """任务进入终态时调用一次:
    1. 在线 → SSE 事件(语音播报);
    2. 离线 → Web Push(VAPID) + 平台推送(如果有 dispatch 上下文)。"""
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
    # 先桥接 done 事件到主 SSE:让侧栏小红点熄灭(在 SSE 播报和离线推送之前推,
    # 别让侧栏一直亮在已结束的任务上,也别卡住后面的异步推送路径)。
    _started_tasks.discard(task_id)  # 清状态锁,支持后续 append 续跑重新亮 dot
    _bridge_event({"conv": f"voice-task:{task_id}", "type": "done"})

    # ── 在线:推 SSE(语音播报) ──────────────────────────────────────
    if is_online():
        audio = None
        if not config.VOICE_OMNI_ENABLED:
            audio = await tts.synthesize(announce_text, config.VOICE_TTS_VOICE)
        payload["audio_b64"] = base64.b64encode(audio).decode("ascii") if audio else None
        _broadcast("task_done", payload)
        return

    # ── 离线:Web Push(VAPID) 发给所有订阅设备 ──────────────────────
    from ..gateway.adapters.web_push import PUSH

    body = task["result_summary"] or task["progress_note"] or "任务已结束"
    await PUSH.notify(
        title=f"任务完成:{task['title']}",
        body=body,
        conv="voice-task",
        kind="done",
        tag=f"voice-task-{task['id']}",
    )

    # ── 平台推送:发给任务的派发者(如果 Web Push 不可配或任务有 dispatch 上下文) ──
    if _platform_push is not None and task.get("dispatch_platform") and task.get("dispatch_chat_id"):
        try:
            await _platform_push(
                task["dispatch_platform"],
                task["dispatch_chat_id"],
                _platform_text(task),
            )
        except Exception:
            pass  # 平台推送失败不影响主流程
