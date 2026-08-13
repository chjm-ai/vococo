"""/compact 主动压缩 —— 命令注册 + 压缩轮行为。

主链路:handle_command 识别 /compact → converse(compact=True) → stream_turn
compact_only=True → client.query("/compact")(不经过模型)→ 读到压缩边界与
ResultMessage → 回池。下一轮正常对话仍接同一会话,落在压缩后的上下文上。
"""
from __future__ import annotations

import asyncio

import pytest

from claude_agent_sdk import ResultMessage, StreamEvent, SystemMessage

from vococo.core import agent
from vococo.core.agent import Compacted, Done
from vococo.gateway import core as gateway_core


def _result_msg(sid: str = "sid-1") -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        session_id=sid,
        is_error=False,
        num_turns=1,
        usage={"input_tokens": 5, "output_tokens": 1},
    )


class CompactFakeClient:
    """模拟 CLI:query("/compact") 返回「边界+ResultMessage」;query 其它内容走正常对话。

    真机顺序(2026-07-22 事故)是 compact_boundary 先到、/compact 自己的 ResultMessage
    后到,用队列驱动消息流模拟这一顺序。"""

    def __init__(self, options=None, *, registry: list):
        self.pending = asyncio.Queue()
        self.queries = 0
        self.main_queries = 0  # 正常对话 prompt 的次数(压缩轮必须为 0)
        self.sid = "sid-1"
        registry.append(self)

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt=None, *args, **kwargs) -> None:
        self.queries += 1
        if isinstance(prompt, str) and prompt.strip() == "/compact":
            self.pending.put_nowait(
                SystemMessage(
                    subtype="compact_boundary",
                    data={"compact_metadata": {"trigger": "manual"}},
                )
            )
            self.pending.put_nowait(_result_msg(self.sid))
            return
        self.main_queries += 1
        self.pending.put_nowait(
            StreamEvent(
                uuid="1",
                session_id=self.sid,
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "回复"},
                },
            )
        )
        self.pending.put_nowait(_result_msg(self.sid))

    def receive_messages(self):
        async def gen():
            while True:
                yield await self.pending.get()

        return gen()

    async def get_context_usage(self):
        return {"totalTokens": 100, "rawMaxTokens": 200_000}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def client_factory(monkeypatch):
    """打桩 stream_turn 的外部依赖,返回 FakeClient 登记表。"""
    made: list[CompactFakeClient] = []
    monkeypatch.setattr(
        agent, "ClaudeSDKClient", lambda options=None: CompactFakeClient(options, registry=made)
    )
    monkeypatch.setattr(agent.providers, "resolve", lambda *a: ("claude-sonnet-4", {}))
    monkeypatch.setattr(agent.settings_store, "vococo_enabled", lambda: False)
    monkeypatch.setattr(agent.settings_store, "effective_external_mcp", lambda: {})
    monkeypatch.setattr(agent.settings_store, "effective_skills", lambda cwd=None: None)
    monkeypatch.setattr(
        agent, "build_system_prompt", lambda cwd=None, cache_key=None: {"append": "p"}
    )
    monkeypatch.setattr(agent, "build_mcp_servers", lambda: {})
    monkeypatch.setattr(agent, "build_hooks", lambda: {})
    return made


def test_handle_command_marks_compact():
    """/compact 被识别为命令:handled=True 且 compact 标志打开;reply 为空(由压缩轮回复)。"""
    outcome = gateway_core.handle_command("/compact", "web:x", "claude-sonnet-4")
    assert outcome.handled and outcome.compact
    assert outcome.reply is None  # 不短路,走压缩轮
    # 前缀误判防护:/compactx 不是命令;其它命令不受影响
    assert not gateway_core.handle_command("/compactx", "web:x", "m").compact
    assert not gateway_core.handle_command("/status", "web:x", "m").compact


@pytest.mark.anyio
async def test_compact_turn_queries_cli_without_model(client_factory):
    """压缩轮:只发 /compact 给 CLI,不进入模型对话;yield 压缩标记(trigger=manual);
    Done 带固定反馈文案;client 干净回池供下一轮复用。"""
    events = []
    async for ev in agent.stream_turn(
        [], "/compact", session_key="web:compact-test", compact_only=True
    ):
        events.append(ev)

    fake = client_factory[0]
    assert fake.queries == 1
    assert fake.main_queries == 0  # 没走正常对话 prompt

    compacted = [e for e in events if isinstance(e, Compacted)]
    assert len(compacted) == 1
    assert compacted[0].trigger == "manual"

    done = [e for e in events if isinstance(e, Done)]
    assert len(done) == 1
    assert "已手动压缩上下文" in done[0].reply.text

    # 下一轮正常对话(resume 同一 sid,与真实会话一致)命中同一 client,上下文
    # 延续——压缩发生在活会话上,后面的对话天然落在压缩后的上下文里。
    from vococo.core import client_pool

    assert "web:compact-test" in client_pool._pool
    async for ev in agent.stream_turn(
        [], "hi", session_key="web:compact-test", resume="sid-1"
    ):
        if isinstance(ev, Done):
            pass
    assert fake.main_queries == 1  # 同一 client 上接着对话,没有新建


@pytest.mark.anyio
async def test_normal_turn_unaffected_by_compact_flag(client_factory):
    """compact_only 默认 False:正常轮行为不变(发正常 prompt,无压缩标记)。"""
    events = []
    async for ev in agent.stream_turn([], "hi", session_key="web:compact-test2"):
        events.append(ev)
    fake = client_factory[0]
    assert fake.main_queries == 1
    assert not any(isinstance(e, Compacted) for e in events)
    done = [e for e in events if isinstance(e, Done)]
    assert done[0].reply.text == "回复"
