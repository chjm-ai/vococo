"""POST /turn/regenerate:重新生成某会话最新一轮 AI 回复(前端消息底部"刷新"按钮)。

只测接口本身的守卫逻辑——真正的"重新入队发送"由前端拿到 {ok, text} 后另调
/send 完成(见 index.html regenerateTurn),不在这个接口里,所以不需要拉起
GatewayRunner/inbox 就能测完。
"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import session_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def regen_web_app(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([web.post("/turn/regenerate", adapter._handle_turn_regenerate)])
    return app


@pytest.mark.anyio
async def test_regenerate_deletes_latest_turn_and_returns_user_text(regen_web_app):
    key = config.resolve_session_key("web", "abc")
    turn_id = session_store.start_turn(key, "重新说一遍")
    session_store.finish_turn(turn_id, "不满意的回答")

    async with TestClient(TestServer(regen_web_app)) as client:
        resp = await client.post("/turn/regenerate", json={"conv": "abc", "id": turn_id})
        assert resp.status == 200
        data = await resp.json()
        assert data == {"ok": True, "text": "重新说一遍"}

    assert session_store.load_history(key) == []


@pytest.mark.anyio
async def test_regenerate_rejects_stale_turn_id(regen_web_app):
    key = config.resolve_session_key("web", "abc")
    older = session_store.start_turn(key, "第一句")
    session_store.finish_turn(older, "回一")
    newer = session_store.start_turn(key, "第二句")
    session_store.finish_turn(newer, "回二")

    async with TestClient(TestServer(regen_web_app)) as client:
        resp = await client.post("/turn/regenerate", json={"conv": "abc", "id": older})
        assert resp.status == 409
        data = await resp.json()
        assert data["ok"] is False

    # 过期请求不能误删任何历史
    assert len(session_store.load_history(key)) == 2


@pytest.mark.anyio
async def test_regenerate_rejects_pending_turn(regen_web_app):
    key = config.resolve_session_key("web", "abc")
    pending = session_store.start_turn(key, "还没答完")

    async with TestClient(TestServer(regen_web_app)) as client:
        resp = await client.post("/turn/regenerate", json={"conv": "abc", "id": pending})
        assert resp.status == 409

    assert len(session_store.load_history(key)) == 1


@pytest.mark.anyio
async def test_regenerate_missing_conv_or_bad_id(regen_web_app):
    async with TestClient(TestServer(regen_web_app)) as client:
        resp = await client.post("/turn/regenerate", json={"conv": "", "id": 1})
        assert resp.status == 400

        resp = await client.post("/turn/regenerate", json={"conv": "abc", "id": "not-a-number"})
        assert resp.status == 400
