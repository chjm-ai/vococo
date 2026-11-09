"""只读访问旧研究数据库；不创建数据库、表或研究记录。"""
from __future__ import annotations

import json
import sqlite3

from vococo import config


def get(task_id: str) -> dict | None:
    path = config.DATA_DIR / "research.db"
    if not path.is_file():
        return None
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research'"
        ).fetchone()
        if not exists:
            return None
        row = connection.execute("SELECT state FROM research WHERE id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        connection.close()
