"""外部 MCP 按任务加载：不改全局开关，手动开关只属于当前会话。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from vococo.core import agent
from vococo.gateway import clarify, settings_store
from vococo.tools import builtin


def _run(coro):
    return asyncio.run(coro)


def _text(result: dict) -> str:
    return result["content"][0]["text"]


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    monkeypatch.setattr(settings_store, "_PATH", tmp_path / "web_settings.json")
    for name in ("lemlist", "dataforseo", "analytics-mcp"):
        err = settings_store.upsert_external(name, {
            "type": "stdio", "command": "python3", "args": [], "env": {}, "enabled": True,
        })
        assert err is None
    return tmp_path


def _load() -> dict:
    return json.loads(settings_store._PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize(("msg", "expected"), [
    ("查一下 Lemlist 的 campaign 回复", {"lemlist"}),
    ("分析这个域名的反链和关键词排名", {"dataforseo"}),
    ("查 GA4 最近 7 天的网站流量", {"analytics-mcp"}),
])
def test_task_intent_selects_only_needed_mcp(msg, expected):
    assert agent._external_mcp_for_task(msg) == expected


@pytest.mark.parametrize("msg", [
    "SEO 是什么意思？", "介绍一下 GA4", "你好", "帮我写篇文章",
])
def test_non_action_text_does_not_load_external_mcp(msg):
    assert agent._external_mcp_for_task(msg) == set()


def test_auto_match_never_mutates_global_enabled(cfg):
    settings_store.set_external_enabled("analytics-mcp", False)
    agent._external_mcp_for_task("查 GA4 网站流量")
    assert _load()["external_mcp"]["analytics-mcp"]["enabled"] is False


def test_effective_external_mcp_filters_requested_servers(cfg):
    assert set(settings_store.effective_external_mcp({"lemlist"})) == {"lemlist"}
    assert set(settings_store.effective_external_mcp(set())) == set()


def test_continue_reuses_recent_automatic_mcp(monkeypatch):
    monkeypatch.setattr(agent.session_store, "set_auto_external_mcp_names", lambda *_: None)
    monkeypatch.setattr(
        agent.session_store, "get_recent_auto_external_mcp_names", lambda *_: {"analytics-mcp"},
    )
    assert agent._external_mcp_for_task("继续查刚才的数据", "web:abc") == {"analytics-mcp"}


def test_manual_switch_only_changes_current_session(monkeypatch, cfg):
    active: set[str] = set()
    monkeypatch.setattr(builtin.session_store, "get_external_mcp_names", lambda _: set(active))
    monkeypatch.setattr(
        builtin.session_store, "set_external_mcp_names", lambda _, names: active.update(names),
    )
    token = clarify._current.set(SimpleNamespace(session_key="web:one"))
    try:
        out = _text(_run(builtin.set_external_mcp.handler({"name": "lemlist", "enabled": True})))
    finally:
        clarify._current.reset(token)
    assert "当前会话" in out
    assert active == {"lemlist"}
    assert _load()["external_mcp"]["lemlist"]["enabled"] is True
