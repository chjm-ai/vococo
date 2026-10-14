"""Web 通用文件附件：类型不设白名单，由模型/API 决定是否可读取。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from vococo.core import agent
from vococo.core.agent import FileAttachment, ImageAttachment
from vococo.gateway.adapters.web import WebAdapter


@pytest.fixture
def adapter():
    return WebAdapter()


@pytest.fixture(autouse=True)
def _no_web_auth(monkeypatch):
    from vococo import config

    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")


@pytest.fixture
def file_app(adapter):
    app = web.Application()
    app.add_routes([
        web.post("/upload_file", adapter._handle_upload_file),
        web.post("/send", adapter._handle_send),
    ])
    return app


@pytest.mark.anyio
async def test_upload_accepts_doc_and_unknown_extension(file_app, adapter):
    """不因 DOC 或任意扩展名在浏览器/服务端被拒绝。"""
    async with TestClient(TestServer(file_app)) as client:
        doc = FormData()
        doc.add_field("file", b"doc bytes", filename="brief.doc", content_type="application/msword")
        doc_resp = await client.post("/upload_file", data=doc, headers={"X-Auth-Token": ""})
        assert doc_resp.status == 200
        doc_data = await doc_resp.json()

        unknown = FormData()
        unknown.add_field("file", b"nd bytes", filename="sample.nd", content_type="application/x-nd")
        unknown_resp = await client.post("/upload_file", data=unknown, headers={"X-Auth-Token": ""})
        assert unknown_resp.status == 200
        unknown_data = await unknown_resp.json()

    assert adapter._pending_files[doc_data["id"]][:3] == (
        b"doc bytes", "brief.doc", "application/msword"
    )
    assert adapter._pending_files[unknown_data["id"]][:3] == (
        b"nd bytes", "sample.nd", "application/x-nd"
    )


@pytest.mark.anyio
async def test_send_consumes_uploaded_file(file_app, adapter, isolated):
    adapter._pending_files["file-id"] = (
        b"pdf bytes", "report.pdf", "application/pdf", 0.0
    )

    async with TestClient(TestServer(file_app)) as client:
        resp = await client.post(
            "/send",
            json={"conv": "main", "text": "", "files": [{"id": "file-id"}]},
            headers={"X-Auth-Token": ""},
        )

    assert resp.status == 200
    inc = await adapter._inbox.get()
    assert "文件附件" in inc.text
    assert [(f.data, f.filename, f.media_type) for f in inc.files] == [
        (b"pdf bytes", "report.pdf", "application/pdf")
    ]
    prompt = agent._build_prompt([], inc.text, inc.images, inc.files)
    message = await anext(prompt)
    assert "pdf bytes" in message["message"]["content"][1]["text"]
    assert "file-id" not in adapter._pending_files


@pytest.mark.anyio
async def test_send_user_event_carries_client_request_id(file_app, adapter, monkeypatch):
    """HTTP 回执丢失时，前端只能用匹配的 SSE 事件确认本次发送已入队。"""
    events = []
    monkeypatch.setattr(adapter, "_emit", events.append)

    async with TestClient(TestServer(file_app)) as client:
        resp = await client.post(
            "/send",
            json={"conv": "main", "text": "确认这条消息", "client_request_id": "msg-123"},
            headers={"X-Auth-Token": ""},
        )

    assert resp.status == 200
    assert events == [{
        "conv": "main", "type": "user", "text": "确认这条消息", "images": [],
        "client_request_id": "msg-123",
    }]


@pytest.mark.anyio
async def test_send_timeout_restores_audio_and_same_request_retry_enqueues_once(
    file_app, adapter, isolated, monkeypatch,
):
    """第二段转写超时也要恢复第一段，重试及重复回执只入队一次。"""
    pending_audio = {
        "first": (b"first bytes", "first.mp3", "audio/mpeg", "第一段", 0.0),
        "second": (b"second bytes", "second.wav", "audio/wav", "", 0.0),
    }
    adapter._pending_audio.update(pending_audio)
    cancelled = asyncio.Event()
    transcribe_calls = []

    async def transcribe(data, filename, media_type, **kwargs):
        transcribe_calls.append((data, filename, media_type))
        if len(transcribe_calls) == 1:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return "第二段", ""

    monkeypatch.setattr("vococo.voice.stt.transcribe_attachment", transcribe)
    monkeypatch.setattr("vococo.gateway.adapters.web._SEND_REQUEST_TIMEOUT", 0.05)
    body = {
        "conv": "main", "text": "听这两段", "client_request_id": "audio-timeout-retry",
        "audios": [{"id": "first"}, {"id": "second"}],
    }
    events = []
    monkeypatch.setattr(adapter, "_emit", events.append)
    async with TestClient(TestServer(file_app)) as client:
        resp = await client.post("/send", json=body)
        assert resp.status == 504
        assert cancelled.is_set()
        assert adapter._pending_audio == pending_audio
        assert not adapter._send_pending
        assert not adapter._send_receipts
        assert adapter._inbox.empty()
        assert not events

        resp = await client.post("/send", json=body)
        assert resp.status == 200
        assert await resp.json() == {"ok": True}
        assert not adapter._pending_audio
        assert adapter._inbox.qsize() == 1
        incoming = adapter._inbox.get_nowait()
        assert incoming.text == body["text"]
        assert [(a.data, a.filename, a.media_type, a.transcript) for a in incoming.audios] == [
            (b"first bytes", "first.mp3", "audio/mpeg", "第一段"),
            (b"second bytes", "second.wav", "audio/wav", "第二段"),
        ]

        resp = await client.post("/send", json=body)
        assert resp.status == 200
        assert await resp.json() == {"ok": True}
        assert adapter._inbox.empty()
        assert len(events) == 1
        assert events[0]["client_request_id"] == body["client_request_id"]
        assert len(transcribe_calls) == 2


@pytest.mark.anyio
async def test_send_reports_expired_uploaded_file_instead_of_silently_dropping(file_app, adapter):
    async with TestClient(TestServer(file_app)) as client:
        resp = await client.post(
            "/send",
            json={"conv": "main", "text": "读取附件", "files": [{"id": "expired-id"}]},
            headers={"X-Auth-Token": ""},
        )
        data = await resp.json()

    assert resp.status == 400
    assert "上传已失效" in data["error"]
    assert adapter._inbox.empty()


@pytest.mark.anyio
async def test_upload_file_over_size_limit_rejected(file_app, adapter, monkeypatch):
    from vococo import config

    monkeypatch.setattr(config, "FILE_MAX_BYTES", 4)
    async with TestClient(TestServer(file_app)) as client:
        form = FormData()
        form.add_field("file", b"way-too-big", filename="large.bin")
        resp = await client.post("/upload_file", data=form, headers={"X-Auth-Token": ""})
        data = await resp.json()

    assert resp.status == 400
    assert "MB 上限" in data["error"]
    assert not adapter._pending_files


def test_sent_bubble_lists_file_even_when_message_has_text():
    # 2026-08-14 前端模块化:发送逻辑在 composer.js(原 index.html 内联)
    html = (Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/composer.js").read_text(
        encoding="utf-8"
    )

    assert 'const fileLabel=files.length ? `\\n\\n📎 附件：${files.join("、")}` : "";' in html
    # 末尾的 true 是 eph 标记:这颗乐观气泡的内容服务端也会落库,权威历史重绘后要清掉它,
    # 否则同一句话会重复显示一遍(见 stream.js 的 tagConvNode / index.html 的 renderTurns)
    # vids 跟在 eph 之后:视频不进 fileLabel 文字,直接在气泡里渲染播放器(见 appendVids)
    assert 'addBubble("me", (shown||fallback)+fileLabel, imgs, auds, true, vids)' in html


def _check_composer_state(scenario):
    import subprocess

    script = Path(__file__).with_name("composer_library_state.cjs")
    subprocess.run(["node", str(script), scenario], check=True, capture_output=True, text=True, timeout=15)


def test_unsent_attachments_are_isolated_by_conversation():
    _check_composer_state("isolation")


def test_file_drop_reuses_upload_validation_and_reports_unsupported_formats():
    composer = (Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/composer.js").read_text(
        encoding="utf-8"
    )

    assert "function handleAttachmentFiles(files)" in composer
    assert "handleAttachmentFiles(e.dataTransfer?.files);" in composer
    assert "function rejectUnsupportedFile(f)" in composer
    assert "暂不支持文件" in composer
    assert "else if(isSupportedFile(f)) addFile(f);" in composer


def test_send_failure_keeps_file_attachment_for_retry():
    _check_composer_state("retry")


def test_send_uses_matching_sse_ack_when_http_receipt_is_lost():
    static = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static"
    composer = (static / "composer.js").read_text(encoding="utf-8")
    stream = (static / "stream.js").read_text(encoding="utf-8")
    core = (static / "app-core.js").read_text(encoding="utf-8")

    assert "sendAcks: {}," in core
    assert "client_request_id:clientRequestId," in composer
    assert "await waitForSendAck(clientRequestId)" in composer
    assert "acknowledgeSend(e.client_request_id)" in stream


def test_history_replays_file_and_audio_names():
    root = Path(__file__).parents[1] / "vococo"
    store = (root / "memory/session_store.py").read_text(encoding="utf-8")
    stream = (root / "gateway/adapters/web_static/stream.js").read_text(encoding="utf-8")
    index = (root / "gateway/adapters/web_static/index.html").read_text(encoding="utf-8")

    assert "entry[\"files\"]" in store
    assert '"filename": e.get("filename", "")' in store
    assert 'name.textContent="🎵 "+a.filename' in stream
    assert 'const fileNames=(t.files||[])' in index


def test_send_clears_visible_composer_and_keeps_background_retry_snapshot():
    _check_composer_state("background")


@pytest.mark.anyio
async def test_image_attachment_includes_persisted_local_path():
    """图片既作为视觉内容传入，也要告诉模型受控落盘路径。"""
    prompt = agent._build_prompt(
        [], "用这张图做头像",
        [ImageAttachment("QUJD", "image/png", "/tmp/chat-images/12_0.png")], [],
    )
    message = await anext(prompt)

    assert message["message"]["content"][1] == {
        "type": "text",
        "text": "[聊天图片附件 1，可用 Read 工具读取]\n本机文件：/tmp/chat-images/12_0.png",
    }
    assert message["message"]["content"][2]["type"] == "image"


@pytest.mark.anyio
async def test_utf8_html_attachment_becomes_text_content_block():
    """HTML 正文必须作为文本送入模型，不能依赖 document block 的供应商兼容性。"""
    prompt = agent._build_prompt(
        [], "读取这个文件", [],
        [FileAttachment(b"<h1>Hello</h1>", "text/html", "sample.html")],
    )
    message = await anext(prompt)

    assert message["message"]["content"][1] == {
        "type": "text",
        "text": "[文件附件: sample.html]\n<h1>Hello</h1>",
    }


@pytest.mark.anyio
async def test_file_attachment_prompt_carries_local_path():
    """落盘路径必须进提示词:没有它模型就只有一个磁盘上不存在的原始文件名,
    想用工具处理原始文件(xlsx 读表/pdf 抽页)只能瞎猜位置。"""
    text_att = FileAttachment(b"# hi", "text/markdown", "方案.md", local_path="/tmp/f/9_0.md")
    bin_att = FileAttachment(b"\0\xff", "application/pdf", "报价.pdf", local_path="/tmp/f/9_1.pdf")
    prompt = agent._build_prompt([], "看看附件", [], [text_att, bin_att])
    message = await anext(prompt)
    content = message["message"]["content"]

    assert content[1] == {
        "type": "text",
        "text": "[文件附件: 方案.md]\n本机文件（可直接读取/处理）：/tmp/f/9_0.md\n# hi",
    }
    assert content[2] == {
        "type": "text",
        "text": "[文件附件: 报价.pdf]\n本机文件（可直接读取/处理）：/tmp/f/9_1.pdf",
    }
    assert content[3]["type"] == "document"  # 二进制正文仍照旧走 document block


@pytest.mark.anyio
async def test_binary_file_attachment_becomes_document_content_block():
    """二进制文件维持 document block，交由上游判断是否支持。"""
    prompt = agent._build_prompt(
        [], "读取这个文件", [],
        [FileAttachment(b"\0\xff", "application/x-nd", "sample.nd")],
    )
    message = await anext(prompt)
    document = message["message"]["content"][1]

    assert document["type"] == "document"
    assert document["title"] == "sample.nd"
    assert document["source"] == {
        "type": "base64",
        "media_type": "application/x-nd",
        "data": "AP8=",
    }
