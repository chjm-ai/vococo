"""clarify 底座 —— 让 agent 中途反问用户并阻塞等回复(async 原生)。

原版 hermes 用线程 + threading.Event;claude-hermes 是 anyio 单事件循环,
这里改用 **contextvar 路由 + anyio.Event 阻塞**,更贴合本架构。

流程:
1. 一轮对话开始前,网关(run.py._handle)用 `set_current(session_key, adapter, chat_id)`
   把"当前轮属于哪个会话/聊天"塞进 contextvar —— 会随 await 传进进程内 MCP 工具。
2. agent 调 `ask_user` 工具 → 读 contextvar 拿到 adapter/chat → 用 present_choice 弹按钮,
   然后 `await wait(...)` 阻塞本轮。
3. 用户点按钮(callback = `/clarify <id> <idx>`)或直接打字回复 → 网关在**拿会话锁之前**
   调 `try_resolve()` 解除阻塞(否则原轮占着锁会死锁),工具拿到答案,agent 继续同一轮。

超时:见 config.CLARIFY_TIMEOUT(应 < config.AGENT_TURN_TIMEOUT,这样 clarify 先返回、
本轮不会被外层硬超时直接砍掉)。
"""
from __future__ import annotations

import contextvars
import uuid
from dataclasses import dataclass, field
from typing import Optional

import anyio


@dataclass
class _Ctx:
    session_key: str
    adapter: object  # 有 present_choice / send 的 adapter
    chat_id: object


# 当前轮的路由信息(随 contextvar 传进 MCP 工具)
_current: contextvars.ContextVar[Optional[_Ctx]] = contextvars.ContextVar(
    "hermes_clarify_current", default=None
)


@dataclass
class _Pending:
    clarify_id: str
    session_key: str
    choices: list[str]
    event: anyio.Event
    response: Optional[str] = None
    awaiting_text: bool = False  # 无选项(开放式)或用户点了"其他" → 下一条文字即答案


_pending: dict[str, _Pending] = {}
_by_session: dict[str, list[str]] = {}


# ── 轮上下文(网关侧设置)──
def set_current(session_key: str, adapter: object, chat_id: object) -> contextvars.Token:
    return _current.set(_Ctx(session_key, adapter, chat_id))


def reset_current(token: contextvars.Token) -> None:
    try:
        _current.reset(token)
    except (ValueError, LookupError):
        pass


def current() -> Optional[_Ctx]:
    return _current.get()


# ── 工具侧:登记 + 阻塞等 ──
def register(session_key: str, choices: list[str]) -> _Pending:
    p = _Pending(
        clarify_id=uuid.uuid4().hex[:10],
        session_key=session_key,
        choices=list(choices or []),
        event=anyio.Event(),
        awaiting_text=not bool(choices),  # 开放式问题:下一条文字即答案
    )
    _pending[p.clarify_id] = p
    _by_session.setdefault(session_key, []).append(p.clarify_id)
    return p


async def wait(clarify_id: str, timeout: float) -> Optional[str]:
    """阻塞到被解除或超时。返回答案字符串,或 None(超时/取消)。"""
    p = _pending.get(clarify_id)
    if p is None:
        return None
    with anyio.move_on_after(timeout):
        await p.event.wait()
    _drop(clarify_id)
    return p.response


def _drop(clarify_id: str) -> None:
    p = _pending.pop(clarify_id, None)
    if p is None:
        return
    ids = _by_session.get(p.session_key)
    if ids and clarify_id in ids:
        ids.remove(clarify_id)
        if not ids:
            _by_session.pop(p.session_key, None)


# ── 网关侧:解除阻塞 ──
def resolve(clarify_id: str, response: str) -> bool:
    p = _pending.get(clarify_id)
    if p is None:
        return False
    p.response = str(response) if response is not None else ""
    p.event.set()
    return True


def has_pending(session_key: str) -> bool:
    return bool(_by_session.get(session_key))


def mark_awaiting_text(clarify_id: str) -> bool:
    """用户点了「其他」→ 标记等待打字。"""
    p = _pending.get(clarify_id)
    if p is None:
        return False
    p.awaiting_text = True
    return True


def resolve_button(clarify_id: str, token: str) -> bool:
    """按钮点击:token 是选项序号(0-based)→ 映射成选项文本后解除。"""
    p = _pending.get(clarify_id)
    if p is None:
        return False
    if token.isdigit() and p.choices and 0 <= int(token) < len(p.choices):
        return resolve(clarify_id, p.choices[int(token)])
    return resolve(clarify_id, token)


def _oldest(session_key: str) -> Optional[_Pending]:
    for cid in _by_session.get(session_key, []):
        p = _pending.get(cid)
        if p is not None:
            return p
    return None


def _coerce(p: _Pending, text: str) -> str:
    """把'2'或选项原文映射成规范选项文本;否则原样返回(自定义答案)。"""
    t = text.strip()
    if p.choices:
        if t.isdigit():
            i = int(t) - 1
            if 0 <= i < len(p.choices):
                return p.choices[i]
        for c in p.choices:
            if t.casefold() == c.strip().casefold():
                return c.strip()
    return t


def resolve_text_for_session(session_key: str, text: str) -> bool:
    """用户直接打字回复 → 解除该会话最老的待答 clarify。"""
    p = _oldest(session_key)
    if p is None:
        return False
    return resolve(p.clarify_id, _coerce(p, text))


def clear_session(session_key: str) -> int:
    """取消该会话所有待答(轮结束/新会话时调,免得阻塞线永远挂着)。"""
    ids = list(_by_session.pop(session_key, []) or [])
    n = 0
    for cid in ids:
        p = _pending.pop(cid, None)
        if p is not None:
            p.response = p.response or ""
            p.event.set()
            n += 1
    return n
