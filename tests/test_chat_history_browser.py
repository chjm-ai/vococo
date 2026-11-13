"""真实页面与聊天 API 验收；只用临时库，不调用模型或音频服务。"""
import asyncio
import json
from pathlib import Path

import pytest

from test_library_integration import _browser_environment, running_library
from vococo import config
from vococo.memory import agents, chats, session_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_chat_history_in_real_browser(running_library, isolated):
    _, origin, *_ = running_library
    owner = agents.create("历史验收 Agent")
    first = chats.create(owner["id"])["conv"]
    session_store.set_title(first, "已保存的旧聊天")
    session_store.append(first, "第一段提问", "第一段回答")
    other = chats.create("general")["conv"]
    session_store.append(other, "别的 Agent 提问", "不应显示")
    process = await asyncio.create_subprocess_exec(
        "node", str(Path(__file__).with_name("chat_history_browser.cjs")),
        origin, config.WEB_AUTH_TOKEN, owner["id"], first, str(isolated),
        env=_browser_environment(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 100)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, stderr.decode() + stdout.decode()
    assert json.loads(stdout)["ok"]
    assert session_store.load_history(first)[0]["assistant"] == "第一段回答"
    assert not session_store.get_worktree(first)
