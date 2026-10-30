"""官方 MCP 客户端 → 真实 HTTP 受限桥 → 临时数据；不启动真实模型。"""
from contextlib import asynccontextmanager
from unittest.mock import Mock

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from vococo import config
from vococo.core import task_runner, tasks
from vococo.gateway import brain_mcp, brain_routes, settings_store
from vococo.memory import agents, session_store

pytestmark = pytest.mark.anyio
UPSTREAM = "public-test-upstream-" + "u" * 40
BRIDGE = "private-test-bridge-" + "b" * 40


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def environment(isolated, monkeypatch):
    monkeypatch.setattr(tasks, "_DB", None)
    monkeypatch.setattr(config, "CRON_JOBS_PATH", isolated / "cron.json")
    monkeypatch.setattr(settings_store, "_PATH", isolated / "settings.json")
    monkeypatch.setattr(task_runner, "_maybe_start_next", Mock())
    monkeypatch.setenv("VOCOCO_MCP_UPSTREAM_TOKEN", UPSTREAM)
    monkeypatch.setenv("VOCOCO_BRAIN_BRIDGE_TOKEN", BRIDGE)
    monkeypatch.setenv("VOCOCO_BRAIN_AGENT_ID", brain_routes.AGENT_ID)
    workdir = isolated / "project"
    workdir.mkdir()
    agents._write_meta(brain_routes.AGENT_ID, {"name": "开发", "workdir": str(workdir),
                                               "execution_mode": "project"})
    app = web.Application()
    brain_routes.register_routes(app)
    async with TestServer(app) as backend:
        server = brain_mcp.create_server(bridge_port=backend.port)
        yield brain_mcp.create_app(server), workdir
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None


@asynccontextmanager
async def session_for(app, token=UPSTREAM, headers=None):
    headers = {"Authorization": f"Bearer {token}", **(headers or {})}
    async with app.app.router.lifespan_context(app.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers=headers) as http:
            async with streamable_http_client("http://127.0.0.1:8852/mcp", http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    yield session


async def call(session, name, args):
    result = await session.call_tool(name, args)
    assert not result.is_error, result.content
    return result.structured_content


async def test_chatgpt_new_protocol_discovers_tools(environment):
    app, _ = environment
    headers = {"Authorization": f"Bearer {UPSTREAM}", "MCP-Protocol-Version": "2026-07-28",
               "Accept": "application/json, text/event-stream", "MCP-Method": "tools/list"}
    async with app.app.router.lifespan_context(app.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers=headers) as client:
            response = await client.post("http://127.0.0.1:8852/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {}}}})
    assert response.status_code == 200, response.text
    assert {"vococo_search_history", "vococo_continue_task"} <= {
        tool["name"] for tool in response.json()["result"]["tools"]}


async def test_chatgpt_new_protocol_calls_read_only_tool(environment):
    app, _ = environment
    headers = {"Authorization": f"Bearer {UPSTREAM}", "MCP-Protocol-Version": "2026-07-28",
               "Accept": "application/json, text/event-stream", "MCP-Method": "tools/call",
               "MCP-Name": "vococo_list_agents"}
    async with app.app.router.lifespan_context(app.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers=headers) as client:
            response = await client.post("http://127.0.0.1:8852/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": "vococo_list_agents", "arguments": {}, "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {}}}})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError") and result["structuredContent"]["agents"]


async def test_official_mcp_client_dispatch_idempotency_result_and_cancel(environment):
    app, workdir = environment
    async with session_for(app) as session:
        tools = (await session.list_tools()).tools
        assert {tool.name for tool in tools} == {"vococo_create_task", "vococo_continue_task", "vococo_get_task", "vococo_cancel_task",
            "vococo_list_agents", "vococo_list_sessions", "vococo_read_session", "vococo_search_history"}
        assert next(t for t in tools if t.name == "vococo_create_task").annotations.read_only_hint is False
        args = {"title": "隔离派单", "prompt": "只运行测试任务", "idempotency_key": "request-test-001"}
        created = await call(session, "vococo_create_task", args)
        repeated = await call(session, "vococo_create_task", args)
        assert created["id"] == repeated["id"] and created["receipt"] != repeated["receipt"]
        row = tasks.get(created["id"])
        assert row["cwd"] == str(workdir) and tasks.automatic_access(row)["remote_brain"] is True
        assert "mcp__vococo__recall_past" in tasks.automatic_access(row)["denied"]
        queried = await call(session, "vococo_get_task", {"id": created["id"]})
        assert queried["status"] == "queued" and "prompt" not in queried and "source_access" not in queried
        cancelled = await call(session, "vococo_cancel_task", {"id": created["id"]})
        assert cancelled["cancel_requested"] and cancelled["status"] == "cancelled"
        assert (await session.call_tool("vococo_create_task", {**args, "prompt": "不同任务"})).is_error


async def test_mcp_continues_owned_task_once(environment, monkeypatch):
    app, _ = environment
    start = Mock(return_value=True)
    monkeypatch.setattr(task_runner, "start_claimed", start)
    async with session_for(app) as session:
        created = await call(session, "vococo_create_task", {
            "title": "续接测试", "prompt": "第一条需求", "idempotency_key": "create-test-01"})
        task_id = created["id"]
        assert tasks.set_status(task_id, "running") and tasks.set_status(task_id, "done")
        session_store.set_sdk_session_id(tasks.session_key(task_id), "sdk-test-session")
        args = {"id": task_id, "instruction": "第二条需求", "idempotency_key": "continue-test-01"}
        assert (await call(session, "vococo_continue_task", args))["id"] == task_id
        assert (await call(session, "vococo_continue_task", args))["continuation"] == "accepted"
        assert start.call_count == 1
        assert (await session.call_tool("vococo_continue_task", {**args, "id": "not-owned"})).is_error


@pytest.mark.parametrize("prefix", ["web:", "session:", "task:"])
async def test_mcp_continues_existing_cross_agent_session(environment, monkeypatch, prefix):
    from vococo.gateway import web_bridge
    from vococo.gateway.adapters.web import WebAdapter
    from vococo.tools import sessions

    app, _ = environment
    other = agents.create("研究协调")
    key = prefix + "existing-research"
    session_store.bind_session(key, agent_id=other["id"])
    session_store.append(key, "原始研究", "已有结论")
    session_store.set_sdk_session_id(key, "original-sdk-context")
    adapter = WebAdapter()
    monkeypatch.setattr(adapter, "_emit", lambda _: None)
    monkeypatch.setattr(web_bridge, "_inject_key_fn", adapter.inject_by_key)
    monkeypatch.setattr(sessions, "_queued", {})
    async with session_for(app) as session:
        tool = next(t for t in (await session.list_tools()).tools if t.name == "vococo_continue_task")
        assert "agent_id" in tool.input_schema["properties"] and "web:/task:/session:" in tool.description
        args = {"id": key, "agent_id": other["id"], "instruction": "只确认收到，不研究",
                "idempotency_key": "existing-session-01"}
        assert (await call(session, "vococo_continue_task", args))["id"] == key
        assert (await call(session, "vococo_continue_task", args))["continuation"] == "accepted"
    incoming = adapter._inbox.get_nowait()
    assert incoming.session_key == key and incoming.text.endswith(args["instruction"])
    assert incoming.source_access["remote_brain"] is True
    assert incoming.source_access["connection"] == brain_mcp.connection_id(UPSTREAM)
    assert adapter._inbox.empty() and tasks.list_recent() == []
    assert session_store.get_sdk_session_id(key) == "original-sdk-context"
    assert session_store._conn().execute("SELECT COUNT(*) FROM turns WHERE session_key=?", (key,)).fetchone()[0] == 1


async def test_mcp_history_pagination_rebuilds_text_and_no_tool_events(environment):
    app, _ = environment
    key = agents.main_session_key(agents.get(brain_routes.AGENT_ID))
    session_store.set_title(key, "实际历史标题")
    turn = session_store.start_turn(key, "问题" * 5000)
    session_store.finish_turn(turn, "回答" * 6000)
    async with session_for(app) as session:
        listed = await call(session, "vococo_list_sessions", {})
        assert listed["sessions"][0]["title"] == "实际历史标题"
        found = await call(session, "vococo_search_history", {"query": "问题"})
        assert found["results"][0]["turn_id"] == turn and found["results"][0]["key"] == key
        assert (await session.call_tool("vococo_search_history", {"query": "   "})).is_error
        user, assistant, args = "", "", {"key": key, "length": 7999}
        while True:
            page = await call(session, "vococo_read_session", args)
            user += page["turn"]["user_text"]
            assistant += page["turn"]["assistant_text"]
            assert "events" not in page["turn"]
            if page["next_offset"] is None:
                break
            args.update(turn_id=turn, offset=page["next_offset"])
        assert user == "问题" * 5000 and assistant == "回答" * 6000
        assert (await session.call_tool("vococo_read_session", {"key": "web:other-agent"})).is_error


async def test_mcp_lists_and_reads_other_agents_with_explicit_agent_id(environment):
    app, workdir = environment
    other_dir = workdir.parent / "other"
    other_dir.mkdir()
    agents._write_meta("d1ee9a7e2b", {"name": "DeepLeaper", "workdir": str(other_dir),
                                     "execution_mode": "project"})
    other = agents.get("d1ee9a7e2b")
    other_key = agents.main_session_key(other)
    session_store.set_title(other_key, "JLL 项目")
    turn = session_store.start_turn(other_key, "看看 JLL")
    session_store.finish_turn(turn, "建议若干")
    dev_key = agents.main_session_key(agents.get(brain_routes.AGENT_ID))
    session_store.set_title(dev_key, "开发会话")
    session_store.start_turn(dev_key, "开发问题")
    async with session_for(app) as session:
        ids = {item["agent_id"]: item for item in (await call(session, "vococo_list_agents", {}))["agents"]}
        assert ids[brain_routes.AGENT_ID]["task_dispatch"] is True
        assert ids["d1ee9a7e2b"] == {"agent_id": "d1ee9a7e2b", "name": "DeepLeaper", "task_dispatch": False,
                                    "session_count": 1}
        default = await call(session, "vococo_list_sessions", {"limit": 20})
        assert {row["agent_id"] for row in default["sessions"]} == {brain_routes.AGENT_ID, "d1ee9a7e2b"}
        legacy = await call(session, "vococo_list_sessions", {"all_agents": False})
        assert {row["agent_id"] for row in legacy["sessions"]} == {brain_routes.AGENT_ID}
        everything = await call(session, "vococo_list_sessions", {"limit": 100, "all_agents": True})
        assert {row["agent_id"] for row in everything["sessions"]} >= {brain_routes.AGENT_ID, "d1ee9a7e2b"}
        only_other = await call(session, "vococo_list_sessions", {"agent_id": "d1ee9a7e2b"})
        assert [row["key"] for row in only_other["sessions"]] == [other_key]
        search = await call(session, "vococo_search_history", {"query": "JLL"})
        assert [(row["agent_id"], row["key"], row["turn_id"]) for row in search["results"]] == [
            ("d1ee9a7e2b", other_key, turn)]
        # 老调用(不带 agent_id)仍按固定开发 Agent 校验归属
        assert (await session.call_tool("vococo_read_session", {"key": other_key})).is_error
        assert await call(session, "vococo_read_session", {"key": dev_key}) is not None
        page = await call(session, "vococo_read_session", {"key": other_key, "agent_id": "d1ee9a7e2b"})
        assert page["turn"]["user_text"] == "看看 JLL" and page["turn"]["assistant_text"] == "建议若干"
        # agent_id 与 key 不匹配时不串读
        assert (await session.call_tool("vococo_read_session",
                                        {"key": dev_key, "agent_id": "d1ee9a7e2b"})).is_error
        assert (await session.call_tool("vococo_read_session",
                                        {"key": other_key, "agent_id": "b" * 12})).is_error
        assert (await session.call_tool("vococo_list_sessions", {"agent_id": "b" * 12})).is_error


async def test_mcp_readable_agents_allowlist(environment, monkeypatch):
    app, workdir = environment
    other_dir = workdir.parent / "other"
    other_dir.mkdir()
    agents._write_meta("d1ee9a7e2b", {"name": "DeepLeaper", "workdir": str(other_dir),
                                     "execution_mode": "project"})
    other_key = agents.main_session_key(agents.get("d1ee9a7e2b"))
    session_store.start_turn(other_key, "私有问题")
    monkeypatch.setenv(brain_routes.READABLE_AGENTS, brain_routes.AGENT_ID)
    async with session_for(app) as session:
        listed = await call(session, "vococo_list_agents", {})
        assert [item["agent_id"] for item in listed["agents"]] == [brain_routes.AGENT_ID]
        assert (await session.call_tool("vococo_list_sessions", {"agent_id": "d1ee9a7e2b"})).is_error
        assert (await session.call_tool("vococo_read_session",
                                        {"key": other_key, "agent_id": "d1ee9a7e2b"})).is_error
        rows = await call(session, "vococo_list_sessions", {"limit": 100, "all_agents": True})
        assert all(row["agent_id"] == brain_routes.AGENT_ID for row in rows["sessions"])
        default = await call(session, "vococo_list_sessions", {})
        assert all(row["agent_id"] == brain_routes.AGENT_ID for row in default["sessions"])
        found = await call(session, "vococo_search_history", {"query": "私有问题"})
        assert found["results"] == []
        assert (await session.call_tool("vococo_search_history", {"query": "私有", "agent_id": "d1ee9a7e2b"})).is_error


@pytest.mark.parametrize("token", ["", "wrong-token", BRIDGE])
async def test_oauth_upstream_secret_is_required(environment, token):
    app, _ = environment
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        response = await client.post("http://127.0.0.1:8852/mcp", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert "no-store" in response.headers["cache-control"]


async def test_mcp_bounds_backend_errors_and_no_secret_logging(environment, monkeypatch, caplog):
    app, _ = environment
    async with session_for(app) as session:
        assert (await session.call_tool("vococo_get_task", {"id": "not-owned"})).is_error
        for args in ({"limit": 101}, {"offset": True}, {"offset": -1}, {"limit": "2"},
                     {"agent_id": "b" * 12}):
            assert (await session.call_tool("vococo_list_sessions", args)).is_error
        monkeypatch.setenv("VOCOCO_BRAIN_BRIDGE_TOKEN", "changed-secret-" + "n" * 32)
        result = await session.call_tool("vococo_list_sessions", {})
        assert result.is_error and "本机桥认证失败" in str(result.content)
        assert UPSTREAM not in caplog.text and BRIDGE not in caplog.text


@pytest.mark.parametrize("upstream,bridge", [("", BRIDGE), (UPSTREAM, "x"), (BRIDGE, BRIDGE),
                                            (UPSTREAM, "带中文" * 20), (UPSTREAM, "x " * 32)])
async def test_weak_or_shared_secrets_refuse_startup(monkeypatch, upstream, bridge):
    monkeypatch.setenv("VOCOCO_MCP_UPSTREAM_TOKEN", upstream)
    monkeypatch.setenv("VOCOCO_BRAIN_BRIDGE_TOKEN", bridge)
    with pytest.raises(ValueError):
        brain_mcp.create_server()


async def test_mcp_error_does_not_follow_redirects_or_reveal_backend_error(monkeypatch):
    async def redirect(_request):
        return web.Response(status=302, headers={"Location": "https://example.com/leak"}, text="sensitive")
    app = web.Application()
    app.router.add_post("/internal/brain/list_sessions", redirect)
    async with TestServer(app) as backend:
        with pytest.raises(ValueError, match="本机桥异常"):
            await brain_mcp._call(backend.port, BRIDGE, "list_sessions", {})
