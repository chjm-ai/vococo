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
async def test_stream_switch_in_browser(isolated, monkeypatch):
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
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        assert process.returncode == 0, stderr.decode() + stdout.decode()
        print(stdout.decode())
    finally:
        if adapter._runner:
            await adapter._runner.cleanup()
