"""converse() 必须把通用文件附件原样转交给 agent 层。"""
from __future__ import annotations

import pytest

from vococo.core.agent import AgentReply, Done, FileAttachment, TextDelta
from vococo.gateway import core
from vococo.gateway.core import Sink


async def _default_cwd(session_key):
    from vococo import config

    return str(config.ROOT_DIR)


@pytest.mark.anyio
async def test_file_attachment_forwarded_to_agent(isolated, monkeypatch):
    from vococo.core import worktree

    monkeypatch.setattr(worktree, "execution_cwd", _default_cwd)
    seen = {}

    async def fake_stream(history, user_text, **kwargs):
        seen["files"] = kwargs["files"]
        yield TextDelta("已收到")
        yield Done(AgentReply(text="已收到", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(core, "stream_turn", fake_stream)
    attachment = FileAttachment(b"doc bytes", "application/msword", "brief.doc")
    await core.converse("web:file-test", "读取附件", None, Sink(), files=[attachment])

    assert seen["files"] == [attachment]
