"""/api/usage 用量查询测试:验证 Claude 官方限额与本地日志估算合并逻辑。

2026-07-28:接入 claude-monitor 作本地估算兜底,避免官方 RateLimitEvent
经常 utilization=null 导致前端看不到具体百分比。
"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_hermes.gateway.adapters.web import WebAdapter


@pytest.fixture
def usage_app():
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([web.get("/api/usage", adapter._handle_api_usage)])
    return app


async def _get(client, path):
    resp = await client.get(path, headers={"X-Auth-Token": ""})
    return resp.status, await resp.json()


def _local_usage(data: dict):
    async def _inner():
        return data
    return _inner


@pytest.mark.anyio
async def test_usage_uses_official_when_utilization_present(usage_app, monkeypatch):
    """官方有 utilization 时优先用官方,但仍附本地详情供 hover 卡片。"""
    monkeypatch.setattr(
        "claude_hermes.gateway.adapters.web.get_rate_limits",
        lambda: {
            "five_hour": {"utilization": 0.5, "limit": 1000, "remaining": 500, "resets_at": 1234567890},
            "seven_day": {"utilization": 0.3, "limit": 7000, "remaining": 4900},
        },
    )
    monkeypatch.setattr(
        "claude_hermes.gateway.adapters.web.get_local_claude_usage",
        _local_usage({
            "provider": "claude",
            "source": "local_estimate",
            "limits": {
                "five_hour": {"utilization": 0.6, "limit": 1000, "remaining": 400},
            },
            "local": {"cost_usd": 1.23, "sent_messages": 10},
            "forecast": {"display": "Soon"},
        }),
    )

    async with TestClient(TestServer(usage_app)) as client:
        status, data = await _get(client, "/api/usage?model=claude-sonnet-4-6")
        assert status == 200
        assert data["provider"] == "claude"
        assert data["source"] == "official"
        assert data["limits"]["five_hour"]["utilization"] == 0.5
        assert data["limits"]["five_hour"]["source"] == "official"
        # 本地详情仍应透传,供 hover 卡片展示
        assert data["local"]["cost_usd"] == 1.23
        assert data["forecast"]["display"] == "Soon"


@pytest.mark.anyio
async def test_usage_falls_back_to_local_when_official_missing(usage_app, monkeypatch):
    """官方 utilization 缺失时退回本地估算,并标注来源。"""
    monkeypatch.setattr(
        "claude_hermes.gateway.adapters.web.get_rate_limits",
        lambda: {"five_hour": {"resets_at": 1234567890}},
    )
    monkeypatch.setattr(
        "claude_hermes.gateway.adapters.web.get_local_claude_usage",
        _local_usage({
            "provider": "claude",
            "source": "local_estimate",
            "limits": {
                "five_hour": {"utilization": 0.65, "limit": 1000, "remaining": 350, "resets_at": 1234567890},
            },
            "local": {"cost_usd": 2.34, "sent_messages": 20},
            "forecast": {},
        }),
    )

    async with TestClient(TestServer(usage_app)) as client:
        status, data = await _get(client, "/api/usage?model=claude-sonnet-4-6")
        assert status == 200
        assert data["source"] == "local_estimate"
        assert data["limits"]["five_hour"]["utilization"] == 0.65
        assert data["limits"]["five_hour"]["source"] == "local_estimate"


@pytest.mark.anyio
async def test_usage_returns_api_for_non_subscription(usage_app, monkeypatch):
    """DeepSeek 等按量计费模型返回 api 类型,不打本地日志。"""
    # 模拟一个按量计费 provider 配置
    def fake_lookup(model: str):
        if "deepseek" in model.lower():
            return {"name": "deepseek", "base_url": "https://api.deepseek.com", "api_key": "sk-x"}
        return None

    monkeypatch.setattr(
        "claude_hermes.gateway.adapters.web.providers.lookup_provider_by_model",
        fake_lookup,
    )

    async with TestClient(TestServer(usage_app)) as client:
        status, data = await _get(client, "/api/usage?model=deepseek-chat")
        assert status == 200
        assert data == {"provider": "api", "type": "api"}
