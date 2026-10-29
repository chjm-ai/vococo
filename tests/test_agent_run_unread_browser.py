"""真实定时结果经 SSE 到达页面后，已查看与未查看的标记应一致。"""
import asyncio
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from vococo import config
from vococo.memory import agents, session_store
from test_library_integration import running_library, _browser_environment  # noqa: F401


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_agent_run_unread_in_browser(running_library, monkeypatch):
    adapter, origin, *_ = running_library
    agent = agents.create("灰点回归")
    monkeypatch.setattr(agents, "_listener", lambda conv: adapter._emit({"conv": conv, "type": "agent_run"}))

    async def record(request):
        data = await request.json()
        tid = agents.record_run(agent["id"], {"id": "unread-test", "name": "测试日报"}, "success", data["text"])
        return web.json_response({"turn_id": tid})

    control = web.Application()
    control.router.add_post("/record", record)
    process = None
    async with TestServer(control) as server:
        try:
            process = await asyncio.create_subprocess_exec(
                "node", str(Path(__file__).with_name("agent_run_unread_browser.cjs")),
                origin, config.WEB_AUTH_TOKEN, str(server.make_url("/record")), agent["main_conv"],
                env=_browser_environment(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
            assert process.returncode == 0, stderr.decode() + stdout.decode()
            assert not next(r for r in session_store.list_sessions("") if r["key"] == agents.main_session_key(agent))["pending_review"]
        finally:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
