"""语音会话:独立历史(data/voice/voice.db)+ 调 core.agent.stream_turn。

不碰 data/state.db、不 import memory/session_store.py 的存储实现——自己一份小 sqlite,
换取「删 voice/ 目录即彻底移除」的隔离约束(见 00-overview.md §2.4)。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import AsyncIterator

from .. import config
from ..core.agent import Event, Turn, stream_turn

SESSION_KEY = "voice:main"
HISTORY_LIMIT = 20

_DB: sqlite3.Connection | None = None


def _db_path() -> Path:
    return config.DATA_DIR / "voice" / "voice.db"


def _conn() -> sqlite3.Connection:
    global _DB
    if _DB is None:
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _DB = sqlite3.connect(path, check_same_thread=False)
        _DB.execute(
            "CREATE TABLE IF NOT EXISTS turns("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "ts REAL NOT NULL,"
            "user_text TEXT NOT NULL,"
            "assistant_text TEXT NOT NULL)"
        )
        _DB.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        _DB.commit()
    return _DB


def load_history(limit: int = HISTORY_LIMIT) -> list[Turn]:
    c = _conn()
    rows = c.execute(
        "SELECT user_text, assistant_text FROM turns ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [Turn(user=u, assistant=a) for u, a in reversed(rows)]


def append(user_text: str, assistant_text: str) -> None:
    c = _conn()
    c.execute(
        "INSERT INTO turns(ts, user_text, assistant_text) VALUES (?,?,?)",
        (time.time(), user_text, assistant_text),
    )
    c.commit()


def get_resume() -> str | None:
    row = _conn().execute("SELECT v FROM meta WHERE k='sdk_session_id'").fetchone()
    return row[0] if row else None


def set_resume(sid: str) -> None:
    c = _conn()
    c.execute(
        "INSERT INTO meta(k, v) VALUES ('sdk_session_id', ?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (sid,),
    )
    c.commit()


def run_turn(prompt_text: str, extra_mcp_servers: dict | None = None) -> AsyncIterator[Event]:
    """载入历史、调 stream_turn,把事件流原样透传给调用方消费。

    调用方负责:收到 Done 后把 (原始 user_text, reply.text) 落库(见 append)、
    存回 reply.sdk_session_id(见 set_resume)——本函数只管跑一轮,不做落库,
    因为落库要存的是剥离指令块后的原文,这层信息只有调用方(routes.py)知道。

    extra_mcp_servers:P1 任务板的三个工具(见 task_tools.build_server()),只有
    语音前台会话传它;后台任务会话(executor.py)直接调 stream_turn,不经过这里。
    """
    history = load_history()
    resume_sid = get_resume()
    return stream_turn(
        history, prompt_text, resume=resume_sid, session_key=SESSION_KEY,
        extra_mcp_servers=extra_mcp_servers,
    )
