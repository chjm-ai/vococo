"""真实页面点击 Agent 动态,验证分页、定位和失效锚点。"""
import asyncio
import json
import shutil
from pathlib import Path

import pytest

from vococo import config
from vococo.memory import agents, session_store
from test_library_integration import running_library, _browser_environment  # noqa: F401


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("width", [390, 1280])
async def test_agent_feed_chat_anchor_in_browser(running_library, width):
    if not shutil.which("node"):
        pytest.skip("浏览器冒烟需要 Node")
    probe = await asyncio.create_subprocess_exec(
        "node", "-e", "require.resolve('playwright')", env=_browser_environment(),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    if await probe.wait() != 0:
        pytest.skip("浏览器冒烟需要已安装的 Playwright")
    _, origin, *_ = running_library
    agent = agents.get(agents.GENERAL_ID)
    task_tid = session_store.start_turn("task:anchor-background", "后台执行")
    session_store.finish_turn(task_tid, "后台完整结果" * 100, session_key="task:anchor-background")
    agents.record_run(agent["id"], {"id": "anchor-background", "name": "后台长回复"}, "success",
                      "独立摘要", to_main=False, source_turn_id=task_tid)
    tid = agents.record_run(agent["id"], {"id": "old-cron", "name": "旧日报"}, "success", "锚点结果")
    for i in range(85):
        turn = session_store.start_turn(agents.main_session_key(agent), f"问题 {i}")
        session_store.finish_turn(turn, "普通回复\n\n" * 8, session_key=agents.main_session_key(agent))
    process = await asyncio.create_subprocess_exec(
        "node", str(Path(__file__).with_name("chat_anchors_browser.cjs")),
        origin, config.WEB_AUTH_TOKEN, str(tid), str(task_tid), str(width), env=_browser_environment(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
    assert process.returncode == 0, stderr.decode() + stdout.decode()
    assert json.loads(stdout)["ok"]
