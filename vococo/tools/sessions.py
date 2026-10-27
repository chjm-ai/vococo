"""普通 Web 聊天的同范围查询；来源由服务端闭包绑定。"""
from __future__ import annotations

import json

from claude_agent_sdk import tool

from .. import config
from ..core import permissions
from ..gateway import clarify, web_bridge
from ..memory import agents, session_store

# inject 入队与网关标记 active 之间有间隙；未确认本条轮次完成前不再投递。
_queued: dict[str, tuple[int, str]] = {}


def _key(reference: str) -> str:
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError("会话 ID 必须是非空文本")
    conv = reference.removeprefix("web:")
    key = config.resolve_session_key("web", conv)
    if (not key.startswith("web:") or not conv
            or conv.startswith(("task:", "voice-chat:", "web:"))):
        raise ValueError("只支持普通 Web 会话，不支持任务或语音会话")
    return key


def _archived(key: str) -> bool:
    row = session_store._conn().execute(
        "SELECT archived FROM session_meta WHERE session_key=?", (key,),
    ).fetchone()
    return bool(row and row[0])


def _scope(key: str) -> tuple:
    project = config.project_hash_from_key(key)
    owner = agents.agent_for_session(key)
    if project and not owner:
        raise ValueError("无法确认会话 Agent，可能已删除")
    return project, owner["id"] if owner else agents.GENERAL_ID


def _source(key: str) -> tuple:
    if not isinstance(key, str) or not key.startswith("web:") or _key(key) != key:
        raise ValueError("来源必须是真实普通 Web 会话")
    if not session_store.find_session(key) or _archived(key):
        raise ValueError("来源会话不存在或已归档")
    return _scope(key)


def _limit(value, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"limit 必须是 1..{maximum} 的整数")
    return value


def _list(source: str, query: str, limit: int) -> dict:
    scope = _source(source)
    limit = _limit(limit, 50)
    if not isinstance(query, str):
        raise ValueError("query 必须是文本")
    query = query.strip().casefold()
    rows = []
    for row in session_store.list_sessions("web:", archived=False):
        key = row["key"]
        if key == source or (query and query not in key.casefold() and query not in row["title"].casefold()):
            continue
        try:
            if _key(key) != key or _scope(key) != scope:
                continue
        except ValueError:
            continue
        rows.append({"session_id": key.removeprefix("web:"), "title": row["title"],
                     "turns": row["turns"], "last_ts": row["last_ts"]})
    truncated = len(rows) > limit
    return {"source_session": source, "sessions": rows[:limit], "total": len(rows),
            "truncated": truncated, "message": "结果已截断，请用 query 缩小范围" if truncated else ""}


def _target(source: str, reference: str) -> tuple[str, dict]:
    scope = _source(source)
    key = _key(reference)
    if key == source:
        raise ValueError("不能操作当前会话自身")
    row = session_store.find_session(key)
    if not row or _archived(key):
        raise ValueError("目标会话不存在或已归档")
    if _scope(key) != scope:
        raise ValueError("目标必须属于同项目、同 Agent")
    return key, row


def _query(source: str, reference: str, limit: int) -> dict:
    key, row = _target(source, reference)
    limit = _limit(limit, 10)
    recent = session_store.load_recent(key, limit + 1)
    turns = [{"user": turn.user[:4000], "assistant": turn.assistant[:4000]}
             for turn in recent[-limit:]]
    truncated = len(recent) > limit or any(
        len(turn.user) > 4000 or len(turn.assistant) > 4000 for turn in recent[-limit:])
    return {"source_session": source, "session_id": key.removeprefix("web:"),
            "title": row["title"], "turns": turns, "truncated": truncated,
            "message": "仅返回最近已完成对话；每条文本最多 4000 字符"}


def _send_access(source: str, target: str, denied: tuple) -> None:
    # bridge 不能传递 source_access；任何限制都不能借真人续聊入口洗掉。
    if (denied or _waiting_for_queue(source)
            or any(session_store.active_source_access(key) for key in (source, target))):
        raise ValueError("受限或自动来源链不能跨会话发送指令")
    source_runtime = agents.runtime_for_session(source)
    target_runtime = agents.runtime_for_session(target)
    if source_runtime.get("disallowed_tools") or target_runtime.get("disallowed_tools"):
        raise ValueError("来源或目标 Agent 当前存在工具权限限制，不能发送")
    if any(source_runtime.get(key) != target_runtime.get(key) for key in ("skills", "mcp")):
        raise ValueError("来源与目标 Agent 当前运行权限不一致")
    if permissions.state(source)["mode"] != "full" and permissions.state(target)["mode"] == "full":
        raise ValueError("目标权限高于来源，不能借续聊提升权限")


def _waiting_for_queue(key: str) -> bool:
    queued = _queued.get(key)
    if not queued:
        return False
    baseline, text = queued
    row = session_store._conn().execute(
        "SELECT assistant_text FROM turns WHERE session_key=? AND id>? AND user_text=? "
        "ORDER BY id LIMIT 1", (key, baseline, text),
    ).fetchone()
    if not row or not row[0]:
        return True
    del _queued[key]
    return False


def is_queued_instruction(key: str, text: str) -> bool:
    queued = _queued.get(key)
    return bool(queued and queued[1] == text)


def dispatch_finished(key: str, text: str) -> None:
    """网关无论成功、失败或取消都释放本条入队标记，不依赖是否写出了回答。"""
    queued = _queued.get(key)
    if queued and queued[1] == text:
        _queued.pop(key, None)


async def _enqueue(key: str, text: str) -> None:
    baseline = session_store._conn().execute(
        "SELECT COALESCE(MAX(id),0) FROM turns WHERE session_key=?", (key,),
    ).fetchone()[0]
    _queued[key] = (baseline, text)
    try:
        await web_bridge.continue_session(key.removeprefix("web:"), text)
    except BaseException:
        _queued.pop(key, None)
        raise


async def _continue(source: str, denied: tuple, reference: str, instruction: str) -> dict:
    key, _ = _target(source, reference)
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 60_000:
        raise ValueError("instruction 必须是非空文本且不超过 60000 字符")
    _send_access(source, key, denied)
    if key in clarify._active_sessions or clarify.has_pending(key) or _waiting_for_queue(key):
        raise ValueError("目标忙碌或正在等待用户回答，未发送且不会打断")
    if not web_bridge.available():
        raise ValueError("网页桥接不可用，指令未入队")
    # 前缀既标记来源，也防止 instruction 被误当成 /clarify 等控制命令。
    text = f"【跨会话指令 · 来源 {source}】\n以下为用户完整指令：\n{instruction}"
    await _enqueue(key, text)
    return {"source_session": source, "session_id": key.removeprefix("web:"),
            "instruction": instruction, "status": "queued", "message": "已入队，不代表执行完成"}


def _reply(value: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}


def build_tools(session_key: str, denied: list[str]):
    denied = tuple(denied)
    @tool("list_sessions", "查找同项目、同 Agent 的普通 Web 会话，排除当前会话和归档；query 匹配标题或 ID，total 为匹配总数。", {
        "type": "object", "properties": {"query": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
    })
    async def list_sessions(args):
        try:
            return _reply(_list(session_key, args.get("query", ""), args.get("limit", 10)))
        except ValueError as exc:
            return _reply({"error": str(exc)})

    @tool("query_session", "查询同项目、同 Agent 普通 Web 会话最近少量已完成对话，支持裸 ID 或完整 web: ID。", {
        "type": "object", "properties": {"session_id": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["session_id"],
    })
    async def query_session(args):
        try:
            return _reply(_query(session_key, args["session_id"], args.get("limit", 3)))
        except (ValueError, KeyError) as exc:
            return _reply({"error": str(exc)})

    @tool("continue_session", "向同项目、同 Agent 的空闲普通 Web 会话发送完整指令；仅入队，不代表执行完成。不创建会话，不打断，不代答。", {
        "type": "object", "properties": {"session_id": {"type": "string"},
        "instruction": {"type": "string"}}, "required": ["session_id", "instruction"],
    })
    async def continue_session(args):
        try:
            return _reply(await _continue(session_key, denied, args["session_id"], args["instruction"]))
        except (ValueError, KeyError, RuntimeError) as exc:
            return _reply({"error": str(exc)})

    return [list_sessions, query_session, continue_session]
