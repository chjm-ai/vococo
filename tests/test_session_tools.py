"""普通聊天会话工具：隔离存储、范围控制与真实桥接入队。"""
import json

import pytest

from vococo import config
from vococo.gateway import clarify, web_bridge
from vococo.memory import agents, session_store
from vococo.tools import sessions


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def chat(isolated, monkeypatch):
    monkeypatch.setattr(sessions, "_queued", {})
    monkeypatch.setattr(web_bridge, "_dispatch_fn", None)
    monkeypatch.setattr(web_bridge, "_inject_key_fn", None)
    monkeypatch.setattr(web_bridge, "_cancel_fn", None)
    monkeypatch.setattr(clarify, "_active_sessions", {})
    monkeypatch.setattr(clarify, "_by_session", {})
    monkeypatch.setattr(clarify, "_pending", {})
    agent = agents.create("测试助理")
    source = f"web:p{agent['project_hash']}:source"
    target = f"web:p{agent['project_hash']}:older"
    session_store.append(target, "旧问题", "旧回答")
    session_store.set_title(target, "早期方案讨论")
    session_store.start_turn(source, "查找之前的讨论")
    return agent, source, target


async def call(source, name, args=None, denied=()):
    tools = {t.name: t for t in sessions.build_tools(source, list(denied))}
    result = await tools[name].handler(args or {})
    return json.loads(result["content"][0]["text"])


@pytest.mark.anyio
async def test_title_search_finds_old_session_before_limiting(chat):
    _, source, target = chat
    for index in range(6):
        session_store.append(target + str(index), "新问题", "新回答")
    result = await call(source, "list_sessions", {"query": "早期方案", "limit": 1})
    assert result["total"] == 1
    assert result["truncated"] is False
    assert result["sessions"][0]["session_id"] == target
    assert result["source_session"] == source


@pytest.mark.anyio
async def test_list_reports_total_truncation_empty_and_id_match(chat):
    agent, source, target = chat
    session_store.append(target + "2", "问", "答")
    result = await call(source, "list_sessions", {"limit": 1})
    assert result["total"] == 3
    all_rows = await call(source, "list_sessions")
    assert {row["session_id"] for row in all_rows["sessions"]} == {
        target, target + "2", agents.main_session_key(agent),
    }
    assert len(result["sessions"]) == 1
    assert result["truncated"] and "query" in result["message"]
    empty = await call(source, "list_sessions", {"query": "不可能匹配"})
    assert empty["total"] == 0 and empty["sessions"] == [] and not empty["truncated"]
    for reference in (target, target.removeprefix("web:")):
        found = await call(source, "list_sessions", {"query": reference})
        assert found["total"] == 2


@pytest.mark.anyio
@pytest.mark.parametrize("limit", [0, 51, -1, True, "2", None])
async def test_list_rejects_invalid_limit(chat, limit):
    assert "error" in await call(chat[1], "list_sessions", {"limit": limit})


@pytest.mark.anyio
@pytest.mark.parametrize("full_id", [False, True])
async def test_query_returns_recent_bounded_turns_for_both_ids(chat, full_id):
    _, source, target = chat
    for index in range(8):
        session_store.append(target, f"问题{index}", f"回答{index}")
    reference = target if full_id else target.removeprefix("web:")
    result = await call(source, "query_session", {"session_id": reference, "limit": 2})
    assert result["source_session"] == source
    assert result["session_id"] == target
    assert result["turns"] == [{"user": "问题6", "assistant": "回答6"},
                               {"user": "问题7", "assistant": "回答7"}]
    assert result["truncated"] is True


@pytest.mark.anyio
@pytest.mark.parametrize("full_id", [False, True])
async def test_continue_really_injects_into_existing_session(chat, monkeypatch, full_id):
    from vococo.gateway.adapters.web import WebAdapter

    _, source, target = chat
    adapter = WebAdapter()
    events = []
    monkeypatch.setattr(adapter, "_emit", events.append)
    web_bridge.register(adapter.inject, lambda _: pytest.fail("不允许打断目标"), adapter.inject_by_key)
    before = session_store.list_sessions("web:")
    instruction = "  /继续\n完整保留指令、换行和末尾空格  "
    result = await call(source, "continue_session", {
        "session_id": target if full_id else target.removeprefix("web:"),
        "instruction": instruction,
    })
    assert result["status"] == "queued"
    assert result["instruction"] == instruction
    assert result["source_session"] == source
    assert "不代表执行完成" in result["message"]
    incoming = adapter._inbox.get_nowait()
    assert incoming.session_key == target
    assert source in incoming.text and incoming.text.endswith(instruction)
    assert "来源" in incoming.text
    assert adapter._inbox.empty()
    assert events[0]["text"] == incoming.text
    assert session_store.list_sessions("web:") == before


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["query_session", "continue_session"])
@pytest.mark.parametrize("kind", ["unknown", "archived", "self", "project", "agent", "task", "voice"])
async def test_target_guards(chat, method, kind):
    _, source, target = chat
    if kind == "unknown":
        target += "missing"
    elif kind == "archived":
        session_store.set_conv_archived(target, True)
    elif kind == "self":
        target = source
    elif kind in {"project", "agent"}:
        other = agents.create("另一个助理")
        target = f"web:p{other['project_hash']}:other" if kind == "agent" else "web:unscoped"
        session_store.append(target, "秘密", "不能泄露")
    else:
        target = "task:123" if kind == "task" else "voice-chat:main"
        session_store.append(target, "秘密", "不能泄露")
    result = await call(source, method, {"session_id": target, "instruction": "继续"})
    if method == "query_session" and kind in {"project", "agent", "task", "voice"}:
        assert result["turns"] == [{"user": "秘密", "assistant": "不能泄露"}]
    elif method == "query_session" and kind == "archived":
        assert result["turns"] == [{"user": "旧问题", "assistant": "旧回答"}]
    else:
        assert "error" in result and "turns" not in result


@pytest.mark.anyio
@pytest.mark.parametrize("source_kind", ["missing", "archived", "task", "bare", "orphan"])
async def test_all_tools_require_real_web_source(chat, source_kind):
    _, source, target = chat
    if source_kind == "missing":
        source += "missing"
    elif source_kind == "archived":
        session_store.set_conv_archived(source, True)
    elif source_kind == "task":
        source = "task:source"
        session_store.append(source, "问", "答")
    elif source_kind == "bare":
        source = source.removeprefix("web:")
    else:
        source = "web:pdeadbeef:source"
        session_store.append(source, "问", "答")
    for method in ("list_sessions", "query_session", "continue_session"):
        result = await call(source, method, {"session_id": target, "instruction": "继续"})
        if source_kind in {"archived", "task", "orphan"} and method != "continue_session":
            assert "error" not in result
        elif source_kind not in {"archived", "task", "orphan"}:
            assert "error" in result


@pytest.mark.anyio
async def test_list_includes_other_scopes_archived_orphan_and_bound_main(chat):
    agent, source, target = chat
    other = agents.create("其他")
    for key in ("web:unscoped", "web:pdeadbeef:orphan", f"web:p{other['project_hash']}:x",
                target + "archived", "task:123", "voice-chat:main"):
        session_store.append(key, "秘密", "不能泄露")
    session_store.set_conv_archived(target + "archived", True)
    result = await call(source, "list_sessions")
    assert result["total"] == 9
    assert {row["session_id"] for row in result["sessions"]} == {
        target, "web:unscoped", "web:pdeadbeef:orphan", f"web:p{other['project_hash']}:x",
        target + "archived", "task:123", "voice-chat:main",
        agents.main_session_key(agent), agents.main_session_key(other),
    }
    assert source not in {row["session_id"] for row in result["sessions"]}


@pytest.mark.anyio
@pytest.mark.parametrize("legacy", [False, True])
async def test_task_session_can_be_searched_queried_and_continued(chat, monkeypatch, legacy):
    from vococo.core import tasks
    from vococo.gateway.adapters.web import WebAdapter

    agent, source, _ = chat
    monkeypatch.setattr(tasks, "_DB", None)
    try:
        task = tasks.create("可续接任务", "检查", agent_id=agent["id"],
                            session_key="task:legacy" if legacy else None)
        key = tasks.session_key(task["id"])
        assert key == task["session_key"]
        assert key == "task:legacy" if legacy else key.startswith("session:")
        session_store.append(key, "任务问题", "任务结果")
        tasks.set_status(task["id"], "running")
        tasks.finish(task["id"], "done", "任务结果", "任务结果")
        session_store.set_title(key, "可续接任务")
        found = await call(source, "list_sessions", {"query": key})
        assert found["total"] == 1
        assert found["sessions"][0]["session_id"] == key
        queried = await call(source, "query_session", {"session_id": key})
        assert queried["turns"] == [{"user": "任务问题", "assistant": "任务结果"}]
        adapter = WebAdapter()
        monkeypatch.setattr(adapter, "_emit", lambda event: None)
        web_bridge.register(adapter.inject, lambda _: pytest.fail("不允许打断任务"), adapter.inject_by_key)
        result = await call(source, "continue_session", {"session_id": key, "instruction": "继续检查"})
        assert result["status"] == "queued" and result["session_id"] == key
        incoming = adapter._inbox.get_nowait()
        assert incoming.session_key == key and incoming.text.endswith("继续检查")
        assert incoming.source_access is None
    finally:
        if tasks._DB is not None:
            tasks._DB.close()
            tasks._DB = None


@pytest.mark.anyio
async def test_restricted_source_cannot_read_other_sessions(chat):
    for method in ("list_sessions", "query_session"):
        result = await call(chat[1], method, {"session_id": chat[2]}, denied=["Read"])
        assert "error" in result


@pytest.mark.anyio
async def test_non_unified_platform_session_is_readable(chat):
    source, target = chat[1], "cli:example"
    session_store.append(target, "命令行问题", "命令行回答")
    assert (await call(source, "query_session", {"session_id": target}))["turns"]
    assert any(row["session_id"] == target for row in (await call(source, "list_sessions"))["sessions"])


@pytest.fixture
def bridge(chat):
    calls = []

    async def dispatch(conv, text):
        calls.append((conv, text))

    with_key = lambda key, text, **kw: calls.append((key.removeprefix("web:"), text))
    web_bridge.register(dispatch, lambda _: pytest.fail("不能取消会话"), with_key)
    return calls


@pytest.mark.anyio
@pytest.mark.parametrize("restriction", ["denied", "source_chain", "target_chain", "agent_denied", "escalation"])
async def test_continue_refuses_restricted_or_automatic_context(chat, bridge, restriction):
    agent, source, target = chat
    denied = ["Bash"] if restriction == "denied" else []
    if restriction in {"source_chain", "target_chain"}:
        session_store.start_turn(source if restriction == "source_chain" else target, "自动消息",
                                 source_access={"session": "task:upstream", "mode": "full"})
    elif restriction == "agent_denied":
        agents.update(agent["id"], disallowed_tools=["Read"])
    elif restriction == "escalation":
        session_store.set_permission(target, "full")
    result = await call(source, "continue_session", {"session_id": target, "instruction": "继续"}, denied)
    assert "error" in result
    assert not bridge


@pytest.mark.anyio
async def test_continue_rechecks_agent_permissions_after_tool_creation(chat, bridge):
    agent, source, target = chat
    tool = next(t for t in sessions.build_tools(source, []) if t.name == "continue_session")
    agents.update(agent["id"], disallowed_tools=["Bash"])
    result = json.loads((await tool.handler({"session_id": target, "instruction": "继续"}))["content"][0]["text"])
    assert "error" in result
    assert not bridge


@pytest.mark.anyio
async def test_continue_checks_current_agent_full_permission_and_session_override(chat, bridge):
    agent, source, target = chat
    tool = next(t for t in sessions.build_tools(source, []) if t.name == "continue_session")
    session_store.set_permission(source, "standard")
    agents.update(agent["id"], permission="full")
    result = json.loads((await tool.handler({"session_id": target, "instruction": "继续"}))["content"][0]["text"])
    assert "error" in result and not bridge


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["busy", "pending", "unavailable"])
async def test_continue_refuses_busy_pending_or_unavailable_bridge(chat, bridge, state):
    _, source, target = chat
    if state == "busy":
        clarify._active_sessions[target] = "运行中"
    elif state == "pending":
        clarify.register(target, ["确认", "取消"])
    else:
        web_bridge._inject_key_fn = None
    result = await call(source, "continue_session", {"session_id": target, "instruction": "继续"})
    assert "error" in result
    assert not bridge
    if state == "pending":
        assert clarify.has_pending(target)


@pytest.mark.anyio
async def test_second_instruction_is_refused_until_queued_turn_finishes(chat, bridge):
    _, source, target = chat
    args = {"session_id": target, "instruction": "继续"}
    assert (await call(source, "continue_session", args))["status"] == "queued"
    assert "error" in await call(source, "continue_session", args)
    assert len(bridge) == 1
    # 真实存储模拟网关开始处理，再模拟已完成；期间不能把第二条当作澄清答案。
    turn = session_store.start_turn(target, bridge[0][1])
    assert "error" in await call(source, "continue_session", args)
    session_store.finish_turn(turn, "完成")
    assert (await call(source, "continue_session", args))["status"] == "queued"
    assert len(bridge) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("instruction", ["", "  ", None, 5, "x" * 60001])
async def test_continue_rejects_invalid_instruction(chat, bridge, instruction):
    result = await call(chat[1], "continue_session", {"session_id": chat[2], "instruction": instruction})
    assert "error" in result and not bridge


@pytest.mark.anyio
@pytest.mark.parametrize("limit", [0, 11, True, "2", None])
async def test_query_rejects_invalid_limit(chat, limit):
    assert "error" in await call(chat[1], "query_session", {"session_id": chat[2], "limit": limit})


@pytest.mark.anyio
async def test_query_bounds_text_and_skips_incomplete_turns(chat):
    _, source, target = chat
    session_store.append(target, "问" * 5000, "答" * 5000)
    session_store.start_turn(target, "还未回答")
    result = await call(source, "query_session", {"session_id": target, "limit": 2})
    assert result["turns"][1] == {"user": "还未回答", "assistant": ""}
    assert len(result["turns"][0]["user"]) == 4000
    assert len(result["turns"][0]["assistant"]) == 4000
    assert result["truncated"]


@pytest.mark.anyio
async def test_exact_query_does_not_scan_all_sessions(chat, monkeypatch):
    monkeypatch.setattr(session_store, "list_sessions", lambda *a, **kw: pytest.fail("应精确查询"))
    assert "turns" in await call(chat[1], "query_session", {"session_id": chat[2]})


@pytest.mark.anyio
async def test_bridge_failure_never_claims_queued(chat):
    def fail_dispatch(conv, text, **kw):
        raise RuntimeError("桥接入队失败")

    web_bridge.register(fail_dispatch, lambda _: False, fail_dispatch)
    result = await call(chat[1], "continue_session", {"session_id": chat[2], "instruction": "继续"})
    assert "error" in result and "status" not in result


@pytest.mark.anyio
async def test_injected_turn_cannot_send_another_cross_session_instruction(chat, bridge):
    _, source, target = chat
    assert (await call(source, "continue_session", {
        "session_id": target, "instruction": "继续",
    }))["status"] == "queued"
    session_store.start_turn(target, bridge[0][1])
    # bridge 不传 source_access，仍须防止收到指令的一方继续自动互发。
    assert session_store.active_source_access(target) == {}
    result = await call(target, "continue_session", {"session_id": source, "instruction": "再次继续"})
    assert "error" in result and len(bridge) == 1


@pytest.mark.anyio
async def test_failed_dispatch_can_be_retried(chat, bridge):
    def fail_dispatch(conv, text, **kw):
        raise RuntimeError("入队失败")

    original_dispatch = web_bridge._dispatch_fn
    original_key_dispatch = web_bridge._inject_key_fn
    web_bridge.register(fail_dispatch, lambda _: False, fail_dispatch)
    args = {"session_id": chat[2], "instruction": "继续"}
    assert "error" in await call(chat[1], "continue_session", args)
    web_bridge.register(original_dispatch, lambda _: False, original_key_dispatch)
    assert (await call(chat[1], "continue_session", args))["status"] == "queued"
    assert len(bridge) == 1


@pytest.mark.anyio
async def test_concurrent_dispatch_reserves_target_before_await(chat):
    import asyncio

    entered, release = asyncio.Event(), asyncio.Event()

    def dispatch(conv, text, **kw):
        entered.set()

    web_bridge.register(dispatch, lambda _: pytest.fail("不允许打断"), dispatch)
    args = {"session_id": chat[2], "instruction": "继续"}
    first = asyncio.create_task(call(chat[1], "continue_session", args))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert "error" in await call(chat[1], "continue_session", args)
    finally:
        release.set()
        result = await first
    assert result["status"] == "queued"


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["skills", "mcp", "disallowed_tools"])
async def test_target_runtime_is_also_checked_each_time(chat, bridge, monkeypatch, field):
    _, source, target = chat
    original = agents.runtime_for_session
    tool = next(t for t in sessions.build_tools(source, []) if t.name == "continue_session")

    def runtime(key):
        value = original(key)
        if key == target:
            value[field] = ["Read"]
        return value

    monkeypatch.setattr(agents, "runtime_for_session", runtime)
    result = json.loads((await tool.handler({"session_id": target, "instruction": "继续"}))["content"][0]["text"])
    assert "error" in result and not bridge


@pytest.mark.anyio
async def test_general_web_sessions_and_resolution_are_supported(isolated, monkeypatch):
    source = config.resolve_session_key("web", "ordinary-source")
    target = config.resolve_session_key("web", "ordinary-target")
    session_store.start_turn(source, "查询")
    session_store.append(target, "问", "答")
    assert (await call(source, "list_sessions"))["total"] == 1
    for reference in (target, "ordinary-target"):
        assert (await call(source, "query_session", {"session_id": reference}))["turns"]
    # main 不是普通 Web 会话，必须遵守 resolve_session_key 的特殊映射。
    monkeypatch.setattr(config, "SESSION_KEY", "main")
    session_store.append("main", "主会话秘密", "不能泄露")
    session_store.append("web:main", "伪别名", "不能泄露")
    assert (await call(source, "query_session", {"session_id": "main"}))["turns"]
    assert "error" in await call(source, "query_session", {"session_id": "web:main"})


def test_tool_names_and_source_not_exposed_in_schema(chat):
    tools = sessions.build_tools(chat[1], [])
    assert {t.name for t in tools} == {"list_sessions", "query_session", "continue_session"}
    for t in tools:
        assert not {"source", "source_session", "session_key", "denied"} & set(t.input_schema.get("properties", {}))


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled", "pending"])
async def test_gateway_releases_queue_even_without_persisted_answer(chat, monkeypatch, outcome):
    import asyncio
    from unittest.mock import AsyncMock
    from vococo.gateway.adapters.web import WebAdapter
    from vococo.gateway.run import GatewayRunner

    _, source, target = chat
    adapter = WebAdapter()
    monkeypatch.setattr(adapter, "_emit", lambda event: None)
    web_bridge.register(adapter.inject, lambda _: False, adapter.inject_by_key)
    args = {"session_id": target, "instruction": "先对齐思路，确认后再修改"}
    assert (await call(source, "continue_session", args))["status"] == "queued"
    incoming = adapter._inbox.get_nowait()
    runner = GatewayRunner([])
    error = {"failure": RuntimeError("创建 worktree 失败"), "cancelled": asyncio.CancelledError()}.get(outcome)
    handle = AsyncMock(side_effect=error)
    monkeypatch.setattr(runner, "_handle", handle)
    monkeypatch.setattr(adapter, "send", AsyncMock())
    if outcome == "pending":
        monkeypatch.setattr(clarify, "has_pending", lambda key: True)
        monkeypatch.setattr(clarify, "resolve_text_for_session", lambda *args: pytest.fail("不能代答"))
    if outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await runner._dispatch(adapter, incoming)
    else:
        await runner._dispatch(adapter, incoming)
    assert target not in sessions._queued
    if outcome == "pending":
        handle.assert_not_called()
        monkeypatch.setattr(clarify, "has_pending", lambda key: False)
    assert (await call(source, "continue_session", args))["status"] == "queued"
