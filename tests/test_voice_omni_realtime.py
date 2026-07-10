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
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_hermes import config
from claude_hermes.voice import executor, notify, omni_realtime as om, routes, task_tools, tasks


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _app() -> web.Application:
    app = web.Application()
    routes.register_routes(app)
    return app


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


@pytest.mark.anyio
async def test_exchange_webrtc_sdp_requires_workspace_id(monkeypatch):
    monkeypatch.setattr(config, "VOICE_OMNI_WORKSPACE_ID", "")
    with pytest.raises(RuntimeError, match="VOICE_OMNI_WORKSPACE_ID"):
        await om.exchange_webrtc_sdp("v=0\r\n...")


@pytest.mark.anyio
async def test_webrtc_route_proxies_offer_and_returns_sdp_answer(monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    seen_offers = []

    async def fake_exchange(offer_sdp):
        seen_offers.append(offer_sdp)
        return "v=0\r\ns=-\r\n...answer..."

    monkeypatch.setattr(routes.omni_realtime, "exchange_webrtc_sdp", fake_exchange)

    async with TestClient(TestServer(_app())) as client:
        resp = await client.post(
            "/voice/omni/webrtc", data="v=0\r\ns=-\r\n...offer...",
            headers={"Content-Type": "application/sdp"},
        )
        assert resp.status == 200
        assert resp.content_type == "application/sdp"
        body = await resp.text()
        assert body == "v=0\r\ns=-\r\n...answer..."
    assert seen_offers == ["v=0\r\ns=-\r\n...offer..."]


@pytest.mark.anyio
async def test_webrtc_route_rejects_empty_body(monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/voice/omni/webrtc", data="")
        assert resp.status == 400


@pytest.mark.anyio
async def test_webrtc_route_surfaces_upstream_error_as_502(monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")

    async def fake_exchange(offer_sdp):
        raise RuntimeError("WebRTC 信令交换失败 status=404")

    monkeypatch.setattr(routes.omni_realtime, "exchange_webrtc_sdp", fake_exchange)

    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/voice/omni/webrtc", data="v=0\r\n...")
        assert resp.status == 502
        body = await resp.json()
        assert "404" in body["error"]


@pytest.mark.anyio
async def test_webrtc_route_requires_auth_token(monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "secret-token")
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/voice/omni/webrtc", data="v=0\r\n...")
        assert resp.status == 401


@pytest.mark.anyio
async def test_config_route_reports_omni_enabled_flag(monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(config, "VOICE_OMNI_ENABLED", True)
    async with TestClient(TestServer(_app())) as client:
        resp = await client.get("/voice/config")
        body = await resp.json()
        assert body["omni_enabled"] is True


@pytest.mark.anyio
async def test_function_call_route_dispatches_task_via_existing_executor(voice_db, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")

    async def fake_stream_turn(history, prompt, cwd=None, session_key=None, **kw):
        return
        yield  # pragma: no cover

    monkeypatch.setattr(executor, "stream_turn", fake_stream_turn)
    monkeypatch.setattr(notify, "on_task_terminal", lambda *_a, **_k: _noop())

    async with TestClient(TestServer(_app())) as client:
        resp = await client.post(
            "/voice/omni/function_call",
            json={"name": "voice_dispatch_task", "arguments": {"title": "翻译", "prompt": "翻译 README"}},
        )
        assert resp.status == 200
        body = await resp.json()
        assert "task_id=" in body["output"]
    assert tasks.get_latest() is not None


@pytest.mark.anyio
async def test_function_call_route_rejects_missing_name(monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/voice/omni/function_call", json={"arguments": {}})
        assert resp.status == 400


@pytest.mark.anyio
async def test_function_call_route_requires_auth_token(monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "secret-token")
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/voice/omni/function_call", json={"name": "x", "arguments": {}})
        assert resp.status == 401
