"""真实浏览器验收统一会话；仅替换模型输出，不访问付费服务。"""
import asyncio
import json
from pathlib import Path

import pytest

from test_library_integration import _browser_environment
from vococo import config
from vococo.core import task_events, task_runner, tasks
from vococo.core.agent import AgentReply, Done, TextDelta, ToolFinished, ToolInput, ToolStarted
from vococo.gateway import core, mcp_events, settings_store
from vococo.gateway.adapters.web import WebAdapter, _WebSink
from vococo.memory import agents, session_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("mobile", [False, True], ids=["desktop", "mobile"])
async def test_unified_session_in_real_browser(isolated, monkeypatch, mobile):
    for name in ("IMAGES_DIR", "AUDIO_DIR", "FILES_DIR", "VIDEOS_DIR", "PUBLISHED_DIR"):
        monkeypatch.setattr(config, name, config.DATA_DIR / name.lower())
    for name in ("CRON_JOBS_PATH", "SUGGESTIONS_PATH", "HEARTBEAT_PATH", "WATCHDOG_LOG_PATH"):
        monkeypatch.setattr(config, name, config.DATA_DIR / name.lower())
    monkeypatch.setattr(config, "OBSIDIAN_VAULT_DIR", isolated / "vault")
    monkeypatch.setattr(config, "WEB_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "WEB_PORT", 0)
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "unified-browser-test")
    monkeypatch.setattr(config, "VOICE_ENABLED", False)
    monkeypatch.setattr(settings_store, "_PATH", config.DATA_DIR / "web_settings.json")
    monkeypatch.setattr(tasks, "_DB", None)
    monkeypatch.setattr(task_events, "_terminal_handlers", [])
    monkeypatch.setattr(task_events, "_main_event_bridge", None)
    monkeypatch.setattr(task_events, "_stream_sink_factory", None)
    monkeypatch.setattr(task_events, "_started_tasks", set())
    monkeypatch.setattr(task_runner, "_running", {})
    monkeypatch.setattr(mcp_events, "_task_registered", None)
    owner = agents.create("浏览器验收项目")
    release = asyncio.Event()

    async def foreground(*args, **kwargs):
        yield TextDelta("普通会话回复")
        yield Done(AgentReply("普通会话回复", [], None, False))

    async def background(*args, **kwargs):
        yield TextDelta("先检查")
        yield TextDelta("代码")
        yield ToolStarted("Read", "read-browser")
        yield ToolInput("Read", "read-browser", {"file_path": "README.md"})
        yield ToolFinished("Read", True, "读取完成", "read-browser")
        yield TextDelta("执行中")
        await release.wait()
        yield TextDelta("全部完成")
        yield Done(AgentReply("先检查代码执行中全部完成", [], None, False))

    monkeypatch.setattr(core, "stream_turn", foreground)
    monkeypatch.setattr(task_runner, "stream_turn", background)
    adapter = WebAdapter()
    monkeypatch.setattr(adapter, "_push_notify", lambda **kwargs: None)

    async def consume():
        while True:
            message = await adapter._inbox.get()
            await core.converse(message.chat_id, message.text, None,
                                _WebSink(adapter, message.chat_id), cwd_override=owner["workdir"])

    consumer = None
    process = None
    running = []
    try:
        await adapter._start_server()
        origin = f"http://127.0.0.1:{adapter._runner.addresses[0][1]}"
        consumer = asyncio.create_task(consume())
        completed = task_runner.dispatch(title="完成验收任务", prompt="检查", origin="chat",
                                         cwd=owner["workdir"])
        stopped = task_runner.dispatch(title="停止验收任务", prompt="检查停止", origin="chat",
                                       cwd=owner["workdir"])
        running = list(task_runner._running.values())
        process = await asyncio.create_subprocess_exec(
            "node", str(Path(__file__).with_name("unified_session_browser.cjs")),
            origin, config.WEB_AUTH_TOKEN, owner["id"], owner["project_hash"],
            completed["session_key"], stopped["session_key"], "mobile" if mobile else "desktop",
            env=_browser_environment(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        ready = await asyncio.wait_for(process.stdout.readline(), timeout=80)
        assert ready.strip() == b"READY", (ready, (await process.stderr.read()).decode())
        release.set()
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        assert process.returncode == 0, stderr.decode() + stdout.decode()
        await asyncio.gather(*running, return_exceptions=True)
        assert tasks.get(completed["id"])["status"] == "done"
        assert tasks.get(stopped["id"])["status"] == "cancelled"
        normal = json.loads(stdout)["normal"]
        assert session_store.session_binding(normal) == {
            "agent_id": owner["id"], "project_hash": owner["project_hash"],
        }
        assert len(session_store.load_history(normal)) == 2
    finally:
        release.set()
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        if consumer:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        await asyncio.gather(*running, return_exceptions=True)
        if adapter._runner:
            await adapter._runner.cleanup()
        if tasks._DB is not None:
            tasks._DB.close()
            tasks._DB = None
