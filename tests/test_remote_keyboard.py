"""用真实 WebAdapter 验证遥控键盘导航，不连接蓝牙或在线模型。"""
import asyncio
import json
from pathlib import Path

import pytest

from vococo import config
from test_library_integration import running_library, _browser_environment  # noqa: F401


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_remote_keyboard_in_browser(running_library, tmp_path):
    _, origin, *_ = running_library
    process = await asyncio.create_subprocess_exec(
        "node", str(Path(__file__).with_name("remote_keyboard_browser.cjs")),
        origin, config.WEB_AUTH_TOKEN, str(tmp_path / "remote-keyboard.png"),
        env=_browser_environment(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
    assert process.returncode == 0, stderr.decode() + stdout.decode()
    assert json.loads(stdout)["ok"]
