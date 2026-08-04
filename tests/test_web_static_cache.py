"""静态资源缓存策略测试(2026-07-23 传输提速,见 web.py 版本号注入/_versioned_static)。

背景:跨境隧道带宽 ~50KB/s、首字节 1.5s+,提速三板斧:
1. index.html 里 CSS/JS 引用注入 ?v=<内容哈希>,带 v 的请求回 immutable 长缓存;
2. 静态资源全部支持 ETag/304 协商,内容没变不重传正文;
3. Service Worker 外壳缓存(前端,见 sw.js)。
"""
from __future__ import annotations

import hashlib
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo.gateway.adapters import web as web_mod
from vococo.gateway.adapters.web import WebAdapter


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def static_app():
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes(
        [
            web.get("/", adapter._handle_index),
            web.get("/styles.css", adapter._handle_styles),
            web.get("/tool-card.js", adapter._handle_tool_card_js),
            web.get("/sw.js", adapter._handle_sw),
        ]
    )
    return app


async def _get(app, path, headers=None):
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(path, headers=headers or {})
        body = await resp.read()
        return resp.status, body, resp.headers


def _digest(name: str) -> str:
    data = (web_mod._STATIC / name).read_bytes()
    return hashlib.md5(data).hexdigest()[:12]


@pytest.mark.anyio
async def test_index_injects_asset_versions(static_app):
    status, body, _ = await _get(static_app, "/")
    assert status == 200
    html = body.decode("utf-8")
    assert f'"/styles.css?v={_digest("styles.css")}"' in html
    assert f'"/tool-card.js?v={_digest("tool-card.js")}"' in html
    # 裸引用不应残留
    assert '"/styles.css"' not in html
    assert '"/tool-card.js"' not in html


@pytest.mark.anyio
async def test_versioned_asset_gets_immutable_cache(static_app):
    status, _, headers = await _get(static_app, f"/styles.css?v={_digest('styles.css')}")
    assert status == 200
    assert headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert headers.get("ETag")


@pytest.mark.anyio
async def test_bare_asset_stays_no_cache(static_app):
    status, _, headers = await _get(static_app, "/styles.css")
    assert status == 200
    assert headers["Cache-Control"] == "no-cache"


@pytest.mark.anyio
async def test_asset_etag_304_roundtrip(static_app):
    _, _, headers = await _get(static_app, "/styles.css")
    etag = headers["ETag"]
    status, body, _ = await _get(static_app, "/styles.css", {"If-None-Match": etag})
    assert status == 304
    assert body == b""


@pytest.mark.anyio
async def test_sw_etag_304_roundtrip(static_app):
    _, _, headers = await _get(static_app, "/sw.js")
    assert headers["Cache-Control"] == "no-cache"
    status, _, _ = await _get(static_app, "/sw.js", {"If-None-Match": headers["ETag"]})
    assert status == 304


@pytest.mark.anyio
async def test_history_etag_304_roundtrip(monkeypatch):
    """/history:重会话 JSON 一包几百 KB,内容没变时必须走 304 空包(切会话大多是回看)。"""
    from vococo.memory import session_store

    adapter = WebAdapter()
    adapter._guard = lambda request: None  # 测缓存协商,鉴权不在本测试范围
    monkeypatch.setattr(
        session_store, "load_history", lambda key, limit=40: [{"user": "hi", "assistant": "yo"}]
    )
    app = web.Application()
    app.add_routes([web.get("/history", adapter._handle_history)])

    status, body, headers = await _get(app, "/history?conv=main")
    assert status == 200
    assert json.loads(body)["turns"][0]["user"] == "hi"
    etag = headers["ETag"]

    status, body, _ = await _get(app, "/history?conv=main", {"If-None-Match": etag})
    assert status == 304
    assert body == b""


@pytest.mark.anyio
async def test_index_etag_tracks_asset_changes(static_app, monkeypatch, tmp_path):
    """CSS 内容一变 → 注入的 v 变 → index.html 字节变 → ETag 变,客户端必拿到新 HTML。"""
    fake = tmp_path / "web_static"
    fake.mkdir()
    (fake / "index.html").write_text('<link href="/styles.css">', encoding="utf-8")
    (fake / "styles.css").write_text("body{}", encoding="utf-8")
    monkeypatch.setattr(web_mod, "_STATIC", fake)

    _, _, h1 = await _get(static_app, "/")
    (fake / "styles.css").write_text("body{color:red}", encoding="utf-8")
    _, body2, h2 = await _get(static_app, "/")

    assert h1["ETag"] != h2["ETag"]
    assert f'?v={_digest("styles.css")}' in body2.decode("utf-8")
