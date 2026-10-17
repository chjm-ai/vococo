"""独立研究检查点；每次操作独立连接，事务内读改写，支持切换 DATA_DIR。"""
from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
import time

from vococo import config


@contextmanager
def _connection():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(config.DATA_DIR / "research.db", timeout=15)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS research (id TEXT PRIMARY KEY, state TEXT NOT NULL)"
        )
        connection.commit()
        yield connection
    finally:
        connection.close()


def close() -> None:
    """连接均在操作结束关闭；兼容调用方统一清理接口。"""


def get(task_id: str) -> dict | None:
    with _connection() as connection:
        row = connection.execute("SELECT state FROM research WHERE id=?", (task_id,)).fetchone()
    return json.loads(row[0]) if row else None


def create(state: dict) -> dict:
    with _connection() as connection, connection:
        connection.execute(
            "INSERT INTO research VALUES (?, ?)",
            (state["id"], json.dumps(state, ensure_ascii=False)),
        )
    return state


def update(task_id: str, mutate) -> dict:
    """扣额、来源登记、阶段文本与游标均在同一写事务中提交。"""
    with _connection() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT state FROM research WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("研究任务不存在")
        state = json.loads(row[0])
        mutate(state)
        state["updated_at"] = time.time()
        connection.execute(
            "UPDATE research SET state=? WHERE id=?",
            (json.dumps(state, ensure_ascii=False), task_id),
        )
    return state


def all_states() -> list[dict]:
    with _connection() as connection:
        return [json.loads(row[0]) for row in connection.execute("SELECT state FROM research")]
