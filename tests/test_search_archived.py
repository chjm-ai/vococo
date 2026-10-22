"""真实页面与本地接口验证搜索默认归档列表。"""
import asyncio
from pathlib import Path

import pytest

from vococo import config
from vococo.memory import session_store
from test_library_integration import running_library, _browser_environment  # noqa: F401


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_search_defaults_to_archived_in_browser(running_library):
    _, origin, *_ = running_library
    for key, title in (("web:archived", "最近归档测试"), ("task:archived", "归档任务测试"),
                       ("web:active", "未归档关键词测试")):
        session_store.set_title(key, title)
        session_store.append(key, "隔离测试消息", "好")
        session_store.set_conv_archived(key, key != "web:active")
    process = await asyncio.create_subprocess_exec(
        "node", str(Path(__file__).with_name("search_archived_browser.cjs")),
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
