"""真实页面验证新建 Agent 与目录选择器的嵌套交互。"""
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
async def test_agent_directory_picker_stays_above_creation_modal(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "WEB_PORT", 0)
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "directory-test")
    monkeypatch.setattr(config, "VOICE_ENABLED", False)
    monkeypatch.setattr(config, "CRON_JOBS_PATH", isolated / "jobs.json")
    directory = isolated / "工作目录"
    directory.mkdir()
    for width in (1280, 390):
        (directory / str(width)).mkdir()
    monkeypatch.setattr(WebAdapter, "_browse_roots", staticmethod(lambda: [isolated]))
    adapter = WebAdapter()
    process = None
    try:
        await adapter._start_server()
        origin = f"http://127.0.0.1:{adapter._runner.addresses[0][1]}"
        process = await asyncio.create_subprocess_exec(
            "node", str(Path(__file__).with_name("agent_directory_browser.cjs")),
            origin, config.WEB_AUTH_TOKEN, str(directory), env=_browser_environment(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
        assert process.returncode == 0, stderr.decode() + stdout.decode()
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        if adapter._runner:
            await adapter._runner.cleanup()
