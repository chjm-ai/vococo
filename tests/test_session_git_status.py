"""多仓库汇总只读取本会话副本，不读取或创建主检出。"""
import subprocess
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.core import execution
from vococo.gateway import git_status
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import agents, session_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def repository(path):
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "测试")
    git(path, "config", "user.email", "test@example.invalid")
    (path / "file.txt").write_text("original\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "初始内容")
    return path


def owner_with_repositories(root):
    sources = [repository(root / name) for name in ("first", "second", "third")]
    owner = agents.create("业务")
    agents.update(owner["id"], repositories=[str(path) for path in sources])
    return owner, sources, agents.main_session_key(owner)


@pytest.mark.anyio
async def test_totals_include_all_current_session_copies_not_source_or_other_session(isolated):
    owner, sources, key = owner_with_repositories(isolated)
    policy = execution.policy_for_session(key)
    await execution.prepare_repositories(policy)
    first, second, _ = [row.checkout for row in policy.repositories]
    (first / "file.txt").write_text("changed\nextra\n")
    (second / "new").mkdir()
    for name in ("a.txt", "b.txt"):
        (second / "new" / name).write_text("new\n")
    git(second, "-c", "user.name=测试", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "独立提交")
    (sources[2] / "source-only.txt").write_text("不能计入\n")
    other_key = config.new_session_key()
    session_store.bind_session(other_key, agent_id=owner["id"])
    other = execution.policy_for_session(other_key)
    await execution.prepare_repositories(other)
    (other.repositories[2].checkout / "other-session.txt").write_text("不能计入\n")

    result = await git_status.session_repository_status(key)
    assert result["repo_count"] == 3
    assert result["changed_repos"] == 2
    assert result["dirty"] == 3  # 未跟踪目录按文件计数，而不是算作一个目录。
    assert (result["added"], result["removed"], result["unmerged"]) == (2, 1, 1)
    assert result["unavailable"] == 0
    assert [row["dirty"] for row in result["repositories"]] == [1, 2, 0]


@pytest.mark.anyio
async def test_missing_copies_do_not_initialize_or_fall_back_to_source(isolated):
    _, sources, key = owner_with_repositories(isolated)
    (sources[0] / "dirty.txt").write_text("主目录的变化\n")
    result = await git_status.session_repository_status(key)
    assert result["repo_count"] == result["unavailable"] == 3
    assert result["dirty"] == result["changed_repos"] == 0
    assert all(row["state"] == "uninitialized" for row in result["repositories"])
    assert not (config.AGENT_WORKSPACES_DIR / ".repositories").exists()


@pytest.mark.anyio
async def test_failed_repository_does_not_hide_healthy_one(isolated):
    _, _, key = owner_with_repositories(isolated)
    policy = execution.policy_for_session(key)
    await execution.prepare_repositories(policy)
    first = policy.repositories[0].checkout
    index = Path(git(first, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    index.write_text("损坏的索引\n")
    (policy.repositories[1].checkout / "file.txt").write_text("healthy\n")
    result = await git_status.session_repository_status(key)
    assert result["repositories"][0]["state"] == "error"
    assert result["unavailable"] == result["dirty"] == result["changed_repos"] == 1


@pytest.mark.anyio
async def test_project_worktree_and_bound_copies_are_deduplicated(isolated):
    source, extra = repository(isolated / "project"), repository(isolated / "extra")
    owner = agents.create("开发", str(source))
    agents.update(owner["id"], repositories=[str(source), str(extra)])
    key = agents.main_session_key(owner)
    worktree = isolated / "session-worktree"
    git(source, "worktree", "add", "-b", "session", str(worktree))
    session_store.set_worktree(key, str(worktree))
    (worktree / "file.txt").write_text("only-session\n")
    (source / "unrelated.txt").write_text("不能计入\n")
    result = await git_status.session_repository_status(key)
    assert result["repo_count"] == 2
    assert result["dirty"] == result["unavailable"] == 1
    assert result["repositories"][0]["path"] == str(worktree)
    session_store.set_worktree(key, "")
    result = await git_status.session_repository_status(key)
    assert result["dirty"] == 0
    assert result["unavailable"] == 2


@pytest.mark.anyio
async def test_agent_without_repositories_hides_git_and_legacy_session_falls_back(isolated):
    owner = agents.create("没有仓库")
    assert await git_status.session_repository_status(agents.main_session_key(owner)) == {"is_project": False}
    assert await git_status.session_repository_status("web:legacy-chat") is None


@pytest.mark.anyio
async def test_invalid_copy_path_does_not_hide_other_repositories(isolated):
    _, sources, key = owner_with_repositories(isolated)
    policy = execution.policy_for_session(key)
    await execution.prepare_repositories(policy)
    first = policy.repositories[0].checkout
    first.rename(first.with_name("saved-copy"))
    first.symlink_to(sources[0], target_is_directory=True)
    (policy.repositories[1].checkout / "file.txt").write_text("healthy\n")
    result = await git_status.session_repository_status(key)
    assert result["repo_count"] == 3
    assert result["repositories"][0]["state"] == "error"
    assert result["unavailable"] == result["dirty"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize("command", ["rev-list", "diff"])
async def test_partial_git_failure_is_not_reported_as_zero(isolated, monkeypatch, command):
    _, _, key = owner_with_repositories(isolated)
    policy = execution.policy_for_session(key)
    await execution.prepare_repositories(policy)
    original = git_status.run_git
    failed_path = str(policy.repositories[0].checkout)

    async def fail_one(cwd, *args):
        if cwd == failed_path and args[0] == command:
            return 124, "", "超时"
        return await original(cwd, *args)

    monkeypatch.setattr(git_status, "run_git", fail_one)
    result = await git_status.session_repository_status(key)
    assert result["repositories"][0]["state"] == "error"
    assert result["unavailable"] == 1
    assert result["repositories"][1]["state"] == "ready"


@pytest.mark.anyio
async def test_new_repository_without_commits_can_show_staged_lines(isolated):
    path = isolated / "new-repo"
    path.mkdir()
    git(path, "init", "-b", "main")
    (path / "first.txt").write_text("first\nsecond\n")
    git(path, "add", ".")
    result = await git_status.git_status(str(path))
    assert not result.get("error")
    assert result["dirty"] == 1
    assert result["added"] == 2
    assert result["unmerged"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("writable", [True, False])
async def test_linked_git_directory_is_shown_without_repository_binding(isolated, monkeypatch, writable):
    source = repository(isolated / "linked-repo")
    (source / "file.txt").write_text("linked change\n")
    documents = isolated / "documents"
    documents.mkdir()
    owner = agents.create("业务")
    agents.update(owner["id"], links=[{"path": str(source), "writable": writable},
                                     {"path": str(documents), "writable": False}])
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    app = web.Application()
    app.add_routes([web.get("/conv/git", WebAdapter()._handle_conv_git)])
    before = git(source, "status", "--porcelain=v1")
    index_stamp = (source / ".git" / "index").stat().st_mtime_ns
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/conv/git", params={"conv": owner["main_conv"]})
        assert response.status == 200
        result = await response.json()
    assert result["is_project"] is True
    assert result["repo_count"] == result["dirty"] == 1
    row, = result["repositories"]
    assert row["scope"] == "shared"
    assert row["path"] == row["project_path"] == str(source)
    assert row["state"] == "ready"
    assert (source / ".git" / "index").stat().st_mtime_ns == index_stamp
    assert git(source, "status", "--porcelain=v1") == before
    assert not (config.AGENT_WORKSPACES_DIR / ".repositories").exists()


@pytest.mark.anyio
async def test_linked_subdirectory_and_alias_are_deduplicated(isolated):
    source = repository(isolated / "source")
    nested = source / "nested"
    nested.mkdir()
    alias = isolated / "alias"
    alias.symlink_to(source, target_is_directory=True)
    owner = agents.create("业务")
    agents.update(owner["id"], links=[{"path": str(path), "writable": True}
                                     for path in (source, nested, alias)])
    result = await git_status.session_repository_status(agents.main_session_key(owner))
    assert result["repo_count"] == 1
    assert result["repositories"][0]["path"] == str(source)


@pytest.mark.anyio
async def test_bound_session_copy_takes_priority_over_linked_source(isolated):
    owner, sources, key = owner_with_repositories(isolated)
    agents.update(owner["id"], links=[{"path": str(sources[0]), "writable": True}])
    policy = execution.policy_for_session(key)
    await execution.prepare_repositories(policy)
    (sources[0] / "source-only.txt").write_text("不混入会话副本\n")
    linked = repository(isolated / "extra-linked")
    (linked / "file.txt").write_text("shared\n")
    agents.update(owner["id"], links=[{"path": str(path), "writable": True}
                                     for path in (sources[0], linked)])
    result = await git_status.session_repository_status(key)
    assert result["repo_count"] == 2  # 关联列表是完整配置，取消的旧仓库不会复活。
    assert result["dirty"] == 1
    assert result["repositories"][0]["scope"] == "session"
    assert result["repositories"][-1]["scope"] == "shared"


@pytest.mark.anyio
async def test_broken_linked_repository_does_not_hide_healthy_link(isolated):
    source, healthy = repository(isolated / "broken"), repository(isolated / "healthy")
    owner = agents.create("业务")
    agents.update(owner["id"], links=[{"path": str(path), "writable": True}
                                     for path in (source, healthy)])
    (source / ".git" / "index").write_text("损坏索引\n")
    (healthy / "file.txt").write_text("healthy\n")
    result = await git_status.session_repository_status(agents.main_session_key(owner))
    assert result["repo_count"] == 2
    assert result["unavailable"] == result["dirty"] == 1
    assert result["repositories"][0]["scope"] == "shared"
    assert result["repositories"][0]["state"] == "error"


@pytest.mark.anyio
async def test_conv_git_endpoint_returns_multi_repo_summary(isolated, monkeypatch):
    owner, _, _ = owner_with_repositories(isolated)
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    app = web.Application()
    app.add_routes([web.get("/conv/git", WebAdapter()._handle_conv_git)])
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/conv/git", params={"conv": owner["main_conv"]})
        assert response.status == 200
        data = await response.json()
    assert data["repo_count"] == 3
    assert data["unavailable"] == 3
    assert data["name"] == "业务"
