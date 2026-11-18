"""隔离临时仓库验证开发回收，不接触真实工作树或数据库。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess

import pytest

from vococo import config
from vococo.core import development_cleanup as cleanup, repository_locks, tasks
from vococo.memory import session_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def repository(isolated, monkeypatch):
    root = isolated / "source"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "测试")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "file.txt").write_text("原文\n")
    (root / ".gitignore").write_text(".env*\nnode_modules/\n.venv/\ndata/\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "初始内容")
    monkeypatch.setattr(config, "ROOT_DIR", root)
    monkeypatch.setattr(tasks, "_DB", None)
    monkeypatch.setattr(config, "CRON_JOBS_PATH", isolated / "cron_jobs.json")
    session_store._conn()
    clock = [1_800_000_000.0]
    monkeypatch.setattr(cleanup.time, "time", lambda: clock[0])
    yield root, clock
    if tasks._DB is not None:
        tasks._DB.close()


def add_worktree(root: Path, name="test"):
    key = f"session:{name}"
    path = root / "data" / "worktrees" / session_store.project_hash(str(root)) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    git(root, "worktree", "add", "-b", f"vococo/{name}", str(path))
    session_store.bind_session(key, agent_id="test-owner")
    session_store.set_worktree(key, str(path))
    return key, path


@pytest.mark.anyio
async def test_deleted_tracked_file_is_preserved_instead_of_failing_inspection(repository):
    root, clock = repository
    key, path = add_worktree(root)
    (path / "file.txt").unlink()
    rows = await cleanup.sweep()
    assert "有未提交或未跟踪成果" in rows[0]["reason"]
    assert "失败" not in rows[0]["reason"]
    clock[0] += 365 * 86400
    assert not (await cleanup.sweep())[0]["removed"]
    assert path.exists() and session_store.get_worktree(key) == str(path)


async def observe_new(root):
    assert await cleanup.sweep() == []
    key, path = add_worktree(root)
    session_store.set_conv_archived(key, True)
    rows = await cleanup.sweep()
    assert rows[0]["reason"] == "归档七天观察期未满"
    return key, path


@pytest.mark.anyio
async def test_new_worktree_requires_full_observed_idle_period(repository):
    root, clock = repository
    key, path = await observe_new(root)
    clock[0] += 7 * 86400 - 1
    assert not (await cleanup.sweep())[0]["removed"]
    clock[0] += 1
    assert (await cleanup.sweep())[0]["removed"]
    assert not path.exists()
    assert session_store.get_worktree(key) is None
    assert (root / "file.txt").read_text() == "原文\n"
    assert git(root, "branch", "--list", "vococo/test")


@pytest.mark.anyio
async def test_development_worktree_backup_starts_at_thirty_days(repository, monkeypatch):
    from vococo.core import repository_backup

    root, clock = repository
    backup = root.parent / "private-backup"
    backup.mkdir(mode=0o700)
    monkeypatch.setattr(config, "REPOSITORY_BACKUP_DIR", str(backup))
    monkeypatch.setattr(repository_backup, "_different_volume", lambda *_: True)
    key, path = add_worktree(root)
    (path / "draft").write_text("本地成果")
    session_store.set_conv_archived(key, True)
    clock[0] += repository_backup.ARCHIVE_AGE - 1
    assert not (await cleanup.sweep())[0]["removed"]
    clock[0] += 1
    active_lease = repository_locks.session_digest(key)
    with repository_locks.lease("test-owner", active_lease):
        assert not (await cleanup.sweep())[0]["removed"]
    row, = await cleanup.sweep()
    assert row["removed"], row
    assert not path.exists() and session_store.get_worktree(key) is None
    assert Path(row["backup_path"]).parent == backup


@pytest.mark.anyio
@pytest.mark.parametrize("days", [7, 30])
@pytest.mark.parametrize("metadata", ["draft", "resolve_undo"])
async def test_checkout_recovery_preserves_private_git_work(repository, monkeypatch, days, metadata):
    from vococo.core import repository_backup

    root, clock = repository
    key, path = await observe_new(root)
    backup = root.parent / "private-backup"
    backup.mkdir(mode=0o700)
    monkeypatch.setattr(config, "REPOSITORY_BACKUP_DIR", str(backup))
    monkeypatch.setattr(repository_backup, "_different_volume", lambda *_: True)
    if metadata == "draft":
        git_dir = Path(git(path, "rev-parse", "--absolute-git-dir"))
        (git_dir / "COMMIT_EDITMSG").write_text("提交草稿\n")
    else:
        oid = git(path, "rev-parse", "HEAD:file.txt")
        index_info = f"0 {'0' * len(oid)}\tfile.txt\n" + "".join(
            f"100644 {oid} {stage}\tfile.txt\n" for stage in (1, 2, 3)
        )
        subprocess.run(["git", "-C", str(path), "update-index", "--index-info"], input=index_info, text=True, check=True)
        git(path, "add", "file.txt")
        assert git(path, "ls-files", "--resolve-undo")
    assert not git(path, "status", "--porcelain")
    clock[0] += days * 86400
    row, = await cleanup.sweep()
    assert not row["removed"] and ("提交消息草稿" if metadata == "draft" else "冲突撤销记录") in row["reason"]
    assert path.exists() and session_store.get_worktree(key) == str(path)
    assert not list(backup.iterdir())


@pytest.mark.anyio
@pytest.mark.parametrize("explicit_task", [False, True])
async def test_web_branch_recreates_retired_development_worktree(repository, monkeypatch, explicit_task):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from vococo.gateway.adapters.web import WebAdapter
    from vococo.memory import agents

    root, clock = repository
    key, path = await observe_new(root)
    revision = git(path, "rev-parse", "HEAD")
    clock[0] += 8 * 86400
    assert (await cleanup.sweep())[0]["removed"]
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(config, "project_root_for", lambda _: str(root))
    monkeypatch.setattr(agents, "agent_for_session", lambda _: {
        "id": "test-owner", "execution_mode": "development", "workdir": str(root),
    })
    if explicit_task:
        different_root = root.parent / "agent-default"
        git(root, "clone", str(root), str(different_root))
        monkeypatch.setattr(config, "project_root_for", lambda _: str(different_root))
        monkeypatch.setattr(agents, "agent_for_session", lambda _: {
            "id": "test-owner", "execution_mode": "development", "workdir": str(different_root),
        })
        connection = tasks._conn()
        connection.execute(
            "INSERT INTO tasks(id,title,prompt,cwd,status,created_at,updated_at,session_key) VALUES (?,?,?,?,?,?,?,?)",
            ("test-task", "测试", "测试", str(root), "done", clock[0], clock[0], key),
        )
        connection.commit()
    adapter = WebAdapter()
    app = web.Application()
    app.router.add_post("/conv/git/branch", adapter._handle_conv_git_branch)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/conv/git/branch", json={"conv": key, "name": "resumed"})
        assert response.status == 200, await response.text()
        assert (await response.json())["project_path"] == str(root)
    assert git(path, "branch", "--show-current") == "resumed"
    assert git(path, "rev-parse", "HEAD") == revision
    assert git(root, "branch", "--show-current") == "main"


@pytest.mark.anyio
async def test_clean_filter_does_not_make_local_bytes_rebuildable(repository):
    root, clock = repository
    git(root, "config", "filter.fold.clean", "tr A B")
    (root / "file.txt").write_text("B\n")
    (root / ".gitattributes").write_text("file.txt filter=fold\n")
    git(root, "add", ".gitattributes", "file.txt")
    git(root, "commit", "-m", "增加内容过滤器")
    key, path = add_worktree(root)
    (path / "file.txt").write_text("A\n")
    assert not git(path, "status", "--porcelain")
    session_store.set_conv_archived(key, True)
    await cleanup.sweep()
    clock[0] += 8 * 86400
    row = (await cleanup.sweep())[0]
    assert not row["removed"] and "原始内容不同" in row["reason"]
    assert (path / "file.txt").read_text() == "A\n"


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["queued", "done"])
async def test_review_task_protects_parent_checkout(repository, status):
    root, clock = repository
    _, path = await observe_new(root)
    clock[0] += 8 * 86400
    connection = tasks._conn()
    connection.execute(
        "INSERT INTO tasks(id,title,prompt,cwd,status,created_at,updated_at,session_key,review_cwd) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("review", "评审", "测试", str(root), status, clock[0], clock[0], "session:review", str(path)),
    )
    connection.commit()
    reason = "会话或任务正在执行/排队" if status == "queued" else "仍有关联评审可续跑，保留原工作树"
    assert (await cleanup.sweep())[0]["reason"] == reason
    assert path.exists()


@pytest.mark.anyio
async def test_checkout_lease_prevents_retirement(repository):
    root, clock = repository
    _, path = await observe_new(root)
    clock[0] += 8 * 86400
    async with repository_locks.checkout_lease(path):
        row = (await cleanup.sweep())[0]
        assert not row["removed"] and "正在执行" in row["reason"]
    assert path.exists()


@pytest.mark.anyio
async def test_interrupted_retirement_reports_existing_recovery_path(repository):
    from vococo.core.repository_recovery import pending_retirements, retirement_record

    root, _ = repository
    _, path = await observe_new(root)
    retired = path.with_name(f".retiring-{path.name}-interrupted")
    with retirement_record(path, retired):
        git(root, "worktree", "move", str(path), str(retired))
    row, = pending_retirements()
    assert row["recovery_path"] == str(retired) and not row["removed"]
    assert not path.exists() and (retired / "file.txt").read_text() == "原文\n"
    git(root, "worktree", "move", str(retired), str(path))
    assert pending_retirements() == []


@pytest.mark.anyio
async def test_first_inventory_is_never_automatically_deleted(repository):
    root, clock = repository
    _, path = add_worktree(root)
    assert (await cleanup.sweep())[0]["reason"] == "旧存量仅列清单"
    clock[0] += 365 * 86400
    assert (await cleanup.sweep())[0]["reason"] == "旧存量仅列清单"
    assert path.exists()


@pytest.mark.anyio
async def test_initial_busy_worktree_is_still_legacy_inventory(repository):
    root, clock = repository
    key, path = add_worktree(root)
    with repository_locks.lease("test-owner", repository_locks.session_digest(key)):
        assert (await cleanup.sweep())[0]["reason"] == "正在执行或初始化"
    clock[0] += 30 * 86400
    assert (await cleanup.sweep())[0]["reason"] == "旧存量仅列清单"
    assert path.exists()


@pytest.mark.anyio
@pytest.mark.parametrize("content", ["dirty", "untracked", ".env.local", "node_modules/local.js", ".venv/config"])
async def test_all_local_results_including_ignored_cache_paths_are_preserved(repository, content):
    root, clock = repository
    _, path = await observe_new(root)
    target = path / ({"dirty": "file.txt", "untracked": "draft.txt"}.get(content, content))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("不能丢失\n")
    clock[0] += 8 * 86400
    row = (await cleanup.sweep())[0]
    assert not row["removed"]
    assert "成果" in row["reason"] or "忽略文件" in row["reason"]
    assert target.read_text() == "不能丢失\n"


@pytest.mark.anyio
@pytest.mark.parametrize("reset", [False, True])
async def test_unmerged_commit_and_reset_reflog_are_preserved(repository, reset):
    root, clock = repository
    _, path = await observe_new(root)
    git(path, "commit", "--allow-empty", "-m", "未合并成果")
    if reset:
        git(path, "reset", "--hard", "HEAD~1")
    clock[0] += 8 * 86400
    row = (await cleanup.sweep())[0]
    assert "未合并提交" in row["reason"]
    assert path.exists()


@pytest.mark.anyio
async def test_merged_commit_resets_observation_then_can_be_collected(repository):
    root, clock = repository
    _, path = await observe_new(root)
    git(path, "commit", "--allow-empty", "-m", "已合并成果")
    git(root, "merge", "--ff-only", "vococo/test")
    clock[0] += 8 * 86400
    assert (await cleanup.sweep())[0]["reason"] == "七天闲置观察期未满"
    clock[0] += 7 * 86400
    assert (await cleanup.sweep())[0]["removed"]


@pytest.mark.anyio
@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
async def test_hidden_index_flags_are_never_treated_as_clean(repository, flag):
    root, clock = repository
    _, path = await observe_new(root)
    git(path, "update-index", flag, "file.txt")
    (path / "file.txt").write_text("隐藏成果\n")
    clock[0] += 8 * 86400
    assert (await cleanup.sweep())[0]["reason"] == "索引隐藏了文件状态"
    assert path.exists()


@pytest.mark.anyio
async def test_execution_lease_blocks_then_restarts_idle_observation(repository):
    root, clock = repository
    key, path = await observe_new(root)
    clock[0] += 8 * 86400
    with repository_locks.lease("test-owner", repository_locks.session_digest(key)):
        assert (await cleanup.sweep())[0]["reason"] == "正在执行或初始化"
    assert (await cleanup.sweep())[0]["reason"] == "七天闲置观察期未满"
    assert path.exists()
    clock[0] += 7 * 86400
    assert (await cleanup.sweep())[0]["removed"]


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["queued", "running", "done"])
async def test_task_activity_and_running_state_are_checked(repository, status):
    root, clock = repository
    key, path = await observe_new(root)
    connection = tasks._conn()
    connection.execute(
        "INSERT INTO tasks(id,title,prompt,cwd,status,created_at,updated_at,session_key) VALUES (?,?,?,?,?,?,?,?)",
        ("task-test", "测试", "测试", str(path), status, clock[0], clock[0] + 6 * 86400, key),
    )
    connection.commit()
    clock[0] += 8 * 86400
    row = (await cleanup.sweep())[0]
    assert not row["removed"]
    assert row["reason"] == ("七天闲置观察期未满" if status == "done" else "会话或任务正在执行/排队")
    assert path.exists()


@pytest.mark.anyio
async def test_new_turn_restarts_idle_period(repository):
    root, clock = repository
    key, path = await observe_new(root)
    clock[0] += 6 * 86400
    connection = session_store._conn()
    connection.execute("INSERT INTO turns(session_key,ts,user_text,assistant_text) VALUES (?,?,?,?)",
                       (key, clock[0], "新的对话", "已完成"))
    connection.commit()
    clock[0] += 2 * 86400
    assert (await cleanup.sweep())[0]["reason"] == "七天闲置观察期未满"
    assert path.exists()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["corrupt-state", "symlink-state", "git-error"])
async def test_check_failures_never_remove_work(repository, monkeypatch, failure):
    root, clock = repository
    _, path = await observe_new(root)
    state = config.DATA_DIR / "development-cleanup-state.json"
    if failure == "corrupt-state":
        state.write_text("{broken")
    elif failure == "symlink-state":
        target = config.DATA_DIR / "saved.json"
        state.rename(target)
        state.symlink_to(target)
    else:
        def fail(*args):
            raise ValueError("SECRET_MUST_NOT_LEAK")
        monkeypatch.setattr(cleanup, "_git", fail)
    clock[0] += 8 * 86400
    rows = await cleanup.sweep()
    assert "失败" in rows[0]["reason"]
    assert "SECRET_MUST_NOT_LEAK" not in json.dumps(rows)
    assert path.exists()


@pytest.mark.anyio
async def test_locked_and_detached_worktrees_and_primary_checkout_are_protected(repository):
    root, clock = repository
    _, path = await observe_new(root)
    git(root, "worktree", "lock", str(path))
    clock[0] += 8 * 86400
    assert not (await cleanup.sweep())[0]["removed"]
    git(root, "worktree", "unlock", str(path))
    git(path, "switch", "--detach")
    assert not (await cleanup.sweep())[0]["removed"]
    assert root.exists() and path.exists()
    with pytest.raises(ValueError):
        cleanup._validate(root, root)


@pytest.mark.anyio
async def test_unknown_and_legacy_paths_are_inventory_only(repository):
    root, clock = repository
    assert await cleanup.sweep() == []
    key, path = add_worktree(root)
    legacy = config.DATA_DIR / "worktrees" / "old-hash" / "test"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    git(root, "worktree", "move", str(path), str(legacy))
    session_store.set_worktree(key, str(legacy))
    assert "非标准路径" in (await cleanup.sweep())[0]["reason"]
    clock[0] += 30 * 86400
    assert not (await cleanup.sweep())[0]["removed"]
    assert legacy.exists()


@pytest.mark.anyio
async def test_missing_session_binding_is_inventory_only(repository):
    root, clock = repository
    key, path = await observe_new(root)
    session_store._conn().execute("DELETE FROM session_meta WHERE session_key=?", (key,))
    session_store._conn().commit()
    clock[0] += 30 * 86400
    assert "绑定" in (await cleanup.sweep())[0]["reason"]
    assert path.exists()


@pytest.mark.anyio
async def test_worktree_local_config_and_git_locks_are_preserved(repository):
    root, clock = repository
    _, path = await observe_new(root)
    git_dir = cleanup._git_path(path, "--git-dir")
    clock[0] += 8 * 86400
    (git_dir / "config.worktree").write_text("[test]\nlocal = private\n")
    assert (await cleanup.sweep())[0]["reason"] == "有工作树专属配置或引用"
    (git_dir / "config.worktree").unlink()
    (git_dir / "index.lock").touch()
    assert (await cleanup.sweep())[0]["reason"] == "Git 操作未结束"
    assert path.exists()


@pytest.mark.anyio
async def test_same_path_recreated_gets_a_new_observation_period(repository):
    root, clock = repository
    _, path = await observe_new(root)
    old = path.with_name("original")
    git(root, "worktree", "move", str(path), str(old))
    clock[0] += 8 * 86400
    git(root, "worktree", "add", "-b", "vococo/recreated", str(path))
    assert (await cleanup.sweep())[0]["removed"] is False
    assert path.exists()


@pytest.mark.anyio
async def test_late_ignored_write_during_retirement_is_restored(repository, monkeypatch):
    root, clock = repository
    _, path = await observe_new(root)
    original_git = cleanup._git
    def write_during_move(cwd, *args):
        if args[:3] == ("worktree", "move", str(path)):
            (path / ".env.local").write_text("PRIVATE_LOCAL_RESULT\n")
        return original_git(cwd, *args)
    sessions = cleanup._sessions()[str(path)]
    _, activity = cleanup._task_state(sessions, path)
    fingerprint = cleanup._fingerprint(path, sessions, activity)
    monkeypatch.setattr(cleanup, "_git", write_during_move)
    row = cleanup._retire(root, path, lambda retired: cleanup._retirement_reason(
        root, retired, sessions, fingerprint, original=path))
    assert not row["removed"]
    assert "本地文件" in row["reason"]
    assert (path / ".env.local").read_text() == "PRIVATE_LOCAL_RESULT\n"


@pytest.mark.anyio
async def test_open_file_handle_prevents_removal_and_restores_original_path(repository):
    root, clock = repository
    _, path = await observe_new(root)
    clock[0] += 8 * 86400
    with (path / "file.txt").open("r"):
        row = (await cleanup.sweep())[0]
    assert not row["removed"]
    assert path.exists()


@pytest.mark.anyio
async def test_unavailable_process_probe_preserves_results(repository, monkeypatch):
    root, clock = repository
    _, path = await observe_new(root)
    monkeypatch.setattr(cleanup.shutil, "which", lambda _: None)
    clock[0] += 8 * 86400
    assert not (await cleanup.sweep())[0]["removed"]
    assert path.is_dir()


@pytest.mark.anyio
async def test_reset_record_old_oid_is_preserved_even_if_prior_records_expired(repository):
    root, clock = repository
    _, path = await observe_new(root)
    git(path, "commit", "--allow-empty", "-m", "唯一成果")
    git(path, "reset", "--hard", "HEAD~1")
    logs = [cleanup._git_path(path, "--git-dir") / "logs/HEAD", root / ".git/logs/refs/heads/vococo/test"]
    for log in logs:
        log.write_bytes(log.read_bytes().splitlines()[-1] + b"\n")
    (cleanup._git_path(path, "--git-dir") / "ORIG_HEAD").unlink(missing_ok=True)
    clock[0] += 8 * 86400
    assert "未合并提交" in (await cleanup.sweep())[0]["reason"]
    assert path.exists()


@pytest.mark.anyio
async def test_reflog_only_activity_restarts_idle_period(repository):
    root, clock = repository
    _, path = await observe_new(root)
    original = git(path, "rev-parse", "HEAD")
    clock[0] += 8 * 86400
    git(path, "commit", "--allow-empty", "-m", "新活动")
    git(root, "merge", "--ff-only", "vococo/test")
    git(path, "reset", "--hard", original)
    assert (await cleanup.sweep())[0]["reason"] == "七天闲置观察期未满"
    assert path.exists()


@pytest.mark.anyio
async def test_failed_state_preflight_never_deletes_worktree(repository):
    root, clock = repository
    _, path = await observe_new(root)
    (config.DATA_DIR / "development-cleanup-state.tmp").mkdir()
    clock[0] += 8 * 86400
    assert "失败" in (await cleanup.sweep())[0]["reason"]
    assert path.exists()


@pytest.mark.anyio
async def test_failed_final_receipt_keeps_actual_removal_result(repository, monkeypatch):
    root, clock = repository
    _, path = await observe_new(root)
    original_write = cleanup._write_state
    calls = []
    def fail_final(state_path, state):
        calls.append(state_path)
        if len(calls) == 3:
            raise OSError("PRIVATE_DETAIL")
        original_write(state_path, state)
    monkeypatch.setattr(cleanup, "_write_state", fail_final)
    clock[0] += 8 * 86400
    rows = await cleanup.sweep()
    assert rows[0]["removed"] and not path.exists()
    assert "保存失败" in rows[-1]["reason"]
    assert "PRIVATE_DETAIL" not in json.dumps(rows)


@pytest.mark.anyio
async def test_runtime_probe_never_loads_or_migrates_cron_configuration(repository, monkeypatch):
    from vococo.cron import scheduler

    root, _ = repository
    def fail_load():
        raise AssertionError("巡检不能迁移定时任务")
    monkeypatch.setattr(scheduler, "load_jobs", fail_load)
    await observe_new(root)
    assert not config.CRON_JOBS_PATH.exists()


@pytest.mark.anyio
async def test_clean_file_restored_by_hand_restarts_idle_period(repository):
    root, clock = repository
    _, path = await observe_new(root)
    clock[0] += 8 * 86400
    (path / "file.txt").write_text("原文\n")
    assert not git(path, "status", "--porcelain")
    assert (await cleanup.sweep())[0]["reason"] == "七天闲置观察期未满"
    assert path.exists()


@pytest.mark.anyio
async def test_missing_execution_lease_owner_never_allows_automatic_removal(repository, monkeypatch):
    root, clock = repository
    key, path = await observe_new(root)
    connection = session_store._conn()
    connection.execute("UPDATE session_meta SET agent_id=NULL WHERE session_key=?", (key,))
    connection.commit()
    monkeypatch.setattr(cleanup, "_legacy_owner", lambda _: None)
    clock[0] += 30 * 86400
    assert "租约归属缺失" in (await cleanup.sweep())[0]["reason"]
    assert path.exists()


@pytest.mark.anyio
async def test_unregistered_directory_is_inventory_and_never_removed(repository):
    root, clock = repository
    assert await cleanup.sweep() == []
    path = root / "data/worktrees/unknown/not-a-git-worktree"
    path.mkdir(parents=True)
    (path / ".env.local").write_text("LOCAL_RESULT")
    clock[0] += 30 * 86400
    assert "失败" in (await cleanup.sweep())[0]["reason"]
    assert (path / ".env.local").read_text() == "LOCAL_RESULT"


@pytest.mark.anyio
async def test_submodules_shallow_history_and_operation_markers_preserve_work(repository):
    root, clock = repository
    _, path = await observe_new(root)
    clock[0] += 8 * 86400
    git_dir = cleanup._git_path(path, "--git-dir")
    (git_dir / "CHERRY_PICK_HEAD").write_text(git(path, "rev-parse", "HEAD") + "\n")
    assert "未结束" in (await cleanup.sweep())[0]["reason"]
    (git_dir / "CHERRY_PICK_HEAD").unlink()
    (root / ".git/shallow").write_text(git(root, "rev-parse", "HEAD") + "\n")
    assert "历史" in (await cleanup.sweep())[0]["reason"]
    (root / ".git/shallow").unlink()
    git(path, "update-index", "--add", "--cacheinfo", "160000", git(root, "rev-parse", "HEAD"), "module")
    (path / "module").mkdir()
    assert "子模块" in (await cleanup.sweep())[0]["reason"]
    assert path.exists()


@pytest.mark.anyio
async def test_running_script_memory_state_blocks_collection_without_loading_jobs(repository, monkeypatch):
    from vococo.cron import scheduler

    root, clock = repository
    key, path = await observe_new(root)
    monkeypatch.setattr(scheduler, "_script_running", {"test-job"})
    monkeypatch.setattr(scheduler, "_script_sessions", {"test-job": key})
    clock[0] += 8 * 86400
    assert "正在执行" in (await cleanup.sweep())[0]["reason"]
    assert path.exists()


@pytest.mark.anyio
async def test_daily_loop_integrates_development_and_writes_empty_receipt(repository, monkeypatch):
    from vococo.core import repository_cleanup

    async def empty():
        return []
    async def stop(delay):
        if delay == 86400:
            raise asyncio.CancelledError
    monkeypatch.setattr(repository_cleanup, "sweep", empty)
    monkeypatch.setattr(cleanup, "sweep", empty)
    monkeypatch.setattr(repository_cleanup.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await repository_cleanup.sweep_loop()
    report = json.loads((config.DATA_DIR / "repository-cleanup.json").read_text())
    assert report["results"] == []
