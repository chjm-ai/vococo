"""每会话独立 git worktree 的绑定表(借鉴 Claude Code)。

会话↔工作目录的绑定落库,而非实时靠「当前分支」猜 —— 这样每个会话读自己的
worktree、各在各的分支,物理隔离,不再互相抢分支。实际的 worktree 创建/清理在
core/worktree.py;这里只管 session_meta.worktree_path 这一列的读写。2026-07-23
从 session_store.py 拆出(见 images.py 顶部说明)。
"""
from __future__ import annotations

from . import _db


def get_worktree(session_key: str) -> str | None:
    """该会话独占的 worktree 目录;没有则 None(回退项目根)。"""
    row = _db.conn().execute(
        "SELECT worktree_path FROM session_meta WHERE session_key=?", (session_key,)
    ).fetchone()
    return (row[0] if row else None) or None


def all_worktree_paths() -> list[str]:
    """DB 里当前所有会话绑定的 worktree 目录 —— 启动清孤儿时的「活会话」白名单。"""
    rows = _db.conn().execute(
        "SELECT worktree_path FROM session_meta "
        "WHERE worktree_path IS NOT NULL AND worktree_path != ''"
    ).fetchall()
    return [r[0] for r in rows]


def set_worktree(session_key: str, path: str) -> None:
    """记住某会话的 worktree 目录(upsert,不动其余字段)。"""
    c = _db.conn()
    c.execute(
        "INSERT INTO session_meta(session_key, watermark_id, worktree_path) VALUES (?,0,?) "
        "ON CONFLICT(session_key) DO UPDATE SET worktree_path=excluded.worktree_path",
        (session_key, path),
    )
    c.commit()


def clear_worktree(session_key: str) -> None:
    """解除会话与 worktree 的绑定(目录被清理后调用)。"""
    c = _db.conn()
    c.execute(
        "UPDATE session_meta SET worktree_path=NULL WHERE session_key=?", (session_key,)
    )
    c.commit()
