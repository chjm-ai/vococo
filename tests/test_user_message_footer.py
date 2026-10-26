"""用户消息底栏的真实页面回归。"""
import asyncio
from pathlib import Path

import pytest

from vococo import config
from test_library_integration import running_library, _browser_environment  # noqa: F401


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_user_message_footer_in_browser(running_library):
    _, origin, *_ = running_library
    process = await asyncio.create_subprocess_exec(
        "node", str(Path(__file__).with_name("user_message_footer_browser.cjs")),
        origin, config.WEB_AUTH_TOKEN, env=_browser_environment(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    communication = asyncio.create_task(process.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout=60)
    except TimeoutError:
        process.kill()
        stdout, stderr = await communication
        pytest.fail("浏览器超时：" + stderr.decode() + stdout.decode())
    assert process.returncode == 0, stderr.decode() + stdout.decode()
