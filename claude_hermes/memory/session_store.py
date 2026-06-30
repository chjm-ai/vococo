"""会话持久化 —— SQLite。

- 按 session_key 隔离(CLI 用 "cli",Telegram 用 "tg:<chat_id>")
- load_recent 取最近 N 轮喂上下文 → 重启不失忆
- search 用 LIKE 子串匹配(中文友好;FTS5 默认分词器切不开中文,个人规模 LIKE 足够)
"""
from __future__ import annotations

import sqlite3
import time

from .. import config
from ..core.agent import Turn

_DB: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns(
  id INTEGER PRIMARY KEY,
  session_key TEXT NOT NULL,
  ts REAL NOT NULL,
  user_text TEXT NOT NULL,
  assistant_text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_key, id);
"""


def _conn() -> sqlite3.Connection:
    global _DB
    if _DB is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _DB = sqlite3.connect(
            str(config.DATA_DIR / "state.db"), check_same_thread=False
        )
        _DB.execute("PRAGMA journal_mode=WAL")
        _DB.executescript(_SCHEMA)
        _DB.commit()
    return _DB


def load_recent(session_key: str, limit: int = 40) -> list[Turn]:
    rows = _conn().execute(
        "SELECT user_text, assistant_text FROM turns "
        "WHERE session_key=? ORDER BY id DESC LIMIT ?",
        (session_key, limit),
    ).fetchall()
    return [Turn(user=u, assistant=a) for u, a in reversed(rows)]


def append(session_key: str, user_text: str, assistant_text: str) -> None:
    c = _conn()
    c.execute(
        "INSERT INTO turns(session_key, ts, user_text, assistant_text) "
        "VALUES (?,?,?,?)",
        (session_key, time.time(), user_text, assistant_text),
    )
    c.commit()


def clear(session_key: str) -> None:
    c = _conn()
    c.execute("DELETE FROM turns WHERE session_key=?", (session_key,))
    c.commit()


def search(text: str, limit: int = 10) -> list[tuple[str, str, str]]:
    """子串检索,返回 (session_key, user_text, assistant_text)。"""
    like = f"%{text}%"
    return _conn().execute(
        "SELECT session_key, user_text, assistant_text FROM turns "
        "WHERE user_text LIKE ? OR assistant_text LIKE ? "
        "ORDER BY id DESC LIMIT ?",
        (like, like, limit),
    ).fetchall()
