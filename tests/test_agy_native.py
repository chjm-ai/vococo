import json
import uuid
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.gateway import agy_trial, core, settings_store
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

    async def generate(command, cwd, timeout, model, prompt, *, resume=None, native=False):
        called.append((cwd, model, resume, native))
        assert prompt.startswith("本会话为纯文本模式，不得调用搜索或任何工具")
        if prompt.endswith("first failure"):
            raise agy_trial.UpstreamError("agy_request_failed")
        if prompt.endswith("test search"):
            raise agy_trial.UpstreamError("agy_unexpected_tool")
        yield "delta", {"text": "测试通过"}
        yield "done", {"session_id": NATIVE_ID}

    monkeypatch.setattr(agy_trial, "native_catalog", catalog)
    monkeypatch.setattr(agy_trial, "generate", generate)
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
        assert called[-1] == (lock["cwd"], "gemini-3.8-flash-low", NATIVE_ID, True)
        assert session_store.agy_native_session(conv) == lock
        locked = await (await client.get("/models", params={"conv": conv})).json()
        assert locked["agy_native_locked"] and not locked["agy_handoff"]
        assert {x[0] for x in locked["choices"]} == {HIGH, LOW}
        assert locked["default"] == "deepseek-flash"


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
