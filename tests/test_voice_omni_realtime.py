"""P3 阶段一:Qwen-Omni-Realtime 后端骨架的测试(见 voice/omni_realtime.py 顶部
docstring)。不连真实 DashScope——events() 解析用假 WS 推事件,function calling
桥接复用 test_voice_p1.py 同款 fake_stream_turn 套路,不实际跑 Claude/网络。
"""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import aiohttp
import pytest

from claude_hermes.voice import executor, notify, omni_realtime as om, task_tools, tasks


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def voice_db(isolated, monkeypatch):
    monkeypatch.setattr(tasks, "_DB", None)
    executor._running.clear()
    yield
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None


class FakeWs:
    """假的 Omni-Realtime WS 连接,只用来喂 events() 解析,不建立真实网络连接。"""

    def __init__(self, events: list[dict]) -> None:
        self._events = events

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        data = self._events.pop(0)
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(data))


def _session_with_fake_ws(events: list[dict]) -> om.OmniRealtimeSession:
    sess = om.OmniRealtimeSession()
    sess._ws = FakeWs(events)
    return sess


def test_build_tools_exposes_the_three_task_tools():
    tools = om._build_tools()
    names = {t["name"] for t in tools}
    assert names == {"voice_dispatch_task", "voice_query_task", "voice_list_tasks"}
    for t in tools:
        assert t["type"] == "function"
        assert t["description"]
        assert t["parameters"]["type"] == "object"


def test_build_instructions_has_no_leftover_placeholders():
    text = om._build_instructions()
    assert "{user_text}" not in text
    assert "{long_task_hint}" not in text
    assert "{timeout_min}" not in text
    assert "voice_dispatch_task" in text


@pytest.mark.anyio
async def test_events_parses_audio_transcript_function_call_and_done():
    audio_b64 = base64.b64encode(b"\x01\x02\x03").decode("ascii")
    sess = _session_with_fake_ws([
        {"type": "response.audio.delta", "delta": audio_b64},
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "帮我查天气"},
        {"type": "response.text.delta", "delta": "好的"},
        {
            "type": "response.function_call_arguments.done",
            "call_id": "call_1",
            "name": "voice_dispatch_task",
            "arguments": json.dumps({"title": "查天气", "prompt": "查一下今天天气"}),
        },
        {"type": "input_audio_buffer.speech_started"},
        {"type": "response.done"},
        {"type": "error", "error": {"message": "限流了"}},
        # 不认识的事件类型应该被跳过,不能让 events() 炸掉
        {"type": "session.updated", "session": {}},
    ])
    out = [ev async for ev in sess.events()]
    assert out == [
        om.OmniAudioDelta(b"\x01\x02\x03"),
        om.OmniTranscript("帮我查天气"),
        om.OmniTextDelta("好的"),
        om.OmniFunctionCall("call_1", "voice_dispatch_task", {"title": "查天气", "prompt": "查一下今天天气"}),
        om.OmniSpeechStarted(),
        om.OmniTurnDone(),
        om.OmniError("限流了"),
    ]


@pytest.mark.anyio
async def test_handle_function_call_dispatches_task_via_existing_executor(voice_db, monkeypatch):
    async def fake_stream_turn(history, prompt, cwd=None, session_key=None, **kw):
        return
        yield  # pragma: no cover

    monkeypatch.setattr(executor, "stream_turn", fake_stream_turn)
    monkeypatch.setattr(notify, "on_task_terminal", lambda *_a, **_k: _noop())

    call = om.OmniFunctionCall("call_1", "voice_dispatch_task", {"title": "翻译", "prompt": "翻译 README"})
    result = await om.handle_function_call(call)
    assert "task_id=" in result
    assert tasks.get_latest() is not None


@pytest.mark.anyio
async def test_handle_function_call_unknown_tool_returns_error_text_not_exception():
    call = om.OmniFunctionCall("call_1", "not_a_real_tool", {})
    result = await om.handle_function_call(call)
    assert "没有名为" in result


async def _noop():
    return None
