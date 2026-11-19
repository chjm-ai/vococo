"""真实线程竞争下,任务配置的整段读改写不能覆盖 Agent 停用结果。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import threading
from unittest.mock import AsyncMock

import pytest

from vococo.core import tasks as task_store
from vococo.cron import scheduler
from vococo.memory import agents
from vococo.tools import builtin, danger


@pytest.fixture
def cron_path(isolated, tmp_path, monkeypatch):
    path = tmp_path / "cron_jobs.json"
    monkeypatch.setattr(task_store, "_DB", None)
    monkeypatch.setattr(scheduler.config, "CRON_JOBS_PATH", path)
    for aid in ("dead000001", "beef000002"):
        workdir = tmp_path / aid
        workdir.mkdir()
        agents._write_meta(aid, {"name": aid, "workdir": str(workdir)})
    yield path
    if task_store._DB is not None:
        task_store._DB.close()
        task_store._DB = None


def test_disable_agent_jobs_preserves_other_agents_and_is_idempotent(cron_path):
    scheduler.save_jobs([
        {"id": "enabled", "agent_id": "dead000001", "enabled": True, "next_run_at": 123},
        {"id": "legacy", "agent_id": "dead000001"},
        {"id": "disabled", "agent_id": "dead000001", "enabled": False, "next_run_at": 123},
        {"id": "other", "agent_id": "beef000002", "enabled": True, "next_run_at": 456},
    ])
    assert [j["id"] for j in scheduler.disable_agent_jobs("dead000001")] == ["enabled", "legacy"]
    jobs = scheduler.load_jobs()
    assert all(j["enabled"] is False and j["next_run_at"] is None for j in jobs[:3])
    assert jobs[-1]["enabled"] is True
    assert jobs[-1]["next_run_at"] == 456
    assert scheduler.disable_agent_jobs("dead000001") == []
    assert scheduler.disable_agent_jobs("missing") == []


def test_tick_does_not_restore_agent_jobs_from_old_snapshot(cron_path, monkeypatch):
    scheduler.create_job(name="定时任务", prompt="x", agent_id="dead000001",
                         schedule={"kind": "interval", "minutes": 1})
    snapshot_loaded = threading.Event()
    release_tick = threading.Event()
    disable_started = threading.Event()
    disable_finished = threading.Event()
    next_run = scheduler._next_run

    def paused_next_run(schedule, after):
        snapshot_loaded.set()
        assert release_tick.wait(5)
        return next_run(schedule, after)

    def disable():
        disable_started.set()
        result = scheduler.disable_agent_jobs("dead000001")
        disable_finished.set()
        return result

    monkeypatch.setattr(scheduler, "_next_run", paused_next_run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        tick = pool.submit(scheduler._tick, None)
        try:
            assert snapshot_loaded.wait(5)
            deleting = pool.submit(disable)
            assert disable_started.wait(5)
            # 无事务锁时,停用先落盘,随后 tick 会用旧快照把 enabled 改回 True。
            disable_finished.wait(0.1)
        finally:
            release_tick.set()
        tick.result(timeout=5)
        assert len(deleting.result(timeout=5)) == 1
    saved = scheduler.load_jobs()[0]
    assert saved["enabled"] is False
    assert saved["next_run_at"] is None


def _race_writer_with_disable(writer, monkeypatch, hook):
    snapshot_loaded, release_writer = threading.Event(), threading.Event()
    disable_started, disable_finished = threading.Event(), threading.Event()
    local = threading.local()
    original = getattr(scheduler, hook)

    def paused(*args, **kwargs):
        result = original(*args, **kwargs) if hook == "load_jobs" else None
        if getattr(local, "pause", False):
            local.pause = False
            snapshot_loaded.set()
            assert release_writer.wait(5)
        return result if hook == "load_jobs" else original(*args, **kwargs)

    def write():
        local.pause = True
        return writer()

    def disable():
        disable_started.set()
        result = scheduler.disable_agent_jobs("dead000001")
        disable_finished.set()
        return result

    monkeypatch.setattr(scheduler, hook, paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(write)
        try:
            assert snapshot_loaded.wait(5)
            deleting = pool.submit(disable)
            assert disable_started.wait(5)
            disable_finished.wait(0.1)
        finally:
            release_writer.set()
        writing.result(timeout=5)
        assert len(deleting.result(timeout=5)) == 1


@pytest.mark.parametrize("operation", ["create", "update", "rotate", "result", "migration"])
def test_config_writers_do_not_restore_disabled_agent(cron_path, monkeypatch, operation):
    job = scheduler.create_job(name="原任务", prompt="x", agent_id="dead000001",
                               schedule={"kind": "webhook"})
    original_conv = job["conv"]
    secret = job["schedule"]["secret"]
    if operation == "migration":
        # 保留旧格式,让 load_jobs 自身的迁移回写也参与竞争。
        job.pop("conv")
        scheduler.save_jobs([job])
    writers = {
        "create": lambda: scheduler.create_job(name="新任务", prompt="y", agent_id="beef000002",
                                                schedule={"kind": "interval", "minutes": 1}),
        "update": lambda: scheduler.update_job(job["id"], name="新名称", prompt="x",
                                                schedule=job["schedule"]),
        "rotate": lambda: scheduler.rotate_webhook_secret(job["id"]),
        "result": lambda: asyncio.run(scheduler._push_job_result(
            job["id"], "success", "", None, silent=True)),
        "migration": scheduler.load_jobs,
    }
    hook = "save_jobs" if operation == "migration" else "load_jobs"
    _race_writer_with_disable(writers[operation], monkeypatch, hook)
    jobs = scheduler.load_jobs()
    saved = next(j for j in jobs if j["id"] == job["id"])
    assert saved["enabled"] is False
    assert saved["next_run_at"] is None
    assert saved["conv"] == (f"task:{job['id']}" if operation == "migration" else original_conv)
    if operation == "create":
        assert len(jobs) == 2 and jobs[-1]["enabled"] is True
    elif operation == "update":
        assert saved["name"] == "新名称"
    elif operation == "rotate":
        assert saved["schedule"]["secret"] != secret
    elif operation == "result":
        assert saved["last_status"] == "success"
        assert saved["last_run_at"] is not None


def test_result_push_releases_lock_before_await(cron_path):
    job = scheduler.create_job(name="结果", prompt="x", agent_id="dead000001",
                               schedule={"kind": "interval", "minutes": 1})

    def disable_during_push():
        if not scheduler.JOBS_LOCK.acquire(timeout=2):
            return None
        try:
            return scheduler.disable_agent_jobs("dead000001")
        finally:
            scheduler.JOBS_LOCK.release()

    async def push(*args):
        # 若跨 await 持 thread RLock,这个真线程会因拿不到锁而超时。
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = await asyncio.wrap_future(pool.submit(disable_during_push))
        assert result is not None, "推送期间仍持有配置锁"
        assert len(result) == 1

    asyncio.run(scheduler._push_job_result(job["id"], "success", "结果", push))
    assert scheduler.load_jobs()[0]["enabled"] is False


@pytest.fixture
def prompt_job(cron_path):
    job = scheduler.create_job(name="原日报", prompt="原指令", agent_id="dead000001",
                               schedule={"kind": "cron", "expr": "43 10 * * *"})
    job.update(next_run_at=2000000000, last_run_at=1900000000, last_status="error",
               fail_streak=1, fail_since=1900000000, fail_digest="旧错误")
    scheduler.save_jobs([job])
    task_store.count_running()  # 临时库初始化；条件更新只查已有执行，不迁移库。
    return job


def _guarded_prompt_args(job, **overrides):
    raw = json.dumps(job, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return {"ref": job["id"], "prompt": "新日报指令", "expected_job_sha256": sha256(raw.encode()).hexdigest(),
            **overrides}


def _guarded_prompt_reply(args):
    return asyncio.run(builtin.update_cron_job.handler(args))["content"][0]["text"]


def test_guarded_prompt_changes_only_prompt_without_migrating_neighbors(cron_path, prompt_job, monkeypatch):
    neighbor = {"id": "neighbor", "name": "原有任务, 尚未迁移会话"}
    scheduler.save_jobs([prompt_job, neighbor])
    cron_path.chmod(0o640)
    approve = AsyncMock(return_value=True)
    monkeypatch.setattr(danger, "require_approval", approve)
    args = _guarded_prompt_args(prompt_job)
    assert "已更新" in _guarded_prompt_reply(args)
    approve.assert_awaited_once()
    assert args["ref"] in approve.await_args.args[1]
    assert json.loads(cron_path.read_bytes()) == [{**prompt_job, "prompt": args["prompt"]}, neighbor]
    assert cron_path.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("patch", [
    {"prompt": "另一个版本"},
    {"name": "另一个名称", "schedule": {"kind": "cron", "expr": "12 8 * * *"}},
    {"agent_id": "beef000002", "conv": "session:other"},
    {"target": {"platform": "web", "chat_id": "other"}},
    {"last_run_at": 1900000200}, {"enabled": 1},
])
def test_guarded_prompt_rejects_changes_during_approval(cron_path, prompt_job, monkeypatch, patch):
    changed_job = {**prompt_job, **patch}

    async def approve(*_):
        await asyncio.to_thread(scheduler.save_jobs, [changed_job])
        return True

    monkeypatch.setattr(danger, "require_approval", approve)
    assert "任务记录已变化" in _guarded_prompt_reply(_guarded_prompt_args(prompt_job))
    assert json.loads(cron_path.read_bytes()) == [changed_job]


@pytest.mark.parametrize("args", [
    {"expected_job_sha256": "bad"}, {"cron": "43 11 * * *"}, {"prompt": "  "},
])
def test_guarded_prompt_rejects_invalid_scope_before_approval(cron_path, prompt_job, monkeypatch, args):
    before = cron_path.read_bytes()
    approve = AsyncMock(return_value=True)
    monkeypatch.setattr(danger, "require_approval", approve)
    assert "已更新" not in _guarded_prompt_reply(_guarded_prompt_args(prompt_job, **args))
    approve.assert_not_awaited()
    assert cron_path.read_bytes() == before


def test_guarded_prompt_never_writes_after_denial(cron_path, prompt_job, monkeypatch):
    scheduler.save_jobs([prompt_job, {"id": "old"}])
    before = cron_path.read_bytes()
    monkeypatch.setattr(danger, "require_approval", AsyncMock(return_value=False))
    assert "未批准" in _guarded_prompt_reply(_guarded_prompt_args(prompt_job))
    assert cron_path.read_bytes() == before


@pytest.mark.parametrize("status", ["queued", "running"])
def test_guarded_prompt_checks_real_queued_or_running_agent_execution(cron_path, prompt_job, monkeypatch, status):
    args = _guarded_prompt_args(prompt_job)
    before = cron_path.read_bytes()
    execution = task_store.create("测试执行", "旧指令", session_key=prompt_job["conv"],
                                  job_id=prompt_job["id"], agent_id=prompt_job["agent_id"], origin="cron")
    if status == "running":
        assert task_store.set_status(execution["id"], "running")
    monkeypatch.setattr(danger, "require_approval", AsyncMock(return_value=True))
    assert "任务在途" in _guarded_prompt_reply(args)
    assert cron_path.read_bytes() == before


def test_guarded_prompt_checks_append_before_coroutine_starts(cron_path, prompt_job, monkeypatch):
    before = cron_path.read_bytes()
    execution = task_store.create("测试执行", "旧指令", session_key=prompt_job["conv"],
                                  job_id=prompt_job["id"], agent_id=prompt_job["agent_id"], origin="cron")
    assert task_store.set_status(execution["id"], "running")
    assert task_store.set_status(execution["id"], "done")
    append = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(scheduler.task_runner, "append", append)
    monkeypatch.setattr(scheduler, "_push", object())
    monkeypatch.setattr(danger, "require_approval", AsyncMock(return_value=True))

    async def simulate():
        scheduler.run_now(prompt_job["id"])
        append.assert_not_awaited()
        result = await builtin.update_cron_job.handler(_guarded_prompt_args(prompt_job))
        assert "任务在途" in result["content"][0]["text"]
        assert cron_path.read_bytes() == before
        await asyncio.sleep(0)
        append.assert_awaited_once()
        await asyncio.sleep(0)  # 已完成的 append 注销后不再误判在途。
        result = await builtin.update_cron_job.handler(_guarded_prompt_args(prompt_job))
        assert "已更新" in result["content"][0]["text"]

    asyncio.run(simulate())


@pytest.mark.parametrize("bad_config, message", [
    ('{"id": "other", "id": "nested-duplicate"}', "重复字段"),
    ('0', "任务配置不可识别"),
])
def test_guarded_prompt_rejects_malformed_neighbor(cron_path, prompt_job, monkeypatch, bad_config, message):
    before = f"[{json.dumps(prompt_job)}, {bad_config}]".encode()
    cron_path.write_bytes(before)
    monkeypatch.setattr(danger, "require_approval", AsyncMock(return_value=True))
    assert message in _guarded_prompt_reply(_guarded_prompt_args(prompt_job))
    assert cron_path.read_bytes() == before


def test_guarded_prompt_requires_execution_state(cron_path, prompt_job, monkeypatch):
    before = cron_path.read_bytes()
    monkeypatch.setattr(task_store, "_db_path", lambda: cron_path.with_name("missing.db"))
    monkeypatch.setattr(danger, "require_approval", AsyncMock(return_value=True))
    assert "无法确认任务是否在途" in _guarded_prompt_reply(_guarded_prompt_args(prompt_job))
    assert cron_path.read_bytes() == before


def test_guarded_prompt_does_not_truncate_job_file_if_replace_fails(cron_path, prompt_job, monkeypatch):
    def failed_replace(*_):
        raise OSError("模拟写盘故障")

    before = cron_path.read_bytes()
    monkeypatch.setattr(scheduler.os, "replace", failed_replace)
    monkeypatch.setattr(danger, "require_approval", AsyncMock(return_value=True))
    assert "未确认更新" in _guarded_prompt_reply(_guarded_prompt_args(prompt_job))
    assert cron_path.read_bytes() == before
    assert list(cron_path.parent.glob(".cron_jobs.json.*")) == []


def test_guarded_prompt_commit_serializes_with_agent_disable(cron_path, prompt_job, monkeypatch):
    args = _guarded_prompt_args(prompt_job)

    def writer():
        scheduler.update_job_prompt(args["ref"], args["prompt"], args["expected_job_sha256"])

    _race_writer_with_disable(writer, monkeypatch, "load_jobs")
    assert json.loads(cron_path.read_bytes()) == [{**prompt_job, "prompt": args["prompt"],
                                                  "enabled": False, "next_run_at": None}]


@pytest.mark.parametrize("terminal_but_cleaning_up", [False, True])
def test_guarded_prompt_checks_old_execution_with_newer_terminal_row(cron_path, prompt_job, monkeypatch,
                                                                     terminal_but_cleaning_up):
    old = task_store.create("旧会话", "旧指令", session_key="session:old", job_id=prompt_job["id"], origin="cron")
    new = task_store.create("新会话", "当前指令", session_key=prompt_job["conv"], job_id=prompt_job["id"], origin="cron")
    assert task_store.set_status(new["id"], "running") and task_store.set_status(new["id"], "done")
    assert task_store.task_for_job(prompt_job["id"])["id"] == new["id"]
    if terminal_but_cleaning_up:
        assert task_store.set_status(old["id"], "running") and task_store.set_status(old["id"], "done")
        monkeypatch.setitem(scheduler.task_runner._running, old["id"], object())
    before = cron_path.read_bytes()
    monkeypatch.setattr(danger, "require_approval", AsyncMock(return_value=True))
    assert "任务在途" in _guarded_prompt_reply(_guarded_prompt_args(prompt_job))
    assert cron_path.read_bytes() == before


@pytest.mark.parametrize("changes", [{"conv": "task:unbound"}, {"agent_id": "beef000002"}])
def test_guarded_prompt_verifies_persisted_binding_without_materializing(cron_path, prompt_job, monkeypatch, changes):
    job = {**prompt_job, **changes}
    scheduler.save_jobs([job])
    before = cron_path.read_bytes()
    monkeypatch.setattr(agents, "get", lambda *_: pytest.fail("持调度锁不能补建 Agent"))
    monkeypatch.setattr(danger, "require_approval", AsyncMock(return_value=True))
    assert "持久会话归属" in _guarded_prompt_reply(_guarded_prompt_args(job))
    assert cron_path.read_bytes() == before


def test_guarded_prompt_approval_binds_new_content_without_echoing_prompt(prompt_job, monkeypatch):
    approve = AsyncMock(return_value=False)
    monkeypatch.setattr(danger, "require_approval", approve)
    details = []
    for text in ("待审批指令甲", "待审批指令乙"):
        assert "未批准" in _guarded_prompt_reply(_guarded_prompt_args(prompt_job, prompt=text))
        detail = approve.await_args.args[1]
        assert text not in detail
        assert scheduler.job_snapshot_sha256({"prompt": text}) in detail
        details.append(detail)
    assert details[0] != details[1]


@pytest.mark.parametrize("completed, has_live_handle", [(True, False), (False, False), (True, True)])
def test_guarded_prompt_does_not_confuse_terminal_notification_with_execution(cron_path, prompt_job,
                                                                               monkeypatch, completed, has_live_handle):
    run_id = "a" * 32
    job = {**prompt_job, "running_run_id": run_id, "pending_run_ids": [run_id],
           "last_run_id": run_id if completed else "b" * 32}
    scheduler.save_jobs([job])
    if has_live_handle:
        monkeypatch.setitem(scheduler._script_tasks, job["id"], object())
    before = cron_path.read_bytes()
    monkeypatch.setattr(danger, "require_approval", AsyncMock(return_value=True))
    args = _guarded_prompt_args(job)
    reply = _guarded_prompt_reply(args)
    if completed and not has_live_handle:
        assert "已更新" in reply
        assert json.loads(cron_path.read_bytes()) == [{**job, "prompt": args["prompt"]}]
    else:
        assert "任务在途" in reply or "标记未核清" in reply
        assert cron_path.read_bytes() == before


def test_legacy_owner_lookup_does_not_migrate_neighbors(cron_path, prompt_job):
    job = {**prompt_job, "id": "legacy", "conv": "task:legacy"}
    scheduler.save_jobs([job, {"id": "neighbor"}])
    before = cron_path.read_bytes()
    assert agents.agent_for_session("task:legacy")["id"] == prompt_job["agent_id"]
    assert cron_path.read_bytes() == before


def test_legacy_model_edit_does_not_restore_old_prompt_after_waiting(cron_path, prompt_job, monkeypatch):
    args = _guarded_prompt_args(prompt_job)

    async def approve(*_):
        scheduler.update_job_prompt(args["ref"], args["prompt"], args["expected_job_sha256"])
        return True

    monkeypatch.setattr(danger, "require_approval", approve)
    assert "已更新" in _guarded_prompt_reply({"ref": prompt_job["id"], "model": "updated-model"})
    job, = json.loads(cron_path.read_bytes())
    assert (job["prompt"], job["model"]) == (args["prompt"], "updated-model")
