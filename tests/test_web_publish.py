"""发布页路由 /pub/{path} 的测试(见 vococo-web-publish skill)。"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.gateway.adapters.web import WebAdapter


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def published_app(isolated, monkeypatch):
    monkeypatch.setattr(config, "PUBLISHED_DIR", isolated / "data" / "published")
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([web.get("/pub/{path:.*}", adapter._handle_publish)])
    return app


async def _get(app, path):
    """拿 (status, text, headers)——resp 对象离开 TestClient 上下文后连接会关掉,
    读 body 得在 `async with` 里做完,不能把 resp 原样带出去。"""
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(path)
        text = await resp.text()
        return resp.status, text, resp.headers


@pytest.mark.anyio
async def test_publish_serves_file(published_app):
    config.PUBLISHED_DIR.mkdir(parents=True)
    (config.PUBLISHED_DIR / "demo.html").write_text("<h1>hi</h1>", encoding="utf-8")

    status, text, headers = await _get(published_app, "/pub/demo.html")

    assert status == 200
    assert text == "<h1>hi</h1>"
    assert "text/html" in headers["Content-Type"]
    assert "sandbox" in headers["Content-Security-Policy"]
    # 允许本站把发布页嵌进文档预览分屏的 iframe(见 openDocPreview);'self' 只放行本站
    # 自己嵌自己,不影响防第三方站点盗嵌那条线——sandbox 没给 allow-same-origin 才是真正
    # 挡"页面被投毒偷 token"的那道防护,这条改动动不到它(2026-07-30 与用户的讨论)。
    assert "frame-ancestors 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Frame-Options"] == "SAMEORIGIN"


@pytest.mark.anyio
async def test_publish_dir_falls_back_to_index(published_app):
    site = config.PUBLISHED_DIR / "mysite"
    site.mkdir(parents=True)
    (site / "index.html").write_text("home", encoding="utf-8")

    status, text, _headers = await _get(published_app, "/pub/mysite/")

    assert status == 200
    assert text == "home"


@pytest.mark.anyio
async def test_publish_missing_file_404(published_app):
    config.PUBLISHED_DIR.mkdir(parents=True)

    status, _text, _headers = await _get(published_app, "/pub/nope.html")

    assert status == 404


@pytest.mark.anyio
async def test_publish_rejects_path_traversal(published_app):
    config.PUBLISHED_DIR.mkdir(parents=True)
    secret = config.PUBLISHED_DIR.parent / "secret.txt"
    secret.write_text("do not leak", encoding="utf-8")

    status, _text, _headers = await _get(published_app, "/pub/..%2Fsecret.txt")

    assert status == 404


@pytest.mark.anyio
async def test_publish_rejects_hidden_paths(published_app):
    config.PUBLISHED_DIR.mkdir(parents=True)
    (config.PUBLISHED_DIR / ".env").write_text("SECRET=1", encoding="utf-8")

    status, _text, _headers = await _get(published_app, "/pub/.env")

    assert status == 404
