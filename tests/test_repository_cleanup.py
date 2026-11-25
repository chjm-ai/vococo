"""真实 Git 回归：按归档期限回收，执行与回收互斥。"""
from __future__ import annotations

import asyncio
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
    reason = results[0]["reason"]
    if legacy:
        assert "旧式完整克隆，仅列清单" in reason and "满十天后" not in reason
    else:
        assert "归档满五天" in reason and "满十天后" in reason
    assert binding.checkout.exists()
    assert (binding.source / "file.txt").read_text() == "原文\n"


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["clean", "dirty", "metadata", "legacy"])
async def test_business_archive_warns_before_starting_cleanup_timer(isolated, monkeypatch, kind):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from vococo.gateway.adapters.web import WebAdapter

    _, key, binding = await setup(isolated, legacy=kind == "legacy")
    file = binding.checkout / ".env.local"
    if kind == "metadata":
        file = Path(git(binding.checkout, "rev-parse", "--absolute-git-dir")) / "config.worktree"
    if kind != "clean":
        file.write_text("[test]\nlocal = true\n")
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    app = web.Application()
    app.router.add_post("/conv/archive", WebAdapter()._handle_conv_archive)
    query = "SELECT archived_at FROM session_archive_times WHERE session_key=?"

    async with TestClient(TestServer(app)) as client:
        response = await client.post("/conv/archive", json={"conv": key})
        data = await response.json()
        if kind != "legacy":
            assert data["need_confirm"] and "归档满十天后" in data["msg"]
            assert session_store._conn().execute(query, (key,)).fetchone() is None
            response = await client.post("/conv/archive", json={"conv": key, "force": True})
            data = await response.json()
            assert data["warning"] and "不备份" in data["msg"]
        assert data["ok"] and not data["cleanup"][0]["removed"]
    assert session_store._conn().execute(query, (key,)).fetchone() is not None
    assert binding.checkout.is_dir()
    if kind != "clean":
        assert file.read_text() == "[test]\nlocal = true\n"


@pytest.mark.anyio
async def test_uninitialized_copy_is_inventory_only_in_archive_preview(isolated):
    _, key, binding = await setup(isolated)
    pending = binding.checkout.parent / ".initializing-unfinished"
    pending.mkdir()
    result = next(row for row in await worktree.preview_session_cleanup(key) if row["path"] == str(pending))
    assert result["reason"] == "初始化残留，仅列清单" and not result.get("need_confirm")
    assert pending.exists()


@pytest.mark.anyio
@pytest.mark.parametrize("legacy", [False, True])
async def test_sweep_reclaims_only_managed_archived_checkout_at_five_days(isolated, monkeypatch, legacy):
    _, key, binding = await setup(isolated, legacy=legacy)
    clock = [time.time()]
    monkeypatch.setattr(cleanup.time, "time", lambda: clock[0])
    revision = git(binding.checkout, "rev-parse", "HEAD")
    session_store.append(key, "测试问题", "测试回复")
    session_store.set_conv_archived(key, True)
    clock[0] += 5 * 86400 - 1
    assert (await cleanup.sweep())[0]["reason"] == "归档五天观察期未满"
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
    monkeypatch.setattr(cleanup.time, "time", lambda: now + 5 * 86400)
    async with locks.checkout_lease(binding.checkout):
        assert (await cleanup.sweep())[0]["reason"] == "正在执行或初始化"
    assert binding.checkout.exists()


@pytest.mark.anyio
async def test_unarchiving_restarts_business_copy_observation(isolated, monkeypatch):
    _, key, binding = await setup(isolated)
    clock = [time.time()]
    monkeypatch.setattr(cleanup.time, "time", lambda: clock[0])
    session_store.set_conv_archived(key, True)
    clock[0] += 5 * 86400
    session_store.set_conv_archived(key, False)
    assert await cleanup.sweep() == []
    session_store.set_conv_archived(key, True)
    assert (await cleanup.sweep())[0]["reason"] == "归档五天观察期未满"
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


@pytest.mark.anyio
async def test_ten_day_recovery_discards_working_files_without_archive(isolated, monkeypatch):
    _, key, binding = await setup(isolated)
    path = binding.checkout
    git(path, "commit", "--allow-empty", "-m", "会话分支提交")
    revision = git(path, "rev-parse", "HEAD")
    session_store.append(key, "测试问题", "测试回复")
    (path / ".env.local").write_text("LOCAL_ONLY=1\n")
    (path / "draft").write_text("可以丢弃的草稿")
    git(path, "add", "draft")
    (path / "outside").symlink_to(binding.source, target_is_directory=True)
    clock = [time.time()]
    monkeypatch.setattr(cleanup.time, "time", lambda: clock[0])
    session_store.set_conv_archived(key, True)
    clock[0] += 10 * 86400 - 1
    assert not (await cleanup.sweep())[0]["removed"]
    clock[0] += 1
    preview, = await worktree.preview_session_cleanup(key)
    assert "不备份" in preview["reason"] and path.exists()
    row, = await cleanup.sweep()
    assert row["removed"] and "归档满十天" in row["reason"], row
    assert not path.exists()
    assert not (config.DATA_DIR / "repository-backups").exists()
    assert session_store.load_recent(key, 1)[0].user == "测试问题"
    assert (binding.source / "file.txt").read_text() == "原文\n"
    assert git(binding.source, "rev-parse", binding.branch) == revision


@pytest.mark.anyio
@pytest.mark.parametrize("protected", ["open", "restored", "nested_repo", "bare_repo", "legacy", "git_lock", "merge"])
async def test_ten_day_recovery_keeps_unsafe_or_reactivated_checkout(isolated, monkeypatch, protected):
    _, key, binding = await setup(isolated, legacy=protected == "legacy")
    path = binding.checkout
    draft = path / "draft"
    draft.write_text("暂不删除")
    session_store.set_conv_archived(key, True)
    now = time.time()
    monkeypatch.setattr(cleanup.time, "time", lambda: now + 10 * 86400)
    if protected == "restored":
        session_store.set_conv_archived(key, False)
        assert await cleanup.sweep() == []
        session_store.set_conv_archived(key, True)
    elif protected in {"nested_repo", "bare_repo"}:
        (path / "nested").mkdir()
        git(path / "nested", "init", *(["--bare"] if protected == "bare_repo" else []))
    elif protected in {"git_lock", "merge"}:
        git_dir = Path(git(path, "rev-parse", "--absolute-git-dir"))
        marker = "index.lock" if protected == "git_lock" else "MERGE_HEAD"
        (git_dir / marker).write_text(git(path, "rev-parse", "HEAD") + "\n")
    if protected == "open":
        with draft.open():
            row, = await cleanup.sweep()
    else:
        row, = await cleanup.sweep()
    assert not row["removed"], row
    assert draft.read_text() == "暂不删除"


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
    clock = [time.time()]
    monkeypatch.setattr(cleanup.time, "time", lambda: clock[0])
    assert await cleanup.sweep() == []
    clock[0] += 7 * 86400 - 1
    assert await cleanup.sweep() == []
    clock[0] += 1
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
    assert (await cleanup.sweep())[0]["reason"] == "归档五天观察期未满"


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
    assert (await cleanup.sweep())[0]["reason"] == "归档五天观察期未满"


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
