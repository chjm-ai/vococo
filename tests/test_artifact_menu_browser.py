"""触屏 WebKit 不保证按钮获焦，菜单关闭不能只依赖失焦。"""
import asyncio
from pathlib import Path

import pytest

from test_library_integration import _browser_environment


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_artifact_menu_webkit_touch_outside_escape_and_action():
    process = await asyncio.create_subprocess_exec(
        "node", str(Path(__file__).with_name("artifact_menu_browser.cjs")),
        env=_browser_environment(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
        if process.returncode == 77:
            pytest.skip("未安装匹配的 WebKit；可用 WEBKIT_PLAYWRIGHT_MODULE 指定现有配套驱动，不自动安装")
        assert process.returncode == 0, stderr.decode() + stdout.decode()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
