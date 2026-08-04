"""P3 阶段二:Qwen-Omni-Realtime WebRTC 信令代理的测试(见 voice/omni_realtime.py
顶部 docstring)。Omni 现在只当"耳朵"(识别+断句),不生成回复、不调用工具,所以
这里只测信令代理本身,不测 function calling/事件解析(那套已经被拆掉,见
2026-07-10 与 Wesley 的架构调整讨论)。
"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.core import tasks
from vococo.voice import omni_realtime as om, routes


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolated_voice_db(isolated, monkeypatch):
    """强制隔离 DATA_DIR + tasks 连接单例——本文件 2026-07-12 前漏了这层,
    register_routes() 当时还挂着孤儿回收副作用,跑一次测试就把真实 voice.db 里
    正在跑的任务全标失败("假失败"事故根因)。副作用已移去 web.py,这里的隔离
    保留当第二道防线:路由测试永远不许摸到真实数据目录。"""
    monkeypatch.setattr(tasks, "_DB", None)
    yield
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None


def _app() -> web.Application:
    app = web.Application()
    routes.register_routes(app)
    return app


@pytest.mark.anyio
async def test_exchange_webrtc_sdp_requires_workspace_id(monkeypatch):
    monkeypatch.setattr(config, "VOICE_OMNI_WORKSPACE_ID", "")
    with pytest.raises(RuntimeError, match="VOICE_OMNI_WORKSPACE_ID"):
        await om.exchange_webrtc_sdp("v=0\r\n...")


@pytest.mark.anyio
async def test_webrtc_route_proxies_offer_and_returns_sdp_answer(monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    seen_offers = []

    async def fake_exchange(offer_sdp):
        seen_offers.append(offer_sdp)
        return "v=0\r\ns=-\r\n...answer..."

    monkeypatch.setattr(routes.omni_realtime, "exchange_webrtc_sdp", fake_exchange)

    async with TestClient(TestServer(_app())) as client:
        resp = await client.post(
            "/voice/omni/webrtc", data="v=0\r\ns=-\r\n...offer...",
            headers={"Content-Type": "application/sdp"},
        )
        assert resp.status == 200
        assert resp.content_type == "application/sdp"
        body = await resp.text()
        assert body == "v=0\r\ns=-\r\n...answer..."
    assert seen_offers == ["v=0\r\ns=-\r\n...offer..."]


@pytest.mark.anyio
async def test_webrtc_route_rejects_empty_body(monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/voice/omni/webrtc", data="")
        assert resp.status == 400


@pytest.mark.anyio
async def test_webrtc_route_surfaces_upstream_error_as_502(monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")

    async def fake_exchange(offer_sdp):
        raise RuntimeError("WebRTC 信令交换失败 status=404")

    monkeypatch.setattr(routes.omni_realtime, "exchange_webrtc_sdp", fake_exchange)

    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/voice/omni/webrtc", data="v=0\r\n...")
        assert resp.status == 502
        body = await resp.json()
        assert "404" in body["error"]


@pytest.mark.anyio
async def test_webrtc_route_requires_auth_token(monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "secret-token")
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/voice/omni/webrtc", data="v=0\r\n...")
        assert resp.status == 401


@pytest.mark.anyio
async def test_config_route_reports_omni_enabled_flag(monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(config, "VOICE_OMNI_ENABLED", True)
    async with TestClient(TestServer(_app())) as client:
        resp = await client.get("/voice/config")
        body = await resp.json()
        assert body["omni_enabled"] is True
