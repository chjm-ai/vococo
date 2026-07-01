"""会话持久化 —— SQLite。

- 按 session_key 隔离(CLI 用 "cli",Telegram 用 "tg:<chat_id>")
- load_recent 取最近 N 轮喂上下文 → 重启不失忆
- search 用 LIKE 子串匹配(中文友好;FTS5 默认分词器切不开中文,个人规模 LIKE 足够)
"""
from __future__ import annotations

import sqlite3
import time
from typing import TYPE_CHECKING

from .. import config

if TYPE_CHECKING:  # 仅类型注解;运行时延迟 import,避免 agent↔store 循环导入
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
        # 迁移:session_meta 加 title 列(Web 会话列表用,老库平滑升级)
        cols = {r[1] for r in _DB.execute("PRAGMA table_info(session_meta)")}
        if "title" not in cols:
            _DB.execute("ALTER TABLE session_meta ADD COLUMN title TEXT")
        _DB.commit()
    return _DB


def _watermark(c: sqlite3.Connection, session_key: str) -> int:
    row = c.execute(
        "SELECT watermark_id FROM session_meta WHERE session_key=?", (session_key,)
    ).fetchone()
    return row[0] if row else 0


def load_recent(session_key: str, limit: int = 40) -> list[Turn]:
    """只取当前会话(水位线之后)的最近 N 轮。"""
    from ..core.agent import Turn  # 延迟 import:打破模块级循环依赖

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


# === Web 会话管理(侧边栏)===
# Web 端自带多会话:每个对话一个 session_key(如 web:1720000000abc)。
# 下面这几个 helper 让前端能列出/命名/删除会话,TG/CLI 不受影响。


def set_title(session_key: str, title: str) -> None:
    """给会话起个显示名(upsert,不动 watermark)。"""
    c = _conn()
    c.execute(
        "INSERT INTO session_meta(session_key, watermark_id, title) VALUES (?,0,?) "
        "ON CONFLICT(session_key) DO UPDATE SET title=excluded.title",
        (session_key, title),
    )
    c.commit()


def get_title(session_key: str) -> str | None:
    row = _conn().execute(
        "SELECT title FROM session_meta WHERE session_key=?", (session_key,)
    ).fetchone()
    return row[0] if row and row[0] else None


def ensure_title(session_key: str, from_text: str, limit: int = 24) -> None:
    """会话还没名字时,拿首句用户输入截断当标题。"""
    if get_title(session_key):
        return
    title = " ".join(from_text.split())[:limit].strip() or "新对话"
    set_title(session_key, title)


def list_sessions(prefix: str) -> list[dict]:
    """列出 session_key 以 prefix 开头的会话摘要,最近活跃的排前面。

    返回 [{key, title, turns, last_ts}, ...]。turns 只算当前上下文窗口
    (水位线之后),= 该会话现在带着多少轮记忆。

    会话键来源取「有 turn」∪「有标题」:新建会话在 AI 回复完之前只写了
    session_meta 标题、还没有 turn 落库,若只按 turns 表列会导致它刚发完
    第一条就从侧边栏消失(要等回复完才出现)。带标题即视为一个会话。
    """
    c = _conn()
    rows = c.execute(
        "WITH keys AS ("
        "  SELECT session_key FROM turns WHERE session_key LIKE ?"
        "  UNION"
        "  SELECT session_key FROM session_meta "
        "  WHERE session_key LIKE ? AND title IS NOT NULL AND title != ''"
        ") "
        # 还没 turn 的新会话用「当前时间」兜底,好让它在等首条回复期间稳定
        # 排在列表最前(前端按 last_ts 倒序);回复落库后自然换成真实 turn 时间。
        "SELECT k.session_key, COUNT(t.id), "
        "  COALESCE(MAX(t.ts), strftime('%s','now')) "
        "FROM keys k "
        "LEFT JOIN session_meta m ON k.session_key = m.session_key "
        "LEFT JOIN turns t ON t.session_key = k.session_key "
        "  AND t.id > COALESCE(m.watermark_id, 0) "
        "GROUP BY k.session_key ORDER BY 3 DESC",
        (prefix + "%", prefix + "%"),
    ).fetchall()
    out: list[dict] = []
    for key, turns, last_ts in rows:
        out.append(
            {
                "key": key,
                "title": get_title(key) or "新对话",
                "turns": turns,
                "last_ts": last_ts,
            }
        )
    return out


def session_summary(session_key: str) -> dict:
    """单个会话摘要(给固定入口如统一主会话用)。"""
    c = _conn()
    wm = _watermark(c, session_key)
    row = c.execute(
        "SELECT COUNT(*), MAX(ts) FROM turns WHERE session_key=? AND id>?",
        (session_key, wm),
    ).fetchone()
    turns = row[0] if row else 0
    last_ts = row[1] if row and row[1] else None
    return {
        "key": session_key,
        "title": get_title(session_key),
        "turns": turns,
        "last_ts": last_ts,
    }


def delete_session(session_key: str) -> None:
    """彻底删掉一个会话(所有轮次 + 元数据)。"""
    c = _conn()
    c.execute("DELETE FROM turns WHERE session_key=?", (session_key,))
    c.execute("DELETE FROM session_meta WHERE session_key=?", (session_key,))
    c.commit()


def search(text: str, limit: int = 10) -> list[tuple[str, str, str]]:
    """子串检索,返回 (session_key, user_text, assistant_text)。

    按空白把查询拆成多个关键词:任一命中即返回(OR),命中关键词多的排前面,
    其次按时间倒序。中文长句直接整串 LIKE 很难命中,拆词后召回率高得多
    (agent 传「出差 名古屋」比传整句更容易召回到历史片段)。
    """
    terms = [t for t in text.split() if t] or [text]
    # 每个关键词:命中 user 或 assistant 记 1 分,SUM 为该轮总命中分
    score = " + ".join(
        "(CASE WHEN user_text LIKE ? OR assistant_text LIKE ? THEN 1 ELSE 0 END)"
        for _ in terms
    )
    where = " OR ".join("user_text LIKE ? OR assistant_text LIKE ?" for _ in terms)
    likes = [f"%{t}%" for t in terms]
    score_params = [p for like in likes for p in (like, like)]
    where_params = list(score_params)
    return _conn().execute(
        f"SELECT session_key, user_text, assistant_text FROM turns "
        f"WHERE {where} ORDER BY ({score}) DESC, id DESC LIMIT ?",
        (*where_params, *score_params, limit),
    ).fetchall()
