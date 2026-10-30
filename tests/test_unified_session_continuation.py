"""统一会话续接：真实入队和执行链路，只用临时库与假模型。"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vococo import config
from vococo.core import task_events, task_runner, tasks, worktree
from vococo.core.agent import AgentReply, Done
from vococo.cron import scheduler
from vococo.gateway import clarify, core, web_bridge
from vococo.gateway.adapters.base import Incoming
from vococo.gateway.adapters.web import WebAdapter
from vococo.gateway.run import GatewayRunner
from vococo.memory import agents, session_store
from vococo.tools import sessions


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def env(isolated, monkeypatch):
    monkeypatch.setattr(config, "CRON_JOBS_PATH", isolated / "cron.json")
    monkeypatch.setattr(config, "MODEL", "fake-model")
    for module, name, value in (
        (tasks, "_DB", None), (sessions, "_queued", {}),
        (task_runner, "_running", {}), (task_runner, "_foreground_scopes", {}),
        (scheduler, "_script_running", set()), (scheduler, "_script_tasks", {}),
        (clarify, "_active_sessions", set()), (clarify, "_by_session", {}),
        (clarify, "_pending", {}), (task_events, "_terminal_handlers", []),
        (task_events, "_subscribers", set()), (task_events, "_started_tasks", set()),
        (task_events, "_main_event_bridge", None),
        (web_bridge, "_dispatch_fn", None), (web_bridge, "_cancel_fn", None),
    ):
        monkeypatch.setattr(module, name, value)
    monkeypatch.setattr(task_runner, "_maybe_start_next", lambda: None)
    monkeypatch.setattr(task_runner, "cancel_and_wait", AsyncMock(
        side_effect=AssertionError("跨会话指令不能取消原执行")))
    owner = agents.create("续接测试")
    source = config.new_session_key()
    session_store.bind_session(source, agent_id=owner["id"])
    session_store.append(source, "请继续检查", "等待结果")
    adapter = WebAdapter()
    monkeypatch.setattr(adapter, "_emit", lambda event: None)
    web_bridge.register(adapter.inject, lambda _: pytest.fail("不能打断目标"), adapter.inject_by_key)
    state = SimpleNamespace(owner=owner, source=source, adapter=adapter,
                            runner=GatewayRunner([]), calls=[], root=str(isolated))
    install_fake_model(state, monkeypatch)
    yield state
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None


def install_fake_model(env, monkeypatch):
    async def cwd(*args):
        return env.root

    async def stream(history, text, **kwargs):
        key = kwargs["session_key"]
        env.calls.append((key, text, history))
        row = tasks.task_for_session(key)
        if row:
            assert row["status"] == "running"
            assert row["id"] in task_runner._running
        yield Done(AgentReply(text="续接完成", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(worktree, "execution_cwd", cwd)
    monkeypatch.setattr(worktree, "execution_cwd_for_task", cwd)
    monkeypatch.setattr(core, "stream_turn", stream)


def target_task(env, legacy=False, status="done"):
    row = tasks.create("可续接任务", "原始任务", agent_id=env.owner["id"],
                       cwd=env.root, session_key="task:legacy-continuation" if legacy else None)
    key = row["session_key"]
    session_store.append(key, "原始任务", "原始结果")
    if status != "queued":
        assert tasks.set_status(row["id"], "running")
    if status == "done":
        assert tasks.finish(row["id"], "done", "原始结果", "原始结果")
    return tasks.get(row["id"])


async def send(env, key, instruction="继续核对原始结果"):
    tool = next(tool for tool in sessions.build_tools(env.source, [])
                if tool.name == "continue_session")
    result = await tool.handler({"session_id": key, "instruction": instruction})
    return json.loads(result["content"][0]["text"])


@pytest.mark.anyio
@pytest.mark.parametrize("legacy", [False, True], ids=["session", "legacy-task"])
async def test_completed_task_continues_in_same_session_through_gateway(env, legacy):
    row = target_task(env, legacy)
    key = row["session_key"]
    assert key.startswith("task:" if legacy else "session:")
    result = await send(env, key)
    assert result["status"] == "queued" and result["session_id"] == key
    incoming = env.adapter._inbox.get_nowait()
    assert incoming.session_key == key and incoming.source_access is None
    assert tasks.get(row["id"])["status"] == "done"
    await env.runner._dispatch(env.adapter, incoming)
    assert len(env.calls) == 1 and env.calls[0][:2] == (key, incoming.text)
    assert env.calls[0][2][0].assistant == "原始结果"
    assert session_store.load_recent(key)[-1].assistant == "续接完成"
    assert tasks.task_for_session(key)["id"] == row["id"]
    assert tasks.get(row["id"])["result_full"] == "续接完成"
    assert tasks.get(row["id"])["status"] == "done"
    assert row["id"] not in task_runner._running and key not in sessions._queued
    task_runner.cancel_and_wait.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("legacy", [False, True], ids=["session", "legacy-task"])
@pytest.mark.parametrize("status", ["queued", "running"])
async def test_task_busy_before_send_is_rejected_without_mutation(env, legacy, status):
    row = target_task(env, legacy, status)
    key = row["session_key"]
    before = session_store.load_recent(key)
    result = await send(env, key)
    assert "忙碌" in result["error"] and "status" not in result
    assert tasks.get(row["id"]) == row
    assert session_store.load_recent(key) == before
    assert env.adapter._inbox.empty() and key not in sessions._queued
    assert not env.calls
    task_runner.cancel_and_wait.assert_not_awaited()


def script_target(env):
    job = scheduler.create_job(name="不启动进程", prompt="", schedule={"kind": "webhook"},
                               mode="script", command="true", agent_id=env.owner["id"])
    assert job["conv"].startswith("session:")
    assert tasks.task_for_session(job["conv"]) is None
    return job


@pytest.mark.anyio
@pytest.mark.parametrize("registry", ["_script_running", "_script_tasks"])
async def test_script_without_tasks_row_is_rejected_while_busy(env, registry):
    job = script_target(env)
    key = job["conv"]
    release = asyncio.Event()
    handle = asyncio.create_task(release.wait())
    active = getattr(scheduler, registry)
    if registry == "_script_running":
        active.add(job["id"])
    else:
        active[job["id"]] = handle
    try:
        result = await send(env, key)
        assert "忙碌" in result["error"] and "status" not in result
        assert not handle.done() and not handle.cancelling()
        assert tasks.task_for_session(key) is None
        assert env.adapter._inbox.empty() and key not in sessions._queued
        assert not env.calls
    finally:
        release.set()
        await handle


@pytest.mark.anyio
@pytest.mark.parametrize("legacy", [False, True], ids=["session", "legacy-task"])
async def test_target_starts_running_after_enqueue_original_turn_survives(env, monkeypatch, legacy):
    row = target_task(env, legacy)
    key = row["session_key"]
    assert (await send(env, key))["status"] == "queued"
    incoming = env.adapter._inbox.get_nowait()
    entered, release = asyncio.Event(), asyncio.Event()

    async def stream(*args, **kwargs):
        env.calls.append(kwargs["session_key"])
        entered.set()
        await release.wait()
        yield Done(AgentReply(text="原执行正常完成", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(core, "stream_turn", stream)
    # 真人起跑的取消入口也替换为空操作，绝不触及真实执行器或进程。
    task_runner.cancel_and_wait.side_effect = None
    original = asyncio.create_task(env.runner._dispatch(
        env.adapter, Incoming(platform="web", chat_id=key, text="真人先开始一轮")))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task_runner.cancel_and_wait.reset_mock()
        task_runner.cancel_and_wait.side_effect = AssertionError("不能取消原执行")
        await assert_busy_dispatch_preserves_original(env, incoming, row, original)
    finally:
        release.set()
        await asyncio.wait_for(original, 2)
    assert env.calls == [key]
    assert session_store.load_recent(key)[-1].assistant == "原执行正常完成"
    assert tasks.get(row["id"])["status"] == "done"


async def assert_busy_dispatch_preserves_original(env, incoming, row, original):
    key = row["session_key"]
    owner = task_runner._running[row["id"]]
    before = session_store.load_recent(key)
    env.adapter.send = AsyncMock()
    await asyncio.wait_for(env.runner._dispatch(env.adapter, incoming), 2)
    assert "目标忙碌" in env.adapter.send.await_args.args[1]
    assert not original.done() and not original.cancelling()
    assert not owner.done() and not owner.cancelling()
    assert task_runner._running[row["id"]] is owner
    assert tasks.get(row["id"])["status"] == "running"
    assert session_store.load_recent(key) == before and key not in sessions._queued
    task_runner.cancel_and_wait.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("registry", ["_script_running", "_script_tasks"])
async def test_script_becomes_busy_after_enqueue_does_not_start_model(env, registry):
    job = script_target(env)
    key = job["conv"]
    assert (await send(env, key))["status"] == "queued"
    incoming = env.adapter._inbox.get_nowait()
    release = asyncio.Event()
    handle = asyncio.create_task(release.wait())
    if registry == "_script_running":
        scheduler._script_running.add(job["id"])
    else:
        scheduler._script_tasks[job["id"]] = handle
    env.adapter.send = AsyncMock()
    try:
        await env.runner._dispatch(env.adapter, incoming)
        assert "目标忙碌" in env.adapter.send.await_args.args[1]
        assert not handle.done() and not handle.cancelling()
        assert tasks.task_for_session(key) is None and not env.calls
        assert key not in sessions._queued
        assert session_store.load_recent(key) == []
    finally:
        release.set()
        await handle


@pytest.mark.anyio
@pytest.mark.parametrize("legacy", [False, True], ids=["session", "legacy-task"])
async def test_busy_after_gateway_check_is_rejected_by_execution_guard(env, monkeypatch, legacy):
    row = target_task(env, legacy)
    key = row["session_key"]
    assert (await send(env, key))["status"] == "queued"
    incoming = env.adapter._inbox.get_nowait()
    release = asyncio.Event()
    handle = asyncio.create_task(release.wait())
    before = session_store.load_recent(key)

    async def cwd(*args):
        # 网关检查已通过，模拟等待工作目录期间另一个执行先抢到目标。
        assert tasks.set_status(row["id"], "running")
        task_runner._running[row["id"]] = handle
        return env.root

    monkeypatch.setattr(worktree, "execution_cwd_for_task", cwd)
    env.adapter.send = AsyncMock()
    try:
        await env.runner._dispatch(env.adapter, incoming)
        assert "目标忙碌" in env.adapter.send.await_args.args[1]
        assert not handle.done() and not handle.cancelling()
        assert task_runner._running[row["id"]] is handle
        assert tasks.get(row["id"])["status"] == "running"
        assert session_store.load_recent(key) == before and not env.calls
        assert key not in sessions._queued
        task_runner.cancel_and_wait.assert_not_awaited()
    finally:
        release.set()
        await handle
