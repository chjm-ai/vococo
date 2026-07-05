"""取消回复不能丢上下文。

回归 bug:第一轮回复被手动取消(scope.cancel → asyncio.CancelledError)时,
旧代码的 `except Exception` 抓不到 CancelledError,收尾落库全被跳过 → turn 行停在
assistant_text='' 被 load_recent 跳过,用户提问凭空消失,下一轮彻底没上下文。
"""
from __future__ import annotations

import asyncio

import pytest

from claude_hermes.core.agent import AgentReply, Done, TextDelta
from claude_hermes.gateway import core
from claude_hermes.gateway.core import Sink
from claude_hermes.memory import session_store


async def _noop_worktree(session_key):  # ensure_worktree 的异步替身
    return None


@pytest.mark.anyio
async def test_cancel_preserves_question_for_next_turn(isolated, monkeypatch):
    from claude_hermes.core import worktree

    monkeypatch.setattr(worktree, "ensure_worktree", _noop_worktree)

    key = "web:cancel-test"

    # 第一轮:吐一段文字后被取消(模拟 scope.cancel 注入 CancelledError)
    async def cancelled_stream(*args, **kwargs):
        yield TextDelta("好,直接调,看它到底行不行:")
        raise asyncio.CancelledError()

    monkeypatch.setattr(core, "stream_turn", cancelled_stream)

    with pytest.raises(asyncio.CancelledError):
        await core.converse(key, "对比我们的系统和 claude code", None, Sink())

    # 取消后:这一轮必须已落库(assistant_text 非空),用户提问不能消失
    recent = session_store.load_recent(key)
    assert len(recent) == 1
    assert recent[0].user == "对比我们的系统和 claude code"
    assert recent[0].assistant == "好,直接调,看它到底行不行:"

    # 第二轮:「如何」应能拿到第一轮上下文(history 非空)
    seen = {}

    async def normal_stream(history, user_text, **kwargs):
        seen["history_len"] = len(history)
        seen["first_user"] = history[0].user if history else None
        yield TextDelta("续上了")
        yield Done(AgentReply(text="续上了", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(core, "stream_turn", normal_stream)
    await core.converse(key, "如何", None, Sink())

    assert seen["history_len"] == 1
    assert seen["first_user"] == "对比我们的系统和 claude code"


@pytest.fixture
def anyio_backend():
    return "asyncio"
