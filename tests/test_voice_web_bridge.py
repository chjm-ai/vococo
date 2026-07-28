"""语音→网页跨端续聊桥接的测试(2026-07-28 补:voice_continue_web / voice_list_web_sessions /
gateway/web_bridge.py / WebAdapter.inject / session_store.last_error)。

覆盖:last_error 落库与 list_sessions 透出、web_bridge 注册前后的 available()/
continue_session()/cancel_if_running()、WebAdapter.inject 与 _handle_send 走同一条
_ingest 流水线、两个新 MCP 工具的 schema 与行为(含没注册 bridge 时的降级提示、
conv/task_id 两套 id 不串)。
"""
from __future__ import annotations

import pytest

from claude_hermes import config
from claude_hermes.gateway import web_bridge
from claude_hermes.gateway.adapters.web import WebAdapter
from claude_hermes.memory import session_store
from claude_hermes.voice import task_tools


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def web_db(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    yield
    # 每条用例结束后清空桥接的全局注册状态,不让状态串到下一条用例
    web_bridge._dispatch_fn = None
    web_bridge._cancel_fn = None


# ── session_store.last_error ────────────────────────────────────────────────


def test_last_error_defaults_false_and_round_trips(web_db):
    key = "web:conv1"
    session_store.append(key, "问", "答")
    rows = session_store.list_sessions("web:")
    assert rows[0]["last_error"] is False

    session_store.set_last_error(key, True)
    rows = session_store.list_sessions("web:")
    assert rows[0]["last_error"] is True

    session_store.set_last_error(key, False)
    rows = session_store.list_sessions("web:")
    assert rows[0]["last_error"] is False


# ── gateway/web_bridge.py ───────────────────────────────────────────────────


def test_bridge_unavailable_before_register(web_db):
    assert web_bridge.available() is False
    assert web_bridge.cancel_if_running("web:conv1") is False


@pytest.mark.anyio
async def test_bridge_dispatch_and_cancel_after_register(web_db):
    calls = []

    async def fake_dispatch(conv: str, text: str) -> None:
        calls.append((conv, text))

    cancelled = []

    def fake_cancel(session_key: str) -> bool:
        cancelled.append(session_key)
        return True

    web_bridge.register(fake_dispatch, fake_cancel)
    assert web_bridge.available() is True

    assert web_bridge.cancel_if_running("web:conv1") is True
    assert cancelled == ["web:conv1"]

    await web_bridge.continue_session("conv1", "接着跑")
    assert calls == [("conv1", "接着跑")]


# ── WebAdapter.inject 与 _handle_send 共用 _ingest ──────────────────────────


@pytest.mark.anyio
async def test_web_adapter_inject_enqueues_same_as_browser_send(web_db):
    adapter = WebAdapter()
    broadcasts = []
    adapter._emit = lambda payload: broadcasts.append(payload)  # 不起真 SSE,只验证广播内容

    await adapter.inject("conv1", "接着跑一下")

    inc = adapter._inbox.get_nowait()
    assert inc.platform == "web"
    assert inc.chat_id == "conv1"
    assert inc.text == "接着跑一下"
    assert inc.session_key == "web:conv1"
    # 跟真实浏览器发消息一样广播了用户气泡,其它客户端(如桌面端)才能实时看到
    assert broadcasts == [{"conv": "conv1", "type": "user", "text": "接着跑一下"}]


# ── 两个新 MCP 工具 ──────────────────────────────────────────────────────────


def test_voice_continue_web_schema():
    assert task_tools.voice_continue_web.name == "voice_continue_web"
    assert task_tools.voice_list_web_sessions.name == "voice_list_web_sessions"


@pytest.mark.anyio
async def test_voice_continue_web_without_bridge_degrades_gracefully(web_db):
    result = await task_tools.voice_continue_web.handler(
        {"conv": "conv1", "instruction": "继续"}
    )
    text = result["content"][0]["text"]
    assert "没有开启网页入口" in text


@pytest.mark.anyio
async def test_voice_continue_web_requires_both_fields(web_db):
    result = await task_tools.voice_continue_web.handler({"conv": "", "instruction": "继续"})
    assert "都非空" in result["content"][0]["text"]


@pytest.mark.anyio
async def test_voice_continue_web_idle_session_dispatches_without_cancel(web_db):
    calls = []

    async def fake_dispatch(conv: str, text: str) -> None:
        calls.append((conv, text))

    def fake_cancel(session_key: str) -> bool:
        return False  # 没在跑

    web_bridge.register(fake_dispatch, fake_cancel)

    result = await task_tools.voice_continue_web.handler(
        {"conv": "conv1", "instruction": "接着查一下天气"}
    )
    text = result["content"][0]["text"]
    assert "conv=conv1" in text
    assert "空闲" in text
    assert calls == [("conv1", "接着查一下天气")]


@pytest.mark.anyio
async def test_voice_continue_web_running_session_interrupts_first(web_db):
    calls = []

    async def fake_dispatch(conv: str, text: str) -> None:
        calls.append((conv, text))

    cancelled = []

    def fake_cancel(session_key: str) -> bool:
        cancelled.append(session_key)
        return True  # 正在跑,打断了

    web_bridge.register(fake_dispatch, fake_cancel)

    result = await task_tools.voice_continue_web.handler(
        {"conv": "conv1", "instruction": "换个方向重新跑"}
    )
    text = result["content"][0]["text"]
    assert "打断" in text
    # cancel 用的是解析后的 session_key(web:conv1),不是裸 conv id
    assert cancelled == ["web:conv1"]
    assert calls == [("conv1", "换个方向重新跑")]


@pytest.mark.anyio
async def test_voice_list_web_sessions_reports_error_flag(web_db):
    session_store.append("web:conv1", "问1", "答1")
    session_store.append("web:conv2", "问2", "答2")
    session_store.set_last_error("web:conv2", True)

    result = await task_tools.voice_list_web_sessions.handler({})
    text = result["content"][0]["text"]
    assert "conv=conv1" in text
    assert "conv=conv2" in text
    assert "⚠️" in text  # conv2 标了报错


@pytest.mark.anyio
async def test_voice_list_web_sessions_empty(web_db):
    result = await task_tools.voice_list_web_sessions.handler({})
    assert "没有任何网页端对话" in result["content"][0]["text"]
