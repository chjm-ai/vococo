"""真实页面回归：切回正在输出的会话时，历史追平不能复制正文。"""
import asyncio
from pathlib import Path

import pytest

from vococo import config
from vococo.gateway.adapters.web import WebAdapter
from test_library_integration import _browser_environment


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("finish_during_restore", [False, True])
async def test_reconnect_replays_completion_before_newer_live_snapshot(isolated, monkeypatch, finish_during_restore):
    import json
    from aiohttp import ClientSession

    monkeypatch.setattr(config, "WEB_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "WEB_PORT", 0)
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "replay-test")
    monkeypatch.setattr(config, "VOICE_ENABLED", False)
    adapter = WebAdapter()
    adapter._emit({"type": "start", "conv": "finished"})
    if not finish_during_restore:
        adapter._emit({"type": "done", "conv": "finished", "text": "完成"})
    adapter._emit({"type": "start", "conv": "running"})
    adapter._emit({"type": "text", "conv": "running", "seg": 0, "text": "仍在输出"})
    if finish_during_restore:
        from aiohttp import web

        original_write = web.StreamResponse.write
        injected = False

        async def write_with_completion(response, data):
            nonlocal injected
            if not injected and b'"live_status"' in data:
                injected = True
                adapter._emit({"type": "done", "conv": "finished", "text": "完成"})
                adapter._emit({"type": "text", "conv": "running", "seg": 0, "text": "继续输出"})
            await original_write(response, data)

        monkeypatch.setattr(web.StreamResponse, "write", write_with_completion)
    try:
        await adapter._start_server()
        origin = f"http://127.0.0.1:{adapter._runner.addresses[0][1]}"
        async with ClientSession() as client:
            async with client.get(origin + "/events", headers={
                "X-Auth-Token": config.WEB_AUTH_TOKEN, "Last-Event-ID": "1",
            }) as response:
                events, last_id = [], 1
                expected_seq = 5 if finish_during_restore else 4
                async with asyncio.timeout(5):
                    while last_id < expected_seq:
                        line = (await response.content.readline()).decode().strip()
                        if not line.startswith("data:"):
                            continue
                        event = json.loads(line[5:])
                        seq = event.get("id", 0)
                        if seq > last_id:
                            events.append(event)
                            last_id = seq
                assert any(e["type"] == "done" and e["conv"] == "finished" for e in events)
                assert [e["id"] for e in events] == sorted(e["id"] for e in events)
    finally:
        if adapter._runner:
            await adapter._runner.cleanup()


@pytest.mark.anyio
async def test_stream_switch_in_browser(isolated, monkeypatch):
    from vococo.core import tasks

    monkeypatch.setattr(tasks, "_DB", None)
    tasks._conn()  # 只读接口需要临时任务库已建好。
    monkeypatch.setattr(config, "WEB_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "WEB_PORT", 0)
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "stream-switch-test")
    monkeypatch.setattr(config, "VOICE_ENABLED", False)
    adapter = WebAdapter()
    try:
        await adapter._start_server()
        origin = f"http://127.0.0.1:{adapter._runner.addresses[0][1]}"
        process = await asyncio.create_subprocess_exec(
            "node", str(Path(__file__).with_name("chat_stream_switch_browser.cjs")),
            origin, config.WEB_AUTH_TOKEN, env=_browser_environment(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        communication = asyncio.create_task(process.communicate())
        try:
            stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout=90)
        except TimeoutError:
            process.kill()
            stdout, stderr = await communication
            pytest.fail("浏览器验证超时：" + stderr.decode() + stdout.decode())
        assert process.returncode == 0, stderr.decode() + stdout.decode()
        print(stdout.decode())
    finally:
        if adapter._runner:
            await adapter._runner.cleanup()
        tasks._DB.close()
        tasks._DB = None
