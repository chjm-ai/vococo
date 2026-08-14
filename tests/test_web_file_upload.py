"""Web 通用文件附件：类型不设白名单，由模型/API 决定是否可读取。"""
from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from vococo.core import agent
from vococo.core.agent import FileAttachment
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
    assert "file-id" not in adapter._pending_files


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
