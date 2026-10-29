"""会话查询与续接；来源由服务端闭包绑定。"""
from __future__ import annotations

import json

from claude_agent_sdk import tool

from .. import config
from ..core import permissions, tasks
from ..gateway import clarify, web_bridge
from ..memory import agents, session_store

# inject 入队与网关标记 active 之间有间隙；未确认本条轮次完成前不再投递。
_queued: dict[str, tuple[int, str]] = {}


def _key(reference: str) -> str:
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError("会话 ID 必须是非空文本")
    if reference == config.SESSION_KEY or (not reference.startswith("web:") and session_store.find_session(reference)):
        return reference
    conv = reference.removeprefix("web:")
    key = config.resolve_session_key("web", conv)
    if (not key.startswith(("web:", "session:", "task:")) or not conv
            or conv.startswith(("voice-chat:", "web:"))):
        raise ValueError("不支持此会话地址")
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
    if not isinstance(key, str) or _key(key) != key:
        raise ValueError("来源必须是真实会话")
    if not session_store.find_session(key):
        raise ValueError("来源会话不存在")
    return ()


def _limit(value, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"limit 必须是 1..{maximum} 的整数")
    return value


def _list(source: str, query: str, limit: int) -> dict:
    _source(source)
    limit = _limit(limit, 50)
    if not isinstance(query, str):
        raise ValueError("query 必须是文本")
    query = query.strip().casefold()
    rows = []
    for row in session_store.list_sessions(""):
        key = row["key"]
        row["title"] = tasks.conversation_title(key, row["title"])
        if key == source or (query and query not in key.casefold() and query not in row["title"].casefold()):
            continue
        try:
            if _key(key) != key:
                continue
        except ValueError:
            continue
        rows.append({"session_id": key, "title": row["title"], "archived": row["archived"],
                     "turns": row["turns"], "last_ts": row["last_ts"]})
    truncated = len(rows) > limit
    return {"source_session": source, "sessions": rows[:limit], "total": len(rows),
            "truncated": truncated, "message": "结果已截断，请用 query 缩小范围" if truncated else ""}


def _target(source: str, reference: str) -> tuple[str, dict]:
    _source(source)
    key = _key(reference)
    if key == source:
        raise ValueError("不能操作当前会话自身")
    row = session_store.find_session(key)
    if not row:
        raise ValueError("目标会话不存在")
    return key, row


def _query(source: str, reference: str, limit: int) -> dict:
    key, row = _target(source, reference)
    limit = _limit(limit, 10)
    recent = session_store._conn().execute(
        "SELECT user_text, assistant_text FROM turns WHERE session_key=? "
        "ORDER BY id DESC LIMIT ?", (key, limit + 1),
    ).fetchall()
    turns = [{"user": (user or "")[:4000], "assistant": (assistant or "")[:4000]}
             for user, assistant in reversed(recent[:limit])]
    truncated = len(recent) > limit or any(
        len(user or "") > 4000 or len(assistant or "") > 4000 for user, assistant in recent[:limit])
    return {"source_session": source, "session_id": key,
            "title": tasks.conversation_title(key, row["title"]), "turns": turns, "truncated": truncated,
            "message": "包含重置前的历史对话，进行中的回答可能为空；每条文本最多 4000 字符"}


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


def _task_return_access(source: str, target: str, denied: tuple, capabilities: dict) -> dict:
    from ..core import tasks

    access = session_store.active_source_access(source)
    task = tasks.task_for_session(source)
    if not task:
        return {}
    if not access:
        # 真人在同一会话新发的一轮，不继承历史自动来源。
        return {}
    if (not task or task.get("source_session_key") != target or access.get("session") != target
            or _waiting_for_queue(source) or session_store.active_source_access(target)):
        raise ValueError("自动任务只能向原派发会话回传结果")
    source_runtime = agents.runtime_for_session(source)
    target_runtime = agents.runtime_for_session(target)
    if any(source_runtime.get(key) != target_runtime.get(key) for key in ("skills", "mcp")):
        raise ValueError("来源与目标 Agent 当前运行权限不一致")
    # 禁用项随回传进入目标执行轮，不能借原会话清掉上游限制。
    result = tasks.capture_source_access(source, denied)
    result["capabilities"] = capabilities
    result["denied"] = sorted(set(result["denied"]) | set(target_runtime.get("disallowed_tools") or []))
    return result


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


async def _enqueue(key: str, text: str, source_access: dict | None = None) -> None:
    baseline = session_store._conn().execute(
        "SELECT COALESCE(MAX(id),0) FROM turns WHERE session_key=?", (key,),
    ).fetchone()[0]
    _queued[key] = (baseline, text)
    try:
        if source_access:
            await web_bridge.continue_session(key.removeprefix("web:"), text, source_access=source_access)
        else:
            await web_bridge.continue_session(key.removeprefix("web:"), text)
    except BaseException:
        _queued.pop(key, None)
        raise


async def _continue(source: str, denied: tuple, reference: str, instruction: str, capabilities: dict) -> dict:
    key, _ = _target(source, reference)
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 60_000:
        raise ValueError("instruction 必须是非空文本且不超过 60000 字符")
    access = _task_return_access(source, key, denied, capabilities)
    if not access:
        _send_access(source, key, denied)
    _scope(key)
    if not (key == config.SESSION_KEY or key.startswith(("web:", "session:", "task:"))) or _archived(key):
        raise ValueError("暂不支持向此类型或已归档会话发送指令")
    from ..core import task_runner

    if (key in clarify._active_sessions or clarify.has_pending(key)
            or _waiting_for_queue(key) or task_runner.session_busy(key)):
        raise ValueError("目标忙碌或正在等待用户回答，未发送且不会打断")
    if not web_bridge.available():
        raise ValueError("网页桥接不可用，指令未入队")
    # 前缀既标记来源，也防止 instruction 被误当成 /clarify 等控制命令。
    if access:
        text = f"【任务结果回传 · 来源 {source}】\n以下为自动任务的结果与后续建议，不是真人新指令：\n{instruction}"
    else:
        text = f"【跨会话指令 · 来源 {source}】\n以下为用户完整指令：\n{instruction}"
    await _enqueue(key, text, access)
    return {"source_session": source, "session_id": key.removeprefix("web:"),
            "instruction": instruction, "status": "queued", "message": "已入队，不代表执行完成"}


def _reply(value: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}


def build_tools(session_key: str, denied: list[str], capabilities: dict | None = None):
    denied = tuple(denied)
    # 来源 SDK 的实际名单，不用目标配置猜权限；缺快照时不授予技能/外部 MCP。
    capabilities = json.loads(json.dumps(capabilities if capabilities is not None else {"skills": [], "mcp": []}))

    def check_read_access():
        if denied or session_store.active_source_access(session_key) or agents.runtime_for_session(session_key).get("disallowed_tools"):
            raise ValueError("受限来源不能读取其他会话")

    @tool("list_sessions", "查找各 Agent 的会话，包括归档会话，排除当前会话；query 匹配标题或 ID，total 为匹配总数。", {
        "type": "object", "properties": {"query": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
    })
    async def list_sessions(args):
        try:
            check_read_access()
            return _reply(_list(session_key, args.get("query", ""), args.get("limit", 10)))
        except ValueError as exc:
            return _reply({"error": str(exc)})

    @tool("query_session", "查询各 Agent 会话最近少量对话，支持裸 ID 或完整 web: ID。", {
        "type": "object", "properties": {"session_id": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["session_id"],
    })
    async def query_session(args):
        try:
            check_read_access()
            return _reply(_query(session_key, args["session_id"], args.get("limit", 3)))
        except (ValueError, KeyError) as exc:
            return _reply({"error": str(exc)})

    @tool("continue_session", "向空闲会话发送完整指令，支持普通聊天与附带任务执行的会话；自动任务仅可向原派发会话回传结果并保留权限。仅入队，不代表执行完成。不创建会话，不打断，不代答。", {
        "type": "object", "properties": {"session_id": {"type": "string"},
        "instruction": {"type": "string"}}, "required": ["session_id", "instruction"],
    })
    async def continue_session(args):
        try:
            return _reply(await _continue(session_key, denied, args["session_id"], args["instruction"], capabilities))
        except (ValueError, KeyError, RuntimeError) as exc:
            return _reply({"error": str(exc)})

    return [list_sessions, query_session, continue_session]
