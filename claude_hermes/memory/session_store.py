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
CREATE TABLE IF NOT EXISTS session_meta(
  session_key TEXT PRIMARY KEY,
  watermark_id INTEGER NOT NULL DEFAULT 0
);
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


def _watermark(c: sqlite3.Connection, session_key: str) -> int:
    row = c.execute(
        "SELECT watermark_id FROM session_meta WHERE session_key=?", (session_key,)
    ).fetchone()
    return row[0] if row else 0


def load_recent(session_key: str, limit: int = 40) -> list[Turn]:
    """只取当前会话(水位线之后)的最近 N 轮。"""
    c = _conn()
    wm = _watermark(c, session_key)
    rows = c.execute(
        "SELECT user_text, assistant_text FROM turns "
        "WHERE session_key=? AND id>? ORDER BY id DESC LIMIT ?",
        (session_key, wm, limit),
    ).fetchall()
    return [Turn(user=u, assistant=a) for u, a in reversed(rows)]


def new_session(session_key: str) -> None:
    """开新会话:把水位线推到当前最大 id —— 旧轮次保留在库(可 search),但不再载入上下文。"""
    c = _conn()
    row = c.execute(
        "SELECT COALESCE(MAX(id),0) FROM turns WHERE session_key=?", (session_key,)
    ).fetchone()
    c.execute(
        "INSERT INTO session_meta(session_key, watermark_id) VALUES (?,?) "
        "ON CONFLICT(session_key) DO UPDATE SET watermark_id=excluded.watermark_id",
        (session_key, row[0]),
    )
    c.commit()


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
