"""真实 WebAdapter 页面上的流式、历史与隔离预览回归；不调用模型。"""
import asyncio
import os
from pathlib import Path

import pytest
from aiohttp import ClientSession

from vococo import config
from vococo.gateway.adapters.web import WebAdapter
from test_library_integration import _browser_environment


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", [
    "existing", "sequence-height-dark", "sequence-height-light", "sequence-theme-layout", "sequence-stream-theme", "manual-height",
])
async def test_chat_preview_real_browser(isolated, monkeypatch, scenario):
    monkeypatch.setattr(config, "WEB_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "WEB_PORT", 0)
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "preview-isolated-test")
    monkeypatch.setattr(config, "VOICE_ENABLED", False)
    adapter = WebAdapter()
    try:
        await adapter._start_server()
        origin = f"http://127.0.0.1:{adapter._runner.addresses[0][1]}"
        async with ClientSession() as client:
            for asset in ("preview.js", "mermaid-11.12.2.min.js"):
                async with client.get(origin + "/" + asset) as response:
                    assert response.status == 200
                    assert response.headers["Content-Type"].startswith("text/javascript")
                    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
        screenshot_dir = Path(os.environ.get("PREVIEW_SCREENSHOTS", str(isolated)))
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            "node", str(Path(__file__).with_name("chat_preview_browser.cjs")),
            origin, config.WEB_AUTH_TOKEN, str(screenshot_dir), scenario, env=_browser_environment(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=150)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        assert process.returncode == 0, stderr.decode() + stdout.decode()
        print(stdout.decode())
    finally:
        if adapter._runner:
            await adapter._runner.cleanup()
