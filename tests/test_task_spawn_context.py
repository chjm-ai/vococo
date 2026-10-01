"""后台任务起跑时不继承派发方会话的上下文(审批 / 铃铛要记在任务自己名下)。

2026-10-01 实测:网页会话里 dispatch_session 派出的任务继承了派发方的 clarify 路由,
任务的审批弹到派发方会话、铃铛批准后「请重新执行」也发回派发方,任务本身收不到。
"""
from __future__ import annotations

import pytest

from vococo.core import task_runner
from vococo.core import tasks as bg_tasks
from vococo.core.agent import AgentReply, Done
from vococo.gateway import clarify
from vococo.tools import danger
from vococo.voice import notify


@pytest.fixture
def env(isolated, monkeypatch):
    monkeypatch.setattr(bg_tasks, "_DB", None)
    task_runner._running.clear()
    yield
    if bg_tasks._DB is not None:
        bg_tasks._DB.close()
        bg_tasks._DB = None


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _noop(*_a, **_k) -> None:
    return None


@pytest.mark.anyio
async def test_dispatched_task_runs_under_its_own_session(env, monkeypatch):
    seen: dict = {}

    async def fake_stream_turn(history, prompt, cwd=None, session_key=None, **kw):
        # 审批闸(danger 的 hook)就在这一层取会话归属
        seen["clarify"] = clarify.current()
        seen["key"] = danger.current_session_key()
        yield Done(AgentReply(text="好了", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(task_runner, "stream_turn", fake_stream_turn)
    monkeypatch.setattr(notify, "on_task_terminal", _noop)

    # 模拟:网页会话这一轮里调 dispatch_session(走真实入口 dispatch → _maybe_start_next)
    ctok = clarify.set_current("web:dispatcher", object(), "chat")
    try:
        task = task_runner.dispatch(title="x", prompt="y", origin="chat",
                                    context_session_key="web:dispatcher")
    finally:
        clarify.reset_current(ctok)
    await task_runner._running[task["id"]]

    assert seen["clarify"] is None
    assert seen["key"] == f"task:{task['id']}"
