"""任务策略来自数据；迁移不覆盖任务状态，不触发真实执行。"""
import json

import pytest

from vococo.cron import scheduler


@pytest.fixture
def policy_env(isolated, monkeypatch):
    directory = isolated / "data"
    directory.mkdir()
    monkeypatch.setattr(scheduler.config, "CRON_JOBS_PATH", directory / "cron_jobs.json")
    return directory


def write_defaults(directory, values):
    path = directory / "instance_config.json"
    path.write_text(json.dumps({"version": 1, "cron_job_defaults": values}))


def test_defaults_only_fill_missing_policy_fields_and_preserve_running_state(policy_env):
    job = {"id": "arbitrary", "conv": "task:fixture", "mode": "script",
           "enabled": True, "command": "never executed", "last_status": "error",
           "running_run_id": "in-flight", "pending_run_ids": ["older", "in-flight"],
           "last_run_id": "last", "schedule": {"kind": "interval", "minutes": 90}}
    scheduler.save_jobs([job])
    defaults = {"failure_policy": "always_notify_script", "track_script_runs": True}
    write_defaults(policy_env, {"arbitrary": defaults, "deleted": defaults})
    assert scheduler.load_jobs() == [{**job, **defaults}]
    assert json.loads(scheduler.config.CRON_JOBS_PATH.read_text()) == [{**job, **defaults}]
    original = scheduler.config.CRON_JOBS_PATH.read_bytes()
    scheduler.load_jobs()
    assert scheduler.config.CRON_JOBS_PATH.read_bytes() == original
    write_defaults(policy_env, {})
    assert scheduler.load_jobs() == [{**job, **defaults}]


def test_explicit_policy_wins_over_migration_defaults(policy_env):
    job = {"id": "arbitrary", "conv": "task:fixture", "failure_policy": "default",
           "track_script_runs": False}
    scheduler.save_jobs([job])
    write_defaults(policy_env, {"arbitrary": {"failure_policy": "always_notify", "track_script_runs": True}})
    assert scheduler.load_jobs() == [job]


def test_missing_instance_config_does_not_change_ordinary_jobs(policy_env):
    jobs = [{"id": name, "conv": f"task:{name}"} for name in ("ordinary", "13f146ce", "f4a2a099")]
    scheduler.save_jobs(jobs)
    assert scheduler.load_jobs() == jobs
    for job in jobs:
        assert scheduler._track_failure(job, "error", "failure") == (False, "")
        assert job["fail_streak"] == 1


@pytest.mark.parametrize("fields", [
    {"failure_policy": "unknown"}, {"failure_policy": []}, {"failure_policy": None},
    {"track_script_runs": "true"}, {"track_script_runs": 1}, {"track_script_runs": None},
    {"command": "not allowed"}, {"enabled": False}, [],
])
def test_invalid_defaults_raise_without_partial_save(policy_env, fields):
    jobs = [{"id": "good", "conv": "task:good"}, {"id": "bad", "conv": "task:bad"}]
    scheduler.save_jobs(jobs)
    original = scheduler.config.CRON_JOBS_PATH.read_bytes()
    write_defaults(policy_env, {"good": {"track_script_runs": True}, "bad": fields})
    with pytest.raises(ValueError):
        scheduler.load_jobs()
    assert scheduler.config.CRON_JOBS_PATH.read_bytes() == original


@pytest.mark.parametrize("fields", [{"failure_policy": "typo"}, {"track_script_runs": "false"}])
def test_invalid_saved_policy_is_not_silently_repaired(policy_env, fields):
    scheduler.save_jobs([{"id": "fixture", "conv": "task:fixture", **fields}])
    write_defaults(policy_env, {"fixture": {"failure_policy": "always_notify", "track_script_runs": True}})
    with pytest.raises(ValueError):
        scheduler.load_jobs()


def test_regular_edit_and_owner_mode_changes_keep_policy_fields(policy_env):
    job = scheduler.create_job(name="fixture", prompt="offline", schedule={"kind": "interval", "minutes": 90})
    job.update(failure_policy="always_notify_script", track_script_runs=True)
    scheduler.save_jobs([job])
    updated = scheduler.update_job(job["id"], name="edited", prompt="offline", schedule=job["schedule"],
                                   mode="script", command="never executed")
    assert updated["track_script_runs"] is True
    assert updated["failure_policy"] == "always_notify_script"
    updated = scheduler.update_job(job["id"], name="edited", prompt="offline", schedule=job["schedule"],
                                   mode="agent", command="", summarize_prompt="")
    assert updated["track_script_runs"] is True
    assert updated["failure_policy"] == "always_notify_script"
    for _ in range(3):
        scheduler._track_failure(updated, "error", "test")
    assert updated["fail_streak"] == 3


@pytest.mark.parametrize("policy,mode,always", [
    ("default", "script", False), ("default", None, False),
    ("always_notify", "script", True), ("always_notify", None, True),
    ("always_notify_script", "script", True), ("always_notify_script", None, False),
])
def test_failure_policy_depends_on_data_not_id(policy, mode, always):
    job = {"id": "random-fixture", "mode": mode, "enabled": True, "failure_policy": policy}
    for _ in range(3):
        quiet, _ = scheduler._track_failure(job, "error", "offline failure")
    assert quiet is not always
    assert ("fail_streak" not in job) is always
    scheduler._track_failure(job, "success", "recovered")
    assert "fail_streak" not in job
