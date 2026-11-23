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


def test_external_mcp_is_filtered_by_agent_selection_and_credentials(monkeypatch):
    from vococo.gateway import settings_store

    table = {
        "local": {"type": "http", "url": "http://127.0.0.1:12345/mcp"},
        "remote": {"type": "http", "url": "https://example.com/mcp"},
        "signed": {"type": "http", "url": "http://127.0.0.1:12345/mcp?token=synthetic"},
        "secret-header": {"type": "http", "url": "http://127.0.0.1:12345/mcp", "headers": {"X-Test": "fake"}},
        "command": {"type": "stdio", "command": "test"},
    }
    monkeypatch.setattr(settings_store, "effective_external_mcp", lambda names: {
        name: cfg for name, cfg in table.items() if name in names
    })
    assert agy_tools.external_servers(set(table), set()) == [
        {"type": "http", "name": "local", "url": "http://127.0.0.1:12345/mcp", "headers": []},
    ]
    assert not agy_tools.external_servers(set(table), {"mcp__local__update"})
    assert not agy_tools.external_servers(set(table), {"mcp__*__update"})
    assert not agy_tools.external_servers({"remote"}, set())


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

    async def upstream(request):
        request_body = await request.json()
        if "id" not in request_body:
            return web.Response(status=202)
        method = request_body["method"]
        if method == "initialize":
            result = {"protocolVersion": request_body["params"]["protocolVersion"], "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fixture", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "send_synthetic", "description": "dry operation",
                                 "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}]}
        else:
            assert method == "tools/call"
            external_calls.append(request_body["params"]["arguments"])
            result = {"content": [{"type": "text", "text": "fixture receipt"}]}
        return web.json_response({"jsonrpc": "2.0", "id": request_body["id"], "result": result})

    async def confirm(server_name, name, detail):
        assert (server_name, name) == ("external-test", "send_synthetic")
        return approved

    monkeypatch.setattr(agy_tools, "_confirm_external", confirm)
    monkeypatch.setattr(settings_store, "vococo_enabled", lambda: False)
    monkeypatch.setattr(agents, "runtime_for_session", lambda _: {})
    app = web.Application()
    app.router.add_post("/external", upstream)
    app.router.add_route("*", agy_tools.PATH, agy_tools.handle)
    key = "session:synthetic-external"
    token = clarify.set_current(key, SimpleNamespace(platform="web"), key, 1)
    try:
        async with TestClient(TestServer(app)) as client:
            async with agy_tools.mount(key, 1, str(isolated)) as disabled:
                assert disabled is None
            candidate = {"name": "external-test", "url": str(client.make_url("/external"))}
            async with agy_tools.mount(key, 1, str(isolated), external=candidate) as relay:
                assert relay["url"] != candidate["url"]
                headers = {item["name"]: item["value"] for item in relay["headers"]}

                async def call(text):
                    response = await client.post(agy_tools.PATH, headers=headers, json={"jsonrpc": "2.0", "id": 1,
                        "method": "tools/call", "params": {"name": "send_synthetic", "arguments": {"text": text}}})
                    return (await response.json())["result"]

                assert (await call("not approved"))["isError"]
                assert not external_calls
                approved = True
                assert not (await call("approved synthetic"))["isError"]
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
