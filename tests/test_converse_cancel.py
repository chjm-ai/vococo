"""取消回复不能丢上下文。

回归 bug:第一轮回复被手动取消(scope.cancel → asyncio.CancelledError)时,
旧代码的 `except Exception` 抓不到 CancelledError,收尾落库全被跳过 → turn 行停在
assistant_text='' 被 load_recent 跳过,用户提问凭空消失,下一轮彻底没上下文。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

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


@pytest.mark.anyio
async def test_cancel_interrupts_sdk_client(monkeypatch):
    """手动取消时应调用 client.interrupt() 通知 CLI 子进程停止生成。"""
    from claude_agent_sdk import StreamEvent

    from claude_hermes.core import agent

    interrupt_mock = AsyncMock()

    # 模拟一个先吐一个字再永久挂起的 SSE 流
    async def slow_stream():
        yield StreamEvent(
            uuid="1",
            session_id="s",
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "一段"},
            },
        )
        await asyncio.Event().wait()

    class FakeClient:
        def __init__(self, options=None):
            self.options = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, *args, **kwargs):
            return None

        def receive_messages(self):
            return slow_stream()

        interrupt = interrupt_mock

    monkeypatch.setattr(agent, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(agent.providers, "resolve", lambda *args: ("claude-sonnet-4", {}))
    monkeypatch.setattr(agent.settings_store, "hermes_enabled", lambda: False)
    monkeypatch.setattr(agent.settings_store, "effective_external_mcp", lambda: {})
    monkeypatch.setattr(agent.settings_store, "effective_skills", lambda: None)
    monkeypatch.setattr(agent, "build_system_prompt", lambda: "")
    monkeypatch.setattr(agent, "build_mcp_servers", lambda: {})
    monkeypatch.setattr(agent, "build_hooks", lambda: {})

    async def consume():
        async for _ev in agent.stream_turn([], "hi"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.1)  # 让 stream_turn 进入 slow_stream 的挂起点
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    interrupt_mock.assert_awaited_once()


@pytest.fixture
def anyio_backend():
    return "asyncio"
