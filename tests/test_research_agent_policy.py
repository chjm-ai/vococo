"""研究 SDK 策略:只用 mock client,不连接模型、不读写个人配置或数据库。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from claude_agent_sdk import HookMatcher, ResultMessage, StreamEvent

from vococo.core import agent


def _decision(value="allow", **extra):
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": value, **extra,
    }}


async def _allow(*args):
    return _decision()


def _event(kind, **data):
    return StreamEvent(uuid="event", session_id="research", event={"type": kind, **data})


def _result(tokens=20):
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, session_id="research",
        is_error=False, num_turns=1,
        usage={"input_tokens": tokens - 2, "output_tokens": 2},
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def sdk(monkeypatch):
    state = SimpleNamespace(clients=[], messages=[_result()], global_calls=[])

    class Client:
        def __init__(self, options):
            self.options = options
            self.prompts = []
            self.interrupt = AsyncMock()
            state.clients.append(self)

        async def connect(self):
            pass

        async def query(self, prompt):
            self.prompts.append(prompt)

        async def get_context_usage(self):
            return {"totalTokens": 20, "rawMaxTokens": 200_000}

        async def receive_messages(self):
            for message in state.messages:
                if isinstance(message, BaseException):
                    raise message
                yield message

    async def global_guard(data, tool_id, context):
        state.global_calls.append((data, tool_id, context))
        return {}

    monkeypatch.setattr(agent, "ClaudeSDKClient", Client)
    monkeypatch.setattr(agent.providers, "resolve", lambda *a: ("claude-sonnet-4", {}))
    monkeypatch.setattr(agent.settings_store, "get_web_effort", lambda *a: "")
    monkeypatch.setattr(agent, "build_hooks", lambda: {
        "PreToolUse": [HookMatcher(matcher=None, hooks=[global_guard])],
    })
    state.discard = AsyncMock()
    monkeypatch.setattr(agent.client_pool, "discard", state.discard)
    return state


async def _run(**kwargs):
    return [event async for event in agent.stream_turn([], "研究公开资料", **kwargs)]


def _hook(client):
    return client.options.hooks["PreToolUse"][0].hooks[0]


@pytest.mark.anyio
async def test_research_configuration_never_loads_private_runtime(sdk, monkeypatch):
    forbidden = Mock(side_effect=AssertionError("研究模式不能读取普通配置"))
    for target, name in [
        (agent.agents, "runtime_for_session"), (agent, "_load_system_prompt"),
        (agent, "build_mcp_servers"), (agent, "_external_mcp_for_task"),
        (agent, "_cli_working_dir"), (agent.reviewer, "definitions"),
        (agent.settings_store, "vococo_enabled"),
        (agent.settings_store, "effective_external_mcp"),
        (agent.settings_store, "effective_skills"),
        (agent.session_store, "get_external_mcp_names"),
        (agent.client_pool, "enabled"), (agent.client_pool, "checkout"),
        (agent.client_pool, "checkin"), (agent, "_compat_base_key"),
    ]:
        monkeypatch.setattr(target, name, forbidden)
    servers = {"research": {"type": "http", "url": "https://research.invalid/mcp"}}
    for _ in range(2):
        events = [event async for event in agent.stream_turn(
            [agent.Turn(user="私人历史")], "研究公开资料",
            tool_guard=_allow, session_key="same-task", resume="private-session",
            cwd="/research-workspace", is_explicit_project=True,
            extra_mcp_servers=servers, system_prompt_extra="仅研究公开资料",
        )]
        assert isinstance(events[-1], agent.Done)
    assert len(sdk.clients) == 2
    for client in sdk.clients:
        options = client.options
        assert options.mcp_servers == servers
        assert options.tools == ["WebSearch", "WebFetch"]
        assert options.strict_mcp_config and options.setting_sources == []
        assert options.skills == [] and options.plugins == [] and options.agents is None
        assert options.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
        assert options.resume is None
        assert isinstance(options.system_prompt, str)
        assert options.system_prompt.endswith("仅研究公开资料")
        assert "私人历史" not in str(client.prompts)
    assert sdk.discard.await_count == 2
    forbidden.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("failed", [True, False])
async def test_failed_result_is_not_hidden_by_stage_convergence(sdk, failed):
    from vococo.core.research import _StageConverged
    result = _result(50)
    result.is_error = failed
    result.result = "HTTP 500" if failed else "完成"
    sdk.messages = [result]
    usage = []
    def converge(total):
        usage.append(total)
        raise _StageConverged("阶段已收束")
    if failed:
        with pytest.raises(RuntimeError, match="HTTP 500") as error:
            await _run(tool_guard=_allow, usage_callback=converge)
        assert not isinstance(error.value, _StageConverged)
    else:
        with pytest.raises(_StageConverged):
            await _run(tool_guard=_allow, usage_callback=converge)
    assert usage == [50]
    sdk.discard.assert_awaited_once()


@pytest.mark.anyio
async def test_guard_is_bound_to_each_new_client(sdk):
    guards = [AsyncMock(return_value=_decision()), AsyncMock(return_value=_decision("deny"))]
    for guard in guards:
        await _run(tool_guard=guard, session_key="same", resume="ordinary")
    data = {"tool_name": "WebSearch", "tool_input": {}}
    first = await _hook(sdk.clients[0])(data, "one", {})
    second = await _hook(sdk.clients[1])(data, "two", {})
    assert first == {} and second["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert guards[0].await_args.args[1] == "one"
    assert guards[1].await_args.args[1] == "two"
    assert len(sdk.global_calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("name", [
    "Agent", "Task", "Bash", "Read", "Write", "Skill", "TaskOutput", "TaskStop",
    "Unknown", "mcp__vococo__save_memory", "mcp__unregistered__search",
])
async def test_tools_outside_research_envelope_are_denied(sdk, name):
    guard = AsyncMock(return_value=_decision())
    await _run(tool_guard=guard)
    result = await _hook(sdk.clients[0])({"tool_name": name}, "id", {})
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    guard.assert_not_awaited()
    assert sdk.global_calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["WebSearch", "WebFetch", "mcp__research__search"])
async def test_explicit_guard_allow_still_passes_global_safety(sdk, name):
    guard = AsyncMock(return_value=_decision())
    await _run(tool_guard=guard, extra_mcp_servers={"research": {"type": "sdk"}})
    data, context = {"tool_name": name, "tool_input": {"query": "公开资料"}}, {}
    assert await _hook(sdk.clients[0])(data, "id", context) == {}
    guard.assert_awaited_once_with(data, "id", context)
    assert sdk.global_calls == [(data, "id", context)]


@pytest.mark.anyio
@pytest.mark.parametrize("decision", [
    {}, None, {"hookSpecificOutput": {}}, _decision("deny"), _decision("ask"),
    _decision(updatedInput={"url": "https://changed.invalid"}),
    {**_decision(), "continue": False},
])
async def test_missing_or_ambiguous_guard_permission_fails_closed(sdk, decision):
    await _run(tool_guard=AsyncMock(return_value=decision))
    result = await _hook(sdk.clients[0])({"tool_name": "WebSearch"}, None, {})
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert sdk.global_calls == []


@pytest.mark.anyio
async def test_guard_exception_fails_closed(sdk):
    await _run(tool_guard=AsyncMock(side_effect=RuntimeError("guard failed")))
    result = await _hook(sdk.clients[0])({"tool_name": "WebSearch"}, None, {})
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert sdk.global_calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("error", [False, True])
async def test_global_denial_or_exception_cannot_be_overridden(sdk, monkeypatch, error):
    safety = AsyncMock(return_value=_decision("deny"))
    if error:
        safety.side_effect = RuntimeError("safety failed")
    monkeypatch.setattr(agent, "build_hooks", lambda: {
        "PreToolUse": [HookMatcher(matcher=None, hooks=[safety])],
    })
    await _run(tool_guard=_allow)
    result = await _hook(sdk.clients[0])({"tool_name": "WebSearch"}, None, {})
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    safety.assert_awaited_once()


@pytest.mark.anyio
async def test_missing_global_safety_refuses_to_start(sdk, monkeypatch):
    monkeypatch.setattr(agent, "build_hooks", lambda: None)
    with pytest.raises(RuntimeError, match="全局 PreToolUse"):
        await _run(tool_guard=_allow)
    assert not sdk.clients


@pytest.mark.anyio
async def test_research_cannot_run_compaction(sdk):
    with pytest.raises(ValueError, match="compact_only"):
        await _run(tool_guard=_allow, compact_only=True)
    assert not sdk.clients


def _usage_messages():
    return [
        _event("message_start", message={"usage": {
            "input_tokens": 10, "cache_creation_input_tokens": 3,
            "cache_read_input_tokens": 9999,
        }}),
        _event("message_delta", usage={"output_tokens": 2}),
        _event("message_delta", usage={"output_tokens": 0}),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("final_tokens,expected", [(20, [13, 15, 20]), (10, [13, 15])])
async def test_usage_reports_growth_and_higher_final_total(sdk, final_tokens, expected):
    sdk.messages = [*_usage_messages(), _result(final_tokens)]
    recorded = []
    events = await _run(tool_guard=_allow, usage_callback=recorded.append)
    assert recorded == expected
    assert events[-1].reply.stream_tokens == 15
    assert events[-1].reply.turn_tokens == final_tokens


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [RuntimeError("stream failed"), asyncio.CancelledError()])
async def test_partial_usage_is_recorded_before_stream_error_or_cancel(sdk, failure):
    sdk.messages = [*_usage_messages(), failure]
    recorded = []
    with pytest.raises(type(failure)):
        await _run(tool_guard=_allow, usage_callback=recorded.append)
    assert recorded == [13, 15]
    sdk.discard.assert_awaited_once_with(sdk.clients[0])
    if isinstance(failure, asyncio.CancelledError):
        sdk.clients[0].interrupt.assert_awaited_once()


@pytest.mark.anyio
async def test_usage_callback_failure_stops_turn_and_closes_client(sdk):
    sdk.messages = [*_usage_messages(), _result()]
    callback = Mock(side_effect=RuntimeError("usage storage failed"))
    with pytest.raises(RuntimeError, match="usage storage failed"):
        await _run(tool_guard=_allow, usage_callback=callback)
    callback.assert_called_once_with(13)
    sdk.discard.assert_awaited_once_with(sdk.clients[0])


@pytest.fixture
def ordinary(monkeypatch):
    monkeypatch.setattr(agent.agents, "runtime_for_session", lambda key: {
        "skills": ["pdf"], "mcp": ["external"], "disallowed_tools": ["Bash"],
    })
    monkeypatch.setattr(agent.settings_store, "vococo_enabled", lambda: True)
    monkeypatch.setattr(agent.settings_store, "effective_external_mcp", lambda names: {"external": {}})
    monkeypatch.setattr(agent.settings_store, "effective_skills", lambda *a, **kw: ["global"])
    monkeypatch.setattr(agent.session_store, "get_external_mcp_names", lambda key: set())
    monkeypatch.setattr(agent, "build_mcp_servers", lambda **kw: {"vococo": {}})
    monkeypatch.setattr(agent, "_load_system_prompt", AsyncMock(return_value={"append": "个人提示"}))
    monkeypatch.setattr(agent, "_cli_working_dir", lambda cwd: (cwd, ""))
    monkeypatch.setattr(agent.reviewer, "definitions", lambda *a: {"reviewer": object()})
    monkeypatch.setattr(agent.client_pool, "enabled", lambda: False)


@pytest.mark.anyio
async def test_ordinary_call_keeps_configuration_and_resume(sdk, ordinary):
    recorded = []
    await _run(session_key="ordinary", resume="old-session", disallowed_tools=["Write"],
               extra_mcp_servers={"injected": {}}, usage_callback=recorded.append)
    options = sdk.clients[0].options
    assert options.tools is None and not options.strict_mcp_config
    assert options.setting_sources is None
    assert options.resume == "old-session"
    assert set(options.mcp_servers) == {"vococo", "external", "injected"}
    assert "pdf" in options.skills and options.plugins and options.agents
    assert options.system_prompt == {"append": "个人提示"}
    assert options.disallowed_tools == ["Write", "Bash"]
    assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY" not in options.env
    assert recorded == [20]


@pytest.mark.anyio
@pytest.mark.parametrize("warm", [False, True])
async def test_usage_callback_error_never_retries_resumed_or_warm_client(sdk, ordinary, monkeypatch, warm):
    sdk.messages = [*_usage_messages(), _result()]
    callback = Mock(side_effect=RuntimeError("usage storage failed"))
    if warm:
        client = agent.ClaudeSDKClient(options=None)
        monkeypatch.setattr(agent.client_pool, "enabled", lambda: True)
        monkeypatch.setattr(agent.client_pool, "checkout", AsyncMock(return_value=client))
        monkeypatch.setattr(agent, "_compat_base_key", lambda *a, **kw: "key")
    with pytest.raises(RuntimeError, match="usage storage failed"):
        await _run(session_key="ordinary", resume="old-session", usage_callback=callback)
    assert len(sdk.clients) == 1
    callback.assert_called_once_with(13)
    sdk.discard.assert_awaited_once()


@pytest.mark.anyio
async def test_research_never_performs_end_of_turn_compaction(sdk, monkeypatch):
    monkeypatch.setattr(agent, "_query_context_usage", AsyncMock(return_value=(
        {"isAutoCompactEnabled": True}, 190_000, 200_000, False,
    )))
    await _run(tool_guard=_allow)
    assert len(sdk.clients[0].prompts) == 1
    assert "/compact" not in sdk.clients[0].prompts


@pytest.mark.anyio
@pytest.mark.parametrize("callback_fails", [False, True])
async def test_ordinary_safety_compaction_reports_usage(sdk, ordinary, monkeypatch, callback_fails):
    monkeypatch.setattr(agent, "_query_context_usage", AsyncMock(return_value=(
        {"isAutoCompactEnabled": True}, 190_000, 200_000, False,
    )))
    sdk.messages = [_result(), *_usage_messages(), _result(30)]
    recorded = []

    def record(total):
        if callback_fails and total > 20:
            raise RuntimeError("compact usage storage failed")
        recorded.append(total)

    if callback_fails:
        with pytest.raises(RuntimeError, match="compact usage storage failed"):
            await _run(resume="old-session", usage_callback=record)
        assert recorded == [20]
    else:
        await _run(usage_callback=record)
        assert recorded == [20, 33, 35, 50]
    assert len(sdk.clients) == 1
    assert sdk.clients[0].prompts[-1] == "/compact"
