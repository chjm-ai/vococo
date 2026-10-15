"""上游报错重试要让人看得见。

用户现象(2026-10-15 实测):上游 503 时 CLI 自己退避重试,最多 10 次、退避能到
十几秒,连着重试能安静地占掉 6 分钟——这期间没有任何正文/思考/工具事件,前端
只剩「思考中」在转,用户以为卡死了,实际还在正常重试。

链路:CLI stdout 的 system/api_retry → SDK 落成 SystemMessage(subtype="api_retry")
→ core/agent.py 转成 UpstreamRetry 事件 → gateway 分发给 sink → Web 端 SSE
推 upstream_retry → 前端把状态行换成「上游 503 · 重试 3/10 · 18s 后重试」。

这里测到 SSE 那一层为止(前端 JS 不在 pytest 范围)。
"""
from __future__ import annotations

import asyncio

import pytest

from claude_agent_sdk import ResultMessage, StreamEvent, SystemMessage

from vococo.core import agent
from vococo.core.agent import AgentReply, Done, TextDelta, UpstreamRetry
from vococo.gateway import core as gateway_core
from vococo.gateway.core import Sink

# CLI 真机抓到的字段(stdout 流形态)
WIRE_DATA = {
    "type": "system",
    "subtype": "api_retry",
    "attempt": 3,
    "max_retries": 10,
    "retry_delay_ms": 18542,
    "error_status": 503,
    "error": "server_error",
}


def test_upstream_retry_maps_wire_fields():
    """stdout 形态:attempt/max_retries/retry_delay_ms/error_status 全部读到。"""
    ev = agent._upstream_retry(WIRE_DATA)
    assert (ev.status, ev.attempt, ev.max_retries, ev.delay_ms, ev.reason) == (
        503, 3, 10, 18542, "server_error",
    )


def test_upstream_retry_maps_transcript_fields():
    """transcript 形态字段名不同,且 status/message 嵌在 error 对象里 —— 按真实记录认。

    (~/.claude/projects 下 624 条 api_error 记录实测:retryInMs/retryAttempt/maxRetries +
    error{message,status};wire twin 是 stdout 的 api_retry。)
    """
    ev = agent._upstream_retry({
        "subtype": "api_error", "level": "error",
        "error": {"message": "503 Service is too busy", "status": 503},
        "retryInMs": 900, "retryAttempt": 2, "maxRetries": 10, "source": "request_retry",
    })
    assert (ev.status, ev.attempt, ev.max_retries, ev.delay_ms) == (503, 2, 10, 900)
    assert ev.reason == "503 Service is too busy"


def test_upstream_retry_tolerates_missing_fields():
    """字段认不出一律留 0,不抛也不编——前端只显示拿到的那部分。"""
    ev = agent._upstream_retry({})
    assert (ev.status, ev.attempt, ev.max_retries, ev.delay_ms, ev.reason) == (0, 0, 0, 0, "")
    assert agent._upstream_retry({"error": {"nested": 1}}).reason == ""  # 非字符串不硬转

def _result_msg() -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, session_id="sid-1",
        is_error=False, num_turns=1, usage={"input_tokens": 5, "output_tokens": 1},
    )


class RetryFakeClient:
    """模拟 CLI:先报一次 api_retry,再正常吐正文。"""

    def __init__(self, options=None):
        self.pending = asyncio.Queue()

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt=None, *args, **kwargs) -> None:
        self.pending.put_nowait(SystemMessage(subtype="api_retry", data=dict(WIRE_DATA)))
        self.pending.put_nowait(
            StreamEvent(
                uuid="1", session_id="sid-1",
                event={"type": "content_block_delta",
                       "delta": {"type": "text_delta", "text": "回复"}},
            )
        )
        self.pending.put_nowait(_result_msg())

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
def fake_cli(monkeypatch):
    monkeypatch.setattr(agent, "ClaudeSDKClient", lambda options=None: RetryFakeClient(options))
    monkeypatch.setattr(agent.providers, "resolve", lambda *a: ("claude-sonnet-4", {}))
    monkeypatch.setattr(agent.settings_store, "vococo_enabled", lambda: False)
    monkeypatch.setattr(agent.settings_store, "effective_external_mcp", lambda: {})
    monkeypatch.setattr(agent.settings_store, "effective_skills", lambda cwd=None: None)
    monkeypatch.setattr(agent, "build_system_prompt", lambda cwd=None, cache_key=None: {"append": "p"})
    monkeypatch.setattr(agent, "build_mcp_servers", lambda: {})
    monkeypatch.setattr(agent, "build_hooks", lambda: {})


@pytest.mark.anyio
async def test_stream_turn_yields_upstream_retry(fake_cli):
    """SystemMessage(api_retry) 不再被静默丢掉:转成事件,且不挡后面的正文。"""
    events = []
    async for ev in agent.stream_turn([], "hi"):
        events.append(ev)

    retries = [e for e in events if isinstance(e, UpstreamRetry)]
    assert len(retries) == 1
    assert (retries[0].status, retries[0].attempt, retries[0].max_retries) == (503, 3, 10)
    assert retries[0].delay_ms == 18542

    # 重试之后照常出正文:重试事件只是"报个状态",不能改变原有事件序
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["回复"]
    assert [e.reply.text for e in events if isinstance(e, Done)] == ["回复"]


class RecordingSink(Sink):
    """记录收到的事件,不渲染。"""

    def __init__(self) -> None:
        super().__init__()
        self.retries: list[tuple] = []

    async def render(self) -> None:
        return None

    async def upstream_retry(self, status=0, attempt=0, max_retries=0, delay_ms=0, reason=""):
        self.retries.append((status, attempt, max_retries, delay_ms, reason))


@pytest.mark.anyio
async def test_converse_delivers_retry_to_sink(isolated, monkeypatch):
    """gateway 分发:UpstreamRetry 必须送到渲染层(以前这条路根本不存在)。"""

    async def stream(*args, **kwargs):
        yield UpstreamRetry(status=503, attempt=3, max_retries=10, delay_ms=18542,
                            reason="server_error")
        yield TextDelta("回复")
        yield Done(AgentReply(text="回复", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(gateway_core, "stream_turn", stream)
    sink = RecordingSink()
    await gateway_core.converse("web:retry-test", "hi", None, sink)

    assert sink.retries == [(503, 3, 10, 18542, "server_error")]


def test_web_sink_emits_upstream_retry():
    """Web 端把重试推成 SSE 事件;并折进「进行中回合」快照,整页刷新/断线重连也能恢复。

    快照这条是评审发现的缺口:不记进去的话,手机上切后台回来(SSE 断线重连、走服务端
    快照恢复)又退回「思考中」,和 SPA 内切会话(走前端缓冲)两条路行为相反。
    """
    from vococo.gateway.adapters.web import _WebSink

    class FakeAdapter:
        def __init__(self):
            self.events = []

        def _emit(self, payload):
            self.events.append(payload)

    a = FakeAdapter()
    sink = _WebSink(a, "conv1")
    asyncio.run(sink.upstream_retry(503, 3, 10, 18542, "server_error"))

    ev = [e for e in a.events if e.get("type") == "upstream_retry"]
    assert ev and ev[0] == {
        "conv": "conv1", "type": "upstream_retry", "status": 503,
        "attempt": 3, "max_retries": 10, "delay_ms": 18542, "reason": "server_error",
    }


def test_track_live_keeps_retry_frame():
    """服务端「进行中回合」快照要收重试帧:断线重连/整页刷新的恢复路径才看得到重试状态。"""
    from vococo.gateway.adapters.web import WebAdapter

    a = WebAdapter()
    a._emit({"conv": "1", "type": "start"})
    a._emit({"conv": "1", "type": "thinking", "text": "想一下"})
    a._emit({"conv": "1", "type": "upstream_retry", "status": 503, "attempt": 3,
             "max_retries": 10, "delay_ms": 18000})

    frames = [p for _, p in a._live["1"]["frames"]]
    assert [f.get("type") for f in frames] == ["start", "thinking", "upstream_retry"]
    assert "1" in a._live  # 重试帧只追加,不能当回合结束把快照收掉

    # 没有进行中的回合时零散重试帧直接忽略(与其它帧一致)
    a._emit({"conv": "2", "type": "upstream_retry", "status": 503})
    assert "2" not in a._live
