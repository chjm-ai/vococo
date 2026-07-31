"""converse() 里的音频转写注入:转写文字要喂给模型,但不该污染存库的那句话。

背景:协议层没有原生 audio content block(见 core/agent.py AudioAttachment 的
说明),只能把转写文字拼进喂给模型的文本里。但存进 turns.user_text 的必须还是
用户自己打的那句短说明——不然历史里的用户气泡会被整段转写文字塞爆,和图片的
处理方式(附件不进气泡正文,只在气泡下面单独挂一块)不一致。
"""
from __future__ import annotations

import pytest

from claude_hermes.core.agent import AgentReply, AudioAttachment, Done, TextDelta
from claude_hermes.gateway import core
from claude_hermes.gateway.core import Sink
from claude_hermes.memory import session_store


async def _noop_worktree(session_key):
    return None


@pytest.mark.anyio
async def test_audio_transcript_injected_into_prompt_not_stored_text(isolated, monkeypatch):
    from claude_hermes import config
    from claude_hermes.core import worktree

    monkeypatch.setattr(worktree, "ensure_worktree", _noop_worktree)
    monkeypatch.setattr(config, "AUDIO_DIR", isolated / "data" / "audio")

    seen = {}

    async def fake_stream(history, user_text, **kwargs):
        seen["prompt"] = user_text
        yield TextDelta("好的,已听完")
        yield Done(AgentReply(text="好的,已听完", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(core, "stream_turn", fake_stream)

    key = "web:audio-test"
    await core.converse(
        key, "帮我听听这个", None, Sink(),
        audios=[AudioAttachment(
            data=b"raw", media_type="audio/mpeg", filename="memo.mp3",
            transcript="今天下午三点开会,记得带材料",
        )],
    )

    # 喂给模型的文本里带着转写内容
    assert "今天下午三点开会,记得带材料" in seen["prompt"]
    assert "帮我听听这个" in seen["prompt"]

    # 但存进库、历史回看用户气泡展示的仍是原话,不含转写大段文字
    recent = session_store.load_recent(key)
    assert recent[0].user == "帮我听听这个"

    # 历史里能拿到独立的音频回放条目 + 转写文字
    history = session_store.load_history(key)
    assert history[0]["audios"][0]["text"] == "今天下午三点开会,记得带材料"
