"""后台任务必须在完成前把过程送到 Web，并能恢复进行中的内容。"""
import pytest

from vococo.core import task_events, task_runner, tasks
from vococo.core.agent import (
    AgentReply, Compacted, Done, TextDelta, ThinkingDelta,
    ToolFinished, ToolInput, ToolStarted, UpstreamRetry,
)
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import session_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["done", "failed", "cancelled", "timeout", "model_error"])
async def test_task_streams_before_completion_and_finishes_once(isolated, monkeypatch, outcome):
    import asyncio

    monkeypatch.setattr(tasks, "_DB", None)
    monkeypatch.setattr(task_events, "_terminal_handlers", [])
    monkeypatch.setattr(task_events, "_main_event_bridge", None)
    monkeypatch.setattr(task_events, "_stream_sink_factory", None)
    monkeypatch.setattr(task_events, "_started_tasks", set())
    monkeypatch.setattr(task_runner, "_running", {})
    adapter = WebAdapter()
    events = []
    original_emit = adapter._emit

    def capture(event):
        events.append(event)
        original_emit(event)

    monkeypatch.setattr(adapter, "_emit", capture)
    monkeypatch.setattr(adapter, "_push_notify", lambda **kw: None)

    reached_checkpoint = False

    async def stream(*args, session_key=None, **kwargs):
        nonlocal reached_checkpoint
        yield ThinkingDelta("正在分析")
        yield TextDelta("先检查")
        yield TextDelta("代码")
        yield ToolStarted("Read", "read-1")
        yield ToolInput("Read", "read-1", {"file_path": "README.md"})
        yield ToolFinished("Read", True, "读取完成", "read-1")
        yield TextDelta("已确认")
        yield UpstreamRetry(status=503, attempt=1, max_retries=3, delay_ms=100)
        yield Compacted("auto")
        types = {event["type"] for event in events}
        assert {"thinking", "text", "tool_start", "tool_input", "tool_end",
                "upstream_retry", "compact"} <= types
        assert "done" not in types
        assert session_key in adapter._live
        assert session_store.load_history(session_key)[-1]["draft"] == "先检查"
        text_events = [e for e in events if e["type"] == "text"]
        assert any(e["text"] == "先检查代码" and e["seg"] == 0 for e in text_events)
        assert any(e["text"] == "已确认" and e["seg"] == 1 for e in text_events)
        reached_checkpoint = True
        if outcome == "cancelled":
            raise asyncio.CancelledError
        if outcome == "failed":
            raise RuntimeError("测试错误")
        if outcome == "timeout":
            raise asyncio.TimeoutError
        yield Done(AgentReply("完成", [], None, outcome == "model_error", error="模型出错" if outcome == "model_error" else ""))

    monkeypatch.setattr(task_runner, "stream_turn", stream)
    row = task_runner.dispatch(title="实时任务", prompt="检查", origin="chat", cwd=str(isolated))
    await task_runner._running[row["id"]]
    key = tasks.session_key(row["id"])
    assert reached_checkpoint, tasks.get(row["id"])
    expected_status = "failed" if outcome in {"timeout", "model_error"} else outcome
    assert tasks.get(row["id"])["status"] == expected_status, tasks.get(row["id"])
    assert {"thinking", "text", "tool_start", "tool_end", "compact"} <= {e["type"] for e in events}
    assert key not in adapter._live
    terminal = [e for e in events if e["type"] in {"done", "cancelled"}]
    assert len(terminal) == 1
    assert terminal[0].get("turn_id") is not None or outcome == "cancelled"
    saved = session_store.load_history(key)[-1]["assistant"]
    assert saved
    if outcome in {"failed", "cancelled", "timeout"}:
        assert "先检查代码已确认" in saved
    assert terminal[0]["is_error"] == (expected_status == "failed")
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None


@pytest.mark.anyio
async def test_foreground_message_refreshes_sidebar_after_pending_turn_is_saved(isolated, monkeypatch):
    from vococo.gateway import core
    from vococo.gateway.adapters.web import _WebSink

    adapter = WebAdapter()
    snapshots = []
    key = "web:ordering-test"
    session_store.append(key, "旧问题", "旧回答")
    old_ts = session_store.list_sessions("web:ordering-test")[0]["last_ts"]

    def capture(event):
        if event["type"] in {"start", "sidebar_refresh"}:
            snapshots.append((event["type"], session_store.list_sessions(key)[0]["last_ts"]))

    monkeypatch.setattr(adapter, "_emit", capture)
    monkeypatch.setattr(adapter, "_push_notify", lambda **kw: None)

    during_stream = []

    async def stream(*args, **kwargs):
        during_stream.extend(snapshots)
        yield Done(AgentReply("完成", [], None, False))

    monkeypatch.setattr(core, "stream_turn", stream)
    await core.converse(key, "新指令", None, _WebSink(adapter, "ordering-test"), cwd_override=str(isolated))
    assert during_stream[0] == ("start", old_ts)
    assert any(kind == "sidebar_refresh" and ts > old_ts for kind, ts in during_stream)


@pytest.mark.anyio
async def test_foreground_task_followup_emits_only_one_done(isolated, monkeypatch):
    from vococo.gateway import core
    from vococo.gateway.adapters.web import _WebSink

    monkeypatch.setattr(tasks, "_DB", None)
    monkeypatch.setattr(task_events, "_terminal_handlers", [])
    monkeypatch.setattr(task_events, "_main_event_bridge", None)
    monkeypatch.setattr(task_events, "_stream_sink_factory", None)
    adapter = WebAdapter()
    events = []
    monkeypatch.setattr(adapter, "_emit", events.append)
    monkeypatch.setattr(adapter, "_push_notify", lambda **kw: None)
    task_events.register_main_event_bridge(events.append)
    row = tasks.create("续聊", "问题", cwd=str(isolated), origin="chat")
    key = tasks.session_key(row["id"])

    async def stream(*args, **kwargs):
        yield TextDelta("续聊回答")
        yield Done(AgentReply("续聊回答", [], None, False))

    monkeypatch.setattr(core, "stream_turn", stream)
    await core.converse(key, "继续", None, _WebSink(adapter, key), cwd_override=str(isolated))
    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1, events
    assert done[0]["turn_id"] is not None
    assert tasks.get(row["id"])["status"] == "done"
    tasks._DB.close()
    tasks._DB = None


@pytest.mark.anyio
async def test_task_live_stream_in_browser(isolated, monkeypatch):
    import asyncio
    from pathlib import Path

    from vococo import config
    from test_library_integration import _browser_environment
    from vococo.gateway import mcp_events

    monkeypatch.setattr(mcp_events, "_task_registered", None)
    monkeypatch.setattr(config, "WEB_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "WEB_PORT", 0)
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "task-live-test")
    monkeypatch.setattr(config, "VOICE_ENABLED", False)
    monkeypatch.setattr(tasks, "_DB", None)
    monkeypatch.setattr(task_events, "_terminal_handlers", [])
    monkeypatch.setattr(task_events, "_main_event_bridge", None)
    monkeypatch.setattr(task_events, "_stream_sink_factory", None)
    monkeypatch.setattr(task_events, "_started_tasks", set())
    monkeypatch.setattr(task_runner, "_running", {})
    release = asyncio.Event()

    async def stream(*args, **kwargs):
        yield ThinkingDelta("正在分析")
        yield TextDelta("先检查")
        yield TextDelta("代码")
        yield ToolStarted("Read", "read-1")
        yield ToolInput("Read", "read-1", {"file_path": "README.md"})
        yield ToolFinished("Read", True, "读取完成", "read-1")
        yield TextDelta("执行中")
        await release.wait()
        yield TextDelta("全部完成")
        yield Done(AgentReply("先检查代码执行中全部完成", [], None, False))

    monkeypatch.setattr(task_runner, "stream_turn", stream)
    adapter = WebAdapter()
    monkeypatch.setattr(adapter, "_push_notify", lambda **kw: None)
    process = None
    running = None
    try:
        await adapter._start_server()
        origin = f"http://127.0.0.1:{adapter._runner.addresses[0][1]}"
        row = task_runner.dispatch(title="实时任务", prompt="检查", origin="chat", cwd=str(isolated))
        running = task_runner._running[row["id"]]
        process = await asyncio.create_subprocess_exec(
            "node", str(Path(__file__).with_name("task_live_browser.cjs")),
            origin, config.WEB_AUTH_TOKEN, tasks.session_key(row["id"]),
            env=_browser_environment(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        ready = await asyncio.wait_for(process.stdout.readline(), timeout=60)
        assert ready.strip() == b"READY", (ready, await process.stderr.read())
        release.set()
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        assert process.returncode == 0, stderr.decode() + stdout.decode()
        await running
    finally:
        release.set()
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        if running:
            await running
        if adapter._runner:
            await adapter._runner.cleanup()
        if tasks._DB is not None:
            tasks._DB.close()
            tasks._DB = None
