"""移除逐轮表达提醒后的回归：原始载荷不变，模型切换仍隔离 SDK 会话。"""
from __future__ import annotations

import asyncio

import pytest

from vococo.core import agent
from vococo.gateway import core
from vococo.memory import session_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_plain_prompt_preserves_text_and_history_on_rebuild():
    history = [agent.Turn(user="原始问题", assistant="原始回答")]
    expected = agent._compose_prompt(history, "继续")
    assert agent._build_prompt(history, "继续", [], []) == expected
    assert agent._build_prompt(history, "继续", [], []) == expected
    assert "[本轮表达提醒" not in expected
    assert history == [agent.Turn(user="原始问题", assistant="原始回答")]
    assert agent._build_prompt([], "  原始问题\n", [], []) == "  原始问题\n"


@pytest.mark.anyio
async def test_attachments_keep_original_order_without_trailing_reminder():
    images = [agent.ImageAttachment("QUJD", "image/png", "/tmp/test.png")]
    files = [agent.FileAttachment(b"hello", "text/plain", "test.txt"),
             agent.FileAttachment(b"\0\xff", "application/pdf", "test.pdf")]
    payload = agent._build_prompt([], "看看附件", images, files)
    message = await anext(payload)
    content = message["message"]["content"]
    assert content == [
        {"type": "text", "text": "看看附件"},
        {"type": "text", "text": "[聊天图片附件 1，可用 Read 工具读取]\n本机文件：/tmp/test.png"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
        {"type": "text", "text": "[文件附件: test.txt]\nhello"},
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "AP8="},
         "title": "test.pdf"},
    ]
    assert "[本轮表达提醒" not in str(content)
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
