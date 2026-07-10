"""语音后台任务的代码隔离:core/worktree.ensure_worktree_for_task。

背景:语音前台会话现在代码层禁掉了 Edit/Write(见 voice/session.py),真要改代码
只能走 voice_dispatch_task 派后台任务;这里验证该任务一旦拿到 git 仓库的 cwd,
就会自动开一个独立 worktree + `hermes/<task_id>` 分支,而不是直接在原目录/原
分支上改——跟 Web/CLI「一会话一分支」看齐。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_ensure_worktree_for_task_creates_isolated_branch(isolated, monkeypatch, tmp_path):
    from claude_hermes.core import worktree
    from claude_hermes.memory import session_store

    monkeypatch.setattr(worktree, "_WT_BASE", tmp_path / "wt-base")

    repo = tmp_path / "proj"
    _init_repo(repo)

    wt_dir = await worktree.ensure_worktree_for_task(str(repo), "task123")
    assert wt_dir is not None
    assert Path(wt_dir).is_dir()
    assert Path(wt_dir) != repo  # 物理隔离,不是原目录

    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=wt_dir, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branch == "hermes/task123"

    # 原仓库分支不受影响(还在 main/master,任务没在原地改)
    root_branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert root_branch != "hermes/task123"

    # 落库绑定 + 幂等复用
    assert session_store.get_worktree("voice-task:task123") == wt_dir
    wt_dir2 = await worktree.ensure_worktree_for_task(str(repo), "task123")
    assert wt_dir2 == wt_dir


@pytest.mark.anyio
async def test_ensure_worktree_for_task_none_for_non_git_dir(isolated, monkeypatch, tmp_path):
    from claude_hermes.core import worktree

    monkeypatch.setattr(worktree, "_WT_BASE", tmp_path / "wt-base")
    plain = tmp_path / "plain"
    plain.mkdir()

    assert await worktree.ensure_worktree_for_task(str(plain), "taskX") is None


@pytest.mark.anyio
async def test_ensure_worktree_for_task_none_without_root(isolated):
    from claude_hermes.core import worktree

    assert await worktree.ensure_worktree_for_task(None, "taskY") is None
    assert await worktree.ensure_worktree_for_task("", "taskY") is None


@pytest.mark.anyio
async def test_two_tasks_same_repo_get_distinct_branches(isolated, monkeypatch, tmp_path):
    from claude_hermes.core import worktree

    monkeypatch.setattr(worktree, "_WT_BASE", tmp_path / "wt-base")
    repo = tmp_path / "proj"
    _init_repo(repo)

    wt_a = await worktree.ensure_worktree_for_task(str(repo), "task-a")
    wt_b = await worktree.ensure_worktree_for_task(str(repo), "task-b")
    assert wt_a != wt_b
