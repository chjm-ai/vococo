"""仅供本机独立 MCP 前端调用的受限后台任务桥。"""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re

from aiohttp import web

from ..core import task_runner, tasks
from ..memory import agents, session_store
from ..tools import danger

AGENT_ID = "286d3c1abf"
_BLOCKED = (
    "delegate_to_agent", "dispatch_session", "run_subagent", "add_cron_job",
    "update_cron_job", "delete_cron_job", "set_cron_job_enabled", "list_cron_jobs", "suggest_automation",
    "add_mcp_server", "remove_mcp_server", "set_external_mcp", "switch_model",
    "restart_self", "update_agent", "create_agent", "delete_agent", "set_permission",
    "recall_past",
)
DENIED = ["Agent", "Task", "SendMessage", "CronCreate", "CronDelete", "CronList", "EnterWorktree", "ExitWorktree",
          *[f"mcp__vococo__{name}" for name in _BLOCKED]]
_FIELDS = {
    "create_task": ({"title", "prompt", "idempotency_key"}, set()),
    "get_task": ({"id"}, {"offset", "length"}),
    "cancel_task": ({"id"}, set()),
    "list_sessions": (set(), {"offset", "limit"}),
    "read_session": ({"key"}, {"before_id", "turn_id", "offset", "length"}),
}
_PROXY_HEADERS = {"forwarded", "origin", "via", "x-real-ip", "true-client-ip",
                  "x-original-url", "x-rewrite-url"}


def _reject(status: int, message: str):
    errors = {400: web.HTTPBadRequest, 401: web.HTTPUnauthorized,
              403: web.HTTPForbidden, 404: web.HTTPNotFound,
              409: web.HTTPConflict, 503: web.HTTPServiceUnavailable}
    raise errors[status](text=json.dumps({"error": message}), content_type="application/json")


def _authenticate(request: web.Request) -> None:
    headers = {name.lower() for name in request.headers}
    if headers & _PROXY_HEADERS or any(
        name.startswith(("x-forwarded-", "cf-", "x-proxy-")) for name in headers
    ):
        _reject(403, "禁止代理和浏览器来源")
    peer = request.transport.get_extra_info("peername") if request.transport else None
    try:
        local = peer and ipaddress.ip_address(peer[0]).is_loopback
    except (ValueError, TypeError):
        local = False
    if not local:
        _reject(403, "仅允许本机连接")
    token = os.environ.get("VOCOCO_BRAIN_BRIDGE_TOKEN", "")
    if len(token) < 32:
        _reject(503, "桥 token 未安全配置")
    expected = f"Bearer {token}".encode()
    if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), expected):
        _reject(401, "桥认证失败")


def _agent() -> dict:
    if os.environ.get("VOCOCO_BRAIN_AGENT_ID", AGENT_ID) != AGENT_ID:
        _reject(503, "桥只能绑定固定 Agent")
    agent = agents.get(AGENT_ID)
    if not agent or not agent.get("workdir"):
        _reject(503, "固定 Agent 不可用")
    return agent


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复字段")
        result[key] = value
    return result


async def _payload(request: web.Request, operation: str) -> dict:
    if request.content_type != "application/json" or request.query_string:
        _reject(400, "只接受 JSON 正文，不接受查询参数")
    body = bytearray()
    async for chunk in request.content.iter_chunked(16384):
        body.extend(chunk)
        if len(body) > 800000:
            _reject(400, "请求过大")
    try:
        data = json.loads(body, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError):
        _reject(400, "无效 JSON")
    required, optional = _FIELDS[operation]
    if not isinstance(data, dict) or not required <= data.keys() or data.keys() - required - optional:
        _reject(400, "字段缺失或包含未定义字段")
    return data


def _text(data: dict, key: str, maximum: int) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _reject(400, f"{key} 必须是非空文本，最多 {maximum} 字符")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        _reject(400, "不接受孤立 Unicode 代理字符")
    return value


def _number(data: dict, key: str, default: int, maximum: int, minimum: int = 0) -> int:
    value = data.get(key, default)
    if type(value) is not int or not minimum <= value <= maximum:
        _reject(400, f"{key} 必须是 {minimum}..{maximum} 的整数")
    return value


def _registry():
    connection = tasks._conn()
    connection.execute(
        "CREATE TABLE IF NOT EXISTS brain_bridge_tasks("
        "task_id TEXT PRIMARY KEY REFERENCES tasks(id),agent_id TEXT NOT NULL,"
        "idempotency_key TEXT NOT NULL,payload TEXT NOT NULL,source_session_key TEXT NOT NULL,"
        "cancel_requested INTEGER NOT NULL DEFAULT 0,UNIQUE(agent_id,idempotency_key))"
    )
    return connection


def _access(agent: dict, connection: str = "") -> dict:
    source = agents.main_session_key(agent)
    runtime = agents.runtime_for_session(source)
    denied = sorted(set([*DENIED, *(runtime.get("disallowed_tools") or [])]))
    access = {"session": source, "mode": "standard", "remote_brain": True, "denied": denied}
    if connection:
        # 连接身份(MCP 前端已按上游凭据哈希):任务终态事件只投给同一连接。
        access["connection"] = connection[:64]
    return access


def _create_atomic(data: dict, agent: dict, connection: str = "") -> dict:
    title, prompt = _text(data, "title", 100), _text(data, "prompt", 60000)
    key = _text(data, "idempotency_key", 128)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", key):
        _reject(400, "idempotency_key 必须是 8..128 位 ASCII 安全字符")
    payload = json.dumps([title, prompt], ensure_ascii=True)
    access = _access(agent, connection)
    conn = _registry()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute(
            "SELECT task_id,payload FROM brain_bridge_tasks WHERE agent_id=? AND idempotency_key=?",
            (AGENT_ID, key),
        ).fetchone()
        if old:
            if old["payload"] != payload:
                _reject(409, "相同幂等键的内容不同")
            return _owned_task(old["task_id"])
        task = tasks.create(
            title, prompt, cwd=agent["workdir"], cwd_explicit=True, origin="chat",
            dispatch_platform="web", dispatch_chat_id=agent["main_conv"], agent_id=AGENT_ID,
            source_session_key=access["session"], source_access=access, commit=False,
        )
        conn.execute(
            "INSERT INTO brain_bridge_tasks(task_id,agent_id,idempotency_key,payload,source_session_key) VALUES (?,?,?,?,?)",
            (task["id"], AGENT_ID, key, payload, access["session"]),
        )
    return task


def _owned_task(task_id: str) -> dict:
    record = _registry().execute(
        "SELECT * FROM brain_bridge_tasks WHERE task_id=? AND agent_id=?", (task_id, AGENT_ID)
    ).fetchone()
    task = tasks.get(task_id)
    if not record or not task or task["agent_id"] != AGENT_ID or task["origin"] != "chat":
        _reject(404, "外部任务不存在")
    access = tasks.automatic_access(task)
    if (access.get("remote_brain") is not True or task["source_session_key"] != access.get("session")
            or task["source_session_key"] != record["source_session_key"]):
        _reject(404, "外部任务来源不匹配")
    task["cancel_requested"] = bool(record["cancel_requested"])
    return task


def _clean(text: str) -> str:
    text = danger.redact_secrets(text or "")
    token = os.environ.get("VOCOCO_BRAIN_BRIDGE_TOKEN", "")
    if token:
        text = text.replace(token, "[已隐藏]")
    # 宁可丢失普通链接，也不把受控下载票据或本机路径作为外部历史返回。
    text = re.sub(r"(?i)(?:https?://|file:/+|data:)[^\s<>\"']+", "[链接已移除]", text)
    text = re.sub(r"(?i)/(?:artifacts/|image\?|audio\?|video\?|file/|doc/|doc-preview\.)[^\s<>\"')]+",
                  "[附件已移除]", text)
    text = re.sub(r"(?m)^(?:[A-Za-z0-9+/]{40,}={0,2}\r?\n){2,}", "[编码已移除]\n", text)
    text = re.sub(r"(?<![\w+/])[A-Za-z0-9+/]{80,}={0,2}(?![\w+/])", "[编码已移除]", text)
    return re.sub(
        r"(?im)^.*\b(?:[\w-]*(?:secret|password|passwd|token)|(?:api|private|access)[_-]?key|authorization|credential[\w-]*)"
        r"[\"']?\s*[:=].*$", "[敏感字段已移除]", text,
    )


def _task_view(task: dict, data: dict) -> dict:
    offset = _number(data, "offset", 0, 2**63 - 1)
    length = _number(data, "length", 8000, 8000, 1)
    result = _clean(task.get("result_full") or task.get("result_summary") or "")
    end = offset + length
    return {"id": task["id"], "title": _clean(task["title"]), "status": task["status"],
            "progress": _clean(task.get("progress_note", ""))[:8000], "result": result[offset:end],
            "next_offset": end if end < len(result) else None,
            "cancel_requested": bool(task.get("cancel_requested"))}


def _create_task(data: dict, agent: dict, connection: str = "") -> dict:
    task = _create_atomic(data, agent, connection)
    task_runner._maybe_start_next()
    task_runner._notify_activity(task["id"])
    return _task_view(_owned_task(task["id"]), {})


def _get_task(data: dict, _agent: dict, _connection: str = "") -> dict:
    return _task_view(_owned_task(_text(data, "id", 128)), data)


def _cancel_task(data: dict, _agent: dict, _connection: str = "") -> dict:
    task = _owned_task(_text(data, "id", 128))
    accepted = task_runner.cancel(task["id"])
    if accepted:
        connection = _registry()
        with connection:
            connection.execute("UPDATE brain_bridge_tasks SET cancel_requested=1 WHERE task_id=?", (task["id"],))
    return _task_view(_owned_task(task["id"]), {})


def _owns_session(key: str) -> bool:
    owner = agents.agent_for_session(key)
    return bool(owner and owner["id"] == AGENT_ID)


def _list_sessions(data: dict, _agent: dict, _connection: str = "") -> dict:
    offset = _number(data, "offset", 0, 2**63 - 1)
    limit = _number(data, "limit", 20, 20, 1)
    rows = session_store._conn().execute(
        "SELECT session_key,MAX(id) AS last_turn_id FROM turns GROUP BY session_key ORDER BY MAX(id) DESC"
    ).fetchall()
    owned = [{"key": key, "last_turn_id": last_id,
              "title": _clean(session_store.get_title(key) or "新对话")[:200]}
             for key, last_id in rows if _owns_session(key)]
    end = offset + limit
    return {"sessions": owned[offset:end], "next_offset": end if end < len(owned) else None,
            "pagination": "非快照；新增或删除会话可能导致分页重复或遗漏"}


def _turn_row(data: dict, key: str):
    if "turn_id" in data:
        if "before_id" in data:
            _reject(400, "turn_id 和 before_id 互斥")
        turn_id = _number(data, "turn_id", 0, 2**63 - 1, 1)
        clause, value = "id=?", turn_id
    else:
        if data.get("offset", 0) != 0:
            _reject(400, "续读 offset 必须带 turn_id")
        clause, value = "id<?", _number(data, "before_id", 2**63 - 1, 2**63 - 1, 1)
    return session_store._conn().execute(
        f"SELECT id,ts,user_text,assistant_text FROM turns WHERE session_key=? AND {clause} ORDER BY id DESC LIMIT 1",
        (key, value),
    ).fetchone()


def _read_session(data: dict, _agent: dict, _connection: str = "") -> dict:
    key = _text(data, "key", 512)
    if not _owns_session(key):
        _reject(404, "会话不存在或不属于固定 Agent")
    offset = _number(data, "offset", 0, 2**63 - 1)
    length = _number(data, "length", 8000, 8000, 1)
    row = _turn_row(data, key)
    if row is None:
        return {"turn": None, "next_offset": None, "next_before_id": None}
    turn_id, timestamp, raw_user, raw_assistant = row
    user, assistant = _clean(raw_user), _clean(raw_assistant)
    end = offset + length
    turn = {"id": turn_id, "ts": timestamp, "user_text": user[offset:end],
            "assistant_text": assistant[max(0, offset - len(user)):max(0, end - len(user))]}
    more = end < len(user) + len(assistant)
    return {"turn": turn, "next_offset": end if more else None,
            "next_before_id": None if more else turn_id}


_HANDLERS = {"create_task": _create_task, "get_task": _get_task, "cancel_task": _cancel_task,
             "list_sessions": _list_sessions, "read_session": _read_session}


async def handle_brain(request: web.Request) -> web.Response:
    _authenticate(request)
    operation = request.match_info["operation"]
    if operation not in _FIELDS:
        _reject(404, "操作不存在")
    agent = _agent()
    # 连接身份由本机 MCP 前端(已过 OAuth)按上游凭据哈希写入;缺失只是老版本前端,不影响派单本身。
    connection = request.headers.get("X-Vococo-Connection", "")
    if connection and not re.fullmatch(r"[A-Za-z0-9]{8,64}", connection):
        _reject(400, "连接身份格式无效")
    data = await _payload(request, operation)
    result = _HANDLERS[operation](data, agent, connection)
    return web.json_response(result, headers={"Cache-Control": "no-store"})


def register_routes(app: web.Application) -> None:
    from . import mcp_events

    app.router.add_post("/internal/brain/{operation}", handle_brain)
    # 远程任务终态 → 事件箱(带签名的 webhook 通知),由本机 MCP 前端投递。
    mcp_events.register_terminal_hook()
    app.on_startup.append(mcp_events.start_scanner)
