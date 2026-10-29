"""共用执行的上下文、预压缩预算和删除保护；不连接真实模型。"""
import pytest

from vococo import config
from vococo.core.agent import AgentReply, Done, SessionStarted
from vococo.core.session_execution import SharedTurn, stream_run
from vococo.memory import agents, session_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def turn(isolated, monkeypatch):
    from vococo.core import session_execution

    owner = agents.create("临时执行")
    key = config.new_session_key()
    session_store.bind_session(key, agent_id=owner["id"])
    session_store.append(key, "旧问题", "旧回复")
    session_store.set_chosen_model(key, "fixture-model")
    session_store.set_sdk_session_id(key, "old-sdk")
    tid = session_store.start_turn(key, "本轮问题")
    monkeypatch.setattr(session_execution.providers, "resolve", lambda *_: ("fixture-model", {}))
    monkeypatch.setattr(session_execution, "effective_context_window", lambda *_: 1000)
    return key, tid


def reply(**kwargs):
    return AgentReply(text="结果", tool_calls=[], cost_usd=None, is_error=False, **kwargs)


def test_fresh_execution_does_not_reinject_history(turn):
    key, tid = turn
    normal = SharedTurn(key, tid)
    fresh = SharedTurn(key, tid, fresh=True)
    assert normal.history and normal.resume == "old-sdk"
    assert fresh.history == [] and fresh.resume is None
    assert session_store.load_history(key)
    assert session_store.get_sdk_session_id(key) == "old-sdk"


@pytest.mark.anyio
@pytest.mark.parametrize("fail", [False, True])
async def test_preflight_counts_budget_and_stops_on_failure(turn, fail):
    key, tid = turn
    session_store.record_usage(key, 800, 0, window=1000)
    calls, usage = [], []

    async def stream(history, text, **options):
        calls.append((history, text, options))
        if options.get("compact_only"):
            yield Done(AgentReply(text="压缩", tool_calls=[], cost_usd=None, is_error=fail,
                                  stream_tokens=40, sdk_session_id="compact-sdk"))
        else:
            options["usage_callback"](20)
            yield Done(reply(stream_tokens=20))

    shared = SharedTurn(key, tid)
    events = [event async for event in stream_run(shared, "本轮", stream_fn=stream,
                                                 token_budget=100, usage_callback=usage.append)]
    assert calls[0][2]["resume"] == "old-sdk"
    assert calls[0][2]["compact_only"]
    if fail:
        assert len(calls) == 1 and events[-1].reply.is_error
    else:
        assert calls[1][2]["resume"] == "compact-sdk"
        assert calls[1][2]["token_budget"] == 60
        assert usage == [60]
        assert events[-1].reply.stream_tokens == 60


@pytest.mark.anyio
async def test_preflight_exhaustion_does_not_send_original_question(turn):
    key, tid = turn
    session_store.record_usage(key, 800, 0, window=1000)
    calls = []

    async def stream(history, text, **options):
        calls.append(text)
        yield Done(reply(stream_tokens=100))

    events = [event async for event in stream_run(SharedTurn(key, tid), "不能再发", stream_fn=stream,
                                                 token_budget=100)]
    assert calls == [""]
    assert events[-1].reply.is_error and events[-1].reply.budget_exceeded


@pytest.mark.anyio
async def test_deleted_turn_cannot_recreate_sdk_session(turn):
    key, tid = turn
    shared = SharedTurn(key, tid)
    session_store._conn().execute("DELETE FROM turns WHERE id=? AND session_key=?", (tid, key))
    session_store._conn().commit()
    await shared.observe(SessionStarted(session_id="late-sdk"))
    shared.record_usage(reply(sdk_session_id="late-sdk", context_tokens=999))
    assert session_store.get_sdk_session_id(key) == "old-sdk"
    assert session_store.session_summary(key).get("ctx_tokens", 0) == 0
