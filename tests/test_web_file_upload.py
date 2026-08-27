"""Web 通用文件附件：类型不设白名单，由模型/API 决定是否可读取。"""
from __future__ import annotations

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
    assert 'addBubble("me", (shown||fallback)+fileLabel, imgs, auds)' in html


def test_unsent_attachments_are_isolated_by_conversation():
    static = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static"
    composer = (static / "composer.js").read_text(encoding="utf-8")
    index = (static / "index.html").read_text(encoding="utf-8")

    assert "function saveComposerAttachments(conv=S.conv)" in composer
    assert "S.composerAttachments[conv]={images:S.images,audios:S.audios,files:S.files}" in composer
    assert "function restoreComposerAttachments(conv)" in composer
    assert "function saveComposerState(conv=S.conv)" in composer
    assert "function restoreComposerState(conv)" in composer
    assert "saveComposerState(S.conv);" in index
    assert "restoreComposerState(conv);" in index


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
    composer = (Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/composer.js").read_text(
        encoding="utf-8"
    )

    assert "if(!r.ok)" in composer
    assert "S.images=sendImages; S.audios=sendAudios; S.files=sendFiles;" in composer
    assert "S.composerAttachments[oldConv]={images:sendImages,audios:sendAudios,files:sendFiles};" in composer
    assert "附件已保留，可重试" in composer


def test_send_clears_composer_only_after_server_confirmation():
    composer = (Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/composer.js").read_text(
        encoding="utf-8"
    )

    assert "sending: {}," in (
        Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/app-core.js"
    ).read_text(encoding="utf-8")
    assert "只有服务端确认接收后才清理草稿/附件" in composer
    assert "delete S.sending[oldConv]; delete S.sending[sendConv];" in composer
    assert composer.index("await api(\"/send\"") < composer.index("只有服务端确认接收后才清理草稿/附件")


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
