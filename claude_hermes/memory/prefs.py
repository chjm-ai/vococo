"""用户偏好设置(key→value 字符串字典,user_prefs 表)。

2026-07-23 从 session_store.py 拆出(见 images.py 顶部说明)。
"""
from __future__ import annotations

from . import _db


def get_prefs() -> dict:
    """返回全部用户偏好(key→value 字符串字典)。"""
    rows = _db.conn().execute("SELECT key, value FROM user_prefs").fetchall()
    return {k: v for k, v in rows}


def set_prefs(updates: dict) -> None:
    """批量写入用户偏好;value=None 时删除该 key 而非存字符串 'None'。"""
    c = _db.conn()
    for k, v in updates.items():
        if v is None:
            c.execute("DELETE FROM user_prefs WHERE key=?", (str(k),))
        else:
            c.execute(
                "INSERT INTO user_prefs(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(k), str(v)),
            )
    c.commit()
