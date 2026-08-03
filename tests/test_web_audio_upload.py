"""音频附件上传/发送:先转写后引用发送(见 web.py 的 /upload_audio + /send)。

不走真实的 DashScope 网络调用——mock claude_hermes.voice.stt.transcribe,只验证
web.py 这层的编排:大小上限拦截、转写失败原样报错、转写成功后暂存待 /send 消费、
/send 时按 id 取出拼进 Incoming.audios(不重新走一遍 base64 JSON)。
"""
from __future__ import annotations

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from claude_hermes.gateway.adapters.web import WebAdapter


@pytest.fixture
def adapter():
    return WebAdapter()


@pytest.fixture(autouse=True)
def _no_web_auth(monkeypatch):
    """同 test_voice_p0.py:清空口令,测试不依赖本机 .env 是否配了 WEB_AUTH_TOKEN。"""
    from claude_hermes import config

    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")


@pytest.fixture
def upload_app(adapter):
    app = web.Application()
    app.add_routes([
        web.post("/upload_audio", adapter._handle_upload_audio),
        web.post("/send", adapter._handle_send),
    ])
    return app


def _auth_headers():
    return {"X-Auth-Token": ""}


@pytest.mark.anyio
async def test_upload_audio_success_stashes_pending(upload_app, adapter, monkeypatch):
    from claude_hermes import config

    monkeypatch.setattr(config, "DASHSCOPE_API_KEY", "fake-key")

    async def _fake_transcribe(audio, filename, ctype, *, timeout_sec=30):
        assert timeout_sec == 180  # 大文件走加长超时,不是语音输入框那 30s
        return "今天下午三点开会", ""

    monkeypatch.setattr("claude_hermes.voice.stt.transcribe", _fake_transcribe)

    async with TestClient(TestServer(upload_app)) as client:
        form = FormData()
        form.add_field("audio", b"fake-bytes", filename="memo.mp3", content_type="audio/mpeg")
        resp = await client.post("/upload_audio", data=form, headers=_auth_headers())
        assert resp.status == 200
        data = await resp.json()
        assert data["filename"] == "memo.mp3"
        assert data["text"] == "今天下午三点开会"
        assert data["id"] in adapter._pending_audio


@pytest.mark.anyio
async def test_upload_audio_over_size_limit_rejected(upload_app, adapter, monkeypatch):
    from claude_hermes import config

    monkeypatch.setattr(config, "DASHSCOPE_API_KEY", "fake-key")
    monkeypatch.setattr(config, "AUDIO_MAX_BYTES", 4)  # 逼小上限,不用真造 100MB 载荷

    called = False

    async def _fake_transcribe(*a, **kw):
        nonlocal called
        called = True
        return "不该被调用", ""

    monkeypatch.setattr("claude_hermes.voice.stt.transcribe", _fake_transcribe)

    async with TestClient(TestServer(upload_app)) as client:
        form = FormData()
        form.add_field("audio", b"way-too-big", filename="big.mp3", content_type="audio/mpeg")
        resp = await client.post("/upload_audio", data=form, headers=_auth_headers())
        assert resp.status == 400
        data = await resp.json()
        assert "MB 上限" in data["error"]
    assert not called
    assert not adapter._pending_audio


@pytest.mark.anyio
async def test_upload_audio_transcribe_failure_surfaces_error(upload_app, adapter, monkeypatch):
    """转写失败(格式不支持/服务出错)直接把错误原样返回——不是"某模型不支持",
    是这条转写请求本身失败了,协议层压根没有别的路可走。"""
    from claude_hermes import config

    monkeypatch.setattr(config, "DASHSCOPE_API_KEY", "fake-key")

    async def _fake_transcribe(audio, filename, ctype, *, timeout_sec=30):
        return None, "转写服务返回 415"

    monkeypatch.setattr("claude_hermes.voice.stt.transcribe", _fake_transcribe)

    async with TestClient(TestServer(upload_app)) as client:
        form = FormData()
        form.add_field("audio", b"bad-format", filename="weird.xyz", content_type="application/octet-stream")
        resp = await client.post("/upload_audio", data=form, headers=_auth_headers())
        assert resp.status == 502
        data = await resp.json()
        assert data["error"] == "转写服务返回 415"
    assert not adapter._pending_audio


@pytest.mark.anyio
async def test_send_consumes_pending_audio_by_id(upload_app, adapter, isolated):
    """/send 带 audios:[{id}] 时,从暂存里取出对应字节+转写文字组成 AudioAttachment
    塞进 Incoming,且这条暂存被消费后不能再用(防止同一个 id 被重复发送)。"""
    aid = "test-aid-1"
    adapter._pending_audio[aid] = (b"raw-bytes", "memo.mp3", "audio/mpeg", "今天下午三点开会", 0.0)

    async with TestClient(TestServer(upload_app)) as client:
        resp = await client.post(
            "/send",
            json={"conv": "main", "text": "帮我看看这段录音", "audios": [{"id": aid}]},
            headers=_auth_headers(),
        )
        assert resp.status == 200

    assert aid not in adapter._pending_audio  # 已被消费,不能重复用
    inc = await adapter._inbox.get()
    assert len(inc.audios) == 1
    au = inc.audios[0]
    assert au.data == b"raw-bytes"
    assert au.filename == "memo.mp3"
    assert au.media_type == "audio/mpeg"
    assert au.transcript == "今天下午三点开会"


@pytest.mark.anyio
async def test_send_audio_only_without_text_gets_fallback_caption(upload_app, adapter, isolated):
    """只发音频不打字时,要有个兜底文案(跟纯发图片的兜底文案是分开的两句)。"""
    aid = "test-aid-2"
    adapter._pending_audio[aid] = (b"x", "a.wav", "audio/wav", "转写内容", 0.0)

    async with TestClient(TestServer(upload_app)) as client:
        resp = await client.post(
            "/send", json={"conv": "main", "text": "", "audios": [{"id": aid}]},
            headers=_auth_headers(),
        )
        assert resp.status == 200

    inc = await adapter._inbox.get()
    assert "语音" in inc.text or "音频" in inc.text
