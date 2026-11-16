"""聊天附件走真实上传、解析、模型参数和历史落盘，不调用外部模型。"""
import asyncio
import base64

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.core.agent import AgentReply, Done, TextDelta
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import chats, session_store
from vococo.voice import routes, session


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def chat_app(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(routes, "_lock", asyncio.Lock())
    monkeypatch.setattr(routes, "_prev_text", "")
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([
        web.post("/upload_file", adapter._handle_upload_file),
        web.post("/upload_audio", adapter._handle_upload_audio),
        web.get("/history", adapter._handle_history),
    ])
    routes.register_routes(app, prepare_message=adapter._prepare_message)
    return app, adapter, chats.create("general")["conv"]


@pytest.mark.anyio
async def test_chat_uploaded_file_image_audio_and_history(chat_app, monkeypatch):
    app, adapter, conv = chat_app
    seen = {}

    async def stream(history, prompt, **kwargs):
        seen.update(kwargs, prompt=prompt)
        yield TextDelta("已读取附件。")
        yield Done(AgentReply(text="已读取附件。", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(session, "stream_turn", stream)
    monkeypatch.setattr(session, "_model_candidates", lambda _: ["test-model"])
    monkeypatch.setattr("vococo.voice.stt.transcribe_attachment", transcribe)
    image = base64.b64encode(b"image bytes").decode()
    async with TestClient(TestServer(app)) as client:
        form = FormData()
        form.add_field("file", b"report contents", filename="report.txt", content_type="text/plain")
        response = await client.post("/upload_file", data=form)
        assert response.status == 200
        file_id = (await response.json())["id"]
        form = FormData()
        form.add_field("audio", b"audio bytes", filename="memo.wav", content_type="audio/wav")
        response = await client.post("/upload_audio", data=form)
        assert response.status == 200
        audio_id = (await response.json())["id"]
        response = await client.post(f"/voice/send?conv={conv}", json={
            "text": "读取这些附件", "tts": False,
            "files": [{"id": file_id}], "audios": [{"id": audio_id}],
            "images": [{"data": image, "media_type": "image/png"}],
        })
        assert response.status == 200
        assert "event: done" in await response.text()
        response = await client.get("/history", params={"conv": conv})
        history = (await response.json())["turns"]
    assert len(history) == 1
    assert history[0]["user"] == "读取这些附件"
    assert history[0]["assistant"] == "已读取附件。"
    assert len(history[0]["images"]) == 1
    assert history[0]["files"][0]["name"] == "report.txt"
    assert history[0]["audios"][0]["text"] == "录音转写内容"
    assert seen["images"][0].data == image
    assert seen["files"][0].data == b"report contents"
    assert seen["files"][1].data.decode() == "录音转写内容"
    assert "本机文件" in seen["prompt"]
    assert not adapter._pending_files and not adapter._pending_audio


@pytest.mark.anyio
async def test_failed_stream_acceptance_restores_all_uploads(chat_app):
    from types import SimpleNamespace

    _, adapter, conv = chat_app
    adapter._pending_files["file"] = (b"data", "file.txt", "text/plain", 0.0)
    adapter._pending_audio["audio"] = (b"audio", "audio.wav", "audio/wav", "转写", 0.0)
    adapter._pending_videos["video"] = (b"video", "video.mp4", "video/mp4", 0.0)

    async def fail():
        raise ConnectionResetError("建流失败")

    with pytest.raises(ConnectionResetError):
        await adapter._prepare_message(SimpleNamespace(host="localhost"), {
            "conv": conv, "text": "", "files": [{"id": "file"}],
            "audios": [{"id": "audio"}], "videos": [{"id": "video"}],
        }, before_accept=fail)
    assert "file" in adapter._pending_files
    assert "audio" in adapter._pending_audio
    assert "video" in adapter._pending_videos
    assert not session_store.load_recent(conv)


@pytest.mark.anyio
async def test_chat_video_and_library_reference_use_shared_prompt(chat_app, monkeypatch):
    from vococo.core.agent import ImageAttachment

    app, adapter, conv = chat_app
    adapter._pending_videos["clip"] = (b"video bytes", "clip.mp4", "video/mp4", 0.0)
    seen = {}
    monkeypatch.setattr("vococo.gateway.library_api.resolve_refs", lambda refs: (
        [ImageAttachment("aW1hZ2U=", "image/png")], ["作品网址：https://example.com/demo"],
    ))

    async def run(prompt, **kwargs):
        seen.update(prompt=prompt, **kwargs["attachments"])
        yield Done(AgentReply(text="收到", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(session, "run_turn", run)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(f"/voice/send?conv={conv}", json={
            "text": "", "tts": False, "videos": [{"id": "clip"}], "library_refs": ["item"],
        })
        assert response.status == 200
        await response.read()
        history = await (await client.get("/history", params={"conv": conv})).json()
    assert "模型无法直接观看视频" in seen["prompt"]
    assert "https://example.com/demo" in seen["prompt"]
    assert len(seen["images"]) == 1
    assert len(history["turns"]) == 1
    assert history["turns"][0]["videos"][0]["filename"] == "clip.mp4"
    assert not adapter._pending_videos


async def transcribe(*args, **kwargs):
    return "录音转写内容", ""


@pytest.mark.anyio
@pytest.mark.parametrize("files", [[{"id": "expired"}], [{"id": "ok"}, {"id": "ok"}], [123]])
async def test_chat_invalid_attachment_does_not_consume_or_save(chat_app, files):
    app, adapter, conv = chat_app
    adapter._pending_files["ok"] = (b"data", "ok.txt", "text/plain", 0.0)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(f"/voice/send?conv={conv}", json={"text": "", "files": files})
        assert response.status == 400
    assert "ok" in adapter._pending_files
    assert session_store.load_recent(conv) == []


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_chat_file_only_persists_one_turn_even_when_interrupted(chat_app, monkeypatch, outcome):
    app, adapter, conv = chat_app
    adapter._pending_files["ok"] = (b"data", "ok.txt", "text/plain", 0.0)

    async def run(prompt, **kwargs):
        assert kwargs["attachments"]["files"][0].filename == "ok.txt"
        if outcome == "error":
            raise RuntimeError("模型失败")
        if outcome == "cancel":
            raise asyncio.CancelledError()
        yield Done(AgentReply(text="完成", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(session, "run_turn", run)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(f"/voice/send?conv={conv}", json={
            "text": "", "files": [{"id": "ok"}], "tts": False,
        })
        assert response.status == 200
        await response.read()
    history = session_store.load_recent(conv)
    assert len(history) == 1
    assert "文件附件" in history[0].user
    assert history[0].assistant
