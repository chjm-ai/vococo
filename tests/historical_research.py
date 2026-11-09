"""仅向临时测试库写入旧格式记录；生产代码不再提供研究创建能力。"""
from __future__ import annotations

import json
import sqlite3
import time

from vococo import config


def initialize(task_id: str, question: str, model: str) -> dict:
    now = time.time()
    state = {
        "id": task_id, "task_id": task_id, "question": question, "model": model,
        "budget": {"seconds": 300, "searches": 12, "fetches": 18, "tokens": 180000, "max_turns": 12},
        "usage": {"searches": 0, "fetches": 0, "tokens": 0},
        "deadline": now + 300, "created_at": now, "updated_at": now,
        "stage": "plan", "status": "ready", "checkpoints": {}, "sources": [],
        "activity": [], "search_results": [], "report": "", "error": "",
    }
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.DATA_DIR / "research.db") as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS research (id TEXT PRIMARY KEY, state TEXT NOT NULL)")
        connection.execute("INSERT INTO research VALUES (?, ?)", (task_id, json.dumps(state)))
    return state


def update(task_id: str, mutate) -> dict:
    with sqlite3.connect(config.DATA_DIR / "research.db") as connection:
        row = connection.execute("SELECT state FROM research WHERE id=?", (task_id,)).fetchone()
        state = json.loads(row[0])
        mutate(state)
        state["updated_at"] = time.time()
        connection.execute("UPDATE research SET state=? WHERE id=?", (json.dumps(state), task_id))
    return state
