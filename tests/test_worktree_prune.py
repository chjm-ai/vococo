"""core/worktree.prune_orphans 的启动回收逻辑。

2026-08-04 增强:DB 还绑着但会话已不活跃的 worktree 也回收(merge-main.sh 收尾后
worktree 没存在意义,不该等「删会话」才清);回收前归档未提交改动、分支有独有
提交一律保留;活跃列表解析失败时保守跳过全部绑定的。
"""
from __future__ import annotations

import json
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
    """建仓库 + worktree 基座,返回 (repo, wt_base, phash)。"""
    from vococo import config

    repo = tmp_path / "proj"
    _init_repo(repo)
    projects.upsert_project(str(repo))
    phash = projects.project_hash(str(repo))

    wt_base = tmp_path / "wt-base"
    monkeypatch.setattr(worktree, "_WT_BASE", wt_base)
    return repo, wt_base, phash, config


@pytest.mark.anyio
async def test_prune_removes_unbound_orphan(isolated, monkeypatch, tmp_path):
    """没绑定的孤儿 worktree:直接回收。"""
    repo, wt_base, phash, _ = _setup(isolated, monkeypatch, tmp_path)
    wt = _add_worktree(repo, wt_base, phash, "s1", "vococo/s1")
    assert wt.is_dir()

    n = await worktree.prune_orphans()
    assert n == 1
    assert not wt.exists()
    # 空分支一并删掉
    out = subprocess.run(["git", "branch"], cwd=repo, capture_output=True, text=True).stdout
    assert "vococo/s1" not in out


@pytest.mark.anyio
async def test_prune_keeps_active_bound(isolated, monkeypatch, tmp_path):
    """DB 绑定 + 活跃列表里 → 保留。"""
    repo, wt_base, phash, config = _setup(isolated, monkeypatch, tmp_path)
    wt = _add_worktree(repo, wt_base, phash, "s1", "vococo/s1")
    key = f"web:p{phash}:s1"
    session_store.set_worktree(key, str(wt))
    (config.DATA_DIR / "active_sessions.json").write_text(json.dumps([key]))

    n = await worktree.prune_orphans()
    assert n == 0
    assert wt.is_dir()


@pytest.mark.anyio
async def test_prune_recycles_inactive_bound_clean(isolated, monkeypatch, tmp_path):
    """DB 绑定但会话不活跃 + worktree 干净 → 回收(核心增强点)。"""
    repo, wt_base, phash, config = _setup(isolated, monkeypatch, tmp_path)
    wt = _add_worktree(repo, wt_base, phash, "s1", "vococo/s1")
    key = f"web:p{phash}:s1"
    session_store.set_worktree(key, str(wt))
    (config.DATA_DIR / "active_sessions.json").write_text(json.dumps(["web:pother:xxx"]))

    n = await worktree.prune_orphans()
    assert n == 1
    assert not wt.exists()


@pytest.mark.anyio
async def test_prune_recycles_inactive_dirty_with_archive(isolated, monkeypatch, tmp_path):
    """不活跃 + 有未提交改动 → 先归档 diff 再回收。"""
    repo, wt_base, phash, config = _setup(isolated, monkeypatch, tmp_path)
    wt = _add_worktree(repo, wt_base, phash, "s1", "vococo/s1")
    key = f"web:p{phash}:s1"
    session_store.set_worktree(key, str(wt))
    (wt / "README.md").write_text("改过但没提交")
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=wt, capture_output=True
    ).stdout.strip()

    n = await worktree.prune_orphans()
    assert n == 1
    assert not wt.exists()
    archive = config.DATA_DIR / "worktree-archive" / "vococo_s1.diff"
    assert archive.exists()
    assert "README.md" in archive.read_text(encoding="utf-8")


@pytest.mark.anyio
async def test_prune_keeps_bound_when_active_json_corrupt(isolated, monkeypatch, tmp_path):
    """active_sessions.json 解析失败 → 保守:绑定的 worktree 全部保留。"""
    repo, wt_base, phash, config = _setup(isolated, monkeypatch, tmp_path)
    wt = _add_worktree(repo, wt_base, phash, "s1", "vococo/s1")
    session_store.set_worktree(f"web:p{phash}:s1", str(wt))
    (config.DATA_DIR / "active_sessions.json").write_text("{ 坏 json !!")

    n = await worktree.prune_orphans()
    assert n == 0
    assert wt.is_dir()


@pytest.mark.anyio
async def test_prune_keeps_branch_with_commits(isolated, monkeypatch, tmp_path):
    """分支有独立提交 → worktree 回收但分支保留(不丢成果)。"""
    repo, wt_base, phash, config = _setup(isolated, monkeypatch, tmp_path)
    wt = _add_worktree(repo, wt_base, phash, "s1", "vococo/s1")
    (wt / "work.txt").write_text("有价值的未合并工作")
    subprocess.run(["git", "add", "."], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "work"], cwd=wt, check=True)
    session_store.set_worktree(f"web:p{phash}:s1", str(wt))
    (config.DATA_DIR / "active_sessions.json").write_text(json.dumps([]))

    n = await worktree.prune_orphans()
    assert n == 1
    assert not wt.exists()
    out = subprocess.run(["git", "branch"], cwd=repo, capture_output=True, text=True).stdout
    assert "vococo/s1" in out  # 分支还在,内容可找回
