"""同步子代理隔离测试：真实模型路由/安全 hook，SDK client 与凭据全部使用假数据。"""
from __future__ import annotations

import asyncio
import contextvars
import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, ResultMessage, TextBlock
from claude_agent_sdk._internal.transport import Transport
from claude_agent_sdk._internal.sdk_mcp_bridge import SdkMcpBridge

from vococo import config, providers
from vococo.core import agent, client_pool, subagent
from vococo.gateway import clarify, settings_store
from vococo.memory import agents, session_store
from vococo.tools import builtin, danger


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def local_settings(isolated, monkeypatch):
    monkeypatch.setattr(settings_store, "_PATH", isolated / "settings.json")
    monkeypatch.setattr(config, "OAUTH_TOKEN", "fake-official-token")
    monkeypatch.setattr(config, "DANGER_GUARD", True)
    monkeypatch.setattr(config, "APPROVAL_GATE", True)
    settings_store.upsert_web_provider("codex-gpt", {
        "base_url": "https://codex.invalid", "api_key": "fake-codex-key", "model": "gpt-6.1-sol",
    })
    settings_store.upsert_web_provider("relay", {
        "base_url": "https://relay.invalid", "api_key": "fake-relay-key", "model": "claude-sonnet-5-5",
    })


def _done(text="子任务结果", *, error=False):
    return ResultMessage(
        subtype="error_max_turns" if error else "success", duration_ms=1, duration_api_ms=1,
        is_error=error, num_turns=1, session_id="child-sdk-id", result=text,
        usage={"input_tokens": 10, "output_tokens": 5},
    )


def _parent(**kwargs):
    return ClaudeAgentOptions(**{
        "model": "gpt-6.1-sol",
        "env": {"ANTHROPIC_BASE_URL": "https://parent.invalid", "ANTHROPIC_API_KEY": "parent-key",
                "CLAUDE_CODE_OAUTH_TOKEN": "", "CLAUDE_CODE_EXTRA_BODY": '{"parent":true}'},
        "cwd": "/project", "resume": "parent-resume", "session_id": "parent-session",
        "system_prompt": {"type": "preset", "preset": "claude_code", "append": "父项目规则"},
        "permission_mode": "bypassPermissions", "hooks": danger.build_hooks(),
        **kwargs,
    })


@pytest.fixture
def sdk(monkeypatch):
    state = SimpleNamespace(clients=[], messages=[_done()], hang=False, connect_error=False,
                            started=asyncio.Event(), contexts=[], probe=None,
                            closing=asyncio.Event(), release_close=asyncio.Event(), hold_close=False)

    class Client:
        def __init__(self, options):
            self.options = options
            self.closed = False
            self.prompts = []
            state.clients.append(self)

        async def connect(self):
            state.contexts.append((danger.current_session_key(), danger.current_cwd(), subagent._DEPTH.get()))
            if state.connect_error:
                raise RuntimeError("不要泄漏的 secret")

        async def query(self, prompt):
            self.prompts.append(prompt)
            state.started.set()
            if state.probe:
                await state.probe(self.options)

        async def receive_response(self):
            if state.hang:
                await asyncio.Event().wait()
            for msg in state.messages:
                if isinstance(msg, Exception):
                    raise msg
                yield msg

        async def disconnect(self):
            state.closing.set()
            if state.hold_close:
                await state.release_close.wait()
            await asyncio.sleep(0)
            self.closed = True

    monkeypatch.setattr(subagent, "ClaudeSDKClient", Client)
    # 任意误用父池都会立即暴露，不连接真实 CLI。
    monkeypatch.setattr(client_pool, "checkout", AsyncMock(side_effect=AssertionError("子代理不能取父池")))
    monkeypatch.setattr(client_pool, "checkin", AsyncMock(side_effect=AssertionError("子代理不能入父池")))
    return state


def _text(result):
    return result["content"][0]["text"]


@pytest.mark.anyio
async def test_inherits_model_but_never_parent_session_or_pool(sdk):
    parent = _parent(disallowed_tools=["Write"], max_turns=8)
    session_store.set_chosen_model("parent", "gpt-6.1-sol")
    before = dict(parent.env)
    tool = subagent.build_tool(parent)
    assert "gpt-6-luna" in tool.description and "relay/claude-sonnet-5-5" in tool.description
    result = await tool.handler({"prompt": "只执行这个完整任务"})
    assert _text(result) == "子任务结果" and not result["is_error"]
    child = sdk.clients[0]
    assert child.options.model == parent.model
    assert child.options.env["ANTHROPIC_API_KEY"] == "parent-key"
    assert child.options.env["CLAUDE_CODE_EXTRA_BODY"] == before["CLAUDE_CODE_EXTRA_BODY"]
    assert parent.env == before
    assert child.options.resume is None and child.options.session_id is None
    assert child.options.session_store is None and not child.options.continue_conversation
    assert "no-session-persistence" in child.options.extra_args
    assert child.options.max_turns == 8
    assert child.options.permission_mode == parent.permission_mode
    assert "Write" not in child.options.tools
    assert child.prompts == ["只执行这个完整任务"] and child.closed
    assert parent.resume == "parent-resume" and parent.session_id == "parent-session"
    assert session_store.get_chosen_model("parent") == "gpt-6.1-sol"
    assert subagent._DEPTH.get() == 0


@pytest.mark.anyio
@pytest.mark.parametrize("model,expected,url,key", [
    ("Luna", "gpt-6-luna", "https://codex.invalid", "fake-codex-key"),
    ("Sol", "gpt-6.1-sol", "https://codex.invalid", "fake-codex-key"),
    ("relay/claude-sonnet-5-5", "claude-sonnet-5-5", "https://relay.invalid", "fake-relay-key"),
    ("claude-sonnet-5-5", "claude-sonnet-5-5", "", ""),
])
async def test_cross_provider_isolated_env(sdk, model, expected, url, key):
    parent = _parent()
    result = await subagent.build_tool(parent).handler({"prompt": "任务", "model": model})
    assert not result["is_error"]
    opts = sdk.clients[0].options
    assert (opts.model, opts.env["ANTHROPIC_BASE_URL"], opts.env["ANTHROPIC_API_KEY"]) == (expected, url, key)
    assert opts.env["CLAUDE_CODE_OAUTH_TOKEN"] == ("" if url else "fake-official-token")
    assert "parent" not in opts.env["CLAUDE_CODE_EXTRA_BODY"]
    assert parent.model == "gpt-6.1-sol" and parent.env["ANTHROPIC_API_KEY"] == "parent-key"


def test_child_compact_inherits_frozen_parent_threshold():
    parent = _parent(extra_args={"autocompact": "12345", "other": "keep"})
    settings_store.upsert_web_extra_model("gpt-6.1-sol", "Sol", context_window=400_000)

    child = subagent._child_options(parent, None)

    assert child.extra_args["autocompact"] == "12345"
    assert parent.extra_args == {"autocompact": "12345", "other": "keep"}


@pytest.mark.parametrize("parent_args,configured,expected", [
    ({}, 400_000, "260000"),
    ({"autocompact": None}, 400_000, "260000"),
    ({}, None, "325000"),
])
def test_child_compact_without_parent_threshold_uses_model_window(parent_args, configured, expected):
    if configured is not None:
        settings_store.upsert_web_extra_model("gpt-6.1-sol", "Sol", context_window=configured)
    child = subagent._child_options(_parent(extra_args=parent_args), None)
    assert child.extra_args["autocompact"] == expected


def test_child_compact_explicit_model_prefers_selection_over_resolved():
    settings_store.upsert_web_extra_model("relay/claude-sonnet-5-5", "别名", context_window=300_000)
    settings_store.upsert_web_extra_model("claude-sonnet-5-5", "模型", context_window=500_000)
    parent = _parent(extra_args={"autocompact": "12345"})

    child = subagent._child_options(parent, "relay/claude-sonnet-5-5")

    assert child.model == "claude-sonnet-5-5"
    assert child.extra_args["autocompact"] == "195000"
    assert parent.extra_args["autocompact"] == "12345"


def test_child_compact_does_not_use_other_provider_window():
    settings_store.upsert_web_extra_model("claude-sonnet-5-5", "模型", context_window=500_000)
    child = subagent._child_options(_parent(extra_args={"autocompact": "12345"}), "relay/claude-sonnet-5-5")
    assert child.extra_args["autocompact"] == "650000"


@pytest.mark.anyio
async def test_model_settings_use_selection_id(sdk):
    settings_store.set_web_effort("low", model="gpt-6-luna")
    settings_store.set_web_verbosity("high", model="gpt-6-luna")
    await subagent.build_tool(_parent()).handler({"prompt": "任务", "model": "Luna"})
    options = sdk.clients[0].options
    assert options.effort == "low"
    assert "high" in options.env["CLAUDE_CODE_EXTRA_BODY"]


@pytest.mark.anyio
@pytest.mark.parametrize("args", [
    {"prompt": ""}, {"prompt": 123}, {"prompt": "任务", "model": 123},
    {"prompt": "任务", "timeout_seconds": 0}, {"prompt": "任务", "timeout_seconds": 1201},
    {"prompt": "任务", "timeout_seconds": True}, {"prompt": "任务", "model": "gpt"},
    {"prompt": "任务", "model": "not-configured"},
])
async def test_bad_input_and_ambiguous_model_do_not_start(sdk, args):
    result = await subagent.build_tool(_parent()).handler(args)
    assert result["is_error"] and sdk.clients == []


@pytest.mark.anyio
async def test_disabled_model_rechecked_after_tool_creation(sdk):
    tool = subagent.build_tool(_parent())
    settings_store.set_model_disabled("gpt-6-luna", True)
    result = await tool.handler({"prompt": "任务", "model": "Luna"})
    assert result["is_error"] and not sdk.clients


@pytest.mark.anyio
async def test_context_snapshot_and_parent_approval_boundary(sdk, monkeypatch):
    seen = []

    async def approve(*args):
        ctx = clarify.current()
        seen.append((ctx.session_key, ctx.chat_id, ctx.turn_id, danger.current_cwd()))
        return False

    monkeypatch.setattr(danger, "_ask_approval", approve)
    route = clarify.set_current("web:parent", SimpleNamespace(platform="web"), "parent-chat", turn_id=12)
    cwd = danger.set_cwd("/parent-worktree", "/parent-root")
    try:
        bound = subagent.build_tool(_parent(cwd="/cli-cwd"))
    finally:
        danger.reset_cwd(cwd)
        clarify.reset_current(route)
    # 模拟持久 MCP worker 持有别的/过时的 context，必须使用绑定时的父轮快照。
    route = clarify.set_current("web:wrong", SimpleNamespace(platform="web"), "wrong-chat", turn_id=99)
    try:
        async def probe(options):
            hook = options.hooks["PreToolUse"][0].hooks[0]
            out = await hook({"tool_name": "Write", "tool_input": {"file_path": "/opt/outside/result.txt"}}, "id", None)
            assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
            # worktree 项目根也被传入，不能写回共享主仓库。
            assert danger._project_root_var.get() == "/parent-root"
        sdk.probe = probe
        await bound.handler({"prompt": "任务"})
        assert clarify.current().session_key == "web:wrong"
    finally:
        clarify.reset_current(route)
    assert sdk.contexts == [("web:parent", "/parent-worktree", 1)]
    assert seen == [("web:parent", "parent-chat", 12, "/parent-worktree")]


@pytest.mark.anyio
async def test_background_identity_preserved_without_new_approval_grants(sdk):
    token = danger.set_task_session("cron:parent")
    try:
        bound = subagent.build_tool(_parent())
    finally:
        danger.reset_task_session(token)
    await bound.handler({"prompt": "任务"})
    assert sdk.contexts[0][0] == "cron:parent"


@pytest.mark.anyio
async def test_concurrent_calls_do_not_share_model_or_context(sdk):
    tools = []
    for name in ("one", "two"):
        token = clarify.set_current(name, object(), name)
        tools.append(subagent.build_tool(_parent()))
        clarify.reset_current(token)
    results = await asyncio.gather(
        tools[0].handler({"prompt": "一", "model": "Luna"}),
        tools[1].handler({"prompt": "二", "model": "claude-sonnet-5-5"}),
    )
    assert all(not result["is_error"] for result in results)
    assert {ctx[0] for ctx in sdk.contexts} == {"one", "two"}
    assert {client.options.model for client in sdk.clients} == {"gpt-6-luna", "claude-sonnet-5-5"}
    assert all(client.closed for client in sdk.clients)


@pytest.mark.anyio
async def test_allowlist_blocks_recursive_and_parent_disabled_tools(sdk):
    parent = _parent(disallowed_tools=["Bash(git:*)", "Write", "Web*"])
    opts = subagent._child_options(parent, None)
    assert opts.mcp_servers == {} and opts.strict_mcp_config and opts.plugins == []
    assert opts.agents is None
    hook = opts.hooks["PreToolUse"][0].hooks[0]
    for name in ("Bash", "Write", "WebSearch", "Agent", "Task", "Workflow", "Skill",
                 subagent.TOOL_NAME, "mcp__vococo__dispatch_session", "mcp__vococo__switch_model",
                 "mcp__voice__voice_dispatch_task", "CronCreate", "SendMessage", "EnterWorktree"):
        assert name not in opts.tools
        out = await hook({"tool_name": name}, "id", None)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", name
    assert await hook({"tool_name": "Read", "tool_input": {"file_path": "/project/test.py"}}, "id", None) == {}


@pytest.mark.anyio
async def test_parent_hook_exception_fails_closed(sdk):
    async def broken(*args):
        raise RuntimeError("secret")
    opts = subagent._child_options(_parent(hooks={"PreToolUse": [HookMatcher(hooks=[broken])]}), None)
    out = await opts.hooks["PreToolUse"][0].hooks[0]({"tool_name": "Read"}, "id", None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    result = await subagent.build_tool(_parent(hooks={})).handler({"prompt": "任务"})
    assert result["is_error"] and sdk.clients == []


@pytest.mark.anyio
async def test_no_recursive_entry_even_if_called_directly(sdk):
    bound = subagent.build_tool(_parent())
    token = subagent._DEPTH.set(1)
    try:
        assert (await bound.handler({"prompt": "递归"}))["is_error"]
        nested = subagent.build_tool(_parent())
    finally:
        subagent._DEPTH.reset(token)
    assert (await nested.handler({"prompt": "递归"}))["is_error"]
    assert sdk.clients == []


@pytest.mark.anyio
async def test_native_agent_cannot_invoke_parent_snapshot():
    hooks = subagent.protect_entry(danger.build_hooks())
    guard = hooks["PreToolUse"][-1].hooks[0]
    out = await guard({"tool_name": subagent.TOOL_NAME, "agent_id": "reviewer"}, "id", None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert await guard({"tool_name": subagent.TOOL_NAME}, "id", None) == {}


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["connect", "stream", "missing", "turn_limit"])
async def test_errors_never_report_success_or_expose_exception(sdk, failure):
    if failure == "connect":
        sdk.connect_error = True
    elif failure == "stream":
        sdk.messages = [RuntimeError("不要泄漏的 secret")]
    elif failure == "missing":
        sdk.messages = []
    else:
        sdk.messages = [_done("部分结果", error=True)]
    result = await subagent.build_tool(_parent()).handler({"prompt": "任务"})
    assert result["is_error"] and "secret" not in _text(result)
    assert sdk.clients[0].closed


@pytest.mark.anyio
async def test_timeout_closes_child(sdk):
    sdk.hang = True
    result = await subagent._run(_parent(), "任务", None, 0.01)
    assert result["is_error"] and "超过" in _text(result)
    assert sdk.clients[0].closed


@pytest.mark.anyio
async def test_parent_cancel_waits_for_child_close(sdk):
    sdk.hang = True
    task = asyncio.create_task(subagent.build_tool(_parent()).handler({"prompt": "任务"}))
    await sdk.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sdk.clients[0].closed


@pytest.mark.anyio
async def test_cancel_during_disconnect_still_waits_for_cleanup(sdk):
    sdk.hold_close = True
    task = asyncio.create_task(subagent.build_tool(_parent()).handler({"prompt": "任务"}))
    await sdk.closing.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()  # SDK 关闭和用户取消可能同时到达。
    await asyncio.sleep(0)
    assert not sdk.clients[0].closed and not task.done()
    sdk.release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sdk.clients[0].closed


@pytest.mark.anyio
async def test_usage_shared_by_calls_and_budget_denies_further_work(sdk):
    usage = subagent.Usage(limit=20)
    bound = subagent.build_tool(_parent(), usage)
    assert not (await bound.handler({"prompt": "第一项"}))["is_error"]
    assert (await bound.handler({"prompt": "第二项"}))["is_error"]
    assert usage.tokens == 30 and usage.exceeded
    assert (await bound.handler({"prompt": "第三项"}))["is_error"]
    assert len(sdk.clients) == 2
    hook = sdk.clients[-1].options.hooks["PreToolUse"][0].hooks[0]
    denied = await hook({"tool_name": "Read"}, "id", None)
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.anyio
async def test_stream_usage_survives_timeout_without_result(sdk):
    from claude_agent_sdk import StreamEvent

    async def messages():
        yield StreamEvent(uuid="e", session_id="child", event={
            "type": "message_start", "message": {"usage": {"input_tokens": 25}},
        })
        await asyncio.Event().wait()

    usage = subagent.Usage()
    observed = []
    usage.on_update = lambda: observed.append(usage.tokens)
    original_client = subagent.ClaudeSDKClient

    def client(options):
        instance = original_client(options)
        instance.receive_response = messages
        return instance

    from unittest.mock import patch
    with patch.object(subagent, "ClaudeSDKClient", client):
        result = await subagent._run(_parent(), "任务", None, 0.01, usage)
    assert result["is_error"] and usage.tokens == 25 and observed == [25]
    assert sdk.clients[0].closed


def test_usage_result_only_adds_missing_tokens():
    from claude_agent_sdk import StreamEvent

    usage = subagent.Usage()
    observe = subagent._usage_observer(usage)
    observe(StreamEvent(uuid="a", session_id="child", event={
        "type": "message_start", "message": {"usage": {
            "input_tokens": 8, "cache_creation_input_tokens": 2, "cache_read_input_tokens": 50,
        }},
    }))
    observe(StreamEvent(uuid="b", session_id="child", event={
        "type": "message_delta", "usage": {"output_tokens": 3},
    }))
    observe(_done())  # Result 总输入10、输出5，不能把已报的13再加一次。
    assert usage.tokens == 15 and usage.input_fresh == 10 and usage.cache_read == 50
    assert usage.output_tokens == 5


def test_usage_cached_input_is_confirmed_before_charging_parent_budget():
    from claude_agent_sdk import StreamEvent

    usage = subagent.Usage(limit=1000)
    observe = subagent._usage_observer(usage)
    observe(StreamEvent(uuid="a", session_id="child", event={
        "type": "message_start", "message": {"usage": {"input_tokens": 5842}},
    }))
    assert not usage.exceeded and usage.tokens == 0
    observe(StreamEvent(uuid="b", session_id="child", event={
        "type": "message_delta", "usage": {
            "input_tokens": 208, "output_tokens": 5, "cache_read_input_tokens": 5632,
        },
    }))
    observe(None)
    assert usage.tokens == 213 and usage.cache_read == 5632
    assert not usage.exceeded


def test_usage_missing_result_usage_keeps_interrupted_input_estimate():
    from claude_agent_sdk import StreamEvent

    usage = subagent.Usage()
    observe = subagent._usage_observer(usage)
    observe(StreamEvent(uuid="a", session_id="child", event={
        "type": "message_start", "message": {"usage": {"input_tokens": 25}},
    }))
    result = _done()
    result.usage = None
    observe(result)
    observe(None)
    assert usage.tokens == 25


def test_usage_final_result_supersedes_unconfirmed_start_on_cleanup():
    from claude_agent_sdk import StreamEvent

    usage = subagent.Usage(limit=1000)
    observe = subagent._usage_observer(usage)
    observe(StreamEvent(uuid="a", session_id="child", event={
        "type": "message_start", "message": {"usage": {"input_tokens": 5842}},
    }))
    observe(_done())
    observe(None)
    assert usage.tokens == 15 and not usage.exceeded


@pytest.mark.anyio
async def test_result_fallback_and_size_bound(sdk):
    sdk.messages = [AssistantMessage(content=[TextBlock(text="最终答复")], model="test"), _done(None)]
    result = await subagent.build_tool(_parent()).handler({"prompt": "任务"})
    assert _text(result) == "最终答复"
    sdk.messages = [_done("x" * (subagent._MAX_RESULT_CHARS + 100))]
    result = await subagent.build_tool(_parent()).handler({"prompt": "任务"})
    assert "已截断" in _text(result)
    assert len(_text(result)) < subagent._MAX_RESULT_CHARS + 100


def test_parent_restricted_tool_surface_and_settings_not_widened():
    callback = AsyncMock()
    parent = _parent(tools=["Read"], skills=["pdf"], can_use_tool=callback,
                     sandbox={"enabled": True}, settings='{"permissions":{"deny":["Bash"]}}')
    child = subagent._child_options(parent, None)
    assert child.tools == ["Read"] and child.can_use_tool is callback
    assert child.setting_sources == ["user", "project"]
    assert child.sandbox == parent.sandbox and child.sandbox is not parent.sandbox
    assert child.settings == parent.settings
    assert child.max_turns == 40


@pytest.mark.anyio
@pytest.mark.parametrize("budget", [0, 10])
@pytest.mark.parametrize("pooling", [False, True])
async def test_stream_turn_registers_real_mcp_tool_with_agent_runtime(sdk, monkeypatch, budget, pooling):
    options = []
    retained = []

    class ParentClient:
        def __init__(self, options):
            self.options = options
            self.interrupt = AsyncMock()
            outer_options.append(options)

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def query(self, prompt):
            pass

        async def get_context_usage(self):
            return {"totalTokens": 15, "rawMaxTokens": 500_000}

        async def receive_messages(self):
            server = self.options.mcp_servers["vococo"]["instance"]
            async def exercise():
                bridge = SdkMcpBridge("vococo", server)
                try:
                    await _initialize_bridge(bridge)
                    listed = await bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                    assert "run_subagent" in {tool["name"] for tool in listed["result"]["tools"]}
                    result = await bridge.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                        "name": "run_subagent", "arguments": {"prompt": "完整子任务", "model": "Luna"}}})
                    if budget:
                        assert "预算" in result["result"]["content"][0]["text"] and result["result"]["isError"]
                    else:
                        assert result["result"]["content"][0]["text"] == "子任务结果"
                        assert not result["result"].get("isError", False)
                finally:
                    await bridge.aclose()
            # MCP worker 使用独立上下文，验证父上下文已经正确捕获。
            await asyncio.create_task(exercise(), context=contextvars.Context())
            yield _done("父结果")

    outer_options = options
    monkeypatch.setattr(agent, "ClaudeSDKClient", ParentClient)
    async def checkout(*args):
        return retained.pop() if retained else None

    async def checkin(key, client, *args):
        retained.append(client)

    monkeypatch.setattr(agent.client_pool, "enabled", lambda: pooling)
    monkeypatch.setattr(agent.client_pool, "checkout", checkout)
    monkeypatch.setattr(agent.client_pool, "checkin", checkin)
    monkeypatch.setattr(agent.settings_store, "vococo_enabled", lambda: True)
    monkeypatch.setattr(agent.settings_store, "effective_skills", lambda *a, **kw: [])
    monkeypatch.setattr(agent, "_load_system_prompt", AsyncMock(return_value={"append": "父规则"}))
    monkeypatch.setattr(agents, "runtime_for_session", lambda key: {
        **dict.fromkeys(agents.RUNTIME_KEYS), "disallowed_tools": ["Bash"],
    })
    token = clarify.set_current("web:parent", SimpleNamespace(platform="web"), "parent", turn_id=23)
    cwd_token = danger.set_cwd("/parent-worktree")
    rounds = 2 if pooling and not budget else 1
    try:
        for _ in range(rounds):
            recorded = []
            events = [ev async for ev in agent.stream_turn(
                [], "完整父任务", model="gpt-6.1-sol", resume="parent-resume", session_key="web:parent",
                cwd="/parent-worktree", disallowed_tools=["Write"], token_budget=budget,
                usage_callback=recorded.append,
            )]
            reply = events[-1].reply
            # 保温第二轮必须重新绑定回调/清零子账，不能算成45或漏记成15。
            assert recorded[-1] == 30 and reply.turn_tokens == 30
            assert reply.input_fresh == 20 and reply.output_tokens == 10
            assert reply.budget_exceeded is bool(budget)
            assert reply.is_error is bool(budget)
    finally:
        danger.reset_cwd(cwd_token)
        clarify.reset_current(token)
    assert len(options) == 1 and options[0].resume == "parent-resume"
    assert events[-1].reply.text == ""  # 假父 SDK 未发送文字流，只验证工具协议。
    assert sdk.contexts == [("web:parent", "/parent-worktree", 1)] * rounds
    assert set(sdk.clients[0].options.disallowed_tools) >= {"Bash", "Write", "Agent", "Task", "Workflow"}
    assert sdk.clients[0].options.model == "gpt-6-luna"


async def _initialize_bridge(bridge):
    result = await bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-11-25", "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    }})
    assert "result" in result
    await bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})


class _MemoryTransport(Transport):
    """只替换 CLI 的线缆；client、Query、MCP 分发和取消链全部使用真实 SDK。"""

    def __init__(self, *, hold_close=False, on_prompt=None, connect_error=False):
        self.incoming = asyncio.Queue()
        self.started = asyncio.Event()
        self.closing = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False
        self.hold_close = hold_close
        self.on_prompt = on_prompt
        self.on_control_response = None
        self.connect_error = connect_error
        self.connect_task = self.close_task = None

    async def connect(self):
        self.connect_task = asyncio.current_task()

    async def write(self, data):
        message = json.loads(data)
        if message["type"] == "control_request":
            if self.connect_error and message["request"]["subtype"] == "initialize":
                raise RuntimeError("初始化失败")
            self.incoming.put_nowait({"type": "control_response", "response": {
                "subtype": "success", "request_id": message["request_id"], "response": {},
            }})
        elif message["type"] == "control_response" and self.on_control_response:
            self.on_control_response(message["response"])
        elif message["type"] == "user":
            self.started.set()
            if self.on_prompt:
                self.on_prompt()

    async def read_messages(self):
        while True:
            message = await self.incoming.get()
            if isinstance(message, BaseException):
                raise message
            yield message

    async def close(self):
        self.close_task = asyncio.current_task()
        self.closing.set()
        if self.hold_close:
            await self.release.wait()
        self.closed = True

    async def end_input(self):
        pass

    def is_ready(self):
        return not self.closed


def _mcp_call(index):
    return {"type": "control_request", "request_id": f"child-{index}", "request": {
        "subtype": "mcp_message", "server_name": "vococo", "message": {
            "jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {
                "name": "run_subagent", "arguments": {"prompt": "完整子任务"},
            },
        },
    }}


@pytest.mark.anyio
async def test_real_sdk_disconnect_from_different_task():
    transport = _MemoryTransport(hold_close=True)
    client = ClaudeSDKClient(options=ClaudeAgentOptions(), transport=transport)
    await client.connect()
    query = client._query
    read_task = query._read_task
    closing = asyncio.create_task(subagent._disconnect(client))
    try:
        await asyncio.wait_for(transport.closing.wait(), 2)
        assert read_task.done() and not transport.closed
        assert transport.connect_task is not transport.close_task
        closing.cancel()
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
    finally:
        transport.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, 2)
    assert transport.closed and client._query is None and client._transport is None


@pytest.mark.anyio
@pytest.mark.parametrize("ending", ["cancel", "anyio_cancel", "error", "early_result", "connect_error", "retry"])
async def test_parent_waits_for_real_sdk_mcp_children(monkeypatch, ending):
    children, parents, clients = [], [], []
    all_started = asyncio.Event()
    lock_released = asyncio.Event()
    scope_ready = asyncio.Event()
    scope_holder = []

    def child_client(options):
        transport = _MemoryTransport(hold_close=True)
        children.append(transport)
        def on_prompt():
            if len(children) == 2 and all(t.started.is_set() for t in children):
                all_started.set()
        transport.on_prompt = on_prompt
        return ClaudeSDKClient(options=options, transport=transport)

    def parent_client(options):
        # resume 失败后的冷重试必须在上一轮全部关闭后才允许启动。
        if parents:
            assert all(t.closed for t in [*children, *parents])
        transport = _MemoryTransport(connect_error=ending == "connect_error")
        parents.append(transport)
        def control(request_id, message):
            transport.incoming.put_nowait({"type": "control_request", "request_id": request_id,
                "request": {"subtype": "mcp_message", "server_name": "vococo", "message": message}})

        def on_response(response):
            if response["request_id"] == "mcp-init":
                assert response["subtype"] == "success" and "result" in response["response"]["mcp_response"]
                control("mcp-ready", {"jsonrpc": "2.0", "method": "notifications/initialized"})
            elif response["request_id"] == "mcp-ready":
                for index in range(2):
                    transport.incoming.put_nowait(_mcp_call(index))

        def on_prompt():
            if len(parents) > 1:
                transport.incoming.put_nowait({"type": "result", **asdict(_done())})
            else:
                control("mcp-init", {"jsonrpc": "2.0", "id": 100, "method": "initialize", "params": {
                    "protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"}}})
        transport.on_prompt = on_prompt
        transport.on_control_response = on_response
        client = ClaudeSDKClient(options=options, transport=transport)
        clients.append(client)
        return client

    monkeypatch.setattr(subagent, "ClaudeSDKClient", child_client)
    monkeypatch.setattr(agent, "ClaudeSDKClient", parent_client)
    monkeypatch.setattr(agent.client_pool, "enabled", lambda: True)
    monkeypatch.setattr(agent.client_pool, "checkout", AsyncMock(return_value=None))
    checkin = AsyncMock()
    monkeypatch.setattr(agent.client_pool, "checkin", checkin)
    monkeypatch.setattr(agent, "_query_context_usage", AsyncMock(return_value=(None, 0, 500_000, False)))
    monkeypatch.setattr(agent.settings_store, "vococo_enabled", lambda: True)
    monkeypatch.setattr(agent.settings_store, "effective_skills", lambda *a, **kw: [])
    monkeypatch.setattr(agent, "_load_system_prompt", AsyncMock(return_value={"append": "父规则"}))
    monkeypatch.setattr(agents, "runtime_for_session", lambda key: dict.fromkeys(agents.RUNTIME_KEYS))

    async def run_parent():
        try:
            with anyio.CancelScope() as scope:
                scope_holder.append(scope)
                scope_ready.set()
                return [event async for event in agent.stream_turn(
                    [], "完整父任务", session_key="web:parent", cwd="/project",
                    model="gpt-6.1-sol", resume="old-session" if ending == "retry" else None,
                )]
        finally:
            lock_released.set()

    parent = asyncio.create_task(run_parent())
    try:
        await asyncio.wait_for(scope_ready.wait(), 2)
        if ending != "connect_error":
            await asyncio.wait_for(all_started.wait(), 2)
            query = clients[0]._query
            assert len(query._child_tasks) >= 2
            if ending == "cancel":
                parent.cancel()
            elif ending == "anyio_cancel":
                scope_holder[0].cancel()
            elif ending in {"error", "retry"}:
                parents[0].incoming.put_nowait(RuntimeError("模拟上游断开"))
            else:
                parents[0].incoming.put_nowait({"type": "result", **asdict(_done())})
            await asyncio.wait_for(asyncio.gather(*(t.closing.wait() for t in children)), 2)
            if ending == "cancel":
                parent.cancel()  # 原生 SDK/用户第二次取消也不能穿透父轮清理屏障。
            await asyncio.sleep(0)
            assert not parent.done() and not lock_released.is_set()
            checkin.assert_not_awaited()
            assert not any(t.closed for t in children)
            usage = clients[0]._vococo_child_usage
            assert usage.closing and len(usage.tasks) == 2
            with pytest.raises(RuntimeError, match="尚未清理"):
                usage.reset(10, lambda: None)
            # 清理期间拒绝迟到的 MCP 调用，不生成第三个子 client。
            server = clients[0].options.mcp_servers["vococo"]["instance"]
            bridge = SdkMcpBridge("late", server)
            try:
                await _initialize_bridge(bridge)
                late = await bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                    "name": "run_subagent", "arguments": {"prompt": "迟到任务"}}})
                assert late["result"]["isError"] and len(children) == 2
            finally:
                await bridge.aclose()
    finally:
        for transport in children:
            transport.release.set()
        if ending == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(parent, 2)
        elif ending in {"error", "connect_error"}:
            with pytest.raises(Exception, match="模拟上游断开|初始化失败"):
                await asyncio.wait_for(parent, 2)
        else:
            await asyncio.wait_for(parent, 2)
        # 重试成功后的父 client 可回池，测试负责关闭它，不留下真实 SDK 读任务。
        for client in clients:
            await client.disconnect()
    assert lock_released.is_set() and all(t.closed for t in [*children, *parents])
    assert all(not client._vococo_child_usage.tasks for client in clients)
    if ending == "retry":
        assert len(parents) == 2 and checkin.await_count == 1
    else:
        checkin.assert_not_awaited()
