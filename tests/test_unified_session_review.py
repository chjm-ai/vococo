"""独立评审回归：只用临时会话库、模型和脚本进程组。"""
import asyncio
import json
import os
import shlex

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.core import delegation, task_runner, tasks
from vococo.cron import scheduler
from vococo.gateway import clarify
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import agents, session_store
from vococo.tools import builtin


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def env(isolated, monkeypatch):
    monkeypatch.setattr(config, "CRON_JOBS_PATH", isolated / "cron.json")
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(tasks, "_DB", None)
    monkeypatch.setattr(task_runner, "_maybe_start_next", lambda: None)
    monkeypatch.setattr(scheduler, "_script_tasks", {})
    monkeypatch.setattr(scheduler, "_script_running", set())
    monkeypatch.setattr(scheduler, "_script_processes", {})
    monkeypatch.setattr(scheduler, "_deleting_jobs", set())
    yield isolated
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None


def source(owner):
    key = config.new_session_key()
    session_store.bind_session(key, agent_id=owner["id"])
    session_store.start_turn(key, "完整任务")
    return key


@pytest.mark.parametrize("key_option", [None, ""])
def test_explicit_empty_session_preserves_chain_and_blocks_ancestor(env, key_option):
    a, b, c = [agents.create(name) for name in ("A", "B", "C")]
    origin = source(a)
    first = delegation.delegate(origin, ["Bash"], b["id"], "B", "完整背景")
    access = json.loads(first["source_access"])
    session_store.start_turn(first["session_key"], "派单", source_access=access)
    child = delegation.create_related_task(first["session_key"], "C", "完整背景",
                                            agent_id=c["id"], session_key=key_option)
    child_access = json.loads(child["source_access"])
    assert child_access["chain_session"] == child["session_key"]
    assert child["session_key"].startswith("session:")
    assert "Bash" in child_access["denied"]
    assert delegation.get(child["id"])["parent_id"] == first["id"]
    assert delegation.get(child["id"])["chain_id"] == delegation.get(first["id"])["chain_id"]
    session_store.start_turn(child["session_key"], "再派单", source_access=child_access)
    with pytest.raises(ValueError, match="两层|祖先"):
        delegation.create_related_task(child["session_key"], "A", "拒绝回环", agent_id=a["id"])
    with pytest.raises(ValueError, match="祖先"):
        delegation.create_related_task(first["session_key"], "A", "拒绝祖先", agent_id=a["id"])


@pytest.mark.anyio
async def test_dispatch_session_reports_real_mapping(env, monkeypatch):
    monkeypatch.setattr(clarify, "current", lambda: None)
    result = await builtin.dispatch_session.handler({"title": "临时任务", "prompt": "不运行模型"})
    task = tasks.list_queued()[0]
    text = result["content"][0]["text"]
    assert f"session_id={task['session_key']}" in text
    assert f"conv={config.conversation_id_for_key(task['session_key'])}。" in text
    assert f"task_id={task['id']}" in text
    assert f"conv=task:{task['id']}" not in text


async def noop(*args, **kwargs):
    pass


def script_job(command, **options):
    return scheduler.create_job(name="临时脚本", prompt="", schedule={"kind": "webhook"},
                                mode="script", command=command, **options)


def delete_app():
    adapter = WebAdapter()
    app = web.Application()
    app.router.add_post("/delete", adapter._handle_cron_delete)
    return adapter, app


async def wait_file(path):
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(.01)


@pytest.mark.anyio
@pytest.mark.parametrize("started", [False, True])
async def test_delete_stops_queued_or_running_script_without_resurrection(env, started):
    ready, late = env / "ready", env / "late"
    command = f"touch {shlex.quote(str(ready))}; sleep 2; touch {shlex.quote(str(late))}"
    job = script_job(command)
    session_store.append(job["conv"], "历史", "保留到删除成功")
    _, app = delete_app()
    scheduler._run_job(job, noop)
    script = scheduler._script_tasks[job["id"]]
    if started:
        await wait_file(ready)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/delete", json={"id": job["id"]})
        assert response.status == 200, await response.text()
    assert script.done()
    assert scheduler.load_jobs() == []
    assert not session_store.session_binding(job["conv"])
    assert session_store.load_recent(job["conv"]) == []
    scheduler._run_job(job, noop)  # 删除前取出的旧快照也不能重建会话。
    await scheduler._run_script_job(job, noop)
    await asyncio.sleep(.05)
    assert not late.exists()
    assert session_store.load_recent(job["conv"]) == []
    assert job["id"] not in scheduler._script_processes


@pytest.mark.anyio
async def test_delete_kills_child_group_and_blocks_triggers_during_wait(env):
    ready = env / "child"
    # 子进程忽略 TERM,逼清理机制走 KILL；shell 本身先退出也不能漏掉子进程。
    command = f"(trap '' TERM; printf '%s' $$ > {shlex.quote(str(ready))}; sleep 60) & wait"
    job = script_job(command)
    adapter, app = delete_app()
    scheduler._run_job(job, noop)
    await wait_file(ready)
    proc = scheduler._script_processes[job["id"]]
    observed = []

    async def cancel_gateway(key):
        scheduler._run_job(job, noop)
        observed.append(job["id"] not in scheduler._script_tasks)
        return False, True

    adapter._cancel_and_wait_callback = cancel_gateway
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/delete", json={"id": job["id"]})
        assert response.status == 200, await response.text()
    with pytest.raises(ProcessLookupError):
        os.killpg(proc.pid, 0)
    assert observed == [True]


@pytest.mark.anyio
async def test_script_stop_timeout_preserves_job_and_history(env, monkeypatch):
    job = script_job("true")
    session_store.append(job["conv"], "原始", "不能丢")
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_cleanup():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await release.wait()

    script = asyncio.create_task(slow_cleanup())
    scheduler._script_tasks[job["id"]] = script
    await entered.wait()
    original_stop = scheduler.stop_script_job

    async def short_stop(job_id):
        return await original_stop(job_id, timeout=.01)

    monkeypatch.setattr(scheduler, "stop_script_job", short_stop)
    _, app = delete_app()
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/delete", json={"id": job["id"]})
            assert response.status == 409
            assert scheduler.load_jobs() == [job]
            assert session_store.load_recent(job["conv"])[0].assistant == "不能丢"
            assert not script.done()
            assert script.cancelling() == 1
            release.set()
            await asyncio.gather(script, return_exceptions=True)
            response = await client.post("/delete", json={"id": job["id"]})
            assert response.status == 200
    finally:
        release.set()
        await asyncio.gather(script, return_exceptions=True)


@pytest.mark.anyio
async def test_cancel_during_temporary_model_summary_cannot_rewrite_history(env, monkeypatch):
    entered = asyncio.Event()

    async def temporary_model(prompt):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduler.providers, "sidecar_chat", temporary_model)
    job = script_job("printf result", summarize_prompt="临时模型总结")
    scheduler._run_job(job, noop)
    script = scheduler._script_tasks[job["id"]]
    await asyncio.wait_for(entered.wait(), 5)
    _, app = delete_app()
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/delete", json={"id": job["id"]})
        assert response.status == 200, await response.text()
    assert script.cancelled()
    assert session_store.load_recent(job["conv"]) == []
    assert scheduler.load_jobs() == []
