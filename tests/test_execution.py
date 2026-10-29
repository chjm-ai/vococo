"""执行空间与成果目录分离；所有 Git 操作只使用本地临时仓库。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vococo import config
from vococo.core import execution, tasks, worktree
from vococo.cron import scheduler
from vococo.memory import agents, projects, session_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def env(isolated, monkeypatch, tmp_path):
    from vococo.core import tasks as task_store

    monkeypatch.setattr(task_store, "_DB", None)
    monkeypatch.setattr(config, "CRON_JOBS_PATH", tmp_path / "cron_jobs.json")
    monkeypatch.setattr(config, "ROOT_DIR", tmp_path / "vococo")
    yield tmp_path
    if task_store._DB is not None:
        task_store._DB.close()
        task_store._DB = None


def _git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _repo(path):
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "-c", "user.name=测试", "-c", "user.email=test@example.invalid",
         "commit", "--allow-empty", "-qm", "初始化")
    return path


def _agent(path, mode=None):
    path.mkdir(parents=True, exist_ok=True)
    agent = agents.create("测试业务", str(path))
    if mode is not None:
        meta = agents._read_meta(agent["id"])
        meta["execution_mode"] = mode
        agents._write_meta(agent["id"], meta)
        agent = agents.get(agent["id"])
    return agent, agents.main_session_key(agent)


def test_mode_defaults_and_explicit_overrides(env):
    repo = _repo(env / "project")
    assert execution.execution_mode(str(repo)) == "project"
    assert execution.execution_mode(str(repo), "workspace") == "workspace"
    assert execution.execution_mode(str(env)) == "workspace"
    assert execution.execution_mode(str(env), "project") == "project"
    assert execution.execution_mode(str(env), "bad") == "workspace"
    agent, _ = _agent(repo, "workspace")
    assert agent["execution_mode"] == "workspace"
    assert agent["workdir"] == str(repo.resolve())


def test_icloud_git_and_symlink_default_to_workspace(env):
    repo = _repo(env / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "成果")
    alias = env / "云盘软链"
    alias.symlink_to(repo, target_is_directory=True)
    assert execution.execution_mode(str(repo)) == "workspace"
    assert execution.execution_mode(str(alias)) == "workspace"
    assert execution.execution_mode(str(alias), "project") == "project"


def test_policy_creates_only_local_workspace_and_tmp(env):
    output = env / "Mobile Documents" / "未下载成果"
    aid = "abcdef1234"
    agents._write_meta(aid, {"workdir": str(output), "execution_mode": "workspace"})
    projects.upsert_project(str(output))
    writable, readonly = env / "可写关联", env / "只读关联"
    agents.update(aid, links=[{"path": str(writable), "writable": True},
                              {"path": str(readonly), "writable": False}])
    key = agents.main_session_key(agents.get(aid))
    policy = execution.policy_for_session(key, cwd=str(config.ROOT_DIR))
    assert policy.agent_id == aid
    assert policy.workspace == (config.AGENT_WORKSPACES_DIR / aid).resolve()
    assert (policy.workspace / "tmp").is_dir()
    assert policy.output_dir == output.resolve()
    assert {policy.workspace, output, agents.home(aid), writable} <= set(policy.writable_roots)
    assert readonly not in policy.writable_roots
    assert not output.exists() and not writable.exists() and not readonly.exists()
    assert sorted(p.name for p in policy.workspace.iterdir()) == ["tmp"]
    assert config.resolve_execution_root(session_key=key) == str(output)


@pytest.mark.anyio
@pytest.mark.parametrize("owner_source", ["tasks", "cron", "cron_without_owner"])
async def test_business_task_ignores_old_vococo_cwd_and_worktree(env, owner_source):
    repo = _repo(config.ROOT_DIR)
    agent, key = _agent(env / "业务成果")
    task_id = "business-task"
    if owner_source.startswith("cron"):
        job = scheduler.create_job(
            name="任务", prompt="巡检", schedule={"kind": "cron", "expr": "0 9 * * *"},
            agent_id=agent["id"] if owner_source == "cron" else None,
        )
        task_id = job["id"]
    tasks.create("任务", "巡检", task_id=task_id, cwd=str(repo), agent_id=agent["id"])
    task_key = tasks.session_key(task_id)
    old = await worktree.ensure_worktree_for_task(str(repo), "old-development")
    session_store.set_worktree(task_key, old)
    session_store.set_worktree(key, old)
    before = _git(repo, "worktree", "list", "--porcelain")
    expected = str((config.AGENT_WORKSPACES_DIR / agent["id"]).resolve())
    assert config.resolve_execution_root(session_key=task_key) == agent["workdir"]
    assert await worktree.execution_cwd_for_task(str(repo), task_id) == expected
    assert await worktree.execution_cwd(key) == expected
    assert await worktree.ensure_worktree(task_key) is None
    assert await worktree.ensure_worktree_for_task(str(repo), task_id) is None
    assert session_store.get_worktree(task_key) == old
    assert session_store.get_worktree(key) == old and Path(old).is_dir()
    assert _git(repo, "worktree", "list", "--porcelain") == before
    result = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                            cwd=expected, capture_output=True)
    assert result.returncode != 0
    assert agents.owner_for_memory(task_key)["workdir"] == agent["workdir"]


@pytest.mark.anyio
async def test_explicit_workspace_for_git_agent_never_creates_worktree(env):
    repo = _repo(env / "nas-skill")
    agent, key = _agent(repo, "workspace")
    meta = agents._read_meta(agent["id"])
    meta["output_dir"] = str(env / "电影报告")
    agents._write_meta(agent["id"], meta)
    before = _git(repo, "worktree", "list", "--porcelain")
    cwd = await worktree.execution_cwd(key)
    assert cwd == str((config.AGENT_WORKSPACES_DIR / agent["id"]).resolve())
    assert await worktree.ensure_worktree(key) is None
    assert not (repo / "data").exists()
    assert _git(repo, "worktree", "list", "--porcelain") == before


@pytest.mark.anyio
async def test_development_agent_preserves_explicit_project_and_isolation(env):
    repo = _repo(env / "dev")
    agent, key = _agent(repo)
    assert agent["execution_mode"] == "project"
    assert execution.policy_for_session(key) is None
    cwd = await worktree.execution_cwd(key)
    assert cwd != str(repo) and Path(cwd, ".git").is_file()
    other = _repo(env / "explicit-project")
    assert config.resolve_execution_root(session_key=key, cwd=str(other)) == str(other)
    tasks.create("开发", "改代码", task_id="dev-task", agent_id=agent["id"])
    task_cwd = await worktree.execution_cwd_for_task(str(other), "dev-task")
    assert _git(task_cwd, "rev-parse", "--git-common-dir") == str(other / ".git")
    tasks.create("继承", "改代码", task_id="inherit-dev", agent_id=agent["id"])
    inherited = await worktree.execution_cwd_for_task(None, "inherit-dev")
    assert _git(inherited, "rev-parse", "--git-common-dir") == str(repo / ".git")


def test_task_keeps_memory_and_skills_owner(env):
    agent, key = _agent(env / "业务目录")
    agents.update(agent["id"], skills=["nas-movie-butler"], mcp=[])
    tasks.create("任务", "巡检", task_id="runtime", agent_id=agent["id"])
    before = agents.runtime_for_session(key)
    execution.policy_for_session(tasks.session_key("runtime"))
    assert agents.runtime_for_session(tasks.session_key("runtime")) == before
    assert before["skills"] == ["nas-movie-butler"]
    assert Path(before["memory_dir"]).name == "业务目录"
    assert agents.get(agent["id"])["project_hash"] == agent["project_hash"]


def test_workspace_keeps_original_git_skill_profile(env, monkeypatch):
    from vococo.gateway import settings_store

    agent, key = _agent(_repo(env / "nas-skill-profile"), "workspace")
    meta = agents._read_meta(agent["id"])
    meta["output_dir"] = str(env / "电影报告")
    agents._write_meta(agent["id"], meta)
    monkeypatch.setattr(settings_store, "_load", lambda: {
        "skill_profiles": {"coding": ["nas-movie-butler"]},
        "skills_mode": "custom", "skills_enabled": ["pdf"],
    })
    assert agents.runtime_for_session(key)["skills"] == ["nas-movie-butler"]
    tasks.create("任务", "巡检", task_id="profile", agent_id=agent["id"])
    execution.policy_for_session(tasks.session_key("profile"))
    assert agents.runtime_for_session(tasks.session_key("profile"))["skills"] == ["nas-movie-butler"]
    agents.update(agent["id"], skills=["xlsx"])
    assert agents.runtime_for_session(tasks.session_key("profile"))["skills"] == ["xlsx"]


def test_mode_falls_back_to_workspace_when_git_cannot_run(env, monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 2)

    monkeypatch.setattr(execution.subprocess, "run", fail)
    assert execution.execution_mode(str(env)) == "workspace"
    assert execution.execution_mode(str(env), "project") == "project"


def test_general_and_unknown_sessions_keep_existing_behavior(env):
    assert execution.policy_for_session(config.SESSION_KEY) is None
    assert execution.policy_for_session("web:unknown") is None
    assert execution.policy_for_session(None) is None
    assert config.resolve_execution_root(session_key=config.SESSION_KEY) == str(config.ROOT_DIR)
    assert not config.AGENT_WORKSPACES_DIR.exists()
