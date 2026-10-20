"""GPT 当轮表达提醒：只加工发送载荷，换模型不能续用旧提醒。"""
from __future__ import annotations

import asyncio

import pytest

from vococo.core import agent
from vococo.core.prompt import reply_style_reminder
from vococo.gateway import core
from vococo.memory import session_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("model,enabled", [
    ("gpt-6.1-sol", True), ("GPT-6-astra", True),
    ("claude-sonnet-5-5", False), ("deepseek-flash", False),
    ("gemini-pro", False), ("codex-provider", False), ("", False),
])
def test_reply_style_model_boundary(model, enabled):
    reminder = reply_style_reminder(model)
    assert bool(reminder) is enabled
    if enabled:
        assert "不设字数、条数或固定格式" in reminder
        assert "我明确要求的内容不能省略" in reminder


def test_plain_prompt_appends_once_without_mutating_history():
    history = [agent.Turn(user="原始问题", assistant="原始回答")]
    reminder = reply_style_reminder("gpt-6.1-sol")
    expected = agent._compose_prompt(history, "继续") + "\n\n" + reminder
    assert agent._build_prompt(history, "继续", [], [], reminder=reminder) == expected
    assert agent._build_prompt(history, "继续", [], [], reminder=reminder) == expected
    assert history[0].user == "原始问题"
    assert agent._build_prompt(history, "继续", [], []) == agent._compose_prompt(history, "继续")


@pytest.mark.anyio
async def test_reminder_after_all_attachments():
    images = [agent.ImageAttachment("QUJD", "image/png", "/tmp/test.png")]
    files = [agent.FileAttachment(b"hello", "text/plain", "test.txt"),
             agent.FileAttachment(b"\0\xff", "application/pdf", "test.pdf")]
    reminder = reply_style_reminder("gpt-6.1-sol")
    payload = agent._build_prompt([], "看看附件", images, files, reminder=reminder)
    message = await anext(payload)
    content = message["message"]["content"]
    assert content[0] == {"type": "text", "text": "看看附件"}
    assert content[-2]["type"] == "document"
    assert content[-1] == {"type": "text", "text": reminder}
    assert sum(reminder in block.get("text", "") for block in content) == 1
    assert any(block["type"] == "image" for block in content)
    with pytest.raises(StopAsyncIteration):
        await anext(payload)


@pytest.mark.anyio
@pytest.mark.parametrize("target", ["claude-sonnet-5-5", "", "gpt-6.1-sol"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_switch_during_turn_cannot_restore_old_resume(isolated, monkeypatch, target, cancelled):
    key = "web:style-mid-turn"
    session_store.set_chosen_model(key, "gpt-6.1-sol")
    session_store.set_sdk_session_id(key, "old-gpt-sid")

    async def fake_stream(*args, **kwargs):
        session_store.set_chosen_model(key, target)
        yield agent.Done(agent.AgentReply(text="已切换", tool_calls=[], cost_usd=None,
                                           is_error=False, sdk_session_id="returned-gpt-sid"))
        if cancelled:
            raise asyncio.CancelledError

    monkeypatch.setattr(core, "stream_turn", fake_stream)
    call = core.converse(key, "切模型", "gpt-6.1-sol", core.Sink(),
                         cwd_override=str(isolated), is_explicit_project_override=False)
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await call
    else:
        reply = await call
        assert reply is not None and not reply.is_error
    expected = "returned-gpt-sid" if target == "gpt-6.1-sol" else None
    assert session_store.get_sdk_session_id(key) == expected
    assert session_store.get_chosen_model(key) == target
    assert session_store.load_recent(key)[0].user == "切模型"


@pytest.mark.parametrize("old_model", [None, ""])
def test_legacy_session_without_model_does_not_resume_on_selection(isolated, old_model):
    key = "web:style-legacy"
    if old_model is not None:
        session_store.set_chosen_model(key, old_model)
    session_store.set_sdk_session_id(key, "unknown-model-sid")
    session_store.set_chosen_model(key, "claude-sonnet-5-5")
    assert session_store.get_sdk_session_id(key) is None
