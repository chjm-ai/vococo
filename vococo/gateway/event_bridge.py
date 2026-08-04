"""非 Web 入口(目前是 Telegram)→ Web 侧 SSE 的事件桥接。

Telegram 走的是自己独立的 _TelegramSink,整轮流程不经过 WebAdapter._emit,所以哪怕
Telegram 私聊跟网页「主会话」共用同一个 session_key(UNIFY_SESSIONS 默认开),网页
侧边栏/当前会话也完全感知不到 Telegram 那边发生了什么,得手动刷新页面才能看到。

TelegramAdapter 不持有 WebAdapter 实例(职责分层,避免循环 import)——运行时注册
回调是本项目里这类跨模块桥接的统一模式,另见 voice/notify.py 的
register_main_event_bridge、gateway/web_bridge.py。
"""
from __future__ import annotations

from typing import Callable

_emit: Callable[[dict], None] | None = None


def register(emit_fn: Callable[[dict], None] | None) -> None:
    """由 WebAdapter 自己在 __init__ 里注册(没有 Web 入口时永远不注册,push() 静默跳过)。"""
    global _emit
    _emit = emit_fn


def push(payload: dict) -> None:
    """尽力而为地广播一条事件给 Web 侧 SSE;没注册(纯 Telegram/无 Web 入口)时静默跳过。"""
    if _emit is not None:
        try:
            _emit(payload)
        except Exception:
            pass
