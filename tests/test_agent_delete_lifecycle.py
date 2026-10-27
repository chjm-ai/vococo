"""删除 Agent 后不再开工,全部使用临时数据和本地假执行器。"""
from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.core import task_runner, tasks
from vococo.cron import scheduler
from vococo.memory import agents


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def owner(isolated, monkeypatch):
    monkeypatch.setattr(config, "CRON_JOBS_PATH", isolated / "jobs.json")
    monkeypatch.setattr(tasks, "_DB", None)
    yield agents.create("待删除")
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None


def make_job(owner, **kwargs):
    return scheduler.create_job(name="巡检", prompt="p", schedule={"kind": "cron", "expr": "0 9 * * *"},
                                agent_id=owner["id"], **kwargs)


@pytest.mark.anyio
@pytest.mark.parametrize("route", ["/agents/delete", "/projects/remove"])
async def test_both_remove_routes_stop_queued_tasks(owner, monkeypatch, route):
    from vococo.gateway.adapters.web import WebAdapter

    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    job = make_job(owner)
    queued = tasks.create("排队", "p", agent_id=owner["id"])
    ad = WebAdapter()
    app = web.Application()
    app.add_routes([web.post("/agents/delete", ad._handle_agent_delete),
                    web.post("/projects/remove", ad._handle_project_remove)])
    async with TestClient(TestServer(app)) as client:
        body = {"id": owner["id"]} if route == "/agents/delete" else {"hash": owner["project_hash"]}
        response = await client.post(route, json=body)
        assert response.status == 200
    assert agents.get(owner["id"]) is None
    assert not next(j for j in scheduler.load_jobs() if j["id"] == job["id"])["enabled"]
    assert tasks.get(queued["id"])["status"] == "cancelled"


@pytest.mark.anyio
async def test_agent_delete_from_real_browser(owner, monkeypatch):
    from pathlib import Path
    from test_library_integration import _browser_environment
    from vococo.gateway.adapters.web import WebAdapter

    monkeypatch.setattr(config, "WEB_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "WEB_PORT", 0)
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "delete-test")
    monkeypatch.setattr(config, "VOICE_ENABLED", False)
    make_job(owner)
    adapter = WebAdapter()
    process = None
    try:
        await adapter._start_server()
        await asyncio.sleep(0)  # 先完成启动时的孤儿回收,再创建本次排队任务。
        queued = tasks.create("排队", "p", agent_id=owner["id"])
        origin = f"http://127.0.0.1:{adapter._runner.addresses[0][1]}"
        process = await asyncio.create_subprocess_exec(
            "node", str(Path(__file__).with_name("agent_delete_browser.cjs")),
            origin, config.WEB_AUTH_TOKEN, owner["id"], env=_browser_environment(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        assert process.returncode == 0, stderr.decode() + stdout.decode()
        assert agents.get(owner["id"]) is None
        assert not scheduler.load_jobs()[0]["enabled"]
        assert tasks.get(queued["id"])["status"] == "cancelled"
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        if adapter._runner:
            await adapter._runner.cleanup()


@pytest.mark.anyio
async def test_deleted_owner_cannot_run_stale_job_or_resume_queue(owner, monkeypatch):
    job = make_job(owner)
    queued = tasks.create("排队", "p", agent_id=owner["id"])
    agents.delete(owner["id"])
    monkeypatch.setattr(task_runner, "_spawn", lambda *a, **k: pytest.fail("已删除 Agent 不应启动"))
    task_runner._maybe_start_next()
    assert tasks.get(queued["id"])["status"] == "cancelled"
    with pytest.raises(ValueError, match="Agent"):
        scheduler._run_job(job, None)
    assert not task_runner._start_one(tasks.get(queued["id"]))


@pytest.mark.anyio
async def test_deleted_owner_cannot_enable_or_try_run(owner, monkeypatch):
    from vococo.gateway.adapters.web import WebAdapter

    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    job = make_job(owner)
    agents.delete(owner["id"])
    monkeypatch.setattr(scheduler, "_push", object())
    ad = WebAdapter()
    app = web.Application()
    app.add_routes([web.post("/enable", ad._handle_cron_set_enabled),
                    web.post("/run", ad._handle_cron_run)])
    async with TestClient(TestServer(app)) as client:
        assert (await client.post("/enable", json={"id": job["id"], "enabled": True})).status == 400
        assert (await client.post("/run", json={"id": job["id"]})).status == 400
    assert not scheduler.load_jobs()[0]["enabled"]


@pytest.mark.anyio
async def test_live_owner_disabled_job_can_still_try_run(owner, monkeypatch):
    job = make_job(owner)
    scheduler.disable_agent_jobs(owner["id"])
    calls = []
    monkeypatch.setattr(scheduler, "_push", object())
    monkeypatch.setattr(task_runner, "dispatch", lambda **kw: calls.append(kw))
    assert scheduler.run_now(job["id"])
    assert len(calls) == 1


@pytest.mark.anyio
async def test_delete_stops_running_task_and_script(owner, monkeypatch):
    job = make_job(owner, mode="script", command="unused")
    row = tasks.create("正在跑", "p", agent_id=owner["id"])
    tasks.set_status(row["id"], "running")
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def fake_run():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            tasks.set_status(row["id"], "cancelled")
            stopped.set()

    running = asyncio.create_task(fake_run())
    monkeypatch.setattr(task_runner, "_running", {row["id"]: running})
    await started.wait()
    proc_started = asyncio.Event()
    terminated = []

    class Process:
        returncode = None

        async def communicate(self):
            proc_started.set()
            await asyncio.Event().wait()

    async def spawn(*args, **kwargs):
        return Process()

    async def terminate(proc):
        terminated.append(proc)
        proc.returncode = -15

    monkeypatch.setattr(asyncio, "create_subprocess_shell", spawn)
    monkeypatch.setattr(scheduler, "_terminate_script_process", terminate)
    scheduler._run_job(job, None)
    await proc_started.wait()
    try:
        result = await agents.delete_and_stop(owner["id"])
        assert stopped.is_set() and terminated
        assert not result["pending_tasks"]
        assert job["id"] not in scheduler._script_running
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        for script in list(getattr(scheduler, "_script_tasks", {}).values()):
            script.cancel()
        await asyncio.sleep(0)


@pytest.mark.anyio
async def test_deleted_owner_cannot_continue_from_original_conversation(owner, monkeypatch):
    from vococo.gateway import core
    from vococo.gateway.adapters.web import WebAdapter, _WebSink

    row = tasks.create("旧任务", "p", cwd=owner["workdir"], agent_id=owner["id"])
    tasks.set_status(row["id"], "cancelled")
    key = tasks.session_key(row["id"])
    agents.delete(owner["id"])

    async def forbidden_stream(*args, **kwargs):
        pytest.fail("不能从旧会话启动已删除的 Agent")
        yield

    monkeypatch.setattr(core, "stream_turn", forbidden_stream)
    with pytest.raises(RuntimeError, match="Agent 已删除"):
        await core.converse(key, "继续", None, _WebSink(WebAdapter(), key), cwd_override=owner["workdir"])
    assert tasks.get(row["id"])["status"] == "cancelled"


@pytest.mark.anyio
@pytest.mark.parametrize("new_owner", [True, False])
async def test_transferred_cron_not_cancelled_when_previous_owner_deleted(owner, new_owner):
    other = agents.create("接管者")
    job = make_job(owner)
    row = tasks.create("旧归属", "p", origin="cron", task_id=job["id"], agent_id=owner["id"])
    scheduler.update_job(job["id"], name=job["name"], prompt=job["prompt"], schedule=job["schedule"],
                         agent_id=other["id"] if new_owner else None)
    result = await agents.delete_and_stop(owner["id"])
    assert not result["pending_tasks"]
    assert tasks.get(row["id"])["status"] == "queued"
    assert task_runner._owner_available(tasks.get(row["id"]))
    assert scheduler.load_jobs()[0]["enabled"]


@pytest.mark.anyio
@pytest.mark.parametrize("transfer", [True, False])
async def test_script_transfer_between_archive_and_cancel_is_preserved(owner, monkeypatch, transfer):
    other = agents.create("接管者")
    job = make_job(owner, mode="script", command="unused")
    script = asyncio.create_task(asyncio.Event().wait())
    monkeypatch.setattr(scheduler, "_script_tasks", {job["id"]: script})

    async def archive_then_transfer(fn, *args):
        result = fn(*args)
        scheduler.update_job(job["id"], name=job["name"], prompt=job["prompt"], schedule=job["schedule"],
                             agent_id=other["id"] if transfer else None)
        return result

    monkeypatch.setattr(asyncio, "to_thread", archive_then_transfer)
    try:
        result = await agents.delete_and_stop(owner["id"])
        assert not script.cancelling()
        assert not result["pending_tasks"]
    finally:
        script.cancel()
        await asyncio.gather(script, return_exceptions=True)


@pytest.mark.anyio
async def test_script_children_stopped_even_when_shell_already_exited(owner, monkeypatch, isolated):
    import shlex

    pid_file = isolated / "child.pid"
    job = make_job(owner, mode="script", command=f"sleep 60 & echo $! > {shlex.quote(str(pid_file))}")
    create = asyncio.create_subprocess_shell
    procs = []

    async def spawn(*args, **kwargs):
        proc = await create(*args, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_shell", spawn)
    scheduler._run_job(job, None)
    script = scheduler._script_tasks[job["id"]]
    try:
        async with asyncio.timeout(5):
            while not procs or procs[0].returncode is None or not pid_file.exists():
                await asyncio.sleep(0.01)
        assert procs[0].returncode == 0
        result = await agents.delete_and_stop(owner["id"])
        assert not result["pending_tasks"]
        probe = await asyncio.create_subprocess_exec("ps", "-p", pid_file.read_text().strip(), "-o", "stat=",
                                                     stdout=asyncio.subprocess.PIPE)
        output, _ = await probe.communicate()
        assert not output.strip() or output.strip().startswith(b"Z")
    finally:
        script.cancel()
        await asyncio.gather(script, return_exceptions=True)
        for proc in procs:
            await scheduler._terminate_script_process(proc)
