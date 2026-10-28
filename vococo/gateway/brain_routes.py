"""仅供本机独立 MCP 前端调用的受限后台任务桥。"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import os
import re

from aiohttp import web

from .. import config
from ..core import task_runner, tasks
from ..memory import agents, projects, session_store
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
    "list_agents": (set(), {"offset", "limit", "query"}),
    "list_sessions": (set(), {"agent_id", "all_agents", "offset", "limit", "query"}),
    "read_session": ({"key"}, {"agent_id", "before_id", "turn_id", "offset", "length"}),
    "search_history": ({"query"}, {"agent_id", "before_id", "limit"}),
}
# 只读接口允许访问的 Agent:这里出现即代表本连接可见。派单仍只绑固定开发 Agent。
READABLE_AGENTS = "VOCOCO_BRAIN_READABLE_AGENT_IDS"  # 逗号分隔;省略=全部 Agent
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


def _history_agents(light: bool = False) -> list[dict]:
    """合并 Agent 目录与全部项目(含隐藏历史),不补写元数据。

    light=True 给只读枚举用:agents.get 会对每个 Agent 跑一次 git 判断执行目录,
    25 个 Agent 就要约 1 秒;列 Agent/会话只要 id、名称和归属,用不到执行目录。"""
    found: dict[str, dict] = {}
    root = agents.root_dir()
    if root.is_dir():
        for path in sorted(root.iterdir()):
            meta = agents._read_meta(path.name) if path.is_dir() and agents.valid_id(path.name) else {}
            if not meta.get("workdir"):
                continue
            if light:
                found[path.name] = _light_meta(path.name)
            else:
                agent = agents.get(path.name)
                if agent:
                    found[agent["id"]] = agent
    found.setdefault(agents.GENERAL_ID, _light_meta(agents.GENERAL_ID) if light else agents.get(agents.GENERAL_ID))
    by_hash = {agent["project_hash"]: agent for agent in found.values() if agent.get("project_hash")}
    # 隐藏只影响侧栏,不撤销历史权限；从项目表补旧项目,避免只读枚举创建 agent.json。
    rows = session_store._conn().execute("SELECT hash,path FROM projects ORDER BY hash").fetchall()
    for phash, workdir_path in rows:
        agent = by_hash.get(phash) or agents._build(phash, agents._read_meta(phash), workdir_path)
        found.setdefault(agent["id"], _light(agent) if light else agent)
    return list(found.values())


def _light(agent: dict) -> dict:
    """只读枚举用到的字段子集。"""
    return {"id": agent["id"], "name": agent["name"], "project_hash": agent.get("project_hash"),
            "main_conv": agent.get("main_conv")}


def _light_meta(agent_id: str) -> dict:
    """不跑 git/建目录的轻量版 agents.get,只给只读枚举用。

    命名与归属规则照搬 agents._build:通用 Agent 无项目,其余按工作目录取项目哈希。"""
    if agent_id == agents.GENERAL_ID:
        name, phash, main = agents._read_meta(agent_id).get("name") or "通用", None, "main"
    else:
        meta = agents._read_meta(agent_id)
        workdir = meta.get("workdir")
        if not workdir:
            return {}
        phash = projects.project_hash(workdir)
        name = meta.get("name") or os.path.basename(workdir) or agent_id
        main = f"p{phash}:main" if phash else "main"
    return {"id": agent_id, "name": name, "project_hash": phash, "main_conv": main}


def _visible_agents() -> list[dict]:
    """单主人 OAuth 账号的可读范围,不是多租户授权机制。显式空白名单表示全部拒绝。"""
    allowed = {item.strip() for item in os.environ.get(READABLE_AGENTS, "").split(",") if item.strip()}
    out = []
    for agent in _history_agents(light=True):
        if READABLE_AGENTS in os.environ and agent["id"] not in allowed:
            continue
        out.append({"agent_id": agent["id"], "name": _clean(agent["name"])[:100],
                    "task_dispatch": agent["id"] == AGENT_ID})
    return sorted(out, key=lambda item: item["agent_id"])


def _agent_index() -> tuple[dict[str, dict], dict[str, dict]]:
    """一次备好两套归属映射,避免每个会话都重扫 Agent 目录。

    - by_hash:项目哈希 → Agent,给 web:p<hash> 会话用
    - by_task:定时任务 id → Agent,给 task: 会话用(先查任务表,兼容未归属的旧任务)
    归属规则与 agents.agent_for_session 相同,只是不逐条重扫磁盘。"""
    found = _history_agents(light=True)
    by_hash = {agent["project_hash"]: agent for agent in found if agent.get("project_hash")}
    from ..cron import scheduler

    by_task = {}
    light_agents: dict[str, dict] = {}

    def light_get(agent_id: str | None) -> dict | None:
        if not agent_id or not agents.valid_id(agent_id):
            return None
        if agent_id not in light_agents:
            light_agents[agent_id] = _light_meta(agent_id)
        return light_agents[agent_id] or None

    for job in scheduler.load_jobs():
        agent = light_get(job.get("agent_id")) if job.get("id") else None
        if agent:
            by_task.setdefault(job["id"], agent)
    # 兼容任务表建表更早的库:没有 agent_id 列就只靠 cron 配置归属
    try:
        rows = session_store._conn().execute(
            "SELECT id,agent_id FROM tasks WHERE agent_id IS NOT NULL AND agent_id != ''").fetchall()
    except Exception:
        rows = []
    for task_id, agent_id in rows:
        agent = light_get(agent_id)
        if agent:
            by_task.setdefault(task_id, agent)
    return by_hash, by_task


def _owner_map(by_hash: dict[str, dict], by_task: dict[str, dict]) -> dict[str, dict]:
    """会话 key → Agent,一次算完;查不到归属的 key(含隐藏旧项目)仍走原逐条判定。"""
    out: dict[str, dict] = {}
    for key, _last_id in _session_rows():
        owner = _fast_owner(key, by_hash, by_task, _history_owner)
        if owner:
            out[key] = owner
    return out


def _fast_owner(session_key: str, by_hash: dict[str, dict], by_task: dict[str, dict],
                legacy) -> dict | None:
    phash = config.project_hash_from_key(session_key)
    if phash:
        return by_hash.get(phash) or legacy(session_key)
    if session_key.startswith("task:"):
        return by_task.get(session_key[5:]) or legacy(session_key)
    return legacy(session_key)


def _query(data: dict) -> str:
    """可选关键词:大小写不敏感,匹配会话标题 / Agent 名称。空串表示不过滤。"""
    value = data.get("query")
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 100:
        _reject(400, "query 必须是最多 100 字符的文本")
    return value.strip().casefold()


def _agent_visibility() -> dict[str, dict]:
    return {item["agent_id"]: item for item in _visible_agents()}


def _resolve_agent(data: dict, visibility: dict[str, dict]) -> str:
    """按 agent_id 定位;省略/null = 固定开发 Agent。不可见与不存在一样回 404。"""
    agent_id = AGENT_ID if data.get("agent_id") is None else _text(data, "agent_id", 128)
    if agent_id not in visibility:
        _reject(404, "Agent 不存在或无权访问")
    return agent_id


def _history_owner(key: str) -> dict | None:
    owner = agents.agent_for_session(key)
    if owner:
        return owner
    # 隐藏旧项目可能从未配置过 Agent,只读查历史时不应补建/复活配置。
    phash = config.project_hash_from_key(key)
    if phash:
        return next((agent for agent in _history_agents() if agent.get("project_hash") == phash), None)
    return None


def _owns_session(key: str, agent_id: str) -> bool:
    owner = _history_owner(key)
    return bool(owner and owner["id"] == agent_id)


def _list_agents(data: dict, _agent: dict, _connection: str = "") -> dict:
    """Agent 目录;query 按名称/agent_id 模糊筛。每项附 session_count,列表本身就是分组视图。"""
    offset = _number(data, "offset", 0, 2**63 - 1)
    limit = _number(data, "limit", 50, 100, 1)
    query = _query(data)
    counts = _session_counts()
    items = [{**item, "session_count": counts.get(item["agent_id"], 0)} for item in _visible_agents()]
    if query:
        items = [item for item in items
                 if query in item["name"].casefold() or query in item["agent_id"].casefold()]
    end = offset + limit
    return {"agents": items[offset:end], "next_offset": end if end < len(items) else None,
            "pagination": "Agent 列表随项目增删变化,非快照"}


def _session_rows() -> list[tuple[str, int]]:
    """全部有轮次的会话,按最后轮次倒序。"""
    return session_store._conn().execute(
        "SELECT session_key,MAX(id) AS last_turn_id FROM turns GROUP BY session_key ORDER BY MAX(id) DESC"
    ).fetchall()


def _session_counts() -> dict[str, int]:
    """各 Agent 名下的会话数(与 list_sessions 同一归属与可见性口径)。"""
    visibility = _agent_visibility()
    by_hash, by_task = _agent_index()
    counts: dict[str, int] = {}
    owners = _owner_map(by_hash, by_task)
    for owner in owners.values():
        if owner["id"] in visibility:
            counts[owner["id"]] = counts.get(owner["id"], 0) + 1
    return counts


def _list_sessions(data: dict, _agent: dict, _connection: str = "") -> dict:
    """按 Agent 分组/筛选列出会话。

    - 省略 agent_id 与 all_agents:列全部可读 Agent
    - agent_id:只列该 Agent
    - all_agents=true:跨全部可读 Agent;false:默认只列固定开发 Agent
    - query:可选关键词,大小写不敏感,匹配会话标题或所属 Agent 名称
    """
    offset = _number(data, "offset", 0, 2**63 - 1)
    limit = _number(data, "limit", 20, 100, 1)
    all_agents = data.get("all_agents", data.get("agent_id") is None)
    if type(all_agents) is not bool or (all_agents and data.get("agent_id") is not None):
        _reject(400, "all_agents 必须是布尔值,且不能与 agent_id 同时指定")
    query = _query(data)
    visibility = _agent_visibility()
    target = None if all_agents else _resolve_agent(data, visibility)
    # 关键词先匹配 Agent 名称:命中的 Agent 名下会话整组保留,其余 Agent 一律排除;
    # 都不命中时才逐条按会话标题筛。
    matched = {aid for aid, item in visibility.items() if query in item["name"].casefold()}
    keywords = None if matched or not query else query
    by_hash, by_task = _agent_index()
    owners = _owner_map(by_hash, by_task)
    owned = []
    for key, last_id in _session_rows():
        owner = owners.get(key)
        # 明确指定 agent_id 时,没有归属的会话(通用对话、CLI)不属于任何 Agent,不返回
        if owner is None or owner["id"] not in visibility or (target and owner["id"] != target):
            continue
        if matched and owner["id"] not in matched:
            continue
        title = _clean(session_store.get_title(key) or "新对话")[:200]
        agent_name = visibility[owner["id"]]["name"]
        if keywords and keywords not in title.casefold():
            continue
        owned.append({"agent_id": owner["id"], "agent_name": agent_name, "key": key,
                      "last_turn_id": last_id, "title": title})
    end = offset + limit
    return {"sessions": owned[offset:end], "next_offset": end if end < len(owned) else None,
            "pagination": "非快照；新增或删除会话可能导致分页重复或遗漏"}


def _search_snippet(text: str, terms: list[str]) -> str:
    text = _clean(text)
    matches = [match.start() for term in terms if (match := re.search(re.escape(term), text, re.IGNORECASE))]
    start = max(0, min(matches, default=0) - 100)
    return text[start:start + 500]


def _search_history(data: dict, _agent: dict, _connection: str = "") -> dict:
    query = _text(data, "query", 100)
    terms = query.split()
    limit = _number(data, "limit", 8, 20, 1)
    before = _number(data, "before_id", 2**63 - 1, 2**63 - 1, 1)
    visibility = _agent_visibility()
    target = _resolve_agent(data, visibility) if data.get("agent_id") is not None else None
    owners = _owner_map(*_agent_index())
    rows = session_store._conn().execute(
        "SELECT id,session_key,ts,user_text,assistant_text FROM turns WHERE id<? ORDER BY id DESC", (before,),
    )
    results = []
    for tid, key, timestamp, user, assistant in rows:
        owner = owners.get(key)
        if not owner or owner["id"] not in visibility or (target and owner["id"] != target):
            continue
        user, assistant = _clean(user), _clean(assistant)
        if not any(re.search(re.escape(term), user, re.IGNORECASE) or
                   re.search(re.escape(term), assistant, re.IGNORECASE) for term in terms):
            continue
        if len(results) == limit:
            return {"results": results, "next_before_id": results[-1]["turn_id"], "pagination": "按轮次倒序，非快照"}
        results.append({"agent_id": owner["id"], "agent_name": visibility[owner["id"]]["name"],
                        "key": key, "turn_id": tid, "ts": timestamp,
                        "user_text": _search_snippet(user, terms), "assistant_text": _search_snippet(assistant, terms)})
    return {"results": results, "next_before_id": None, "pagination": "按轮次倒序，非快照"}


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
    # 带 agent_id 时按「这个 Agent 名下」定位,防止同名/key 串到别的 Agent;省略则沿用固定开发 Agent
    if not _owns_session(key, _resolve_agent(data, _agent_visibility())):
        _reject(404, "会话不存在或不属于该 Agent")
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
             "list_agents": _list_agents, "list_sessions": _list_sessions, "read_session": _read_session,
             "search_history": _search_history}


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
    if operation == "search_history":
        result = await asyncio.to_thread(_HANDLERS[operation], data, agent, connection)
    else:
        result = _HANDLERS[operation](data, agent, connection)
    return web.json_response(result, headers={"Cache-Control": "no-store"})


def register_routes(app: web.Application) -> None:
    from . import mcp_events

    app.router.add_post("/internal/brain/{operation}", handle_brain)
    # 远程任务终态 → 事件箱(带签名的 webhook 通知),由本机 MCP 前端投递。
    mcp_events.register_terminal_hook()
    app.on_startup.append(mcp_events.start_scanner)
