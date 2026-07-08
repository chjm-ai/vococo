"""P1 任务板:任务表 CRUD + 状态机(data/voice/voice.db 的 tasks 表)。

独立连接、独立表,不碰 session.py 的 turns/meta 表,同一 db 文件下互不干扰
(见 00-overview.md §2.4 的隔离约束)。

状态机:queued → running → {done, failed, cancelled};running → cancelled。
不允许的迁移(如 done → running)一律拒绝,由 set_status() 返回 False 体现。
"""
from __future__ import annotations

import secrets
import sqlite3
import time
from pathlib import Path

from .. import config

_DB: sqlite3.Connection | None = None

TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"done", "failed", "cancelled"}),
    "done": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


def _db_path() -> Path:
    return config.DATA_DIR / "voice" / "voice.db"


def _conn() -> sqlite3.Connection:
    global _DB
    if _DB is None:
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _DB = sqlite3.connect(path, check_same_thread=False)
        _DB.row_factory = sqlite3.Row
        _DB.execute(
            "CREATE TABLE IF NOT EXISTS tasks("
            "id TEXT PRIMARY KEY,"
            "title TEXT NOT NULL,"
            "prompt TEXT NOT NULL,"
            "cwd TEXT,"
            "status TEXT NOT NULL,"
            "progress_note TEXT NOT NULL DEFAULT '',"
            "result_summary TEXT NOT NULL DEFAULT '',"
            "result_full TEXT NOT NULL DEFAULT '',"
            "created_at REAL NOT NULL,"
            "updated_at REAL NOT NULL)"
        )
        _DB.commit()
    return _DB


def _row(r: sqlite3.Row) -> dict:
    return dict(r)


def create(title: str, prompt: str, cwd: str | None = None) -> dict:
    """落库一条 queued 任务,返回完整行。id 是 8 位短随机串(碰撞概率可忽略)。"""
    c = _conn()
    task_id = secrets.token_hex(4)
    now = time.time()
    c.execute(
        "INSERT INTO tasks(id,title,prompt,cwd,status,progress_note,result_summary,"
        "result_full,created_at,updated_at) VALUES (?,?,?,?,'queued','','','',?,?)",
        (task_id, title, prompt, cwd, now, now),
    )
    c.commit()
    return get(task_id)


def get(task_id: str) -> dict | None:
    row = _conn().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _row(row) if row else None


def get_latest() -> dict | None:
    row = _conn().execute(
        "SELECT * FROM tasks ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return _row(row) if row else None


def list_recent(limit: int = 20) -> list[dict]:
    rows = _conn().execute(
        "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row(r) for r in rows]


def list_queued() -> list[dict]:
    rows = _conn().execute(
        "SELECT * FROM tasks WHERE status='queued' ORDER BY created_at ASC"
    ).fetchall()
    return [_row(r) for r in rows]


def count_running() -> int:
    row = _conn().execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE status='running'"
    ).fetchone()
    return int(row["n"])


def set_status(task_id: str, status: str, progress_note: str | None = None) -> bool:
    """按状态机校验后迁移;非法迁移(如终态再迁移)返回 False,不改库。"""
    cur = get(task_id)
    if cur is None:
        return False
    if status not in _ALLOWED_TRANSITIONS.get(cur["status"], frozenset()):
        return False
    c = _conn()
    if progress_note is None:
        c.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
            (status, time.time(), task_id),
        )
    else:
        c.execute(
            "UPDATE tasks SET status=?, progress_note=?, updated_at=? WHERE id=?",
            (status, progress_note, time.time(), task_id),
        )
    c.commit()
    return True


def set_progress(task_id: str, note: str) -> None:
    """只更新 progress_note(不涉及状态迁移,不做状态机校验)。"""
    c = _conn()
    c.execute(
        "UPDATE tasks SET progress_note=?, updated_at=? WHERE id=?",
        (note, time.time(), task_id),
    )
    c.commit()


def finish(task_id: str, status: str, result_full: str, result_summary: str) -> bool:
    """写终态 + 完整结果 + 摘要。status 必须是 done/failed/cancelled 之一。"""
    if status not in TERMINAL_STATUSES:
        return False
    cur = get(task_id)
    if cur is None or status not in _ALLOWED_TRANSITIONS.get(cur["status"], frozenset()):
        return False
    c = _conn()
    c.execute(
        "UPDATE tasks SET status=?, result_full=?, result_summary=?, updated_at=? WHERE id=?",
        (status, result_full, result_summary, time.time(), task_id),
    )
    c.commit()
    return True


def mark_orphans_failed() -> list[dict]:
    """serve 重启后调用一次:把残留的 queued/running 任务标记失败(不续跑)。

    queued 一并处理,而不只是 running——本进程的执行器队列在内存里,重启后
    queued 任务永远不会被捡起,放着不管会变成"永远排队中"的僵尸记录。
    返回受影响的任务(供 executor 逐个走通知分发)。
    """
    c = _conn()
    rows = c.execute(
        "SELECT * FROM tasks WHERE status IN ('queued','running')"
    ).fetchall()
    orphans = [_row(r) for r in rows]
    if orphans:
        now = time.time()
        c.executemany(
            "UPDATE tasks SET status='failed', progress_note=?, updated_at=? WHERE id=?",
            [("服务重启,任务中断", now, o["id"]) for o in orphans],
        )
        c.commit()
    return orphans
