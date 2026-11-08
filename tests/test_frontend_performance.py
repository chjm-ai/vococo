"""真实页面性能回归：统计无效 DOM 更新，不以机器速度设脆弱阈值。"""
import asyncio
from pathlib import Path

import pytest

from vococo import config
from vococo.core import tasks
from vococo.gateway.adapters.web import WebAdapter
from test_library_integration import _browser_environment


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("viewport", ["desktop", "mobile"])
async def test_frontend_rendering_in_browser(isolated, monkeypatch, viewport):
    monkeypatch.setattr(config, "WEB_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "WEB_PORT", 0)
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "render-performance-test")
    monkeypatch.setattr(config, "VOICE_ENABLED", False)
    monkeypatch.setattr(tasks, "_DB", None)
    tasks._conn()
    adapter = WebAdapter()
    try:
        await adapter._start_server()
        origin = f"http://127.0.0.1:{adapter._runner.addresses[0][1]}"
        process = await asyncio.create_subprocess_exec(
            "node", str(Path(__file__).with_name("frontend_performance_browser.cjs")),
            origin, config.WEB_AUTH_TOKEN, viewport, env=_browser_environment(),
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
        tasks._DB.close()
        tasks._DB = None
