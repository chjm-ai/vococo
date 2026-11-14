"""聊天与开发任务分开存储身份，旧对话不丢失、不原地转换。"""
from __future__ import annotations

import uuid

from .. import config
from . import _db, agents, session_store


def _is_agent_main(key: str, agent_id: str | None) -> bool:
    owner = agents.get(agent_id) if agent_id else None
    if owner is None and (phash := config.project_hash_from_key(key)):
        owner = agents.by_project_hash(phash)
    return bool(owner and owner["id"] != "general" and agents.main_session_key(owner) == key)


def mode(key: str | None) -> str:
    if not key:
        return "task"
    if key in {"main", "web:main", "voice-chat:main"}:
        return "chat"
    row = _db.conn().execute(
        "SELECT conversation_mode, agent_id FROM session_meta WHERE session_key=?", (key,),
    ).fetchone()
    # 旧版本读聊天历史时误标过 Agent 主会话；直接执行也要纠正，不能依赖先打开历史。
    if row and row[0] == "chat" and _is_agent_main(key, row[1]):
        return "task"
    return row[0] if row and row[0] else "task"


def preserve_legacy(owner: dict) -> None:
    keys = [agents.main_session_key(owner)]
    if owner["id"] == "general":
        keys.append("voice-chat:main")
    legacy_mode = "chat" if owner["id"] == "general" else "task"
    for key in keys:
        if not session_store.session_summary(key)["turns"]:
            continue
        if not session_store.session_binding(key):
            session_store.bind_session(key, agent_id=owner["id"], project_hash=owner.get("project_hash"))
        with _db.conn() as connection:
            connection.execute("UPDATE session_meta SET conversation_mode=? WHERE session_key=?", (legacy_mode, key))


def _summary(key: str) -> dict:
    return {**session_store.session_summary(key), **session_store.session_binding(key),
            "conv": key, "conversation_mode": "chat",
            "title": session_store.get_title(key) or "新聊天"}


def create(agent_id: str) -> dict:
    owner = agents.get(agent_id)
    if not owner:
        raise ValueError("Agent 不存在")
    preserve_legacy(owner)
    key = f"session:{uuid.uuid4().hex}"
    session_store.bind_session(key, agent_id=agent_id, project_hash=owner.get("project_hash"))
    with _db.conn() as connection:
        connection.execute("UPDATE session_meta SET conversation_mode='chat', title='新聊天' WHERE session_key=?", (key,))
    session_store.set_prefs({f"active_chat:{agent_id}": key})
    return _summary(key)


def current(agent_id: str) -> dict:
    key = session_store.get_prefs().get(f"active_chat:{agent_id}")
    if key and mode(key) == "chat" and session_store.session_binding(key).get("agent_id") == agent_id:
        return _summary(key)
    history = recent(agent_id)
    if history:
        return select(history[0]["conv"], agent_id)
    return create(agent_id)


def select(key: str, agent_id: str) -> dict:
    if mode(key) != "chat" or session_store.session_binding(key).get("agent_id") != agent_id:
        raise ValueError("聊天不属于当前 Agent")
    session_store.set_prefs({f"active_chat:{agent_id}": key})
    return _summary(key)


def recent(agent_id: str) -> list[dict]:
    owner = agents.get(agent_id)
    if not owner:
        raise ValueError("Agent 不存在")
    preserve_legacy(owner)
    keys = _db.conn().execute(
        "SELECT session_key FROM session_meta WHERE agent_id=? AND conversation_mode='chat' "
        "AND COALESCE(archived,0)=0", (agent_id,),
    ).fetchall()
    rows = [_summary(key) for (key,) in keys if mode(key) == "chat"]
    return sorted((row for row in rows if row.get("turns")),
                  key=lambda row: row.get("last_ts") or 0, reverse=True)[:30]
