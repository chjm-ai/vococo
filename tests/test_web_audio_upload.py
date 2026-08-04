"""音频附件上传/发送:上传只存文件秒回,转写挪到 /send 消费时现场做(见 web.py)。

交互设计:选完文件立刻上传(失败当场报),发送不被"转写中"卡住——/send 收到
待转写音频时现场 await 转写(短音频 1~2s,会议录音 ≥40 分钟走 paraformer 分离),
转写失败把错误文本拼进 transcript 由模型回应,不丢消息。
"""
from __future__ import annotations

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from vococo.gateway.adapters.web import WebAdapter


@pytest.fixture
def adapter():
    return WebAdapter()


@pytest.fixture(autouse=True)
def _no_web_auth(monkeypatch):
    """同 test_voice_p0.py:清空口令,测试不依赖本机 .env 是否配了 WEB_AUTH_TOKEN。"""
    from vococo import config

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


async def _send(upload_app, payload):
    async with TestClient(TestServer(upload_app)) as client:
        return await client.post("/send", json=payload, headers=_auth_headers())


@pytest.mark.anyio
async def test_upload_audio_success_stashes_pending(upload_app, adapter):
    """上传只存文件秒回:返回 id、text 为空(转写留到 /send),暂存里有原始字节。"""
    async with TestClient(TestServer(upload_app)) as client:
        form = FormData()
        form.add_field("audio", b"fake-bytes", filename="memo.mp3", content_type="audio/mpeg")
        resp = await client.post("/upload_audio", data=form, headers=_auth_headers())
        assert resp.status == 200
        data = await resp.json()
        assert data["filename"] == "memo.mp3"
        assert data["text"] == ""  # 转写不在上传做
        assert data["id"] in adapter._pending_audio
        assert adapter._pending_audio[data["id"]][0] == b"fake-bytes"  # 原始字节已存


@pytest.mark.anyio
async def test_upload_audio_over_size_limit_rejected(upload_app, adapter, monkeypatch):
    from vococo import config

    monkeypatch.setattr(config, "AUDIO_MAX_BYTES", 4)  # 逼小上限,不用真造 100MB 载荷

    async with TestClient(TestServer(upload_app)) as client:
        form = FormData()
        form.add_field("audio", b"way-too-big", filename="big.mp3", content_type="audio/mpeg")
        resp = await client.post("/upload_audio", data=form, headers=_auth_headers())
        assert resp.status == 400
        data = await resp.json()
        assert "MB 上限" in data["error"]
    assert not adapter._pending_audio


@pytest.mark.anyio
async def test_send_transcribes_pending_audio_on_the_fly(upload_app, adapter, isolated, monkeypatch):
    """发送时音频还没转写(text 空)→ /send 现场调 transcribe_attachment 转写,
    转写完成才进对话流(带公网 host 供会议路径拼 URL)。"""
    aid = "test-aid-1"
    adapter._pending_audio[aid] = (b"raw-bytes", "memo.mp3", "audio/mpeg", "", 0.0)

    seen = {}

    async def _fake_transcribe(audio, filename, ctype, *, host, timeout_sec=180):
        seen["host"] = host
        seen["timeout"] = timeout_sec
        return "今天下午三点开会", ""

    monkeypatch.setattr("vococo.voice.stt.transcribe_attachment", _fake_transcribe)

    resp = await _send(upload_app, {"conv": "main", "text": "帮我听听", "audios": [{"id": aid}]})
    assert resp.status == 200
    assert seen["timeout"] == 180  # 大文件走加长超时
    inc = await adapter._inbox.get()
    assert len(inc.audios) == 1
    assert inc.audios[0].transcript == "今天下午三点开会"
    assert aid not in adapter._pending_audio  # 已消费


@pytest.mark.anyio
async def test_send_transcribe_failure_injected_into_transcript(upload_app, adapter, isolated, monkeypatch):
    """发送时转写失败:不丢消息,错误文本拼进 transcript,由模型回复里说明。"""
    aid = "test-aid-2"
    adapter._pending_audio[aid] = (b"bad", "weird.xyz", "audio/mpeg", "", 0.0)

    async def _fake_transcribe(audio, filename, ctype, *, host, timeout_sec=180):
        return None, "转写服务返回 415"

    monkeypatch.setattr("vococo.voice.stt.transcribe_attachment", _fake_transcribe)

    resp = await _send(upload_app, {"conv": "main", "text": "听听这个", "audios": [{"id": aid}]})
    assert resp.status == 200
    inc = await adapter._inbox.get()
    assert "转写失败" in inc.audios[0].transcript
    assert "415" in inc.audios[0].transcript


@pytest.mark.anyio
async def test_send_consumes_pending_audio_by_id(upload_app, adapter, isolated, monkeypatch):
    """/send 带已转写好的音频(text 非空)时不再重复调转写,直接消费。"""
    aid = "test-aid-3"
    adapter._pending_audio[aid] = (b"raw-bytes", "memo.mp3", "audio/mpeg", "今天下午三点开会", 0.0)

    called = False

    async def _fake_transcribe(*a, **kw):
        nonlocal called
        called = True
        return "不该被调用", ""

    monkeypatch.setattr("vococo.voice.stt.transcribe_attachment", _fake_transcribe)

    resp = await _send(upload_app, {"conv": "main", "text": "帮我看看这段录音", "audios": [{"id": aid}]})
    assert resp.status == 200
    assert not called  # 已有转写文字,不重复转写
    assert aid not in adapter._pending_audio  # 已被消费,不能重复用
    inc = await adapter._inbox.get()
    assert len(inc.audios) == 1
    au = inc.audios[0]
    assert au.data == b"raw-bytes"
    assert au.filename == "memo.mp3"
    assert au.media_type == "audio/mpeg"
    assert au.transcript == "今天下午三点开会"


@pytest.mark.anyio
async def test_send_audio_only_without_text_gets_fallback_caption(upload_app, adapter, isolated, monkeypatch):
    """只发音频不打字时,要有个兜底文案(跟纯发图片的兜底文案是分开的两句)。"""
    aid = "test-aid-4"
    adapter._pending_audio[aid] = (b"x", "a.wav", "audio/wav", "转写内容", 0.0)

    async def _fake_transcribe(*a, **kw):
        return "不该被调用", ""

    monkeypatch.setattr("vococo.voice.stt.transcribe_attachment", _fake_transcribe)

    resp = await _send(upload_app, {"conv": "main", "text": "", "audios": [{"id": aid}]})
    assert resp.status == 200
    inc = await adapter._inbox.get()
    assert "语音" in inc.text or "音频" in inc.text
