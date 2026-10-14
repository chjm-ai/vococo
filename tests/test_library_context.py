"""作品来源必须绑定实际执行轮次，包括 SDK 复用与后台任务。"""
import asyncio
import contextvars

import pytest

from vococo.core import task_runner, tasks, worktree
from vococo.core.agent import AgentReply, Done
from vococo.gateway import clarify, core
from vococo.gateway.core import Sink
from vococo.memory import session_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_context_without_route_and_explicit_turn_binding():
    assert clarify.get_context() is None
    source = clarify.bind_turn("task:one", 17)
    try:
        assert clarify.current() is None
        assert clarify.get_context().turn_id == 17
        assert clarify.get_context().session_key == "task:one"
    finally:
        clarify.unbind_turn("task:one", 17, source)
    assert clarify.get_context() is None


def test_ended_snapshot_never_claims_a_new_turn():
    route = clarify.set_current("web:one", object(), "one")
    first = clarify.bind_turn("web:one", 1)
    snapshot = contextvars.copy_context()
    clarify.unbind_turn("web:one", 1, first)
    second = clarify.bind_turn("web:one", 2)
    try:
        assert snapshot.run(clarify.get_context).turn_id is None
        assert clarify.get_context().turn_id == 2
    finally:
        clarify.unbind_turn("web:one", 2, second)
        clarify.reset_current(route)
    assert snapshot.run(clarify.get_context).turn_id is None


@pytest.mark.anyio
async def test_sdk_pool_compatibility_rejects_previous_turn_snapshot(monkeypatch):
    from vococo import config
    from vococo.core import agent, client_pool

    monkeypatch.setattr(client_pool, "_pool", {})
    monkeypatch.setattr(config, "CLIENT_POOL_IDLE_TTL", 120)
    class Client:
        disconnected = False
        async def disconnect(self):
            self.disconnected = True

    def key():
        return agent._compat_base_key("test", {}, {}, [], "/tmp", True, {})

    first_token = clarify.bind_turn("task:pool", 100)
    old_snapshot = contextvars.copy_context()
    old_key = key()
    old_client = Client()
    await client_pool.checkin("task:pool", old_client, old_key, "sdk")
    clarify.unbind_turn("task:pool", 100, first_token)
    second_token = clarify.bind_turn("task:pool", 101)
    active_snapshot = contextvars.copy_context()
    try:
        assert key() != old_key
        assert await client_pool.checkout("task:pool", key(), "sdk") is None
        assert old_client.disconnected
        overlapping = clarify.bind_turn("task:pool", 102)
        try:
            assert active_snapshot.run(clarify.get_context).turn_id == 101
            assert old_snapshot.run(clarify.get_context).turn_id is None
            assert active_snapshot.run(key) != key()
        finally:
            clarify.unbind_turn("task:pool", 102, overlapping)
    finally:
        clarify.unbind_turn("task:pool", 101, second_token)


@pytest.mark.anyio
async def test_concurrent_sessions_keep_separate_turns():
    async def execute(key, turn):
        token = clarify.bind_turn(key, turn)
        try:
            await asyncio.sleep(0)
            assert clarify.get_context().session_key == key
            assert clarify.get_context().turn_id == turn
        finally:
            clarify.unbind_turn(key, turn, token)
    await asyncio.gather(execute("a", 3), execute("b", 4))
    assert clarify.get_context() is None


@pytest.mark.anyio
async def test_overlapping_same_session_never_overwrites_running_source():
    first_ready, second_ready, first_checked = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def first():
        token = clarify.bind_turn("task:overlap", 101)
        snapshot = contextvars.copy_context()
        first_ready.set()
        try:
            await second_ready.wait()
            assert clarify.get_context().turn_id == 101
            assert snapshot.run(clarify.get_context).turn_id == 101
            first_checked.set()
        finally:
            clarify.unbind_turn("task:overlap", 101, token)

    async def second():
        await first_ready.wait()
        token = clarify.bind_turn("task:overlap", 102)
        second_ready.set()
        try:
            await first_checked.wait()
            assert clarify.get_context().turn_id == 102
        finally:
            clarify.unbind_turn("task:overlap", 102, token)

    await asyncio.gather(first(), second())
    assert clarify.get_context() is None


@pytest.mark.anyio
async def test_converse_binds_started_turn_and_cleans_up_on_failure(isolated, monkeypatch):
    async def cwd(_):
        return str(isolated)

    seen = []
    async def stream(*args, **kwargs):
        ctx = clarify.get_context()
        seen.append((ctx.session_key, ctx.turn_id))
        yield Done(AgentReply(text="完成", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(worktree, "execution_cwd", cwd)
    monkeypatch.setattr(core, "stream_turn", stream)
    await core.converse("web:library", "问题", None, Sink())
    turn = session_store.load_history("web:library")[0]
    assert seen == [("web:library", turn["id"])]
    assert clarify.get_context() is None

    async def cancelled(*args, **kwargs):
        assert clarify.get_context().turn_id is not None
        raise asyncio.CancelledError()
        yield

    monkeypatch.setattr(core, "stream_turn", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await core.converse("web:library", "取消", None, Sink())
    assert clarify.get_context() is None


@pytest.mark.anyio
async def test_background_task_binds_own_turn_without_inheriting_route(isolated, monkeypatch):
    from vococo.voice import notify

    monkeypatch.setattr(tasks, "_DB", None)
    task_runner._running.clear()
    seen = []
    async def noop(*args, **kwargs):
        pass

    async def stream(*args, **kwargs):
        seen.append((clarify.current(), clarify.get_context().session_key, clarify.get_context().turn_id))
        yield Done(AgentReply(text="完成", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(task_runner, "stream_turn", stream)
    monkeypatch.setattr(notify, "on_task_terminal", noop)
    token = clarify.set_current("web:dispatcher", object(), "dispatcher")
    try:
        task = task_runner.dispatch(title="test", prompt="测试", origin="chat",
                                    context_session_key="web:dispatcher", cwd=str(isolated))
    finally:
        clarify.reset_current(token)
    try:
        await task_runner._running[task["id"]]
        key = tasks.session_key(task["id"])
        turn = session_store.load_history(key)[0]
        assert seen == [(None, key, turn["id"])]
        assert clarify.get_context() is None
    finally:
        if tasks._DB is not None:
            tasks._DB.close()
            tasks._DB = None
