"""语音会话:主会话历史落进跟普通文字对话共用的 memory/session_store(data/state.db),
固定键 `voice-chat:main` —— 这样侧边栏「语音任务」分组、`/history` 接口、消息渲染都能
直接复用普通会话那一套现成代码,不用为语音再单独拼一套跨库读取逻辑(见
03-phase2-实现记录.md 存储统一改动一节)。

历史上这里曾经是完全独立的一份 sqlite(data/voice/voice.db),2026-07-08 起改为委托
session_store,对外的 load_history/append/get_resume/set_resume/run_turn 签名不变,
ws.py/routes.py 的调用点不用跟着改。
"""
from __future__ import annotations

from typing import AsyncIterator

from ..core.agent import Event, stream_turn
from ..memory import session_store

SESSION_KEY = "voice-chat:main"
HISTORY_LIMIT = 20


def load_history(limit: int = HISTORY_LIMIT) -> list:
    return session_store.load_recent(SESSION_KEY, limit=limit)


def append(user_text: str, assistant_text: str) -> None:
    session_store.append(SESSION_KEY, user_text, assistant_text)


def get_resume() -> str | None:
    return session_store.get_sdk_session_id(SESSION_KEY)


def set_resume(sid: str) -> None:
    session_store.set_sdk_session_id(SESSION_KEY, sid)


def run_turn(prompt_text: str, extra_mcp_servers: dict | None = None) -> AsyncIterator[Event]:
    """载入历史、调 stream_turn,把事件流原样透传给调用方消费。

    调用方负责:收到 Done 后把 (原始 user_text, reply.text) 落库(见 append)、
    存回 reply.sdk_session_id(见 set_resume)——本函数只管跑一轮,不做落库,
    因为落库要存的是剥离指令块后的原文,这层信息只有调用方(routes.py)知道。

    extra_mcp_servers:P1 任务板的三个工具(见 task_tools.build_server()),只有
    语音前台会话传它;后台任务会话(executor.py)直接调 stream_turn,不经过这里。
    """
    history = load_history()
    resume_sid = get_resume()
    return stream_turn(
        history, prompt_text, resume=resume_sid, session_key=SESSION_KEY,
        extra_mcp_servers=extra_mcp_servers,
    )
