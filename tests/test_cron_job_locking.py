"""真实线程竞争下,任务配置的整段读改写不能覆盖 Agent 停用结果。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from vococo.cron import scheduler


@pytest.fixture
def cron_path(isolated, tmp_path, monkeypatch):
    path = tmp_path / "cron_jobs.json"
    monkeypatch.setattr(scheduler.config, "CRON_JOBS_PATH", path)
    return path


def test_disable_agent_jobs_preserves_other_agents_and_is_idempotent(cron_path):
    scheduler.save_jobs([
        {"id": "enabled", "agent_id": "deleted", "enabled": True, "next_run_at": 123},
        {"id": "legacy", "agent_id": "deleted"},
        {"id": "disabled", "agent_id": "deleted", "enabled": False, "next_run_at": 123},
        {"id": "other", "agent_id": "kept", "enabled": True, "next_run_at": 456},
    ])
    assert [j["id"] for j in scheduler.disable_agent_jobs("deleted")] == ["enabled", "legacy"]
    jobs = scheduler.load_jobs()
    assert all(j["enabled"] is False and j["next_run_at"] is None for j in jobs[:3])
    assert jobs[-1]["enabled"] is True
    assert jobs[-1]["next_run_at"] == 456
    assert scheduler.disable_agent_jobs("deleted") == []
    assert scheduler.disable_agent_jobs("missing") == []


def test_tick_does_not_restore_agent_jobs_from_old_snapshot(cron_path, monkeypatch):
    scheduler.create_job(name="定时任务", prompt="x", agent_id="deleted",
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
        result = scheduler.disable_agent_jobs("deleted")
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
        result = scheduler.disable_agent_jobs("deleted")
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
    job = scheduler.create_job(name="原任务", prompt="x", agent_id="deleted",
                               schedule={"kind": "webhook"})
    secret = job["schedule"]["secret"]
    if operation == "migration":
        # 保留旧格式,让 load_jobs 自身的迁移回写也参与竞争。
        job.pop("conv")
        scheduler.save_jobs([job])
    writers = {
        "create": lambda: scheduler.create_job(name="新任务", prompt="y", agent_id="kept",
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
    assert saved["conv"] == f"task:{job['id']}"
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
    job = scheduler.create_job(name="结果", prompt="x", agent_id="deleted",
                               schedule={"kind": "interval", "minutes": 1})

    def disable_during_push():
        if not scheduler.JOBS_LOCK.acquire(timeout=2):
            return None
        try:
            return scheduler.disable_agent_jobs("deleted")
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
