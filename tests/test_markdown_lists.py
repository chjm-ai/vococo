"""真实聊天气泡的多层列表回归，不调用模型。"""
import asyncio
from pathlib import Path

import pytest

from vococo import config
from vococo.gateway.adapters.web import WebAdapter
from vococo.core.prompt import PERSONA
from test_library_integration import _browser_environment


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_output_prompt_preserves_list_hierarchy():
    assert "多层有序、无序或混合列表" in PERSONA
    assert "子项的列表标记对齐父项正文起点" in PERSONA


@pytest.mark.anyio
async def test_nested_lists_in_real_chat(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "WEB_PORT", 0)
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "lists-isolated-test")
    monkeypatch.setattr(config, "VOICE_ENABLED", False)
    adapter = WebAdapter()
    try:
        await adapter._start_server()
        origin = f"http://127.0.0.1:{adapter._runner.addresses[0][1]}"
        process = await asyncio.create_subprocess_exec(
            "node", str(Path(__file__).with_name("markdown_lists_browser.cjs")),
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
    finally:
        if adapter._runner:
            await adapter._runner.cleanup()
