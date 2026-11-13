"""core/worktree 的 worktree 生命周期测试。

回收依据只有用户意图(2026-08-04 定案):
- 归档会话 → recycle_empty_worktree(空壳才回收,有内容不动)
- 删除会话 → remove_worktree(无条件回收)
- 启动兜底 prune_orphans → 只运行 git worktree prune 清理失效登记，不删除目录
  或分支；有无 DB 绑定都保留，实际目录回收交给安全巡检。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vococo.core import worktree
from vococo.memory import projects, session_store


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def _add_worktree(root: Path, wt_base: Path, phash: str, slug: str, branch: str) -> Path:
    """在 _WT_BASE/<phash>/<slug> 建 worktree 绑到新分支,返回目录。"""
    wt = wt_base / phash / slug
    wt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", "-b", branch, str(wt)], cwd=root, check=True)
    return wt


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _setup(isolated, monkeypatch, tmp_path):
    """建仓库 + worktree 基座,返回 (repo, wt_base, phash, config)。"""
    from vococo import config

    repo = tmp_path / "proj"
    _init_repo(repo)
    projects.upsert_project(str(repo))
    phash = projects.project_hash(str(repo))

    wt_base = tmp_path / "wt-base"
    # _WT_BASE 常量已改为 _wt_base(root) 函数:统一指向 tmp 基座,隔离真实目录
    monkeypatch.setattr(worktree, "_wt_base", lambda root: wt_base)
    return repo, wt_base, phash, config


# ── ensure_worktree:草稿会话(local-)也自动建 worktree ─────────────────────


@pytest.mark.anyio
async def test_ensure_worktree_for_local_draft_key(isolated, monkeypatch, tmp_path):
    """前端新建会话 key 是 web:local-p<hash>:<slug>,同样要自动建 worktree(2026-08-04 修复)。"""
    repo, wt_base, phash, _ = _setup(isolated, monkeypatch, tmp_path)
    key = f"web:local-p{phash}:newsess"

    wt_dir = await worktree.ensure_worktree(key)
    assert wt_dir is not None
    assert Path(wt_dir).is_dir()
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=wt_dir, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branch == "vococo/newsess"


# ── worktree_dirty_summary:归档/删除前的未提交内容摘要 ────────────────────


@pytest.mark.anyio
async def test_dirty_summary_clean(isolated, monkeypatch, tmp_path):
    """干净 worktree → None(无提示)。"""
    repo, wt_base, phash, _ = _setup(isolated, monkeypatch, tmp_path)
    wt = _add_worktree(repo, wt_base, phash, "s1", "vococo/s1")
    session_store.set_worktree(f"web:p{phash}:s1", str(wt))

    assert await worktree.worktree_dirty_summary(f"web:p{phash}:s1") is None


@pytest.mark.anyio
async def test_dirty_summary_reports_uncommitted(isolated, monkeypatch, tmp_path):
    """有未提交改动 + 未跟踪 + 独立提交 → 摘要齐全。"""
    repo, wt_base, phash, _ = _setup(isolated, monkeypatch, tmp_path)
    wt = _add_worktree(repo, wt_base, phash, "s1", "vococo/s1")
    (wt / "README.md").write_text("改了没提交")
    (wt / "newfile.txt").write_text("未跟踪")
    (wt / "work.txt").write_text("已提交")
    subprocess.run(["git", "add", "work.txt"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "work"], cwd=wt, check=True)
    session_store.set_worktree(f"web:p{phash}:s1", str(wt))

    d = await worktree.worktree_dirty_summary(f"web:p{phash}:s1")
    assert d is not None
    assert d["uncommitted"] >= 1  # README.md
    assert d["untracked"] >= 1    # newfile.txt
    assert d["commits"] == 1      # work.txt 的提交


# ── prune_orphans:只清失效登记，不回收目录 ─────────────────────────────────


@pytest.mark.anyio
async def test_prune_keeps_unbound_orphan_and_only_prunes_registration(isolated, monkeypatch, tmp_path):
    """未绑定且干净的目录也保留；启动仅运行 git worktree prune。"""
    repo, wt_base, phash, _ = _setup(isolated, monkeypatch, tmp_path)
    wt = _add_worktree(repo, wt_base, phash, "s1", "vococo/s1")
    assert wt.is_dir()
    calls = []
    original_git = worktree._git

    async def record_git(root, *args):
        calls.append((root, args))
        return await original_git(root, *args)

    monkeypatch.setattr(worktree, "_git", record_git)
    n = await worktree.prune_orphans()
    assert n == 0
    assert wt.is_dir()
    assert (wt / "README.md").read_text() == "hi"
    assert (str(repo), ("worktree", "prune")) in calls
    assert all(args == ("worktree", "prune") for _, args in calls)
    out = subprocess.run(["git", "branch"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "vococo/s1" in out  # 启动不擅自删空分支。
    registered = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert f"worktree {wt}" in registered


@pytest.mark.anyio
async def test_prune_keeps_bound_even_inactive(isolated, monkeypatch, tmp_path):
    """绑定的 worktree 一律保留——不管活跃与否、干净与否(核心原则)。"""
    repo, wt_base, phash, _ = _setup(isolated, monkeypatch, tmp_path)
    # ① 干净且不活跃
    wt1 = _add_worktree(repo, wt_base, phash, "s1", "vococo/s1")
    session_store.set_worktree("web:pXXX:s1", str(wt1))
    # ② 脏且不活跃(有未提交改动)
    wt2 = _add_worktree(repo, wt_base, phash, "s2", "vococo/s2")
    (wt2 / "README.md").write_text("改了没提交")
    session_store.set_worktree("web:pXXX:s2", str(wt2))

    n = await worktree.prune_orphans()
    assert n == 0
    assert wt1.is_dir()
    assert wt2.is_dir()


@pytest.mark.anyio
async def test_prune_keeps_bound_with_commits(isolated, monkeypatch, tmp_path):
    """绑定的 worktree,分支有独立提交 → 保留。"""
    repo, wt_base, phash, _ = _setup(isolated, monkeypatch, tmp_path)
    wt = _add_worktree(repo, wt_base, phash, "s1", "vococo/s1")
    (wt / "work.txt").write_text("有价值的未合并工作")
    subprocess.run(["git", "add", "."], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "work"], cwd=wt, check=True)
    session_store.set_worktree("web:pXXX:s1", str(wt))

    n = await worktree.prune_orphans()
    assert n == 0
    assert wt.is_dir()


# ── recycle_empty_worktree:归档时空壳回收 ──────────────────────────────────


@pytest.mark.anyio
async def test_recycle_empty_worktree_on_archive(isolated, monkeypatch, tmp_path):
    """归档不直接回收开发副本，目录、分支和绑定留给每日安全巡检。"""
    repo, wt_base, phash, _ = _setup(isolated, monkeypatch, tmp_path)
    wt = _add_worktree(repo, wt_base, phash, "s1", "vococo/s1")
    session_store.set_worktree(f"web:p{phash}:s1", str(wt))
    assert wt.is_dir()

    recycled = await worktree.recycle_empty_worktree(f"web:p{phash}:s1")
    assert recycled is False
    assert wt.is_dir()
    assert session_store.get_worktree(f"web:p{phash}:s1") == str(wt)
    out = subprocess.run(["git", "branch"], cwd=repo, capture_output=True, text=True).stdout
    assert "vococo/s1" in out


@pytest.mark.anyio
async def test_archive_keeps_ignored_files_and_active_lease(isolated, monkeypatch, tmp_path):
    from vococo.core.repository_locks import lease, session_digest

    repo, wt_base, phash, _ = _setup(isolated, monkeypatch, tmp_path)
    wt = _add_worktree(repo, wt_base, phash, "s1", "vococo/s1")
    key = f"web:p{phash}:s1"
    session_store.set_worktree(key, str(wt))
    exclude = repo / ".git" / "info" / "exclude"
    with exclude.open("a") as handle:
        handle.write("\n.env\n")
    (wt / ".env").write_text("LOCAL_TEST_VALUE=preserve")
    with lease("general", session_digest(key)):
        assert not await worktree.recycle_empty_worktree(key)
    assert (wt / ".env").read_text() == "LOCAL_TEST_VALUE=preserve"
    assert session_store.get_worktree(key) == str(wt)


@pytest.mark.anyio
async def test_recycle_keeps_worktree_with_commits(isolated, monkeypatch, tmp_path):
    """归档场景:分支有独立提交(干过活) → 不动,留给合并流程。"""
    repo, wt_base, phash, _ = _setup(isolated, monkeypatch, tmp_path)
    wt = _add_worktree(repo, wt_base, phash, "s1", "vococo/s1")
    (wt / "work.txt").write_text("有价值的工作")
    subprocess.run(["git", "add", "."], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "work"], cwd=wt, check=True)
    session_store.set_worktree(f"web:p{phash}:s1", str(wt))

    recycled = await worktree.recycle_empty_worktree(f"web:p{phash}:s1")
    assert recycled is False
    assert wt.is_dir()
    assert session_store.get_worktree(f"web:p{phash}:s1") == str(wt)


@pytest.mark.anyio
async def test_recycle_keeps_worktree_dirty(isolated, monkeypatch, tmp_path):
    """归档场景:有未提交改动 → 不动,内容不能丢。"""
    repo, wt_base, phash, _ = _setup(isolated, monkeypatch, tmp_path)
    wt = _add_worktree(repo, wt_base, phash, "s1", "vococo/s1")
    (wt / "README.md").write_text("改了没提交")
    session_store.set_worktree(f"web:p{phash}:s1", str(wt))

    recycled = await worktree.recycle_empty_worktree(f"web:p{phash}:s1")
    assert recycled is False
    assert wt.is_dir()


@pytest.mark.anyio
async def test_recycle_keeps_worktree_untracked(isolated, monkeypatch, tmp_path):
    """归档场景:只有未跟踪文件也要保留——新代码没 add 前就是未跟踪状态,不能当
    临时产物无声丢掉;且 dirty_summary 会把它算进 dirty,回收判定必须同一语义。"""
    repo, wt_base, phash, _ = _setup(isolated, monkeypatch, tmp_path)
    wt = _add_worktree(repo, wt_base, phash, "s1", "vococo/s1")
    (wt / "newfile.txt").write_text("新写的还没 add")
    session_store.set_worktree(f"web:p{phash}:s1", str(wt))

    recycled = await worktree.recycle_empty_worktree(f"web:p{phash}:s1")
    assert recycled is False
    assert wt.is_dir()
    assert (wt / "newfile.txt").exists()
