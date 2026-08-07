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
from ..core import task_words, tasks
from ..core.task_words import status_emoji
from ..memory import session_store
from . import tts

_subscribers: set[asyncio.Queue] = set()

# 注册的平台推送回调:async (platform, chat_id, text) -> None
_platform_push: Callable[[str, str, str], Awaitable[None]] | None = None

# ── 主 SSE 桥接(后台任务侧栏小红点) ──────────────────────────────────
_main_event_bridge: Callable[[dict], None] | None = None
# 已发 start 的任务 id 集合,防止进度更新重复发(仅首次起跑发一次)。
_started_tasks: set[str] = set()


def register_main_event_bridge(fn: Callable[[dict], None] | None) -> None:
    """注册/注销主 SSE 桥接回调,把语音任务状态变化(起跑/终态)以 start/done
    事件推给主 SSE 通道,让后台任务侧栏行的小红点能像普通会话行一样闪烁。

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
            "updated_at": task["updated_at"],
            "dispatch_chat_id": task.get("dispatch_chat_id"),
            "origin": task.get("origin"),  # 前端按 origin 分流:voice→通话条,chat→会话条,cron 不进
        },
    )
    # 桥接到主 SSE:任务起跑 → 前端侧栏显示小红点。_started_tasks 防重复:
    # 进度更新(工具调用等)也走本函数,但只有首次起跑的 running 才需要发 start。
    if task["status"] == "running" and task["id"] not in _started_tasks:
        _started_tasks.add(task["id"])
        _bridge_event({"conv": tasks.session_key(task["id"]), "type": "start"})


def on_sdk_task_activity(task: dict) -> None:
    """SDK 待办变化只推网页状态条,不触发后台任务通知或侧栏会话。"""
    _broadcast("sdk_task_update", task)


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


# ── cron 来源任务的完成钩子 ──────────────────────────────────────────────────
# cron 任务(origin="cron")的"如何通知"跟语音/chat 任务本质不同:必须无条件写回
# 自己的专属会话 + 推给可选的额外目标(如 web),不能像语音任务那样"正好有人
# 在通话里就只播报、不落别的推送"——cron 任务的结果落地跟"有没有人在通话"完全
# 无关。所以单独开一个钩子,cron/scheduler.py 启动时注册,on_task_terminal 对
# origin="cron" 的任务直接转发给它,不复用下面语音专属的 online/offline 分支。
_cron_terminal_hook: Callable[[dict], Awaitable[None]] | None = None


def register_cron_terminal_hook(fn: Callable[[dict], Awaitable[None]] | None) -> None:
    """由 cron/scheduler.py 的 run_scheduler() 注册一次。传 None 可注销(测试用)。"""
    global _cron_terminal_hook
    _cron_terminal_hook = fn


def _announce_text(task: dict) -> str:
    task_words.flag_if_reversed_direction(task["result_summary"], "notify._announce_text")
    if task["status"] == "done":
        return f"对了,「{task['title']}」办完了,{task['result_summary'] or '结果已经出来了'}。"
    if task["status"] == "cancelled":
        return f"「{task['title']}」这个任务已经取消了。"
    return f"「{task['title']}」这个任务没办成:{task['result_summary'] or task['progress_note']}。"


def _platform_text(task: dict) -> str:
    """平台推送用的文字消息（含 emoji 状态标记）,比语音播报更紧凑。"""
    emoji = status_emoji(task["status"])
    summary = task["result_summary"] or task["progress_note"] or ""
    task_words.flag_if_reversed_direction(summary, "notify._platform_text")
    sep = " — " if summary else ""
    return f"{emoji} 任务「{task['title']}」{task['status']}{sep}{summary}"


async def on_task_terminal(task_id: str) -> None:
    """任务进入终态时调用一次(所有 origin 共用的入口):
    1. cron 来源 → 转发给 register_cron_terminal_hook 注册的钩子,提前返回;
    2. 语音来源 + 当前有人在通话 → SSE 事件(语音播报);
    3. 其余(chat 来源,或语音来源但没人在通话)→ Web Push(VAPID) + 平台推送
       (如果有 dispatch 上下文)。"""
    task = tasks.get(task_id)
    if task is None:
        return
    # 完成态未读标记跟 web/cron-task 侧栏对齐(2026-07-29 统一):任务真正跑出
    # 结果(done/failed)才标未读,用户主动 cancelled 的不标——跟 _WebSink.done()
    # 只在真正产出内容时才 set_pending_review(而不是用户手动 /abort 时)是同一套
    # 语义,不需要"哦这条被我自己取消的任务也弹了个未读点"这种噪音。
    if task["status"] != "cancelled":
        session_store.set_pending_review(tasks.session_key(task_id), True)
    # 先桥接 done 事件到主 SSE:让侧栏小红点熄灭(在播报/推送之前推,别让侧栏
    # 一直亮在已结束的任务上,也别卡住后面的异步推送路径)——这一步跟 origin
    # 无关,任何来源的任务都要让侧栏及时刷新。
    _started_tasks.discard(task_id)  # 清状态锁,支持后续续跑重新亮 dot
    _bridge_event({"conv": tasks.session_key(task_id), "type": "done"})

    origin = task.get("origin") or "voice"
    if origin == "cron":
        if _cron_terminal_hook is not None:
            await _cron_terminal_hook(task)
        return

    announce_text = _announce_text(task)
    payload = {
        "id": task["id"],
        "title": task["title"],
        "status": task["status"],
        "result_summary": task["result_summary"],
        "announce_text": announce_text,
        "updated_at": task["updated_at"],
        "dispatch_chat_id": task.get("dispatch_chat_id"),
        "origin": origin,
    }

    # ── SSE task_done:所有非 cron 来源都推(状态条/侧栏实时刷新用)。──────
    # 2026-08-06 修复:原来 _broadcast 只落在"voice 来源 + 在线"分支里,chat 来源
    # 的任务完成时前端状态条永远停在"进行中",直到手动刷新页面才全量校准回来。
    # 现在 chat 也推,前端按 origin 决定是否播报(chat 不插播语音)。
    # 语音来源 + 在线:额外合成 TTS 音频带上——"页面开着(is_online)"不等于"用户
    # 能听到":挂断通话后前端 EventSource 一直挂着(teardownCallResources 不关它),
    # 而 Omni 断开时前端只能靠 audio_b64 播放——若没有系统级推送兜底,挂断后完成
    # 的任务会彻底静默(真机反馈"不到一半的任务完成会播报")。Web Push 是系统通知,
    # 页面在前台也不碍事,双发不会吵。chat 来源哪怕这时刚好有人在通话,也不该
    # 突然插播一句不相关任务的播报(由前端按 origin 过滤)。
    if origin == "voice" and is_online():
        # 2026-08-04:Omni 出声模式下这里也合成 TTS。原来 Omni 模式跳过合成
        # (audio_b64=None,指望前端交给 Omni 念)——但挂断通话后 omniDc 关闭、
        # 不重连,前端没有 Omni 也没有 audio_b64,播报只落得一个气泡、无声。
        # 现在始终带上音频:通话中前端仍优先 Omni 朗读(双声不并存,见前端
        # flushAnnouncements 的 omniDc 分支),挂断后 Web Audio 也能念出来。
        # 代价是每次终态多一次 TTS 合成(几百毫秒),换来"挂断后也能听到"。
        audio = await tts.synthesize(announce_text, config.VOICE_TTS_VOICE)
        payload["audio_b64"] = base64.b64encode(audio).decode("ascii") if audio else None
    _broadcast("task_done", payload)

    # ── Web Push(VAPID) 发给所有订阅设备 ──────────────────────────────
    from ..gateway.adapters.web_push import PUSH

    body = task["result_summary"] or task["progress_note"] or "任务已结束"
    session_key = tasks.session_key(task_id)
    # 标题按状态区分——以前一律"任务完成",失败/取消的任务推一条"任务完成"会误导。
    title_prefix = {"done": "任务完成", "failed": "任务失败", "cancelled": "任务取消"}.get(
        task["status"], "任务结束"
    )
    await PUSH.notify(
        title=f"{title_prefix}:{task['title']}",
        body=body,
        conv=session_key,  # 点开推送直接跳到这个任务自己的会话,而不是一个不存在的占位 conv
        kind="done" if task["status"] == "done" else ("error" if task["status"] == "failed" else "cancelled"),
        tag=f"task-{task['id']}",
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
