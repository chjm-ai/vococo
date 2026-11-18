"""真实 Git 回归：只删可重建目录，执行与回收互斥。"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from vococo import config
from vococo.core import execution, repository_cleanup as cleanup, repository_locks as locks, tasks, worktree
from vococo.memory import agents, session_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def task_database(monkeypatch):
    monkeypatch.setattr(tasks, "_DB", None)
    yield
    if tasks._DB is not None:
        tasks._DB.close()


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


async def setup(root, *, legacy=False):
    source = root / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "测试")
    git(source, "config", "user.email", "test@example.invalid")
    (source / "file.txt").write_text("原文\n")
    (source / ".gitignore").write_text(".env*\nnode_modules/\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "初始内容")
    owner = agents.create("业务")
    agents.update(owner["id"], repositories=[str(source)])
    key = agents.main_session_key(owner)
    binding = execution.policy_for_session(key).repositories[0]
    if legacy:
        binding.checkout.parent.mkdir(parents=True)
        git(source, "clone", "--no-local", "--", str(source), str(binding.checkout))
        git(binding.checkout, "config", "vococo.source", str(source))
    else:
        await execution.prepare_repositories(execution.policy_for_session(key))
    return owner, key, binding


@pytest.mark.anyio
@pytest.mark.parametrize("legacy", [False, True])
async def test_archive_previews_rebuildable_copy(isolated, legacy):
    _, key, binding = await setup(isolated, legacy=legacy)
    session_store.set_conv_archived(key, True)
    results = await worktree.preview_session_cleanup(key)
    assert ("旧式完整克隆" if legacy else "归档满七天") in results[0]["reason"]
    assert binding.checkout.exists()
    assert (binding.source / "file.txt").read_text() == "原文\n"


@pytest.mark.anyio
@pytest.mark.parametrize("legacy", [False, True])
async def test_sweep_reclaims_only_managed_archived_checkout_after_seven_days(isolated, monkeypatch, legacy):
    _, key, binding = await setup(isolated, legacy=legacy)
    clock = [time.time()]
    monkeypatch.setattr(cleanup.time, "time", lambda: clock[0])
    revision = git(binding.checkout, "rev-parse", "HEAD")
    session_store.append(key, "测试问题", "测试回复")
    session_store.set_conv_archived(key, True)
    clock[0] += cleanup._ARCHIVE_GRACE - 1
    assert (await cleanup.sweep())[0]["reason"] == "归档七天观察期未满"
    clock[0] += 1
    with (binding.checkout / "file.txt").open():
        assert not (await cleanup.sweep())[0]["removed"]
    row = (await cleanup.sweep())[0]
    if legacy:
        assert "旧式完整克隆" in row["reason"] and binding.checkout.exists()
        return
    assert row["removed"] and row["bytes"] > 0 and not binding.checkout.exists()
    assert session_store.load_recent(key, 1)[0].user == "测试问题"
    assert git(binding.source, "rev-parse", binding.branch) == revision
    session_store.set_conv_archived(key, False)
    await execution.prepare_repositories(execution.policy_for_session(key))
    assert git(binding.checkout, "rev-parse", "HEAD") == revision


@pytest.mark.anyio
async def test_business_checkout_with_review_lease_is_not_removed(isolated, monkeypatch):
    _, key, binding = await setup(isolated)
    session_store.set_conv_archived(key, True)
    now = time.time()
    monkeypatch.setattr(cleanup.time, "time", lambda: now + cleanup._ARCHIVE_GRACE)
    async with locks.checkout_lease(binding.checkout):
        assert (await cleanup.sweep())[0]["reason"] == "正在执行或初始化"
    assert binding.checkout.exists()


@pytest.mark.anyio
async def test_unarchiving_restarts_business_copy_observation(isolated, monkeypatch):
    _, key, binding = await setup(isolated)
    clock = [time.time()]
    monkeypatch.setattr(cleanup.time, "time", lambda: clock[0])
    session_store.set_conv_archived(key, True)
    clock[0] += cleanup._ARCHIVE_GRACE
    session_store.set_conv_archived(key, False)
    assert await cleanup.sweep() == []
    session_store.set_conv_archived(key, True)
    assert (await cleanup.sweep())[0]["reason"] == "归档七天观察期未满"
    assert binding.checkout.exists()


@pytest.mark.anyio
async def test_delete_keeps_local_configuration_and_records_path(isolated):
    _, key, binding = await setup(isolated)
    config_file = binding.checkout / ".env.local"
    config_file.write_text("LOCAL_ONLY=1\n")
    results = await worktree.remove_worktree(key)
    cleanup.record_retained_session(key, results)
    session_store.delete_session(key)
    assert config_file.read_text() == "LOCAL_ONLY=1\n"
    receipt = (config.DATA_DIR / "retained-session-copies.jsonl").read_text()
    assert str(binding.checkout) in receipt
    assert "有本地配置" in receipt


@pytest.mark.anyio
@pytest.mark.parametrize("content", ["dirty", "untracked", "commit", "ignored", "detached"])
async def test_preserves_work_in_archived_session(isolated, content):
    _, key, binding = await setup(isolated)
    path = binding.checkout
    if content == "dirty":
        (path / "file.txt").write_text("不能丢\n")
    elif content == "untracked":
        (path / "draft.txt").write_text("不能丢\n")
    elif content == "ignored":
        (path / ".env.local").write_text("LOCAL_ONLY=1\n")
    elif content == "detached":
        git(path, "switch", "--detach")
    else:
        (path / "file.txt").write_text("独有提交\n")
        git(path, "add", ".")
        git(path, "commit", "-m", "尚未合并")
    session_store.set_conv_archived(key, True)
    results = await cleanup.sweep()
    assert results and not results[0]["removed"] and results[0]["reason"]
    assert path.is_dir()


@pytest.fixture
def backup_directory(isolated, monkeypatch):
    from vococo.core import repository_backup

    folder = isolated / "private-backup"
    folder.mkdir(mode=0o700)
    monkeypatch.setattr(config, "REPOSITORY_BACKUP_DIR", str(folder))
    monkeypatch.setattr(repository_backup, "_different_volume", lambda *_: True)
    return folder


@pytest.mark.anyio
async def test_thirty_day_backup_restores_unique_commit_and_working_files(isolated, monkeypatch, backup_directory):
    _, key, binding = await setup(isolated)
    path = binding.checkout
    git(path, "commit", "--allow-empty", "-m", "独有提交")
    revision = git(path, "rev-parse", "HEAD")
    (path / "file.txt").unlink()
    (path / ".env.local").write_text("LOCAL_ONLY=1\n")
    (path / "draft").write_text("本地成果")
    (path / "link").symlink_to("draft")
    (path / "empty").mkdir()
    clock = [time.time()]
    monkeypatch.setattr(cleanup.time, "time", lambda: clock[0])
    session_store.set_conv_archived(key, True)
    clock[0] += cleanup.repository_backup.ARCHIVE_AGE - 1
    assert not (await cleanup.sweep())[0]["removed"]
    clock[0] += 1
    row, = await cleanup.sweep()
    assert row["removed"], row
    assert not path.exists() and (binding.source / "file.txt").exists()
    backup = Path(row["backup_path"])
    assert backup.parent == backup_directory
    receipt = config.DATA_DIR / "repository-backups" / f"{backup.name}.json"
    assert json.loads(receipt.read_text())["backup_path"] == str(backup)
    restored = isolated / "restored"
    git(isolated, "clone", "--no-checkout", "--branch", binding.branch, str(backup / "history.bundle"), str(restored))
    git(restored, "read-tree", "HEAD")
    subprocess.run(["tar", "-xpf", str(backup / "files.tar"), "-C", str(restored)], check=True)
    assert git(restored, "rev-parse", "HEAD") == revision
    assert "D file.txt" in git(restored, "status", "--porcelain") and not (restored / "file.txt").exists()
    assert (restored / ".env.local").read_text() == "LOCAL_ONLY=1\n"
    assert (restored / "link").is_symlink() and (restored / "link").read_text() == "本地成果"
    assert (restored / "empty").is_dir()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [
    "same_volume", "unmounted", "staged", "open", "changed", "last_git_check", "receipt_denied", "unconfigured",
])
async def test_thirty_day_backup_failure_keeps_checkout(isolated, monkeypatch, backup_directory, failure):
    from vococo.core import repository_backup

    _, key, binding = await setup(isolated)
    path = binding.checkout
    draft = path / "draft"
    draft.write_text("保留")
    session_store.set_conv_archived(key, True)
    now = time.time()
    monkeypatch.setattr(cleanup.time, "time", lambda: now + repository_backup.ARCHIVE_AGE)
    if failure == "same_volume":
        monkeypatch.setattr(repository_backup, "_different_volume", lambda _dest, _path: False)
    elif failure == "unmounted":
        monkeypatch.setattr(config, "REPOSITORY_BACKUP_DIR", str(backup_directory / "missing"))
    elif failure == "staged":
        git(path, "add", "draft")
    elif failure == "unconfigured":
        monkeypatch.setattr(config, "REPOSITORY_BACKUP_DIR", "")
    elif failure == "changed":
        snapshot = repository_backup._snapshot

        def changed(source, destination):
            stamps = snapshot(source, destination)
            (source / "draft").write_text("最新内容")
            return stamps

        monkeypatch.setattr(repository_backup, "_snapshot", changed)
    elif failure == "last_git_check":
        git_revision = repository_backup._git_revision

        def changed_in_git(source, *args):
            revision = git_revision(source, *args)
            if list(backup_directory.glob("vococo-*")):
                (source / "draft").write_text("最新内容")
            return revision

        monkeypatch.setattr(repository_backup, "_git_revision", changed_in_git)
    elif failure == "receipt_denied":
        (config.DATA_DIR / "repository-backups").mkdir(mode=0o755)
    if failure == "open":
        with draft.open():
            row, = await cleanup.sweep()
    else:
        row, = await cleanup.sweep()
    assert not row["removed"] and "三十天" in row["reason"], row
    assert draft.read_text() == ("最新内容" if failure in {"changed", "last_git_check"} else "保留")
    assert not row.get("backup_path")
    assert not list(backup_directory.iterdir())


@pytest.mark.anyio
async def test_sweep_preserves_unarchived_even_if_old(isolated):
    _, _, binding = await setup(isolated)
    old = time.time() - 40 * 86400
    os.utime(binding.checkout, (old, old))
    assert await cleanup.sweep() == []
    assert binding.checkout.exists()


@pytest.mark.anyio
async def test_deleted_session_has_grace_period(isolated, monkeypatch):
    _, key, binding = await setup(isolated)
    old = time.time() - 8 * 86400
    os.utime(binding.checkout, (old, old))
    session_store.delete_session(key)
    assert await cleanup.sweep() == []
    observed = time.time()
    monkeypatch.setattr(cleanup.time, "time", lambda: observed + 8 * 86400)
    results = await cleanup.sweep()
    assert not results[0]["removed"]
    assert "找不到已归档会话" in results[0]["reason"]
    assert binding.checkout.exists()


@pytest.mark.anyio
async def test_active_lease_blocks_cleanup_then_releases(isolated):
    owner, key, binding = await setup(isolated)
    session_store.set_conv_archived(key, True)
    with locks.lease(owner["id"], locks.session_digest(key)):
        results = await cleanup.sweep()
        assert results[0]["reason"] == "正在执行或初始化"
        assert binding.checkout.exists()
    assert (await cleanup.sweep())[0]["reason"] == "归档七天观察期未满"


@pytest.mark.anyio
async def test_initializer_lock_blocks_cleanup(isolated):
    import fcntl

    _, key, binding = await setup(isolated)
    session_store.set_conv_archived(key, True)
    with (binding.checkout.parent / f".{binding.checkout.name}.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        assert (await cleanup.sweep())[0]["reason"] == "正在执行或初始化"
        assert binding.checkout.exists()


@pytest.mark.anyio
async def test_stream_lease_covers_execution_and_is_released_on_close(isolated):
    owner, key, binding = await setup(isolated)
    session_store.set_conv_archived(key, True)

    @locks.protect_repositories
    async def stream(history, text, session_key=None):
        yield "开始"
        yield "结束"

    events = stream([], "任务", session_key=key)
    assert await anext(events) == "开始"
    assert not (await cleanup.sweep())[0]["removed"]
    await events.aclose()
    assert (await cleanup.sweep())[0]["reason"] == "归档七天观察期未满"


@pytest.mark.anyio
async def test_converse_waits_before_reactivating_archived_session(isolated, monkeypatch):
    from vococo.gateway import core

    owner, key, _ = await setup(isolated)
    session_store.set_conv_archived(key, True)
    entered = asyncio.Event()

    async def converse(*_args):
        entered.set()

    monkeypatch.setattr(core, "_converse", converse)
    query = "SELECT archived_at FROM session_archive_times WHERE session_key=?"
    with locks.lease(owner["id"], locks.session_digest(key), exclusive=True):
        pending = asyncio.create_task(core.converse(key, "继续", None, None))
        await asyncio.sleep(0.15)
        assert not entered.is_set()
        assert session_store._conn().execute(query, (key,)).fetchone()
    await pending
    assert entered.is_set()
    assert session_store._conn().execute(query, (key,)).fetchone() is None


@pytest.mark.anyio
async def test_new_execution_waits_for_collector_lease(isolated):
    owner, key, _ = await setup(isolated)
    entered = asyncio.Event()

    @locks.protect_repositories
    async def stream(session_key=None):
        entered.set()
        yield "完成"

    with locks.lease(owner["id"], locks.session_digest(key), exclusive=True):
        events = stream(session_key=key)
        pending = asyncio.create_task(anext(events))
        await asyncio.sleep(0.15)
        assert not entered.is_set()
    assert await pending == "完成"
    await events.aclose()


@pytest.mark.anyio
async def test_ignored_dependency_may_contain_local_results(isolated):
    _, key, binding = await setup(isolated)
    dependency = binding.checkout / "node_modules"
    dependency.mkdir()
    (dependency / "cache.js").write_text("本地文件\n")
    session_store.set_conv_archived(key, True)
    assert (await cleanup.sweep())[0]["reason"] == "有本地配置或其它忽略文件"
    assert (dependency / "cache.js").exists()


@pytest.mark.anyio
async def test_legacy_unique_commit_is_preserved(isolated):
    _, key, binding = await setup(isolated, legacy=True)
    git(binding.checkout, "-c", "user.name=测试", "-c", "user.email=test@example.invalid",
        "commit", "--allow-empty", "-m", "独有成果")
    session_store.set_conv_archived(key, True)
    assert (await cleanup.sweep())[0]["reason"] == "有原仓库未保存的提交"
    assert binding.checkout.exists()


@pytest.mark.anyio
@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
async def test_hidden_index_changes_are_preserved(isolated, flag):
    _, key, binding = await setup(isolated)
    git(binding.checkout, "update-index", flag, "file.txt")
    (binding.checkout / "file.txt").write_text("隐藏的成果\n")
    assert not git(binding.checkout, "status", "--porcelain")
    session_store.set_conv_archived(key, True)
    assert (await cleanup.sweep())[0]["reason"] == "索引隐藏了部分文件状态"
    assert (binding.checkout / "file.txt").read_text() == "隐藏的成果\n"


@pytest.mark.anyio
@pytest.mark.parametrize("legacy", [False, True])
async def test_submodule_with_hidden_changes_is_preserved(isolated, legacy):
    _, key, binding = await setup(isolated, legacy=legacy)
    module = isolated / "module-source"
    module.mkdir()
    git(module, "init", "-b", "main")
    (module / "file.txt").write_text("原文\n")
    git(module, "add", ".")
    git(module, "-c", "user.name=测试", "-c", "user.email=test@example.invalid",
        "commit", "-m", "子模块初始内容")
    path = binding.checkout
    git(path, "-c", "protocol.file.allow=always", "submodule", "add", str(module), "module")
    git(path, "-c", "user.name=测试", "-c", "user.email=test@example.invalid",
        "commit", "-am", "增加子模块")
    if legacy:
        git(binding.source, "fetch", str(path), "HEAD")
        git(binding.source, "merge", "--ff-only", "FETCH_HEAD")
    else:
        git(binding.source, "merge", "--ff-only", binding.branch)
    git(path, "config", "submodule.module.ignore", "all")
    (path / "module" / "file.txt").write_text("隐藏成果\n")
    assert not git(path, "status", "--porcelain")
    session_store.set_conv_archived(key, True)
    assert (await cleanup.sweep())[0]["reason"] == "含子模块，需人工确认成果"
    assert (path / "module" / "file.txt").read_text() == "隐藏成果\n"


@pytest.mark.anyio
async def test_reset_commit_in_reflog_is_preserved(isolated):
    _, key, binding = await setup(isolated)
    git(binding.checkout, "commit", "--allow-empty", "-m", "尚未保存的成果")
    git(binding.checkout, "reset", "--hard", "HEAD~1")
    session_store.set_conv_archived(key, True)
    assert (await cleanup.sweep())[0]["reason"] == "有未合并提交"
    assert binding.checkout.exists()


@pytest.mark.anyio
async def test_restored_session_restarts_orphan_grace(isolated, monkeypatch):
    _, key, binding = await setup(isolated)
    session_store.delete_session(key)
    assert await cleanup.sweep() == []
    marker = binding.checkout.parent / ".orphaned-at"
    assert marker.exists()
    session_store.set_conv_archived(key, False)
    assert await cleanup.sweep() == []
    assert not marker.exists()
    session_store.delete_session(key)
    now = time.time()
    monkeypatch.setattr(cleanup.time, "time", lambda: now + 8 * 86400)
    assert await cleanup.sweep() == []
    assert binding.checkout.exists()


@pytest.mark.anyio
@pytest.mark.parametrize("old", [False, True])
async def test_initializing_worktree_has_additional_grace(isolated, old):
    _, key, binding = await setup(isolated)
    staging = binding.checkout.parent / ".initializing-test"
    git(binding.source, "worktree", "move", str(binding.checkout), str(staging))
    if old:
        stamp = time.time() - 2 * 86400
        os.utime(staging, (stamp, stamp))
    session_store.set_conv_archived(key, True)
    results = await cleanup.sweep()
    assert bool(results) == old
    assert staging.exists()


@pytest.mark.anyio
async def test_sweep_loop_isolated_failure_does_not_stop_service(isolated, monkeypatch):
    from vococo.core import storage_maintenance
    called = []
    monkeypatch.setattr(storage_maintenance, "maintain", lambda *args, **kwargs: called.append(args))

    async def failed():
        raise ValueError("失败必须保留目录")

    async def stop(delay):
        if delay == 86400:
            raise asyncio.CancelledError

    monkeypatch.setattr(cleanup, "sweep", failed)
    monkeypatch.setattr(cleanup.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await cleanup.sweep_loop()
    assert len(called) == 1
