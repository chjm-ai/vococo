import json
import uuid
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.gateway import agy_acp, agy_handoff, agy_trial, core, settings_store
from vococo.gateway.adapters.web import WebAdapter
from vococo.gateway.run import GatewayRunner
from vococo.memory import session_store

HIGH = "agy-native:gemini-3.8-flash-high"
LOW = "agy-native:gemini-3.8-flash-low"
NATIVE_ID = "3596d05e-5d0e-47a7-b5a8-77aee32011d2"


@pytest.mark.anyio
async def test_web_native_locks_only_after_success(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "test-web")
    monkeypatch.setattr(settings_store, "_PATH", isolated / "settings.json")
    monkeypatch.setattr(agy_trial, "cli_binary", lambda: Path("/bin/sh"))
    monkeypatch.setattr(agy_trial, "enabled", lambda: True)
    settings_store.set_web_default_model("deepseek-flash")
    called = []

    async def catalog():
        return {HIGH: "Gemini 3.8 Flash (High)", LOW: "Gemini 3.8 Flash (Low)"}

    workspace = isolated / "workspaces" / "agy-handoff" / "abc123-test"
    (workspace / ".agents").mkdir(parents=True)
    (workspace / ".agents/mcp_config.json").write_text(json.dumps({"mcpServers": {}}))
    meta = {"agent_id": "general", "global_agent_id": "general", "workspace": str(workspace)}

    def prepare(key, *, draft_owner=None):
        assert draft_owner is None
        return dict(meta)

    def prepare_into(path, key, *, draft_owner=None):
        assert Path(path) == workspace
        return dict(meta)

    async def stream_turn(cwd, timeout, model, prompt, *, session_id=None, **kwargs):
        called.append((str(cwd), model, session_id))
        assert prompt.startswith("本会话由 vococo 网页代理")
        if prompt.endswith("first failure"):
            raise agy_acp.AcpError("agy_request_failed")
        if prompt.endswith("test search"):
            raise agy_acp.AcpError("agy_unexpected_tool")
        yield "session", NATIVE_ID
        yield "delta", "测试通过"
        yield "done", None

    async def no_mcp():
        return None

    monkeypatch.setattr(agy_trial, "native_catalog", catalog)
    monkeypatch.setattr(agy_trial, "_vococo_mcp_bundle", no_mcp)
    monkeypatch.setattr(agy_handoff, "prepare", prepare)
    monkeypatch.setattr(agy_handoff, "prepare_into", prepare_into)
    monkeypatch.setattr(agy_acp, "stream_turn", stream_turn)
    adapter = WebAdapter()
    runner = GatewayRunner([adapter])
    app = web.Application()
    app.add_routes([web.get("/models", adapter._handle_models), web.post("/model", adapter._handle_model_switch),
                    web.post("/send", adapter._handle_send)])
    conv = "session:" + uuid.uuid4().hex

    async with TestClient(TestServer(app), headers={"X-Auth-Token": "test-web"}) as client:
        async def choose(model):
            return await client.post("/model", json={"conv": conv, "model": model})

        async def send(text, model):
            resp = await client.post("/send", json={"conv": conv, "agent_id": "general", "text": text,
                                                    "agy_trial_model": model})
            assert resp.status == 200, await resp.text()
            await runner._handle(adapter, adapter._inbox.get_nowait())

        before = await (await client.get("/models", params={"conv": conv})).json()
        assert not before["agy_native_locked"] and any(item[2] == "agy-native" for item in before["choices"])
        assert (await choose("agy-native:missing")).status == 400
        assert (await choose(HIGH)).status == 200
        secret = 'synthetic-test-secret-value'
        monkeypatch.setattr(config, 'DASHSCOPE_API_KEY', secret)
        body = {"conv": conv, "agent_id": "general", "agy_trial_model": HIGH}
        assert (await client.post('/send', json={**body, 'text': secret})).status == 400
        assert not session_store.load_recent(conv) and adapter._inbox.empty()
        assert (await client.post('/send', json={**body, 'text': '/help', 'library_refs': ['test']})).status == 400
        await send("first failure", HIGH)
        assert session_store.agy_native_session(conv) is None
        await send("test search", HIGH)
        assert session_store.agy_native_session(conv) is None
        assert "Antigravity 网页只支持纯文本" in str(session_store.load_recent(conv))
        assert "用户消息：" not in str(session_store.load_recent(conv))
        assert (await choose("deepseek-flash")).status == 200
        assert (await choose(HIGH)).status == 200
        await send("first success", HIGH)
        lock = session_store.agy_native_session(conv)
        assert lock["id"] == NATIVE_ID
        assert (await choose("deepseek-flash")).status == 409
        assert core.handle_command("/model deepseek-flash", conv, HIGH).new_model is None
        assert session_store.get_chosen_model(conv) == HIGH
        assert (await choose(LOW)).status == 200
        await send("same native session", LOW)
        assert called[-1] == (lock["cwd"], "gemini-3.8-flash-low", NATIVE_ID)
        assert session_store.agy_native_session(conv) == lock
        locked = await (await client.get("/models", params={"conv": conv})).json()
        assert locked["agy_native_locked"] and not locked["agy_handoff"]
        assert {x[0] for x in locked["choices"]} == {HIGH, LOW}
        assert locked["default"] == "deepseek-flash"


@pytest.mark.anyio
async def test_antigravity_models_available_in_draft_but_not_main(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "test-web")
    monkeypatch.setattr(agy_trial, "cli_binary", lambda: Path("/bin/sh"))
    monkeypatch.setattr(agy_trial, "enabled", lambda: True)

    async def catalog():
        return {HIGH: "Gemini 3.8 Flash (High)"}

    monkeypatch.setattr(agy_trial, "native_catalog", catalog)
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([web.get("/models", adapter._handle_models), web.post("/model", adapter._handle_model_switch)])

    async with TestClient(TestServer(app), headers={"X-Auth-Token": "test-web"}) as client:
        draft = "local-" + uuid.uuid4().hex
        data = await (await client.get("/models", params={"conv": draft})).json()
        assert any(item[2] == "agy-native" for item in data["choices"])
        assert (await client.post("/model", json={"conv": draft, "model": HIGH})).status == 200
        main = await (await client.get("/models", params={"conv": "main"})).json()
        assert not any(item[2] == "agy-native" for item in main["choices"])
        assert (await client.post("/model", json={"conv": "main", "model": HIGH})).status == 400


@pytest.mark.anyio
async def test_legacy_session_falls_back_to_new_acp_session(isolated, monkeypatch):
    workspace = isolated / "workspaces" / "agy-handoff" / "session-legacy"
    workspace.mkdir(parents=True)
    key = "session:legacy"
    session_store.set_chosen_model(key, HIGH)
    conn = session_store._conn()
    conn.execute("UPDATE session_meta SET agy_native_session=? WHERE session_key=?",
                 (json.dumps({"id": NATIVE_ID, "cwd": str(workspace)}), key))
    conn.commit()
    new_id = "8a5bd0ee-0c3a-4bbf-8e0f-a99b5dbe7ec8"

    async def catalog():
        return {HIGH: "Gemini 3.8 Flash (High)"}

    def prepare_into(path, key, *, draft_owner=None):
        return {"agent_id": "general", "global_agent_id": "general"}

    async def stream_turn(cwd, timeout, model, prompt, *, session_id=None, **kwargs):
        if session_id:
            raise agy_acp.AcpError("agy_session_mismatch")
        yield "session", new_id
        yield "delta", "继续了"
        yield "done", None

    async def no_mcp():
        return None

    monkeypatch.setattr(agy_trial, "native_catalog", catalog)
    monkeypatch.setattr(agy_trial, "_vococo_mcp_bundle", no_mcp)
    monkeypatch.setattr(agy_handoff, "prepare_into", prepare_into)
    monkeypatch.setattr(agy_acp, "stream_turn", stream_turn)
    events = [event async for event in agy_trial.stream_native(key, 1, HIGH, "继续")]
    done = events[-1]
    assert "继续了" in done.reply.text and "原 Antigravity 会话无法恢复" in done.reply.text
    assert not done.reply.is_error
    lock = session_store.agy_native_session(key)
    assert lock["id"] == new_id and lock["acp"] is True


@pytest.mark.anyio
async def test_vococo_mcp_bundle_exposes_read_and_write_tools(monkeypatch):
    class Writer:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def connect(host, port):
        assert (host, port) == ('127.0.0.1', 8852)
        return None, Writer()

    monkeypatch.setenv('VOCOCO_MCP_STATIC_TOKEN', 'x' * 32)
    monkeypatch.setattr(agy_trial.asyncio, 'open_connection', connect)
    monkeypatch.setattr(agy_trial, '_vococo_mcp_cache', (0.0, None))
    bundle = await agy_trial._vococo_mcp_bundle()
    assert bundle['server']['url'] == agy_trial.VOCOCO_MCP_URL
    assert bundle['server']['headers'][0]['value'] == 'Bearer ' + 'x' * 32
    assert 'mcp_vococo-local_vococo_list_sessions' in bundle['read']
    assert 'mcp_vococo-local_vococo_create_task' in bundle['write']
    assert bundle['read']['mcp_vococo-local_vococo_list_sessions'] == 'vococo_list_sessions'


@pytest.mark.anyio
async def test_write_tool_approval_shows_prompt_and_allows(monkeypatch):
    from vococo.gateway import clarify

    shown = []

    class Adapter:
        async def present_choice(self, chat_id, choice):
            shown.append((chat_id, choice.prompt, [label for _, label in choice.options]))

    class Ctx:
        session_key = 'session:qa'
        chat_id = 'conv1'
        adapter = Adapter()

    class Pending:
        clarify_id = 'cid'

    monkeypatch.setattr(clarify, 'current', lambda: Ctx())
    registered = []
    monkeypatch.setattr(clarify, 'register',
                        lambda key, choices, notice=None: registered.append(notice) or Pending())

    async def wait(_cid, _timeout):
        return '允许一次'

    monkeypatch.setattr(clarify, 'wait', wait)
    decision, note = await agy_trial._approve_vococo_tool(
        'vococo_create_task', {'rawInput': {'title': '测试派单', 'prompt': '具体任务'}})
    assert (decision, note) == ('allow', '')
    assert shown and '测试派单' in shown[0][1] and '具体任务' in shown[0][1]
    assert shown[0][2] == ['允许一次', '拒绝']
    assert '````text' in shown[0][1]
    assert registered and registered[0]['kind'] == 'approval'


@pytest.mark.anyio
async def test_write_tool_approval_rejects_secrets_and_long_text(monkeypatch):
    from vococo.gateway import clarify

    shown = []

    class Adapter:
        async def present_choice(self, chat_id, choice):
            shown.append(choice)

    class Ctx:
        session_key = 'session:qa'
        chat_id = 'conv1'
        adapter = Adapter()

    monkeypatch.setattr(clarify, 'current', lambda: Ctx())
    decision, note = await agy_trial._approve_vococo_tool(
        'vococo_create_task', {'rawInput': {'title': 'x', 'prompt': 'api_key=' + 'a' * 32}})
    assert decision == 'deny' and '凭据' in note and not shown
    decision, note = await agy_trial._approve_vococo_tool(
        'vococo_create_task', {'rawInput': {'title': 'x', 'prompt': '长' * 4001}})
    assert decision == 'deny' and '过长' in note and not shown


@pytest.mark.anyio
async def test_approval_present_failure_abandons_pending(monkeypatch):
    from vococo.gateway import clarify

    class Adapter:
        async def present_choice(self, chat_id, choice):
            raise RuntimeError('sse down')

    class Ctx:
        session_key = 'session:qa'
        chat_id = 'conv1'
        adapter = Adapter()

    class Pending:
        clarify_id = 'cid'

    abandoned = []
    monkeypatch.setattr(clarify, 'current', lambda: Ctx())
    monkeypatch.setattr(clarify, 'register', lambda key, choices, notice=None: Pending())
    monkeypatch.setattr(clarify, 'abandon', abandoned.append)
    decision, note = await agy_trial._approve_vococo_tool(
        'vococo_create_task', {'rawInput': {'title': 'x', 'prompt': '任务'}})
    assert decision == 'deny' and '没有送达' in note
    assert abandoned == ['cid']


@pytest.mark.anyio
async def test_denied_write_tool_without_model_text_returns_receipt_and_does_not_lock(isolated, monkeypatch):
    workspace = isolated / "workspaces" / "agy-handoff" / "session-denied"
    workspace.mkdir(parents=True)
    key = "session:denied"
    session_store.set_chosen_model(key, HIGH)

    async def catalog():
        return {HIGH: "Gemini 3.8 Flash (High)"}

    def prepare(key, *, draft_owner=None):
        return {"workspace": str(workspace), "agent_id": "general", "global_agent_id": "general"}

    async def bundle():
        return {"server": {"name": "vococo-local"}, "read": {},
                "write": {"mcp_vococo-local_vococo_create_task": "vococo_create_task"}}

    async def stream_turn(cwd, timeout, model, prompt, *, session_id=None, approve=None, **kwargs):
        yield "session", NATIVE_ID
        assert await approve("mcp_vococo-local_vococo_create_task", {"rawInput": {"title": "x"}}) == "deny"
        yield "done", None

    async def deny(tool, call):
        return "deny", "已拒绝 Antigravity 的派发请求，本轮未执行。"

    monkeypatch.setattr(agy_trial, "native_catalog", catalog)
    monkeypatch.setattr(agy_trial, "_vococo_mcp_bundle", bundle)
    monkeypatch.setattr(agy_trial, "_approve_vococo_tool", deny)
    monkeypatch.setattr(agy_handoff, "prepare", prepare)
    monkeypatch.setattr(agy_acp, "stream_turn", stream_turn)
    events = [event async for event in agy_trial.stream_native(key, 1, HIGH, "派个任务")]
    done = events[-1]
    assert not done.reply.is_error
    assert "已拒绝" in done.reply.text
    assert session_store.agy_native_session(key) is None


@pytest.mark.anyio
async def test_native_parser_rejects_session_mismatch_and_unexpected_tools(tmp_path):
    class Proc:
        async def wait(self):
            return 0

    import asyncio

    def proc_for(events):
        proc = Proc()
        proc.stdout = asyncio.StreamReader()
        proc.stdout.feed_data("".join(json.dumps(e) + "\n" for e in events).encode())
        proc.stdout.feed_eof()
        return proc

    model = "gemini-3.8-flash-high"
    init = {"event": "init", "conversation_id": NATIVE_ID, "init": {"model": model, "cwd": str(tmp_path)}}
    result = {"event": "result", "result": {"status": "SUCCESS", "response": "测试通过"}}
    with pytest.raises(agy_trial.UpstreamError, match="agy_session_mismatch"):
        async for _ in agy_trial.read_answer(proc_for([init, result]), model, tmp_path,
                                              native=True, resume=str(uuid.uuid4())):
            pass
    tool = {"event": "step_update", "step_update": {"step_type": "tool"}}
    with pytest.raises(agy_trial.UpstreamError, match="agy_unexpected_tool"):
        async for _ in agy_trial.read_answer(proc_for([init, tool, result]), model, tmp_path, native=True):
            pass
    denied = {"event": "result", "result": {"status": "ERROR", "denied_actions": ["search_web"]}}
    with pytest.raises(agy_trial.UpstreamError, match="agy_unexpected_tool"):
        async for _ in agy_trial.read_answer(proc_for([init, denied]), model, tmp_path, native=True):
            pass
