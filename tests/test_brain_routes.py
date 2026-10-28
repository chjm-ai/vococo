"""受限桥通过真实 HTTP 和临时数据库验证，不启动模型。"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.core import permissions, task_runner, tasks
from vococo.gateway import brain_routes as brain, settings_store, task_routes
from vococo.memory import agents, approvals, session_store
from vococo.tools import danger

pytestmark = pytest.mark.anyio
TOKEN = "bridge-test-token-" + "x" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CREATE = {"title": "测试任务", "prompt": "请整理资料", "idempotency_key": "request-0001"}
PUBLIC = {"id", "title", "status", "progress", "result", "next_offset", "cancel_requested"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def bridge_db(isolated, monkeypatch):
    monkeypatch.setattr(tasks, "_DB", None)
    monkeypatch.setattr(config, "CRON_JOBS_PATH", isolated / "cron.json")
    monkeypatch.setattr(settings_store, "_PATH", isolated / "settings.json")
    monkeypatch.setenv("VOCOCO_BRAIN_BRIDGE_TOKEN", TOKEN)
    monkeypatch.setenv("VOCOCO_BRAIN_AGENT_ID", brain.AGENT_ID)
    monkeypatch.setattr(task_runner, "_maybe_start_next", Mock())
    for aid in (brain.AGENT_ID, "abcdef1234"):
        workdir = isolated / aid
        workdir.mkdir()
        agents._write_meta(aid, {"name": aid, "workdir": str(workdir), "permission": "full",
                                 "disallowed_tools": ["WebFetch"], "execution_mode": "project"})
    yield isolated
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None


@pytest.fixture
async def client(bridge_db):
    app = web.Application()
    task_routes.register_routes(app)
    async with TestClient(TestServer(app)) as api:
        yield api


async def post(client, operation, payload, status=200, headers=None):
    response = await client.post(f"/internal/brain/{operation}", json=payload,
                                 headers=AUTH if headers is None else headers)
    assert response.status == status, await response.text()
    return await response.json()


async def create(client, **changes):
    return await post(client, "create_task", {**CREATE, **changes})


def session(aid=brain.AGENT_ID):
    return agents.main_session_key(agents.get(aid))


def turn(user="问题", assistant="回复", key=None):
    key = key or session()
    tid = session_store.start_turn(key, user)
    session_store.finish_turn(tid, assistant)
    return tid


async def test_search_history_filters_agents_paginates_and_cleans(client, monkeypatch):
    own = turn("关键词 100%", "链接 https://example.com/private", session())
    other = turn("关键词 other", "回复", session("abcdef1234"))
    first = await post(client, "search_history", {"query": "关键词", "limit": 1})
    assert first["results"][0]["turn_id"] == other
    second = await post(client, "search_history", {"query": "关键词", "before_id": first["next_before_id"]})
    assert second["results"][0]["turn_id"] == own
    assert "https://" not in second["results"][0]["assistant_text"]
    assert len((await post(client, "search_history", {"query": "100%"}))["results"]) == 1
    assert not (await post(client, "search_history", {"query": "100_"}))["results"]
    turn("ß" * 200 + "独特检索词", "", session())
    snippet = (await post(client, "search_history", {"query": "独特检索词"}))["results"][0]["user_text"]
    assert "独特检索词" in snippet
    turn("token=confidential-needle", "", session())
    assert not (await post(client, "search_history", {"query": "confidential-needle"}))["results"]
    monkeypatch.setenv(brain.READABLE_AGENTS, brain.AGENT_ID)
    hidden = await post(client, "search_history", {"query": "other"})
    assert hidden["results"] == [] and hidden["next_before_id"] is None
    assert [r["turn_id"] for r in (await post(client, "search_history", {"query": "关键词"}))["results"]] == [own]
    await post(client, "search_history", {"query": "关键词", "agent_id": "abcdef1234"}, 404)
    await post(client, "search_history", {"query": " "}, 400)
    await post(client, "search_history", {"query": "x", "limit": 21}, 400)


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer web-token"},
                                     {"Authorization": "bearer " + TOKEN}])
async def test_bridge_token_required_even_with_web_auth(client, headers, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "web-token")
    await post(client, "create_task", CREATE, 401, headers)
    assert not tasks.list_queued()


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded", "CF-Connecting-IP",
    "Origin", "Via", "X-Real-IP", "True-Client-IP", "X-Forwarded-Host", "CF-Ray",
    "X-Proxy-ID", "X-Original-URL", "X-Rewrite-URL"])
async def test_proxy_headers_rejected_even_empty(client, header):
    await post(client, "create_task", CREATE, 403, {**AUTH, header: ""})
    assert not tasks.list_queued()


async def test_remote_peer_fails_closed(bridge_db):
    request = Mock(headers=AUTH)
    request.transport.get_extra_info.return_value = ("192.0.2.4", 4321)
    with pytest.raises(web.HTTPForbidden):
        brain._authenticate(request)
    request.transport = None
    with pytest.raises(web.HTTPForbidden):
        brain._authenticate(request)


@pytest.mark.parametrize("token", ["", "x" * 31])
async def test_weak_server_token_disabled(client, monkeypatch, token):
    monkeypatch.setenv("VOCOCO_BRAIN_BRIDGE_TOKEN", token)
    await post(client, "create_task", CREATE, 503)


async def test_agent_configuration_cannot_target_another_agent(client, monkeypatch):
    monkeypatch.setenv("VOCOCO_BRAIN_AGENT_ID", "abcdef1234")
    await post(client, "create_task", CREATE, 503)
    monkeypatch.delenv("VOCOCO_BRAIN_AGENT_ID")
    assert (await create(client))["status"] == "queued"


@pytest.mark.parametrize("field", ["cwd", "source", "session", "permissions", "agent_id",
                                   "source_access", "dispatch_chat_id", "model"])
async def test_client_cannot_supply_trusted_fields(client, field):
    await post(client, "create_task", {**CREATE, field: "spoofed"}, 400)


@pytest.mark.parametrize("changes", [
    {"title": "a" * 101}, {"title": " "}, {"prompt": "a" * 60001}, {"prompt": 123},
    {"idempotency_key": "short"}, {"idempotency_key": "a" * 129},
    {"idempotency_key": "非ASCII字符长度"}, {"idempotency_key": "../bad key"},
    {"title": "\ud800"},
])
async def test_create_validates_lengths_and_types(client, changes):
    await post(client, "create_task", {**CREATE, **changes}, 400)


async def test_json_contract_rejects_duplicates_query_and_nonobjects(client):
    for body in ('[]', 'null', '{"title":"a","title":"b"}', '{bad'):
        response = await client.post("/internal/brain/create_task", data=body,
                                     headers={**AUTH, "Content-Type": "application/json"})
        assert response.status == 400
    response = await client.post("/internal/brain/create_task?cwd=bad", json=CREATE, headers=AUTH)
    assert response.status == 400
    response = await client.post("/internal/brain/create_task", data="text", headers=AUTH)
    assert response.status == 400
    await post(client, "not_defined", {}, 404)


async def test_create_maximum_unicode_and_runtime_access(client):
    result = await create(client, title="题" * 100, prompt="答" * 60000, idempotency_key="x" * 128)
    assert set(result) == PUBLIC
    row = tasks.get(result["id"])
    agent = agents.get(brain.AGENT_ID)
    assert (row["agent_id"], row["cwd"], row["origin"]) == (brain.AGENT_ID, agent["workdir"], "chat")
    assert row["dispatch_platform"] == "web" and row["dispatch_chat_id"] == agent["main_conv"]
    access = tasks.automatic_access(row)
    assert access["mode"] == "standard" and access["remote_brain"] is True
    assert set(brain.DENIED + ["WebFetch"]) <= set(access["denied"])
    assert row["source_session_key"] == session() == access["session"]
    assert session_store._conn().execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
    tid = session_store.start_turn(tasks.session_key(row["id"]), row["prompt"], source_access=access)
    assert session_store.active_source_access(tasks.session_key(row["id"])) == access
    assert permissions.state(tasks.session_key(row["id"]))["mode"] == "standard"
    session_store.finish_turn(tid, "完成")
    task_runner._maybe_start_next.assert_called_once()
    visible = tasks.list_recent_for_source(source="conversation", dispatch_chat_id=agent["main_conv"])
    assert row["id"] in {item["id"] for item in visible}


async def test_idempotency_concurrent_conflict_and_persistence(client):
    results = await asyncio.gather(*[create(client) for _ in range(6)])
    assert len({item["id"] for item in results}) == 1
    assert len(tasks.list_queued()) == 1
    await post(client, "create_task", {**CREATE, "prompt": "不同内容"}, 409)
    tasks._DB.close()
    tasks._DB = None
    assert (await create(client))["id"] == results[0]["id"]
    assert len(tasks.list_queued()) == 1


async def test_task_and_idempotency_record_rollback_together(client, monkeypatch):
    original = tasks.create
    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("注入事务失败")
    monkeypatch.setattr(tasks, "create", fail_after_insert)
    with pytest.raises(RuntimeError):
        brain._create_atomic(CREATE, agents.get(brain.AGENT_ID))
    assert tasks._conn().execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert tasks._conn().execute("SELECT COUNT(*) FROM brain_bridge_tasks").fetchone()[0] == 0
    monkeypatch.setattr(tasks, "create", original)
    assert (await create(client))["status"] == "queued"


@pytest.mark.parametrize("operation", ["get_task", "cancel_task"])
async def test_foreign_and_nonbridge_tasks_are_not_accessible(client, operation):
    local = tasks.create("本地", "提示词", agent_id=brain.AGENT_ID, origin="chat")
    await post(client, operation, {"id": local["id"]}, 404)
    task = await create(client)
    connection = tasks._conn()
    for update, values in [
        ("agent_id=?", ("abcdef1234",)), ("origin=?", ("voice",)),
        ("source_session_key=?", ("web:other",)), ("source_access=?", ("{}",)),
    ]:
        with connection:
            connection.execute(f"UPDATE tasks SET {update} WHERE id=?", (*values, task["id"]))
        await post(client, operation, {"id": task["id"]}, 404)
        with connection:
            connection.execute("UPDATE tasks SET agent_id=?,origin='chat',source_session_key=?,source_access=? WHERE id=?",
                               (brain.AGENT_ID, session(), json.dumps(brain._access(agents.get(brain.AGENT_ID))), task["id"]))


async def test_cancel_acknowledges_request_not_completion(client, monkeypatch):
    task = await create(client)
    tasks.set_status(task["id"], "running")
    monkeypatch.setattr(task_runner, "cancel", Mock(return_value=True))
    result = await post(client, "cancel_task", {"id": task["id"]})
    assert result["cancel_requested"] is True and result["status"] == "running"
    assert (await post(client, "get_task", {"id": task["id"]}))["cancel_requested"] is True
    monkeypatch.setattr(task_runner, "cancel", Mock(return_value=False))
    assert (await post(client, "cancel_task", {"id": task["id"]}))["status"] == "running"


async def test_cancel_queued_task_reaches_terminal_state(client):
    task = await create(client)
    result = await post(client, "cancel_task", {"id": task["id"]})
    assert result["status"] == "cancelled" and result["cancel_requested"] is True


async def test_task_result_pages_rebuild_sanitized_text(client):
    task = await create(client)
    text = "第一段。\n" * 4000
    tasks.set_status(task["id"], "running")
    tasks.finish(task["id"], "done", text, "摘要")
    rebuilt, offset = "", 0
    while True:
        page = await post(client, "get_task", {"id": task["id"], "offset": offset, "length": 7999})
        assert set(page) == PUBLIC and len(page["result"]) <= 7999
        rebuilt += page["result"]
        offset = page["next_offset"]
        if offset is None:
            break
    assert rebuilt == text


async def test_history_owner_checked_on_every_read_and_list(client, monkeypatch):
    own_id, foreign_id = turn(), turn(key=session("abcdef1234"))
    legacy = await post(client, "list_sessions", {})
    assert [row["agent_id"] for row in legacy["sessions"]] == [brain.AGENT_ID]
    result = await post(client, "list_sessions", {"all_agents": True})
    # 显式跨 Agent 枚举时只返回本连接可见的会话,每条带所属 agent_id
    assert result["sessions"] == [
        {"agent_id": "abcdef1234", "agent_name": "abcdef1234", "key": session("abcdef1234"),
         "last_turn_id": foreign_id, "title": "新对话"},
        {"agent_id": brain.AGENT_ID, "agent_name": brain.AGENT_ID, "key": session(),
         "last_turn_id": own_id, "title": "新对话"},
    ]
    assert "非快照" in result["pagination"]
    assert (await post(client, "list_sessions", {"agent_id": "abcdef1234"}))["sessions"] == [
        {"agent_id": "abcdef1234", "agent_name": "abcdef1234", "key": session("abcdef1234"),
         "last_turn_id": foreign_id, "title": "新对话"}]
    await post(client, "read_session", {"key": session("abcdef1234")}, 404)
    read = await post(client, "read_session", {"key": session("abcdef1234"), "agent_id": "abcdef1234"})
    assert read["turn"]["id"] == foreign_id
    assert (await post(client, "read_session", {"key": session(), "turn_id": foreign_id}))["turn"] is None
    # 逐条按 key 复核归属能挡住伪造;但只覆盖 _history_owner,绕得过批量索引——
    # 批量索引与逐条判定同源,所以这里只验证读接口,列表仍以索引为准
    monkeypatch.setattr(agents, "agent_for_session", lambda key: {"id": "abcdef1234"})
    await post(client, "read_session", {"key": session(), "agent_id": brain.AGENT_ID, "turn_id": own_id}, 404)


async def test_list_agents_groups_with_session_count_and_query(client):
    turn()
    turn(key=session("abcdef1234").replace(":main", ":conv1"))
    turn(key=session("abcdef1234").replace(":main", ":conv2"))
    first = await post(client, "list_agents", {})
    counts = {item["agent_id"]: item["session_count"] for item in first["agents"]}
    assert counts == {brain.AGENT_ID: 1, "abcdef1234": 2, "general": 0}
    # query 按名称/agent_id 模糊筛,不区分大小写
    assert [item["agent_id"] for item in (await post(client, "list_agents", {"query": "ABCDEF"}))["agents"]] == [
        "abcdef1234"]
    assert (await post(client, "list_agents", {"query": "不存在的名字"}))["agents"] == []
    # 分页照旧
    assert (await post(client, "list_agents", {"limit": 1}))["next_offset"] == 1


async def test_list_sessions_filter_by_query_and_agent_name(client):
    turn()
    session_store.set_title(session(), "面料获客调研")
    turn(key=session("abcdef1234"))
    session_store.set_title(session("abcdef1234"), "玩具网站首站")
    # 只命中标题 → 逐条筛,其它 Agent 的会话不混进来
    only = await post(client, "list_sessions", {"all_agents": True, "query": "面料"})
    assert [row["key"] for row in only["sessions"]] == [session()]
    # 命中 Agent 名称 → 整组保留该 Agent 的会话,其余 Agent 排除
    by_name = await post(client, "list_sessions", {"all_agents": True, "query": "abcdef"})
    assert [row["agent_id"] for row in by_name["sessions"]] == ["abcdef1234"]
    assert (await post(client, "list_sessions", {"all_agents": True, "query": "nope"}))["sessions"] == []
    # 与 agent_id 叠加时先按 Agent 再按关键词
    mixed = await post(client, "list_sessions", {"agent_id": "abcdef1234", "query": "面料"})
    assert mixed["sessions"] == []


async def test_list_sessions_task_owner_uses_index_without_rescan(client, monkeypatch):
    from vococo.cron import scheduler

    calls = []
    real = agents.agent_for_session
    monkeypatch.setattr(agents, "agent_for_session", lambda key: calls.append(key) or real(key))
    monkeypatch.setattr(scheduler, "load_jobs", lambda: [{"id": "job00001", "agent_id": brain.AGENT_ID}])
    turn(key="task:job00001")
    rows = (await post(client, "list_sessions", {}))["sessions"]
    assert [row["key"] for row in rows] == ["task:job00001"]
    # 定时任务会话的归属走一次性索引,不再逐条重扫磁盘
    assert "task:job00001" not in calls


async def test_history_session_list_limit_offset(client):
    root = session()
    for index in range(23):
        turn(key=root.replace(":main", f":conv{index}"))
    first = await post(client, "list_sessions", {"limit": 20})
    second = await post(client, "list_sessions", {"offset": first["next_offset"]})
    assert len(first["sessions"]) == 20 and len(second["sessions"]) == 3
    assert len({item["key"] for item in first["sessions"] + second["sessions"]}) == 23
    assert second["next_offset"] is None


async def test_history_pages_rebuild_roles_and_include_precompaction_turns(client):
    old = turn("旧问题", "旧答案")
    user, assistant = "问。" * 6000, "答！" * 10000
    current = turn(user, assistant)
    connection = session_store._conn()
    connection.execute("INSERT OR REPLACE INTO session_meta(session_key,watermark_id) VALUES (?,?)", (session(), current))
    connection.commit()
    page = await post(client, "read_session", {"key": session()})
    rebuilt_user, rebuilt_assistant = "", ""
    while True:
        part = page["turn"]
        assert part["id"] == current and len(part["user_text"]) + len(part["assistant_text"]) <= 8000
        rebuilt_user += part["user_text"]
        rebuilt_assistant += part["assistant_text"]
        if page["next_offset"] is None:
            break
        page = await post(client, "read_session", {"key": session(), "turn_id": current, "offset": page["next_offset"]})
    assert (rebuilt_user, rebuilt_assistant) == (user, assistant)
    previous = await post(client, "read_session", {"key": session(), "before_id": page["next_before_id"]})
    assert previous["turn"]["id"] == old and previous["turn"]["assistant_text"] == "旧答案"
    assert (await post(client, "read_session", {"key": session(), "before_id": old}))["turn"] is None


async def test_secrets_attachments_urls_base64_are_not_returned(client, monkeypatch):
    monkeypatch.setattr(danger, "_known_secret_values", lambda: ["known-private-value"])
    raw = ('普通内容\nknown-private-value\napi_key: unknown-value\n{"client_secret": "hidden"}\n'
           'file:///private/report.pdf https://host/files/ticket?token=private\n'
           '/artifacts/opaque/download?ticket=opaque /image?name=private-image\n'
           'data:application/pdf;base64,' + 'QUJD' * 100 + '\n' + 'QUJD' * 100 + '\n'
           + ('YWJj' * 19 + '\n') * 3 + TOKEN)
    tid = turn(raw, raw)
    result = await post(client, "read_session", {"key": session(), "turn_id": tid})
    encoded = json.dumps(result, ensure_ascii=False)
    for secret in ["known-private-value", "unknown-value", "hidden", "file:///", "https://",
                   "QUJD", "YWJj", "opaque", "private-image", TOKEN]:
        assert secret not in encoded
    assert "普通内容" in encoded
    task = await create(client)
    tasks.set_status(task["id"], "running")
    tasks.finish(task["id"], "done", raw, "摘要")
    result = await post(client, "get_task", {"id": task["id"]})
    assert "unknown-value" not in result["result"] and "https://" not in result["result"]


@pytest.mark.parametrize("operation,payload", [
    ("get_task", {"id": "x", "length": 8001}),
    ("list_sessions", {"limit": 101}), ("list_sessions", {"offset": -1}),
    ("list_agents", {"limit": 0}), ("list_agents", {"offset": True}),
    ("list_sessions", {"agent_id": 123}),
    ("list_sessions", {"limit": True}),
    ("read_session", {"length": 8001}), ("read_session", {"offset": 1}),
    ("read_session", {"turn_id": 1, "before_id": 2}),
    ("read_session", {"turn_id": False}), ("read_session", {"offset": -1}),
])
async def test_paging_rejects_out_of_contract_values(client, operation, payload):
    if operation == "get_task":
        payload = {**payload, "id": (await create(client))["id"]}
    if operation == "read_session":
        payload = {"key": session(), **payload}
    await post(client, operation, payload, 400)


@pytest.mark.parametrize("restrict", [False, True])
async def test_remote_approval_precedes_full_and_permanent_rules(client, monkeypatch, restrict):
    task = await create(client)
    key = tasks.session_key(task["id"])
    monkeypatch.setattr(permissions, "auto_allows", Mock(return_value=True))
    monkeypatch.setattr(approvals, "match_rule", Mock(return_value={"id": "permanent"}))
    background = AsyncMock(return_value=False)
    monkeypatch.setattr(danger, "_approve_background", background)
    token = danger.set_task_session(key)
    try:
        assert not await danger._approve("写文件", "细节", restrict, tool="Write", rule=("dir", "/", "全部"))
    finally:
        danger.reset_task_session(token)
    permissions.auto_allows.assert_not_called()
    approvals.match_rule.assert_not_called()
    assert background.call_args.args[3:5] == (None, key)


async def test_remote_approval_uses_turn_access_and_fails_closed(client, monkeypatch):
    key = session()
    session_store.start_turn(key, "远程轮次", source_access={"mode": "standard", "remote_brain": True})
    monkeypatch.setattr(danger, "_approve_background", AsyncMock(side_effect=RuntimeError("故障")))
    monkeypatch.setattr(permissions, "auto_allows", Mock(return_value=True))
    token = danger.set_task_session(key)
    try:
        assert not await danger._approve("操作", "细节", False)
        monkeypatch.setattr(session_store, "active_source_access", Mock(side_effect=RuntimeError("数据库故障")))
        assert not await danger._approve("操作", "细节", False)
    finally:
        danger.reset_task_session(token)
    permissions.auto_allows.assert_not_called()


async def test_history_never_loads_raw_events_or_attachments(client, monkeypatch):
    tid = session_store.start_turn(session(), "问题")
    session_store.append_artifact(session(), tid, "private.txt", "私密附件", b"private-content")
    session_store.finish_turn(tid, "纯文本", events=[{"type": "tool", "secret": "raw-private"}])
    monkeypatch.setattr(session_store, "load_history", Mock(side_effect=AssertionError("禁止原始历史")))
    result = await post(client, "read_session", {"key": session(), "turn_id": tid})
    assert set(result["turn"]) == {"id", "ts", "user_text", "assistant_text"}
    assert result["turn"]["assistant_text"] == "纯文本"
    assert "private" not in json.dumps(result)
    session_store.load_history.assert_not_called()


async def test_remote_without_background_adapter_is_denied(client, monkeypatch):
    from vococo.gateway import clarify
    task = await create(client)
    monkeypatch.setattr(clarify, "background_adapter", lambda: None)
    monkeypatch.setattr(danger, "_record_deferred", Mock())
    token = danger.set_task_session(tasks.session_key(task["id"]))
    try:
        assert not await danger._approve("本地操作", "细节", False)
    finally:
        danger.reset_task_session(token)
    danger._record_deferred.assert_called_once()
