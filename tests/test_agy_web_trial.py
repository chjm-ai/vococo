"""试用仅从已认证的 Web 文字入口接入，不进入其他模型调度路径。"""

import asyncio
import json
import os
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config, providers
from vococo.gateway import agy_trial, core, settings_store
from vococo.gateway.adapters.web import WebAdapter
from vococo.gateway.run import GatewayRunner
from vococo.memory import session_store

MEDIUM = "agy-trial:gemini-3.8-flash-medium"
TOKEN = "test-web-token"
HEADERS = {"X-Auth-Token": TOKEN}


@pytest.fixture
def trial_web(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(settings_store, "_PATH", isolated / "web_settings.json")
    monkeypatch.setenv("VOCOCO_AGY_TRIAL_UNTIL", "2099-01-01")
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([
        web.get("/events", adapter._handle_events), web.get("/models", adapter._handle_models),
        web.post("/model", adapter._handle_model_switch), web.post("/send", adapter._handle_send),
        web.post("/turn/regenerate", adapter._handle_turn_regenerate),
    ])
    return adapter, app


async def wait_event(response, event_type):
    async with asyncio.timeout(95):
        async for raw in response.content:
            if raw.startswith(b"data: "):
                event = json.loads(raw[6:])
                if event.get("type") == event_type:
                    return event
    raise AssertionError(f"未收到 {event_type}")


async def select_model(client, model):
    response = await client.post("/model", json={"conv": "main", "model": model})
    assert response.status == 200, await response.text()


async def submit(client, adapter, text, model=MEDIUM):
    response = await client.post("/send", json={"conv": "main", "text": text, "agy_trial_model": model})
    assert response.status == 200, await response.text()
    return adapter._inbox.get_nowait()


@pytest.mark.anyio
async def test_trial_requires_auth_opt_in_and_does_not_change_default(trial_web, monkeypatch):
    _, app = trial_web
    monkeypatch.setattr(agy_trial, "cli_binary", lambda: config.ROOT_DIR / "deploy/run.sh")
    monkeypatch.setattr(agy_trial, "cli_config_is_clean", lambda: True)
    settings_store.set_web_default_model("deepseek-flash")
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/models")).status == 401
        assert (await client.post("/model", json={"model": MEDIUM})).status == 401
        client.session.headers.update(HEADERS)
        data = await (await client.get("/models")).json()
        assert {mid for mid, _, group in data["choices"] if group == "agy"} == set(agy_trial.CHOICES)
        await select_model(client, MEDIUM)
        key = config.resolve_session_key("web", "main")
        assert settings_store.get_web_default_model() == "deepseek-flash"
        assert not core.handle_command(f"/model {MEDIUM}", key, "deepseek-flash").new_model
        assert all(not agy_trial.is_trial_model(mid) for mid, _, _ in providers.available_models([]))
        with pytest.raises(ValueError, match="网页手动"):
            providers.resolve(MEDIUM, "deepseek-flash")
        stale = await client.post("/send", json={"text": "未同意发送给试用模型"})
        assert stale.status == 409
        denied = await client.post("/send", json={"text": "读这个", "agy_trial_model": MEDIUM,
                                                  "library_refs": [{"id": "unknown"}]})
        assert denied.status == 400
        assert (await client.post("/turn/regenerate", json={"conv": "main", "id": 1})).status == 409
        monkeypatch.setattr(agy_trial, "accepts_session", lambda key: False)
        assert (await client.post("/model", json={"model": MEDIUM})).status == 400
        assert (await client.post("/send", json={"text": "任务会话", "agy_trial_model": MEDIUM})).status == 400
        monkeypatch.setattr(agy_trial, "accepts_session", lambda key: True)
        monkeypatch.setenv("VOCOCO_AGY_TRIAL_UNTIL", "2020-01-01")
        data = await (await client.get("/models")).json()
        assert not any(group == "agy" for _, _, group in data["choices"])
        assert (await client.post("/model", json={"model": MEDIUM})).status == 400
        assert (await client.post("/send", json={"text": "到期了", "agy_trial_model": MEDIUM})).status == 400
        monkeypatch.setenv("VOCOCO_AGY_TRIAL_UNTIL", "2099-01-01")
        monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
        assert (await client.post("/model", json={"model": MEDIUM})).status == 400


@pytest.mark.anyio
async def test_trial_streams_through_web_and_does_not_leak_history(trial_web, monkeypatch):
    adapter, app = trial_web
    runner = GatewayRunner([adapter])
    monkeypatch.setattr(agy_trial, "enabled", lambda: True)
    release = asyncio.Event()
    captured = []

    async def fake_cli(command, cwd, timeout, model, prompt):
        captured.append((model, prompt))
        yield "delta", {"text": "第一段"}
        await release.wait()
        yield "delta", {"text": "第二段"}
        yield "done", {"model": model}

    monkeypatch.setattr(agy_trial, "generate", fake_cli)
    key = config.resolve_session_key("web", "main")
    session_store.append(key, "不要发给试用模型的历史", "不要发送")
    async with TestClient(TestServer(app, handler_cancellation=True), headers=HEADERS) as client:
        await select_model(client, MEDIUM)
        async with await client.get("/events") as stream:
            await wait_event(stream, "hello")
            incoming = await submit(client, adapter, "只发送这句")
            task = asyncio.create_task(runner._handle(adapter, incoming))
            try:
                async with asyncio.timeout(5):
                    assert (await wait_event(stream, "text"))["text"] == "第一段"
                    assert not task.done()
                    release.set()
                    assert (await wait_event(stream, "done"))["text"] == "第一段第二段"
            finally:
                release.set()
                await asyncio.wait_for(task, 5)
        assert captured == [("gemini-3.8-flash-medium", "只发送这句")]
        assert session_store.load_recent(key)[-1].assistant == "第一段第二段"

        await adapter.inject("main", "语音注入，不应发送")
        await runner._handle(adapter, adapter._inbox.get_nowait())
        assert "只支持在网页手动" in session_store.load_recent(key)[-1].assistant
        queued = await submit(client, adapter, "入队后切了模型，不应发送")
        await select_model(client, "deepseek-flash")
        assert (await client.post("/send", json={"text": "过期页面", "agy_trial_model": MEDIUM})).status == 409
        await runner._handle(adapter, queued)
        assert len(captured) == 1


def test_global_hook_and_mcp_are_not_inherited(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    shared = tmp_path / ".gemini/config"
    shared.mkdir(parents=True)
    mcp = shared / "mcp_config.json"
    mcp.write_text("")
    assert agy_trial.cli_config_is_clean()
    mcp.write_text('{"mcpServers": {"test": {}}}')
    assert not agy_trial.cli_config_is_clean()
    mcp.write_text('{"mcpServers": {}}')
    assert agy_trial.cli_config_is_clean()
    (shared / "hooks.json").touch()
    assert not agy_trial.cli_config_is_clean()


@pytest.mark.anyio
@pytest.mark.skipif(os.environ.get("RUN_REAL_AGY_TRIAL") != "1", reason="需本机登录 agy")
@pytest.mark.parametrize("model", list(agy_trial.CHOICES))
async def test_real_agy_response_in_vococo_web(trial_web, model):
    adapter, app = trial_web
    runner = GatewayRunner([adapter])
    async with TestClient(TestServer(app, handler_cancellation=True), headers=HEADERS) as client:
        await select_model(client, model)
        async with await client.get("/events") as stream:
            await wait_event(stream, "hello")
            incoming = await submit(client, adapter, "不要用工具，逐行写出 22 条关于月球的短事实。", model)
            start = time.monotonic()
            task = asyncio.create_task(runner._handle(adapter, incoming))
            try:
                first = await wait_event(stream, "text")
                first_s = time.monotonic() - start
                done = await wait_event(stream, "done")
                finished_s = time.monotonic() - start
                print(f"{model}: 首段 {first_s:.2f}s，完成 {finished_s:.2f}s，{len(done['text'])} 字", flush=True)
                assert not done.get("is_error") and len(done["text"]) > 100, done
                assert done["text"].startswith(first["text"])
                assert len(first["text"]) < len(done["text"])
                assert finished_s - first_s > 0.5, "正文和终态同时到达，未验证真实流式"
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
