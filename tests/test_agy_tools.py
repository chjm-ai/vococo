from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo.gateway import agy_tools, clarify
from vococo.memory import agents
from vococo.tools import danger


@pytest.mark.anyio
async def test_builtin_mcp_is_turn_scoped_and_preserves_approval(isolated, monkeypatch):
    calls = []
    verdict = False

    async def run(args):
        calls.append((clarify.current().session_key, danger.current_cwd(), args))
        return {"content": [{"type": "text", "text": "synthetic result"}]}

    async def approval(*args):
        return verdict

    def tool(name):
        return SimpleNamespace(name=name, description=name, handler=run, input_schema={
            "type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"],
        })

    names = ["search_library", "generate_image", "delete_workbench_task"]
    monkeypatch.setattr(agy_tools, "_registry", lambda: {name: tool(name) for name in names})
    monkeypatch.setattr(agents, "runtime_for_session", lambda _: {
        "disallowed_tools": ["mcp__vococo__delete_workbench_task"],
    })
    monkeypatch.setattr(danger, "require_approval", approval)
    app = web.Application()
    app.router.add_route("*", agy_tools.PATH, agy_tools.handle)
    key = "session:synthetic"
    ctx_token = clarify.set_current(key, SimpleNamespace(platform="web"), key, 7)
    try:
        async with TestClient(TestServer(app)) as client:
            async with agy_tools.mount(key, 8, str(isolated)) as wrong_turn:
                assert wrong_turn is None
            async with agy_tools.mount(key, 7, str(isolated)) as server:
                headers = {item["name"]: item["value"] for item in server["headers"]}

                async def rpc(method, params=None, **extra):
                    return await client.post(agy_tools.PATH, headers={**headers, **extra}, json={
                        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
                    })

                assert (await rpc("tools/list", Host="example.com")).status == 404
                assert (await rpc("tools/list", Authorization="Bearer wrong")).status == 401
                result = await (await rpc("tools/list")).json()
                assert {item["name"] for item in result["result"]["tools"]} == {"search_library", "generate_image"}
                invalid = await (await rpc("tools/call", {"name": "search_library", "arguments": {"prompt": []}})).json()
                assert invalid["result"]["isError"]
                denied = await (await rpc("tools/call", {"name": "generate_image", "arguments": {"prompt": "dry"}})).json()
                assert denied["result"]["isError"] and not calls
                verdict = True
                for name in ("generate_image", "search_library"):
                    ok = await (await rpc("tools/call", {"name": name, "arguments": {"prompt": "approved payload"}})).json()
                    assert ok["result"]["content"][0]["text"] == "synthetic result"
                repeated = await (await rpc("tools/call", {"name": "generate_image", "arguments": {"prompt": "approved payload"}})).json()
                assert repeated["result"]["isError"]
                assert calls == [(key, str(isolated), {"prompt": "approved payload"})] * 2
            assert (await rpc("tools/list")).status == 401
    finally:
        clarify.reset_current(ctx_token)


def test_external_mcp_accepts_future_registered_transports_but_not_unselected_or_plain_remote(monkeypatch):
    from vococo.gateway import settings_store

    table = {
        "local": {"type": "http", "url": "http://127.0.0.1:12345/mcp"},
        "future-remote": {"type": "http", "url": "https://example.com/mcp"},
        "future-sse": {"type": "sse", "url": "https://example.com/sse?token=synthetic-query-credential"},
        "future-auth": {"type": "http", "url": "http://127.0.0.1:12345/mcp", "headers": {"X-Test": "fake-token"}},
        "future-command": {"type": "stdio", "command": "test", "env": {"API_KEY": "fake-env-secret"}},
        "plain-remote": {"type": "http", "url": "http://example.com/mcp"},
    }
    monkeypatch.setattr(settings_store, "effective_external_mcp", lambda names: {
        name: cfg for name, cfg in table.items() if name in names
    })
    chosen = agy_tools.external_servers(set(table), set())
    assert {server.name for server in chosen} == set(table) - {"plain-remote"}
    assert "fake-env-secret" not in repr(chosen) and "fake-token" not in repr(chosen)
    assert "local" not in {s.name for s in agy_tools.external_servers(set(table), {"mcp__local__update"})}
    assert not agy_tools.external_servers(set(table), {"mcp__*__update"})
    assert {s.name for s in agy_tools.external_servers({"future-remote"}, set())} == {"future-remote"}


@pytest.mark.anyio
async def test_native_commands_run_through_existing_guard(monkeypatch, tmp_path):
    import json

    from vococo.gateway import agy_trial

    calls = []

    async def guard(payload, *_):
        calls.append((payload, danger.current_cwd()))
        return {"hookSpecificOutput": {"permissionDecision": "deny"}}

    monkeypatch.setattr(danger, "pretool_guard_hook", guard)
    settings, denied, brain = agy_trial._native_policy(
        {"Write", "mcp__vococo__dispatch_session"}, agy_trial.VOCOCO_MCP_WRITE,
    )
    rules = json.loads(settings)["permissions"]
    assert "command(*)" in rules["ask"] and "write_file(*)" in rules["deny"]
    assert "write_file" in denied and "vococo_create_task" in brain
    assert await agy_trial._approve_native_tool("run_command", {}, str(tmp_path), "main") == "deny"
    assert not calls
    call = {"rawInput": {"CommandLine": "printf synthetic"}}
    assert await agy_trial._approve_native_tool("run_command", call, str(tmp_path), "main") == "deny"
    assert calls[0][0]["tool_name"] == "Bash" and calls[0][1] == str(tmp_path)


@pytest.mark.anyio
async def test_external_streamable_http_is_proxied_only_after_confirmation(isolated, monkeypatch):
    from vococo.gateway import settings_store

    external_calls = []
    approved = False
    registered = {}
    secret = "synthetic-upstream-value"
    path_secret = "synthetic-path-credential"
    revoke_during_handshake = False

    async def upstream(request):
        assert request.headers.get("Authorization") == f"Bearer {secret}"
        request_body = await request.json()
        if "id" not in request_body:
            return web.Response(status=202)
        method = request_body["method"]
        if method == "initialize":
            if revoke_during_handshake:
                registered.clear()
            result = {"protocolVersion": request_body["params"]["protocolVersion"], "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fixture", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "send_synthetic", "description": "dry operation",
                                 "inputSchema": {"type": "object", "properties": {
                                     "text": {"type": "string", "pattern": "^remote-only$"}}}}]}
        else:
            assert method == "tools/call"
            external_calls.append(request_body["params"]["arguments"])
            result = {"content": [{"type": "text", "text": f"fixture receipt {secret} {path_secret}"}]}
        return web.json_response({"jsonrpc": "2.0", "id": request_body["id"], "result": result})

    async def confirm(server_name, name, detail):
        assert (server_name, name) == ("external-test", "send_synthetic")
        return approved

    monkeypatch.setattr(agy_tools, "_confirm_external", confirm)
    monkeypatch.setattr(settings_store, "vococo_enabled", lambda: False)
    monkeypatch.setattr(agents, "runtime_for_session", lambda _: {"mcp": ["external-test"]})
    monkeypatch.setattr(settings_store, "effective_external_mcp", lambda names: {
        name: spec.copy() for name, spec in registered.items() if name in names
    })
    app = web.Application()
    external_path = f"/external/{path_secret}"
    app.router.add_post(external_path, upstream)
    app.router.add_route("*", agy_tools.PATH, agy_tools.handle)
    key = "session:synthetic-external"
    token = clarify.set_current(key, SimpleNamespace(platform="web"), key, 1)
    try:
        async with TestClient(TestServer(app)) as client:
            async with agy_tools.mount(key, 1, str(isolated)) as disabled:
                assert disabled is None
            assert not agy_tools.external_servers({"external-test"}, set())
            url = str(client.make_url(external_path))
            registered["external-test"] = {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {secret}"}}
            candidate, = agy_tools.external_servers({"external-test"}, set())
            async with agy_tools.mount(key, 1, str(isolated), external=candidate) as relay:
                assert relay["url"] != url
                assert secret not in repr(relay) and path_secret not in repr(relay)
                headers = {item["name"]: item["value"] for item in relay["headers"]}

                async def call(text):
                    response = await client.post(agy_tools.PATH, headers=headers, json={"jsonrpc": "2.0", "id": 1,
                        "method": "tools/call", "params": {"name": "send_synthetic", "arguments": {"text": text}}})
                    return (await response.json())["result"]

                assert (await call("not approved"))["isError"]
                assert not external_calls
                approved = True
                result = await call("approved synthetic")
                assert not result["isError"] and "fixture receipt" in repr(result)
                assert secret not in repr(result) and path_secret not in repr(result)
                assert (await call(secret))["isError"]
                revoke_during_handshake = True
                assert (await call("revoked during the handshake"))["isError"]
                assert (await call("after unmount"))["isError"]
                assert external_calls == [{"text": "approved synthetic"}]
    finally:
        clarify.reset_current(token)


def test_workbench_replay_key_normalizes_assignee_default():
    assert agy_tools._fingerprint("create_workbench_task", {"title": "test"}) == agy_tools._fingerprint(
        "create_workbench_task", {"title": " test ", "assignee": "human", "detail": ""},
    )


@pytest.mark.anyio
async def test_cancelled_owner_invalidates_token_before_child_cleanup(isolated, monkeypatch):
    from vococo.gateway import settings_store

    monkeypatch.setattr(settings_store, "vococo_enabled", lambda: True)
    monkeypatch.setattr(agents, "runtime_for_session", lambda _: {})
    key = "session:cancel-probe"
    app = web.Application()
    app.router.add_route("*", agy_tools.PATH, agy_tools.handle)
    route = clarify.set_current(key, SimpleNamespace(platform="web"), key, 1)
    try:
        async with TestClient(TestServer(app)) as client, agy_tools.mount(key, 1, str(isolated)) as server:
            headers = {item["name"]: item["value"] for item in server["headers"]}
            request = {"jsonrpc": "2.0", "method": "tools/list", "id": 1}
            assert (await client.post(agy_tools.PATH, headers=headers, json=request)).status == 200
            token = headers["Authorization"][7:]
            agy_tools._bindings[token].owner = SimpleNamespace(done=lambda: False, cancelling=lambda: 1)
            assert (await client.post(agy_tools.PATH, headers=headers, json=request)).status == 401
    finally:
        clarify.reset_current(route)


@pytest.mark.anyio
async def test_new_stdio_mcp_keeps_env_in_subprocess_and_filters_results(isolated, monkeypatch):
    import sys

    from vococo.gateway import settings_store

    program = """
import json, os, sys
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    if request['method'] == 'initialize':
        result = {'protocolVersion': request['params']['protocolVersion'], 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'fixture', 'version': '1'}}
    elif request['method'] == 'tools/list':
        assert os.environ.get('FIXTURE_PRIVATE') and os.getcwd() == os.path.abspath(os.sep)
        result = {'tools': [{'name': 'lookup', 'inputSchema': {'type':'object'}}]}
    else:
        result = {'structuredContent': {'ok': 'result', 'echo': os.environ['FIXTURE_PRIVATE']}, 'content': []}
    print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}), flush=True)
"""
    name, secret = "new-stdio", "synthetic-stdio-private-value"
    registry = {name: {"type": "stdio", "command": sys.executable, "args": ["-u", "-c", program],
                       "env": {"FIXTURE_PRIVATE": secret}}}

    async def approve(*args):
        return True

    monkeypatch.setattr(agents, "runtime_for_session", lambda _: {"mcp": [name]})
    monkeypatch.setattr(settings_store, "effective_external_mcp", lambda names: {
        name: spec.copy() for name, spec in registry.items() if name in names
    })
    monkeypatch.setattr(agy_tools, "_confirm_external", approve)
    app = web.Application()
    app.router.add_route("*", agy_tools.PATH, agy_tools.handle)
    key = "session:stdio-probe"
    ctx = clarify.set_current(key, SimpleNamespace(platform="web"), key, 1)
    try:
        async with TestClient(TestServer(app)) as client:
            candidate, = agy_tools.external_servers({name}, set())
            async with agy_tools.mount(key, 1, str(isolated), external=candidate) as relay:
                assert relay is not None and relay["name"] == name and secret not in repr(relay)
                headers = {item["name"]: item["value"] for item in relay["headers"]}
                result = await (await client.post(agy_tools.PATH, headers=headers, json={
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "lookup", "arguments": {}},
                })).json()
                assert not result["result"]["isError"]
                assert "result" in repr(result) and secret not in repr(result)
    finally:
        clarify.reset_current(ctx)


@pytest.mark.anyio
async def test_sse_mcp_uses_only_registered_endpoint_and_its_header():
    import asyncio
    import json

    queue = asyncio.Queue()
    secret = "synthetic-sse-credential"

    async def events(request):
        assert request.headers["Authorization"] == f"Bearer {secret}"
        sse = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await sse.prepare(request)
        await sse.write(b"event: endpoint\ndata: /message\n\n")
        try:
            while True:
                data = await queue.get()
                await sse.write(f"event: message\ndata: {json.dumps(data)}\n\n".encode())
                if data["result"].get("tools") is not None:
                    break
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return sse

    async def message(request):
        assert request.headers["Authorization"] == f"Bearer {secret}"
        data = await request.json()
        if "id" in data:
            result = ({"protocolVersion": data["params"]["protocolVersion"], "capabilities": {"tools": {}},
                       "serverInfo": {"name": "sse-test", "version": "1"}} if data["method"] == "initialize"
                      else {"tools": [{"name": "lookup", "inputSchema": {"type": "object"}}]})
            await queue.put({"jsonrpc": "2.0", "id": data["id"], "result": result})
        return web.Response(status=202)

    app = web.Application()
    app.router.add_get("/sse", events)
    app.router.add_post("/message", message)
    async with TestServer(app) as server:
        spec = {"type": "sse", "url": str(server.make_url("/sse")), "headers": {"Authorization": f"Bearer {secret}"}}
        external = agy_tools._External("sse-fixture", spec, agy_tools._upstream_values(spec))
        assert set(await agy_tools._external_tools(external)) == {"lookup"}


def test_upstream_sdk_logs_are_hidden_only_inside_bridge(caplog):
    import logging

    logger = logging.getLogger("mcp.client.sse")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        logger.warning("outside-bridge")
        state = agy_tools._private_upstream_io.set(True)
        try:
            logger.warning("synthetic-private-url")
        finally:
            agy_tools._private_upstream_io.reset(state)
    assert caplog.messages == ["outside-bridge"]
