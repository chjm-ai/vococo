"""外贸 MCP 按需加载:A(一句话开关 set_external_mcp)+ B(关键词自动触发)。

monkeypatch config.DATA_DIR 指向临时目录(settings_path 跟随),不碰真实 data/web_settings.json。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from vococo import config
from vococo.core import agent
from vococo.gateway import settings_store
from vococo.tools import builtin


def _run(coro):
    return asyncio.run(coro)


def _text(result: dict) -> str:
    return result["content"][0]["text"]


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    """把 settings_store 指到临时目录,并预置一个外部 MCP(默认关闭)。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    err = settings_store.upsert_external("lemlist", {
        "type": "stdio", "command": "python3",
        "args": ["lemlist_lite.py"], "env": {"LEMLIST_API_KEY": "k"},
        "enabled": False,
    })
    assert err is None
    return tmp_path


def _load() -> dict:
    return json.loads(settings_store._path().read_text(encoding="utf-8"))


# === A:set_external_mcp 一句话开关 ===
def test_set_external_mcp_off_to_on(cfg):
    out = _text(_run(builtin.set_external_mcp.handler({"name": "lemlist", "enabled": True})))
    assert "开启" in out and "lemlist" in out
    assert _load()["external_mcp"]["lemlist"]["enabled"] is True


def test_set_external_mcp_on_to_off(cfg):
    settings_store.set_external_enabled("lemlist", True)
    out = _text(_run(builtin.set_external_mcp.handler({"name": "lemlist", "enabled": False})))
    assert "关闭" in out
    assert _load()["external_mcp"]["lemlist"]["enabled"] is False


def test_set_external_mcp_unknown_name(cfg):
    out = _text(_run(builtin.set_external_mcp.handler({"name": "nope", "enabled": True})))
    assert "未找到" in out
    assert "nope" not in _load()["external_mcp"]  # 未知名不会被误建


def test_set_external_mcp_toolkit_all(cfg):
    """「外贸工具包」一键全开:多个 server 一起切。"""
    settings_store.upsert_external("dataforseo", {"type": "stdio", "command": "npx", "args": [], "env": {}, "enabled": False})
    out = _text(_run(builtin.set_external_mcp.handler({"name": "外贸工具包", "enabled": True})))
    assert "开启" in out and "lemlist" in out and "dataforseo" in out
    d = _load()["external_mcp"]
    assert d["lemlist"]["enabled"] is True and d["dataforseo"]["enabled"] is True


# === B:关键词自动触发 ===
@pytest.mark.parametrize("msg", [
    "帮我拓客,找越南面料采购", "查一下 lemlist 的 campaign 回复", "外贸客户开发",
    "看看 google ads 搜索量", "GA4 网站流量分析", "退订名单里有谁",
])
def test_auto_enable_on_trade_keyword(cfg, msg):
    agent._maybe_auto_enable_trade_mcp(msg)
    assert _load()["external_mcp"]["lemlist"]["enabled"] is True


@pytest.mark.parametrize("msg", ["你好", "帮我写篇文章", "整理一下 Things 任务"])
def test_auto_enable_ignores_normal_talk(cfg, msg):
    agent._maybe_auto_enable_trade_mcp(msg)
    assert _load()["external_mcp"]["lemlist"]["enabled"] is False


def test_auto_enable_idempotent_no_crash(cfg):
    """已开启时再触发不报错、状态保持。"""
    settings_store.set_external_enabled("lemlist", True)
    agent._maybe_auto_enable_trade_mcp("外贸拓客")
    assert _load()["external_mcp"]["lemlist"]["enabled"] is True


def test_auto_enable_empty_msg(cfg):
    agent._maybe_auto_enable_trade_mcp("")
    assert _load()["external_mcp"]["lemlist"]["enabled"] is False
