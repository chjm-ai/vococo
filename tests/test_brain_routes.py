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


async def test_continue_owned_task_once_and_reject_busy(client, monkeypatch):
    created = await create(client)
    task_id = created["id"]
    payload = {"id": task_id, "instruction": "继续处理第二个需求", "idempotency_key": "continue-0001"}
    start = Mock(return_value=True)
    monkeypatch.setattr(task_runner, "start_claimed", start)
    await post(client, "continue_task", payload, 409)
    start.assert_not_called()
    assert tasks.set_status(task_id, "running")
    assert tasks.set_status(task_id, "done")
    await post(client, "continue_task", payload, 409)  # 无法恢复上下文时不假装续接
    session_store.set_sdk_session_id(tasks.session_key(task_id), "sdk-test-session")
    result = await post(client, "continue_task", payload)
    assert result["id"] == task_id and result["continuation"] == "accepted"
    assert start.call_count == 1 and start.call_args.args[1] == payload["instruction"]
    await post(client, "continue_task", payload)
    assert start.call_count == 1
    await post(client, "continue_task", {**payload, "instruction": "不同需求"}, 409)
    await post(client, "continue_task", {**payload, "id": "not-owned"}, 404)


async def test_continue_legacy_task_id_keeps_get_task_contract(client, monkeypatch):
    task_id = (await create(client))["id"]
    legacy_key = f"task:{task_id}"
    tasks._conn().execute("UPDATE tasks SET session_key=? WHERE id=?", (legacy_key, task_id))
    tasks._conn().commit()
    session_store.append(legacy_key, "旧任务问题", "旧任务结果")
    session_store.set_sdk_session_id(legacy_key, "legacy-sdk")
    assert tasks.set_status(task_id, "running") and tasks.set_status(task_id, "done")
    start = Mock()
    monkeypatch.setattr(task_runner, "start_claimed", start)
    result = await post(client, "continue_task", {"id": task_id, "instruction": "补充",
                                                "idempotency_key": "legacy-contract-01"})
    assert result["id"] == task_id and start.call_count == 1
    assert (await post(client, "get_task", {"id": result["id"]}))["status"] == "running"


async def test_continue_respects_capacity_and_hides_old_result(client, monkeypatch):
    task_id = (await create(client))["id"]
    assert tasks.set_status(task_id, "running")
    assert tasks.finish(task_id, "done", "上一轮结果", "上一轮结果")
    session_store.set_sdk_session_id(tasks.session_key(task_id), "sdk-test-session")
    start = Mock(return_value=True)
    monkeypatch.setattr(task_runner, "start_claimed", start)
    monkeypatch.setattr(tasks, "count_running", lambda: config.TASK_MAX_CONCURRENCY)
    payload = {"id": task_id, "instruction": "继续", "idempotency_key": "capacity-0001"}
    await post(client, "continue_task", payload, 409)
    start.assert_not_called()
    assert tasks.set_status(task_id, "running")
    assert (await post(client, "get_task", {"id": task_id}))["result"] == ""


async def test_cancelled_continuation_does_not_return_old_result(client, monkeypatch):
    task_id = (await create(client))["id"]
    assert tasks.set_status(task_id, "running")
    assert tasks.finish(task_id, "done", "上一轮结果", "上一轮结果")
    session_store.set_sdk_session_id(tasks.session_key(task_id), "sdk-test-session")
    monkeypatch.setattr(task_runner, "start_claimed", Mock())
    await post(client, "continue_task", {
        "id": task_id, "instruction": "继续", "idempotency_key": "cancel-old-result-1"})
    assert tasks.set_status(task_id, "cancelled")
    result = await post(client, "get_task", {"id": task_id})
    assert result["status"] == "cancelled" and result["result"] == ""


async def test_continue_starts_same_task_with_original_sdk_session(client, monkeypatch):
    task_id = (await create(client))["id"]
    assert tasks.set_status(task_id, "running") and tasks.set_status(task_id, "done")
    session_store.set_sdk_session_id(tasks.session_key(task_id), "sdk-test-session")
    spawn = Mock(return_value=object())
    monkeypatch.setattr(task_runner, "_running", {})
    monkeypatch.setattr(task_runner, "_spawn", spawn)
    monkeypatch.setattr(task_runner, "_notify_activity", Mock())
    result = await post(client, "continue_task", {
        "id": task_id, "instruction": "第二个需求", "idempotency_key": "continue-real-01"})
    assert result["id"] == task_id and result["status"] == "running"
    assert session_store.get_sdk_session_id(tasks.session_key(task_id)) == "sdk-test-session"
    spawn.assert_called_once_with(task_id, "第二个需求")
    await post(client, "continue_task", {
        "id": task_id, "instruction": "第三个需求", "idempotency_key": "continue-real-02"}, 409)
    spawn.assert_called_once()


async def test_continue_runs_with_original_sdk_session(client, monkeypatch):
    from vococo.core.agent import AgentReply, Done
    from vococo import providers

    task_id = (await create(client))["id"]
    assert tasks.set_status(task_id, "running") and tasks.set_status(task_id, "done")
    session_store.set_sdk_session_id(tasks.session_key(task_id), "original-sdk-session")
    calls = []

    async def execute(history, prompt, **kwargs):
        calls.append((history, prompt, kwargs))
        yield Done(AgentReply(text="续接结果", tool_calls=[], cost_usd=None, is_error=False))

    async def cwd(root, task_id):
        return root

    monkeypatch.setattr(task_runner, "stream_turn", execute)
    monkeypatch.setattr(task_runner.worktree, "execution_cwd_for_task", cwd)
    monkeypatch.setattr(providers, "sidecar_env", lambda *a, **k: None)
    monkeypatch.setattr(task_runner, "_running", {})
    result = await post(client, "continue_task", {
        "id": task_id, "instruction": "第二个需求", "idempotency_key": "continue-run-01"})
    assert result["id"] == task_id
    running = task_runner._running.get(task_id)
    if running is not None:
        await asyncio.wait_for(running, timeout=10)
    assert tasks.get(task_id)["status"] == "done"
    assert calls[0][1].startswith("第二个需求")
    assert calls[0][2]["resume"] == "original-sdk-session"
    assert calls[0][2]["require_resume"] is True


async def test_continue_unknown_outcome_cannot_run_twice(client, monkeypatch):
    task_id = (await create(client))["id"]
    assert tasks.set_status(task_id, "running") and tasks.set_status(task_id, "done")
    session_store.set_sdk_session_id(tasks.session_key(task_id), "sdk-test-session")
    monkeypatch.setattr(session_store, "set_conv_archived", Mock(side_effect=RuntimeError("启动时异常")))
    payload = {"id": task_id, "instruction": "继续", "idempotency_key": "continue-0002"}
    response = await client.post("/internal/brain/continue_task", json=payload, headers=AUTH)
    assert response.status == 500
    result = await post(client, "continue_task", payload)
    assert result["continuation"] == "accepted" and result["status"] == "failed"
    assert tasks.get(task_id)["status"] == "failed"


async def test_continue_failure_after_start_never_repeats(client, monkeypatch):
    task_id = (await create(client))["id"]
    assert tasks.set_status(task_id, "running") and tasks.set_status(task_id, "done")
    session_store.set_sdk_session_id(tasks.session_key(task_id), "sdk-test-session")

    def start_then_fail(task, instruction):
        assert tasks.get(task["id"])["status"] == "running"
        raise RuntimeError("提交后异常")

    start = Mock(side_effect=start_then_fail)
    monkeypatch.setattr(task_runner, "start_claimed", start)
    payload = {"id": task_id, "instruction": "继续", "idempotency_key": "after-start-01"}
    response = await client.post("/internal/brain/continue_task", json=payload, headers=AUTH)
    assert response.status == 500
    assert (await post(client, "continue_task", payload))["status"] == "running"
    start.assert_called_once()


async def test_old_continuation_key_does_not_return_new_round(client, monkeypatch):
    task_id = (await create(client))["id"]
    assert tasks.set_status(task_id, "running") and tasks.set_status(task_id, "done")
    session_store.set_sdk_session_id(tasks.session_key(task_id), "sdk-test-session")
    monkeypatch.setattr(task_runner, "start_claimed", Mock())
    first = {"id": task_id, "instruction": "第一轮续接", "idempotency_key": "old-round-01"}
    assert (await post(client, "continue_task", first))["continuation_instruction"] == "第一轮续接"
    assert tasks.finish(task_id, "done", "第一轮结果", "第一轮结果")
    second = {**first, "instruction": "第二轮续接", "idempotency_key": "new-round-01"}
    await post(client, "continue_task", second)
    await post(client, "continue_task", first, 409)
    row = brain._registry().execute(
        "SELECT instruction FROM brain_bridge_continuations WHERE idempotency_key=?", (first["idempotency_key"],)
    ).fetchone()
    assert row["instruction"] == "第一轮续接"


async def test_continue_reservation_rolls_back_if_claim_fails(client, monkeypatch):
    task_id = (await create(client))["id"]
    assert tasks.set_status(task_id, "running") and tasks.set_status(task_id, "done")
    session_store.set_sdk_session_id(tasks.session_key(task_id), "sdk-test-session")
    payload = {"id": task_id, "instruction": "继续", "idempotency_key": "claim-fail-01"}
    observed = []

    def fail_after_reservation():
        observed.append(bool(brain._registry().execute(
            "SELECT 1 FROM brain_bridge_continuations WHERE idempotency_key=?", (payload["idempotency_key"],)
        ).fetchone()))
        raise RuntimeError("事务中失败")

    monkeypatch.setattr(tasks, "count_running", fail_after_reservation)
    response = await client.post("/internal/brain/continue_task", json=payload, headers=AUTH)
    assert response.status == 500
    assert observed == [True]
    assert brain._registry().execute(
        "SELECT 1 FROM brain_bridge_continuations WHERE idempotency_key=?", (payload["idempotency_key"],)
    ).fetchone() is None
    assert tasks.get(task_id)["status"] == "done"


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
    default = await post(client, "list_sessions", {})
    assert {row["agent_id"] for row in default["sessions"]} == {brain.AGENT_ID, "abcdef1234"}
    legacy = await post(client, "list_sessions", {"all_agents": False})
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


# ---- 历史会话通用续接(不新建平行会话) ----

def history_session(aid=brain.AGENT_ID, conv="mvt8qtgdps2j", user="研究进度", assistant="已完成一半"):
    from vococo.memory import projects

    workdir = agents.get(aid)["workdir"]
    phash = projects.project_hash(workdir)
    key = f"web:p{phash}:{conv}"
    tid = session_store.start_turn(key, user)
    session_store.finish_turn(tid, assistant)
    return key, phash


def fake_web(monkeypatch):
    from vococo.gateway import web_bridge

    from vococo.tools import sessions

    calls = []

    def deliver(key, text, **kw):
        calls.append((key, text))

    monkeypatch.setattr(web_bridge, "_inject_key_fn", deliver)
    sessions._queued.clear()
    return calls


async def test_continue_history_session_in_place_once(client, monkeypatch):
    key, phash = history_session()
    delivery = fake_web(monkeypatch)
    payload = {"id": key, "instruction": "只确认收到，不要改研究计划", "idempotency_key": "hist-session-01"}
    first = await post(client, "continue_task", payload)
    assert (first["id"], first["continuation"]) == (key, "accepted") and first["agent_id"] == brain.AGENT_ID
    assert delivery == [(key, delivery[0][1])] and delivery[0][1].endswith(payload["instruction"])
    assert "跨会话指令" in delivery[0][1]
    # 同键重放不再投递,也不报错
    assert (await post(client, "continue_task", payload))["continuation"] == "accepted"
    assert len(delivery) == 1
    await post(client, "continue_task", {**payload, "instruction": "换内容"}, 409)
    # 目标上一轮尚未收尾时拒绝叠加第二条,不打断、不排队
    await post(client, "continue_task", {**payload, "idempotency_key": "hist-session-02",
                                         "instruction": "再发一次"}, 409)
    assert len(delivery) == 1


@pytest.mark.parametrize("project_prefix", [False, True])
async def test_continue_history_session_accepts_bare_and_prefix_keys(client, monkeypatch, project_prefix):
    key, phash = history_session()
    helper_task_id = (await create(client))["id"]
    delivery = fake_web(monkeypatch)
    target = key.removeprefix("web:") if project_prefix else "mvt8qtgdps2j"
    await post(client, "continue_task", {"id": target, "instruction": "确认",
                                         "idempotency_key": "hist-bare-0001"})
    # 绑定到该 Agent 的统一会话只写裸 slug,也应能按整 key/后缀解析到
    bare_key = "mvt8qtgdps2j-unify"
    session_store.bind_session(f"web:{bare_key}", agent_id=brain.AGENT_ID)
    tid = session_store.start_turn(f"web:{bare_key}", "统一会话")
    session_store.finish_turn(tid, "在")
    await post(client, "continue_task", {"id": bare_key, "instruction": "继续",
                                         "idempotency_key": "hist-bare-0002"})
    assert delivery[1][0] == f"web:{bare_key}"
    # 会话归哪条任务由 task: id 查任务表决定:任务结束后该会话可经历史会话链路续接。
    task_key = tasks.session_key(helper_task_id)
    from vococo.tools import sessions

    sessions.dispatch_finished(key, delivery[0][1])
    # 派出的任务还没被 worker 领走(queued):task: 会话按任务闸拒绝,不插话
    await post(client, "continue_task", {"id": task_key, "instruction": "补充",
                                         "idempotency_key": "hist-task-0000"}, 409)
    tasks._conn().execute("UPDATE tasks SET status='done' WHERE id=?", (helper_task_id,))
    tasks._conn().commit()
    tid = session_store.start_turn(task_key, "后台任务")
    session_store.finish_turn(tid, "完成")
    await post(client, "continue_task", {"id": task_key, "instruction": "补充",
                                         "idempotency_key": "hist-task-0001"})
    assert delivery[2][0] == task_key


async def test_continue_history_session_scope_and_validation(client, monkeypatch):
    key, _ = history_session()
    delivery = fake_web(monkeypatch)
    await post(client, "continue_task", {"id": key, "instruction": "x", "idempotency_key": "scope-0000001",
                                         "agent_id": "abcdef1234"}, 404)  # 不是该 Agent 的会话
    await post(client, "continue_task", {"id": "web:nope", "instruction": "x", "idempotency_key": "scope-0000002"}, 404)
    await post(client, "continue_task", {"id": "main", "instruction": "x", "idempotency_key": "scope-0000003"}, 400)
    await post(client, "continue_task", {"id": "web:a b", "instruction": "x", "idempotency_key": "scope-0000004"}, 400)
    await post(client, "continue_task", {"id": key, "instruction": "x", "idempotency_key": "scope-0000005",
                                         "agent_id": "nobody"}, 404)
    assert delivery == []


async def test_continue_history_session_rejects_busy_without_sending(client, monkeypatch):
    from vococo.gateway import clarify

    key, _ = history_session()
    delivery = fake_web(monkeypatch)
    monkeypatch.setattr(clarify, "_active_sessions", {key})
    assert clarify.has_pending(key) is False
    payload = {"id": key, "instruction": "不要在生成时插话", "idempotency_key": "busy-00000001"}
    await post(client, "continue_task", payload, 409)
    assert delivery == []
    # 忙碌不算已受理:同一个键可以在空闲后原样重试
    clarify._active_sessions.discard(key)
    await post(client, "continue_task", payload)
    assert len(delivery) == 1


async def test_continue_history_preserves_remote_permissions(client, monkeypatch):
    from vococo.gateway import web_bridge
    from vococo.core import permissions

    other = agents.create("研究协调")
    key, _ = history_session(other["id"])
    session_store.set_permission(key, "full")
    deliveries = []
    monkeypatch.setattr(web_bridge, "_inject_key_fn", lambda k, text, **kw: deliveries.append((k, text, kw)))
    payload = {"id": key, "agent_id": other["id"], "instruction": "只确认收到",
               "idempotency_key": "remote-access-01"}
    await post(client, "continue_task", payload)
    access = deliveries[0][2]["source_access"]
    assert access["remote_brain"] is True and access["mode"] == "standard"
    assert set(brain.DENIED) <= set(access["denied"])
    assert access["session"] == agents.main_session_key(agents.get(brain.AGENT_ID))
    assert access["capabilities"] == {"skills": [], "mcp": []}
    turn = session_store.start_turn(key, deliveries[0][1], source_access=access)
    assert permissions.state(key)["mode"] == "standard"
    assert (await post(client, "continue_task", payload))["continuation"] == "accepted"
    session_store.finish_turn(turn, "已收到")
    assert len(deliveries) == 1


@pytest.mark.parametrize("state", ["locked", "inbox", "scheduled"])
async def test_continue_refuses_gateway_preparation_and_queued_messages(client, monkeypatch, state):
    from vococo.gateway import web_bridge
    from vococo.gateway.adapters.web import WebAdapter
    from vococo.gateway.run import GatewayRunner

    key, _ = history_session()
    adapter = WebAdapter()
    monkeypatch.setattr(adapter, "_emit", lambda _: None)
    runner = GatewayRunner([adapter])
    monkeypatch.setattr(web_bridge, "_inject_key_fn", adapter.inject_by_key)
    monkeypatch.setattr(web_bridge, "_busy_fn", runner.session_busy)
    lock = runner._lock_for(key)
    if state == "locked":
        await lock.acquire()
    elif state == "inbox":
        adapter.inject_by_key(key, "真人消息尚未处理")
    else:
        runner._pending_dispatch[key] = 1
    try:
        before = adapter._inbox.qsize()
        await post(client, "continue_task", {"id": key, "instruction": "不可插话",
                                            "idempotency_key": "preparation-busy-01"}, 409)
        assert adapter._inbox.qsize() == before
    finally:
        if lock.locked():
            lock.release()


async def test_continue_history_replay_while_busy_is_not_resent(client, monkeypatch):
    from vococo.gateway import clarify

    key, _ = history_session()
    delivery = fake_web(monkeypatch)
    payload = {"id": key, "instruction": "确认", "idempotency_key": "replay-busy-01"}
    await post(client, "continue_task", payload)
    monkeypatch.setattr(clarify, "_active_sessions", {key: "正在执行"})
    assert (await post(client, "continue_task", payload))["continuation"] == "accepted"
    assert len(delivery) == 1


@pytest.mark.parametrize("delivered", [False, True])
async def test_continue_history_uncertain_delivery_never_repeats(client, monkeypatch, delivered):
    from vococo.gateway import web_bridge
    from vococo.tools import sessions

    key, _ = history_session()
    calls = []

    def fail(target, text, **kw):
        if delivered:
            calls.append((target, text))
        raise RuntimeError("投递结果未知")

    monkeypatch.setattr(web_bridge, "_inject_key_fn", fail)
    monkeypatch.setattr(sessions, "_queued", {})
    payload = {"id": key, "instruction": "确认", "idempotency_key": "uncertain-session-01"}
    response = await client.post("/internal/brain/continue_task", json=payload, headers=AUTH)
    assert response.status == 500
    await post(client, "continue_task", payload, 409)
    assert len(calls) == int(delivered)
    assert brain._registry().execute(
        "SELECT state FROM brain_bridge_continuations WHERE idempotency_key=?",
        (payload["idempotency_key"],)).fetchone()[0] == "pending"


async def test_continue_history_session_unavailable_web_entry(client, monkeypatch):
    from vococo.gateway import web_bridge

    key, _ = history_session()
    monkeypatch.setattr(web_bridge, "_inject_key_fn", None)
    await post(client, "continue_task", {"id": key, "instruction": "x", "idempotency_key": "nobridge-0001"}, 503)
    row = brain._registry().execute(
        "SELECT 1 FROM brain_bridge_continuations WHERE idempotency_key='nobridge-0001'").fetchone()
    assert row is None


async def test_continue_dispatched_task_ignores_agent_id(client, monkeypatch):
    task_id = (await create(client))["id"]
    assert tasks.set_status(task_id, "running") and tasks.set_status(task_id, "done")
    session_store.set_sdk_session_id(tasks.session_key(task_id), "sdk-test-session")
    start = Mock()
    monkeypatch.setattr(task_runner, "start_claimed", start)
    result = await post(client, "continue_task", {"id": task_id, "instruction": "继续", "agent_id": "abcdef1234",
                                                  "idempotency_key": "task-agent-01"})
    assert result["id"] == task_id and start.call_count == 1


async def test_continuation_table_upgrades_old_rows(bridge_db):
    conn = brain._registry()
    conn.execute("DROP TABLE brain_bridge_continuations")
    conn.execute("CREATE TABLE brain_bridge_continuations(idempotency_key TEXT PRIMARY KEY,task_id TEXT NOT NULL,"
                 "content_hash TEXT NOT NULL,state TEXT NOT NULL,instruction TEXT NOT NULL DEFAULT '',"
                 "run_version INTEGER NOT NULL DEFAULT -1)")
    conn.execute("INSERT INTO brain_bridge_continuations VALUES ('old-key-0001','t1','h','accepted','旧',0)")
    conn.commit()
    conn = brain._registry()
    row = conn.execute("SELECT kind,target_id,agent_id FROM brain_bridge_continuations").fetchone()
    assert (row["kind"], row["target_id"]) == ("task", "t1")
    # 升级后的第一条真实请求也要能 BEGIN IMMEDIATE，不能遗留隐式写事务。
    assert not conn.in_transaction
    conn.execute("BEGIN IMMEDIATE")
    conn.rollback()
