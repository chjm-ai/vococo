"""文档预览分屏的接口:GET /doc/preview(见与用户的设计讨论——聊天里的文档链接/
工具卡文件路径点击后右侧滑出预览,本地文件这条路径需要后端读文件内容出来)。

_conv_cwd 直接打桩成一个临时目录,不去绕 session_store 的项目哈希映射——
这里只测路径安全校验(越界拒绝)和按后缀/大小分支的行为,不是测项目绑定逻辑本身。
"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_hermes import config
from claude_hermes.gateway.adapters.web import WebAdapter


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def doc_root(isolated):
    root = isolated / "project"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def doc_app(isolated, doc_root, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(WebAdapter, "_conv_cwd", lambda self, conv: str(doc_root))
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([web.get("/doc/preview", adapter._handle_doc_preview)])
    return app


async def _get(app, path):
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(path)
        body = await resp.read()
        return resp.status, body, resp.headers


@pytest.mark.anyio
async def test_reads_text_file_in_conv_cwd(doc_app, doc_root):
    (doc_root / "report.md").write_text("# 标题\n正文", encoding="utf-8")

    status, body, headers = await _get(doc_app, "/doc/preview?conv=x&path=report.md")

    assert status == 200
    assert body.decode("utf-8") == "# 标题\n正文"
    assert "text" in headers["Content-Type"]


@pytest.mark.anyio
async def test_reads_absolute_path_inside_cwd(doc_app, doc_root):
    (doc_root / "notes.txt").write_text("hi", encoding="utf-8")

    status, body, _headers = await _get(doc_app, "/doc/preview?conv=x&path=" + str(doc_root / "notes.txt"))

    assert status == 200
    assert body == b"hi"


@pytest.mark.anyio
async def test_rejects_path_traversal(doc_app, doc_root):
    secret = doc_root.parent / "secret.txt"
    secret.write_text("do not leak", encoding="utf-8")

    status, _body, _headers = await _get(doc_app, "/doc/preview?conv=x&path=../secret.txt")

    assert status == 404


@pytest.mark.anyio
async def test_rejects_absolute_path_outside_cwd(doc_app, doc_root):
    secret = doc_root.parent / "secret.txt"
    secret.write_text("do not leak", encoding="utf-8")

    status, _body, _headers = await _get(doc_app, "/doc/preview?conv=x&path=" + str(secret))

    assert status == 404


@pytest.mark.anyio
async def test_missing_file_404(doc_app):
    status, _body, _headers = await _get(doc_app, "/doc/preview?conv=x&path=nope.md")

    assert status == 404


@pytest.mark.anyio
async def test_oversized_file_413(doc_app, doc_root, monkeypatch):
    from claude_hermes.gateway.adapters import web as web_module

    monkeypatch.setattr(web_module, "_DOC_PREVIEW_MAX", 10)
    (doc_root / "big.txt").write_text("x" * 100, encoding="utf-8")

    status, _body, _headers = await _get(doc_app, "/doc/preview?conv=x&path=big.txt")

    assert status == 413


@pytest.mark.anyio
async def test_pdf_gets_pdf_content_type(doc_app, doc_root):
    (doc_root / "doc.pdf").write_bytes(b"%PDF-1.4 fake")

    status, _body, headers = await _get(doc_app, "/doc/preview?conv=x&path=doc.pdf")

    assert status == 200
    assert headers["Content-Type"] == "application/pdf"


@pytest.mark.anyio
async def test_requires_auth_token_when_configured(isolated, doc_root, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "s3cr3t")
    monkeypatch.setattr(WebAdapter, "_conv_cwd", lambda self, conv: str(doc_root))
    (doc_root / "a.txt").write_text("hi", encoding="utf-8")
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([web.get("/doc/preview", adapter._handle_doc_preview)])

    status, _body, _headers = await _get(app, "/doc/preview?conv=x&path=a.txt")

    assert status == 401


# ── AI_BRAIN 兜底根目录:非项目会话下 AI 常把笔记直接写进 Obsidian vault(比如
# "00-inbox/xxx.md" 这种收件箱惯例),不在会话 cwd 范围内——不是"公网访问不到本地
# 文件"(HTTP 请求本来就是服务端执行,客户端在哪没区别),原来只认会话 cwd 会把这类
# 合法文件误判成"越界"拒绝,见与用户的讨论。 ──────────────────────────────────


@pytest.mark.anyio
async def test_falls_back_to_ai_brain_when_not_in_conv_cwd(doc_app):
    inbox = config.AI_BRAIN_DIR / "00-inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "note.md").write_text("笔记正文", encoding="utf-8")

    status, body, _headers = await _get(doc_app, "/doc/preview?conv=x&path=00-inbox/note.md")

    assert status == 200
    assert body.decode("utf-8") == "笔记正文"


@pytest.mark.anyio
async def test_conv_cwd_wins_over_ai_brain_on_name_collision(doc_app, doc_root):
    (doc_root / "same.md").write_text("项目里的版本", encoding="utf-8")
    (config.AI_BRAIN_DIR).mkdir(parents=True, exist_ok=True)
    (config.AI_BRAIN_DIR / "same.md").write_text("AI_BRAIN 里的版本", encoding="utf-8")

    status, body, _headers = await _get(doc_app, "/doc/preview?conv=x&path=same.md")

    assert status == 200
    assert body.decode("utf-8") == "项目里的版本"


@pytest.mark.anyio
async def test_ai_brain_fallback_still_rejects_traversal(doc_app):
    secret = config.AI_BRAIN_DIR.parent / "secret.txt"
    secret.write_text("do not leak", encoding="utf-8")
    config.AI_BRAIN_DIR.mkdir(parents=True, exist_ok=True)

    status, _body, _headers = await _get(doc_app, "/doc/preview?conv=x&path=../secret.txt")

    assert status == 404
