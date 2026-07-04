"""每会话独立 git worktree —— 借鉴 Claude Code。

病根:旧版同一项目下所有会话共用一个仓库目录,谁 `checkout -b` 都会把整个目录
(以及所有会话看到的分支)一起拽走,于是「分支名全一样」「互相抢分支」。

解法:给每个项目会话开一个物理独立的 git worktree(独立目录 + 独立分支)。同一仓库
可挂 N 个 worktree,各干各的,git 层面根本碰不到对方。worktree 放在 Hermes 自己的
data 目录下(不污染用户项目,免改 .gitignore),路径 data/worktrees/<项目哈希>/<会话slug>。
"""
from __future__ import annotations

import asyncio
import os
import re

from .. import config
from ..memory import session_store

_WT_BASE = config.DATA_DIR / "worktrees"


async def _git(cwd: str, *args: str) -> tuple[int, str, str]:
    """在 cwd 跑一条 git,返回 (returncode, stdout, stderr)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
    except (OSError, ValueError) as e:
        return 127, "", str(e)
    return (
        proc.returncode or 0,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


async def _is_git_repo(path: str) -> bool:
    code, out, _ = await _git(path, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out.strip() == "true"


def _slug(conv: str) -> str:
    """conv id → 安全的分支/目录名片段(只留字母数字和 .-_)。"""
    s = re.sub(r"[^A-Za-z0-9._-]", "-", conv).strip("-._")
    return s or "session"


async def ensure_worktree(session_key: str) -> str | None:
    """确保项目会话有独立 worktree;返回其路径,否则 None(回退项目根)。

    幂等:已绑定且目录仍在 → 直接返回。非项目会话 / 非 git 仓库 / 建失败 → None,
    不阻塞对话(这一轮就回落项目根,下一轮再试)。
    """
    root = config.project_root_for(session_key)
    if not root:  # 非项目会话(CLI/TG/main),秒退,零 DB 开销
        return None

    existing = session_store.get_worktree(session_key)
    if existing:
        if os.path.isdir(existing):
            return existing
        session_store.clear_worktree(session_key)  # 目录被手删 → 清绑定重来

    if not os.path.isdir(root) or not await _is_git_repo(root):
        return None

    slug = _slug(session_key.split(":")[-1])
    branch = f"hermes/{slug}"
    # 项目哈希取自 key 的第二段 p<hash>(project_root_for 已保证 key 是三段项目会话)
    phash = session_key.split(":")[1][1:]
    wt_dir = str(_WT_BASE / phash / slug)
    os.makedirs(os.path.dirname(wt_dir), exist_ok=True)

    # 分支可能已存在(会话删了又建同名) → 复用,不加 -b
    code, _, _ = await _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if code == 0:
        code, _, _err = await _git(root, "worktree", "add", wt_dir, branch)
    else:
        code, _, _err = await _git(root, "worktree", "add", wt_dir, "-b", branch)
    if code != 0:
        return None  # 磁盘/权限/冲突等 → 回退项目根,不炸对话

    session_store.set_worktree(session_key, wt_dir)
    return wt_dir


async def remove_worktree(session_key: str) -> None:
    """会话删除时清理 worktree(强制,连未提交改动一起删)。分支保留,避免误删工作成果。"""
    path = session_store.get_worktree(session_key)
    if not path:
        return
    root = config.project_root_for(session_key)
    session_store.clear_worktree(session_key)
    if root and os.path.isdir(root):
        await _git(root, "worktree", "remove", "--force", path)
