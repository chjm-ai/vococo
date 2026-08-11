"""Web 通用文件附件：类型不设白名单，由模型/API 决定是否可读取。"""
from __future__ import annotations

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


@pytest.mark.anyio
async def test_file_attachment_becomes_document_content_block():
    """文件原样编码为 document block，不按类型提前转换或拒绝。"""
    prompt = agent._build_prompt(
        [], "读取这个文件", [],
        [FileAttachment(b"hello", "application/x-nd", "sample.nd")],
    )
    message = await anext(prompt)
    document = message["message"]["content"][1]

    assert document["type"] == "document"
    assert document["title"] == "sample.nd"
    assert document["source"] == {
        "type": "base64",
        "media_type": "application/x-nd",
        "data": "aGVsbG8=",
    }
