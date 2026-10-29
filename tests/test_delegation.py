"""真实执行器→stream_turn→SDK/MCP 路径；仅模型传输为本地夹具，所有存储在 tmp_path。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk import ResultMessage, StreamEvent
from claude_agent_sdk._internal.sdk_mcp_bridge import SdkMcpBridge

from vococo import config
from vococo.core import agent, delegation as d, task_events, task_runner as runner, tasks
from vococo.gateway import clarify, settings_store
from vococo.memory import agents, session_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def env(isolated, monkeypatch):
    monkeypatch.setattr(tasks, "_DB", None)
    monkeypatch.setattr(config, "CRON_JOBS_PATH", isolated / "cron.json")
    monkeypatch.setattr(settings_store, "_PATH", isolated / "settings.json")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(isolated / "sdk-config"))
    monkeypatch.setattr(config, "TASK_MAX_CONCURRENCY", 3)
    monkeypatch.setattr(runner, "_running", {})
    monkeypatch.setattr(d, "_pump", None)
    monkeypatch.setattr(d, "_draining", False)
    monkeypatch.setattr(clarify, "_active_sessions", set())
    monkeypatch.setattr(clarify, "_pending", {})
    monkeypatch.setattr(clarify, "_by_session", {})
    state_events = []
    monkeypatch.setattr(task_events, "_main_event_bridge", state_events.append)
    monkeypatch.setattr(agent.providers, "resolve", lambda *_: ("fixture-model", {}))
    monkeypatch.setattr(agent.providers, "sidecar_env", lambda *a, **kw: None)
    monkeypatch.setattr(agent.client_pool, "discard", AsyncMock())
    monkeypatch.setattr(agent.client_pool, "_pool", {})
    monkeypatch.setattr(config, "CLIENT_POOL_IDLE_TTL", 300)
    monkeypatch.setattr(runner, "_push_budget_warning", AsyncMock())
    state = SimpleNamespace(clients=[], behavior={}, errors=set(), usage=100, summary_calls=0,
                            children=[], sdk_transcripts={})

    class Client:
        def __init__(self, options):
            self.options = options
            self.interrupt = AsyncMock()
            self.text = "执行结果"
            self.context = list(state.sdk_transcripts.get(options.resume, []))
            state.clients.append(self)

        async def connect(self):
            pass

        async def query(self, prompt):
            self.prompt = prompt
            ctx = clarify.get_context()
            tid = tasks.task_id_from_session_key(ctx.session_key) if ctx else None
            row = tasks.get(tid) if tid else None
            self.context.append(str(prompt))
            if row:
                self.row = row
                callback = state.behavior.get(row["title"])
                if callback:
                    await callback(self)
            else:
                self.row = None
                state.summary_calls += 1
                self.text = "普通回复"
                if self.options.tools is None:
                    self.context.append(str(prompt))
                    state.sdk_transcripts["fixture-session"] = list(self.context)
                if self.options.tools is None and getattr(state, "source_behavior", None):
                    callback, state.source_behavior = state.source_behavior, None
                    await callback(self)

        async def get_context_usage(self):
            return {"totalTokens": 100, "rawMaxTokens": 200_000}

        async def receive_messages(self):
            # 模拟真实模型→工具→模型往返，走原 stream_turn 的逐次用量累计。
            if self.row and getattr(state, "round_usage", None):
                for index, usage in enumerate(state.round_usage):
                    yield StreamEvent(uuid=f"usage-{index}", session_id="fixture-session", event={
                        "type": "message_start", "message": {"usage": {"input_tokens": usage}}})
                    listed = await call(self, "list_delegate_agents", {})
                    assert any(item["id"] == state.b["id"] for item in listed)
                    yield StreamEvent(uuid=f"output-{index}", session_id="fixture-session", event={
                        "type": "message_delta", "usage": {"output_tokens": 10}})
            failed = bool((self.row and self.row["title"] in state.errors)
                          or (self.options.tools is None and getattr(state, "fail_source", False)))
            yield StreamEvent(uuid="fixture-event", session_id="fixture-session", event={
                "type": "content_block_delta", "delta": {"type": "text_delta", "text": self.text}})
            yield ResultMessage(subtype="error" if failed else "success", duration_ms=1, duration_api_ms=1,
                                is_error=failed, num_turns=1, session_id="fixture-session", result=self.text,
                                usage={"input_tokens": state.usage, "output_tokens": 10})

    monkeypatch.setattr(agent, "ClaudeSDKClient", Client)
    state.events = state_events
    state.a = agents.create("A")
    state.b = agents.create("B")
    state.c = agents.create("C")
    state.source = f"web:p{state.a['project_hash']}:main"
    state.delegate = d.delegate
    state.turn = session_store.start_turn(state.source, "委托任务")
    session_store.set_chosen_model(state.source, "fixture-model")
    yield state
    for work in list(runner._running.values()):
        work.cancel()
    await asyncio.gather(*list(runner._running.values()), return_exceptions=True)
    if d._pump:
        d._pump.cancel()
        await asyncio.gather(d._pump, return_exceptions=True)
    if tasks._DB:
        tasks._DB.close()
        tasks._DB = None


async def call(client, name, args):
    full_name = f"mcp__vococo__{name}"
    if d._blocked(full_name, client.options.disallowed_tools):
        return {"error": "工具拒绝"}
    for matcher in client.options.hooks["PreToolUse"]:
        for hook in matcher.hooks:
            result = await hook({"tool_name": full_name, "tool_input": args}, "call", {})
            if result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
                return {"error": "工具拒绝"}
    server = client.options.mcp_servers["vococo"]["instance"]
    bridge = SdkMcpBridge("vococo", server)
    try:
        initialized = await bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"}}})
        assert "result" in initialized
        await bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        result = await bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                      "params": {"name": name, "arguments": args}})
        return json.loads(result["result"]["content"][0]["text"])
    finally:
        await bridge.aclose()


def dispatch(env, **kw):
    return env.delegate(env.source, [], env.b["id"], kw.pop("title", "B任务"), "完整任务背景", **kw)


async def settle():
    for _ in range(300):
        if not runner._running and not d._draining:
            await d.drain()
            return
        await asyncio.sleep(.01)
    raise AssertionError("执行器没有结束")


def receipts(session):
    return session_store._conn().execute(
        "SELECT * FROM turns WHERE session_key=? AND user_text=''", (session,)).fetchall()


@pytest.mark.anyio
async def test_delegation_danger_gate_still_denies_without_background_approval(env, monkeypatch):
    from vococo.tools import danger
    from vococo.core import permissions

    agents.update(env.b["id"], permission="full")
    agents.update(env.b["id"], mcp=["business"])
    monkeypatch.setattr(settings_store, "effective_external_mcp", lambda names: {n: {} for n in names})
    monkeypatch.setattr(config, "APPROVAL_GATE", True)
    original = danger.classify
    monkeypatch.setattr(danger, "classify", lambda name, args, **kw:
                        ("escalate", "测试外部业务操作", True) if name == "mcp__business__send_email"
                        else original(name, args, **kw))
    approved = []
    async def reject(*args, **kw):
        approved.append(True)
        assert not permissions.is_full(danger.current_session_key())
        return False
    monkeypatch.setattr(danger, "_approve_background", reject)
    async def execute(client):
        assert not await guard_tool(client, "mcp__business__send_email")
    env.behavior["B任务"] = execute
    row = dispatch(env)
    await settle()
    assert approved == [True] and tasks.get(row["id"])["status"] == "done"


@pytest.mark.anyio
@pytest.mark.parametrize("warm", [True, False])
async def test_receipt_after_41_turns_preserves_customer_constraint_in_sdk_context(env, warm):
    constraint = "客户限制：不得向竞争对手提供报价"
    for index in range(41):
        text = constraint if index == 0 else f"普通对话{index}"
        async for event in agent.stream_turn(session_store.load_recent(env.source), text,
                                             session_key=env.source,
                                             resume=session_store.get_sdk_session_id(env.source)):
            if isinstance(event, agent.Done):
                session_store.set_sdk_session_id(env.source, event.reply.sdk_session_id)
                session_store.append(env.source, text, event.reply.text)
    first = agent.client_pool._pool[env.source].client
    assert constraint not in str(session_store.load_recent(env.source))
    session_store.append_external(env.source, "专项报价结果", "after-41")
    if not warm:
        agent.client_pool._pool.clear()
    async for _ in agent.stream_turn(session_store.load_recent(env.source), "按客户限制处理报价",
                                     session_key=env.source, resume="fixture-session"):
        pass
    current = agent.client_pool._pool[env.source].client
    assert (current is first) == warm
    assert current.options.resume == (None if warm else "fixture-session")
    assert constraint in str(current.context)
    assert "专项报价结果" in str(current.prompt)
    assert constraint not in str(current.prompt)  # 旧限制靠 SDK 原上下文保留，不是重放近 40 轮。


@pytest.mark.anyio
@pytest.mark.parametrize("with_db_history", [True, False])
async def test_cold_receipt_is_available_without_history_and_not_duplicated(env, with_db_history):
    text = "独立冷启动回执内容"
    session_store.append_external(env.source, text, "cold-result")
    session_store.append_external(env.source, text, "cold-result")
    history = session_store.load_recent(env.source) if with_db_history else []
    async for _ in agent.stream_turn(history, "请总结结果", session_key=env.source):
        pass
    current = agent.client_pool._pool[env.source].client
    assert str(current.prompt).count(text) == 1
    assert "不可信数据" in str(current.prompt)
    assert len(receipts(env.source)) == 1
    _, pending = session_store.external_context_state(env.source, "fixture-session")
    assert not pending


@pytest.mark.anyio
async def test_receipt_ack_is_per_sdk_and_only_successful_rounds(env):
    text = "只在成功后确认的回执"
    session_store.append_external(env.source, text, "ack-success-only")
    env.fail_source = True
    async for event in agent.stream_turn([], "追问", session_key=env.source):
        if isinstance(event, agent.Done):
            assert event.reply.is_error
    _, pending = session_store.external_context_state(env.source, "fixture-session")
    assert [item["text"] for item in pending] == [text]
    env.fail_source = False
    async for _ in agent.stream_turn([], "重试", session_key=env.source, resume="fixture-session"):
        pass
    _, pending = session_store.external_context_state(env.source, "fixture-session")
    assert not pending
    _, other = session_store.external_context_state(env.source, "another-sdk-session")
    assert [item["text"] for item in other] == [text]
    session_store.sync_external_context(env.source, "another-sdk-session", other[-1]["id"])
    _, original = session_store.external_context_state(env.source, "fixture-session")
    assert not original  # 另一个 SDK 的确认不会覆盖本 SDK 的进度。



async def guard_tool(client, name, args=None):
    if d._blocked(name, client.options.disallowed_tools):
        return False
    import re
    for matcher in client.options.hooks["PreToolUse"]:
        if matcher.matcher and not re.search(matcher.matcher, name):
            continue
        for hook in matcher.hooks:
            result = await hook({"tool_name": name, "tool_input": args or {}}, "tool", {})
            if result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
                return False
    return True


@pytest.mark.anyio
async def test_regular_runner_target_tools_mcp_skills_and_nested_receipts(env, monkeypatch):
    agents.update(env.b["id"], skills=["test-skill"], mcp=["business"])
    monkeypatch.setattr(settings_store, "effective_external_mcp", lambda names: {n: {} for n in names})
    session_store.append(f"web:p{env.b['project_hash']}:main", "目标主会话秘密", "不得继承")

    async def forward(client):
        assert client.options.tools is None
        assert client.options.setting_sources is None
        assert client.options.plugins
        assert "business" in client.options.mcp_servers
        assert "test-skill" in client.options.skills
        assert "目标主会话秘密" not in str(client.prompt)
        for name in ("Write", "Edit", "Skill", "mcp__business__lookup"):
            args = {"file_path": str(Path(client.options.cwd) / "fixture.txt"), "content": "隔离"}
            assert await guard_tool(client, name, args), name
        child = await call(client, "delegate_to_agent", {
            "agent": env.c["id"], "title": "C任务", "prompt": "完整背景", "source_agent": "伪造"})
        assert "task_id" in child, child
        env.children.append(child["task_id"])
    env.behavior["B任务"] = forward
    row = dispatch(env, tokens=1, seconds=-1)  # 兼容参数不改变普通执行规则。
    await settle()
    assert tasks.get(row["id"])["status"] == "done"
    child = d.get(env.children[0])
    assert child["source_agent"] == env.b["id"] and child["parent_id"] == row["id"]
    assert child["depth"] == 2
    assert len(receipts(env.source)) == len(receipts(tasks.session_key(row["id"]))) == 1
    assert env.children[0] in d.get(row["id"])["receipt"]
    assert env.summary_calls == 0  # 不另起付费摘要模型。
    assert d.query(env.source, env.children[0])["status"] == "done"


@pytest.mark.anyio
async def test_automatic_depth_cycle_ancestor_and_five_limit(env, monkeypatch):
    async def nested(client):
        source = tasks.session_key(client.row["id"])
        for ref in (env.a["id"], env.b["id"]):
            with pytest.raises(ValueError, match="祖先"):
                d.delegate(source, [], ref, "循环", "背景")
        child = d.delegate(source, [], env.c["id"], "C任务", "背景")
        env.children.append(child["id"])
    async def third(client):
        with pytest.raises(ValueError, match="两层"):
            d.delegate(tasks.session_key(client.row["id"]), [], agents.GENERAL_ID, "三层", "背景")
    env.behavior.update({"B任务": nested, "C任务": third})
    row = dispatch(env)
    await settle()
    assert tasks.get(row["id"])["status"] == "done"
    assert tasks.get(env.children[0])["status"] == "done"
    monkeypatch.setattr(runner, "_maybe_start_next", lambda: None)
    for _ in range(3):
        dispatch(env)
    with pytest.raises(ValueError, match="五个"):
        dispatch(env)
    assert tasks._conn().execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 5


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
async def test_terminal_append_keeps_history_and_delivers_new_version(env, status):
    row = dispatch(env)
    await settle()
    key = tasks.session_key(row["id"])
    sid = session_store.get_sdk_session_id(key)
    old_turn = d._version(row["id"])
    c = tasks._conn()
    c.execute("UPDATE tasks SET status=?, progress_note='旧预算耗尽' WHERE id=?", (status, row["id"]))
    c.execute("UPDATE agent_delegations SET tokens=1, used=999999, deadline=0, policy=NULL WHERE task_id=?", (row["id"],))
    c.commit()
    env.behavior["B任务"] = lambda client: set_reply(client, "续聊的新结果")
    result = await runner.append(row["id"], "继续")
    assert result["ok"]
    await settle()
    assert tasks.get(row["id"])["status"] == "done"
    history = session_store.load_history(key)
    assert [h["user"] for h in history] == ["完整任务背景", "继续"]
    assert history[0]["assistant"] == "执行结果"
    assert session_store.get_sdk_session_id(key) == sid
    assert d._version(row["id"]) > old_turn
    assert len(receipts(env.source)) == 2
    assert "续聊的新结果" in receipts(env.source)[-1][4]
    assert "续聊的新结果" not in receipts(env.source)[0][4]
    await runner.heal_after_restart()
    await d.drain()
    assert len(receipts(env.source)) == 2


async def set_reply(client, text):
    client.text = text


@pytest.mark.anyio
async def test_failed_receipt_delivery_restart_and_busy_source(env, monkeypatch):
    row = dispatch(env)
    clarify._active_sessions.add(env.source)
    await settle()
    assert not receipts(env.source)
    clarify._active_sessions.clear()
    original = session_store.append_external
    def interrupted(*args):
        original(*args)
        raise RuntimeError("落库后崩溃")
    monkeypatch.setattr(session_store, "append_external", interrupted)
    await d.drain()
    assert len(receipts(env.source)) == 1 and d.get(row["id"])["delivered_turn"] == -1
    monkeypatch.setattr(session_store, "append_external", original)
    await runner.heal_after_restart()
    await d.drain()
    assert len(receipts(env.source)) == 1
    assert d.query(env.source, row["id"])["receipt_delivered"]


@pytest.mark.anyio
async def test_receipts_never_answer_clarify_or_approval(env, monkeypatch):
    from vococo.gateway.run import GatewayRunner
    monkeypatch.setattr(GatewayRunner, "_dispatch", AsyncMock(side_effect=AssertionError("不能注入真人输入")))
    monkeypatch.setattr(clarify, "resolve_text_for_session", lambda *_: pytest.fail("不能回答提问"))
    pending = clarify._Pending("question", env.source, ["允许", "拒绝"], asyncio.Event())
    clarify._pending["question"] = pending
    clarify._by_session[env.source] = ["question"]
    row = dispatch(env)
    await settle()
    assert not receipts(env.source) and pending.response is None
    clarify._pending.clear()
    clarify._by_session.clear()
    await d.drain()
    assert len(receipts(env.source)) == 1 and pending.response is None
    assert d.get(row["id"])["delivered"]


@pytest.mark.anyio
async def test_source_denials_survive_target_full_and_nested(env):
    from vococo.core import permissions
    agents.update(env.b["id"], permission="full")
    agents.update(env.c["id"], permission="full")
    async def parent(client):
        assert "Read" in client.options.disallowed_tools
        assert not await guard_tool(client, "Read")
        assert not permissions.is_full(tasks.session_key(client.row["id"]))
        result = await call(client, "delegate_to_agent", {"agent": env.c["id"], "title": "C任务", "prompt": "背景"})
        env.children.append(result["task_id"])
    async def child(client):
        assert "Read" in client.options.disallowed_tools
        assert not permissions.is_full(tasks.session_key(client.row["id"]))
    env.behavior.update({"B任务": parent, "C任务": child})
    row = d.delegate(env.source, ["Read"], env.b["id"], "B任务", "背景")
    await settle()
    assert tasks.get(row["id"])["status"] == "done"
    assert tasks.get(env.children[0])["status"] == "done"
    with pytest.raises(ValueError, match="禁止"):
        d.delegate(env.source, ["mcp__vococo__delegate*"], env.b["id"], "拒绝", "背景")
    with pytest.raises(ValueError, match="来源"):
        d.delegate("unknown", [], env.b["id"], "拒绝", "背景")


@pytest.mark.anyio
async def test_tool_schema_old_arguments_optional(env):
    tools = d.build_tools(env.source, [])
    schema = next(t.input_schema for t in tools if t.name == "delegate_to_agent")
    assert schema["required"] == ["agent", "title", "prompt"]
    assert "token_budget" in schema["properties"]
    row = dispatch(env, tokens=1, seconds=1)
    env.usage = 150000
    await settle()
    assert tasks.get(row["id"])["status"] == "done"
    assert tasks.get(row["id"])["kind"] == "chat"


@pytest.mark.anyio
async def test_gateway_continue_legacy_failed_normal_permissions_and_new_receipt(env, monkeypatch):
    monkeypatch.setattr(config, "TASK_MAX_CONCURRENCY", 1)
    from vococo.core import permissions
    from vococo.gateway.run import GatewayRunner
    from vococo.gateway.adapters.base import Incoming
    agents.update(env.b["id"], permission="full", skills=["target-skill"])
    row = d.delegate(env.source, ["Read"], env.b["id"], "B任务", "保留历史")
    await settle()
    key = tasks.session_key(row["id"])
    tasks._conn().execute("UPDATE tasks SET status='failed' WHERE id=?", (row["id"],))
    tasks._conn().execute("UPDATE agent_delegations SET depth=2, tokens=1, used=999999, deadline=0, policy=NULL WHERE task_id=?", (row["id"],))
    tasks._conn().commit()
    checked = []
    async def continued(client):
        assert "Read" not in client.options.disallowed_tools
        assert "target-skill" in client.options.skills
        assert permissions.is_full(key)
        assert not session_store.active_source_access(key)
        for name in ("Edit", "Write", "Skill"):
            assert await guard_tool(client, name, {"file_path": str(Path(client.options.cwd) / "test.txt")})
        # 真人新轮次可以重新发起自动链，不继承旧任务的 depth=2。
        child = await call(client, "delegate_to_agent", {"agent": env.a["id"], "title": "真人新派单", "prompt": "背景"})
        assert "task_id" in child, child
        assert d.get(child["task_id"])["depth"] == 1
        checked.append(True)
        client.text = "正常继续完成"
    env.behavior["B任务"] = continued
    class Adapter:
        platform = "web"
        async def send(self, *args):
            pytest.fail(str(args))
        def make_sink(self, _):
            return Sink()
    class Sink:
        def __getattr__(self, _):
            return AsyncMock()
    adapter = Adapter()
    gateway = GatewayRunner([adapter])
    incoming = Incoming(platform="web", chat_id=key, text="继续")
    assert incoming.session_key == key
    await gateway._dispatch(adapter, incoming)
    await settle()
    assert checked == [True]
    assert tasks.get(row["id"])["status"] == "done"
    assert [h["user"] for h in session_store.load_history(key) if h["user"]] == ["保留历史", "继续"]
    assert len(receipts(env.source)) == 2
    assert "正常继续完成" in receipts(env.source)[-1][4]


@pytest.mark.anyio
async def test_alternative_dispatch_cannot_reset_chain_or_bypass_source(env):
    agents.update(env.c["id"], permission="full")
    async def parent(client):
        # SDK 自动执行没有前台 current，但服务器仍能绑定来源。
        assert clarify.current() is None
        with pytest.raises(ValueError, match="祖先"):
            runner.dispatch("同 Agent 派生", "不能借新会话重置", origin="chat")
        child = runner.dispatch("普通派发到 C", "完整背景", cwd=env.c["workdir"], origin="chat")
        env.children.append(child["id"])
        assert d.get(child["id"])["depth"] == 2
        assert json.loads(child["source_access"])["mode"] == "standard"
        assert "Read" in json.loads(child["source_access"])["denied"]
    async def child(client):
        from vococo.core import permissions
        assert not permissions.is_full(tasks.session_key(client.row["id"]))
        assert "Read" in client.options.disallowed_tools
        with pytest.raises(ValueError, match="两层"):
            d.delegate(tasks.session_key(client.row["id"]), [], agents.GENERAL_ID, "第三层", "背景")
    env.behavior.update({"B任务": parent, "普通派发到 C": child})
    row = d.delegate(env.source, ["Read"], env.b["id"], "B任务", "背景")
    await settle()
    assert tasks.get(row["id"])["status"] == "done"
    assert len(env.children) == 1
    assert tasks.get(env.children[0])["status"] == "done"


@pytest.mark.anyio
@pytest.mark.parametrize("before_start", [True, False])
async def test_cancellation_uses_normal_task_finalization(env, before_start):
    started = asyncio.Event()
    async def waiting(client):
        started.set()
        await asyncio.Event().wait()
    env.behavior["B任务"] = waiting
    row = dispatch(env)
    if not before_start:
        await asyncio.wait_for(started.wait(), 2)
    assert runner.cancel(row["id"])
    await settle()
    assert tasks.get(row["id"])["status"] == "cancelled"
    assert len(receipts(env.source)) == 1
    assert "cancelled" in receipts(env.source)[0][4]


@pytest.mark.anyio
async def test_legacy_metadata_migrates_without_old_policy_execution(env):
    row = d.delegate(env.source, ["Read"], env.b["id"], "B任务", "旧任务")
    await settle()
    c = tasks._conn()
    c.execute("UPDATE tasks SET source_access=NULL WHERE id=?", (row["id"],))
    c.execute("UPDATE agent_delegations SET policy=NULL, tokens=1, used=999999, deadline=0 WHERE task_id=?", (row["id"],))
    c.commit()
    c.close()
    tasks._DB = None
    access = tasks.automatic_access(tasks.get(row["id"]))
    assert "Read" in access["denied"] and access["mode"] == "standard"
    assert access["chain_session"] == tasks.session_key(row["id"])
    env.behavior["B任务"] = lambda client: set_reply(client, "已恢复")
    assert (await runner.append(row["id"], "继续"))["ok"]
    await settle()
    assert tasks.get(row["id"])["status"] == "done"
    assert len(receipts(env.source)) == 2


class _TurnSink:
    def __getattr__(self, _name):
        return AsyncMock()


@pytest.mark.anyio
async def test_old_background_done_callback_cannot_fail_gateway_new_turn(env, monkeypatch):
    from vococo.gateway import core

    notifying, release_old, generating, release_new = (asyncio.Event() for _ in range(4))
    original = task_events.emit_terminal
    async def delayed_notify(task_id, **kwargs):
        notifying.set()
        await release_old.wait()
        await original(task_id, **kwargs)
    monkeypatch.setattr(task_events, "emit_terminal", delayed_notify)
    row = dispatch(env)
    old_owner = runner._running[row["id"]]
    await asyncio.wait_for(notifying.wait(), 2)
    async def waiting(client):
        generating.set()
        await release_new.wait()
    env.behavior["B任务"] = waiting
    foreground = asyncio.create_task(core.converse(
        tasks.session_key(row["id"]), "真人继续", None, _TurnSink(), cwd_override=row["cwd"]))
    try:
        await asyncio.wait_for(generating.wait(), 2)
        release_old.set()
        await old_owner
        await asyncio.sleep(0)  # 强制旧 done callback 在真人新轮次仍生成时触发。
        assert tasks.get(row["id"])["status"] == "running"
        assert not foreground.done()
    finally:
        release_old.set()
        release_new.set()
        await asyncio.gather(foreground, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["cancel", "append"])
async def test_gateway_turn_uses_normal_task_cancel_and_append_ownership(env, operation):
    from vococo.gateway import core

    row = dispatch(env)
    await settle()
    generating, release = asyncio.Event(), asyncio.Event()
    async def waiting(client):
        if "真人继续" in str(client.prompt):
            generating.set()
            await release.wait()
    env.behavior["B任务"] = waiting
    foreground = asyncio.create_task(core.converse(
        tasks.session_key(row["id"]), "真人继续", None, _TurnSink(), cwd_override=row["cwd"]))
    try:
        await asyncio.wait_for(generating.wait(), 2)
        assert runner._running[row["id"]] is not foreground
        if operation == "cancel":
            assert runner.cancel(row["id"])
            await asyncio.wait_for(foreground, 2)
            assert tasks.get(row["id"])["status"] == "cancelled"
        else:
            result = await runner.append(row["id"], "后台追加")
            assert result["ok"], result
            await asyncio.wait_for(foreground, 2)
            await settle()
            assert tasks.get(row["id"])["status"] == "done"
            assert session_store.load_history(tasks.session_key(row["id"]))[-1]["user"] == "后台追加"
    finally:
        release.set()
        await asyncio.gather(foreground, return_exceptions=True)


@pytest.mark.anyio
async def test_each_cancel_before_first_schedule_has_distinct_persisted_receipt_version(env):
    row = dispatch(env)
    assert runner.cancel(row["id"])
    await settle()
    first_version = d.get(row["id"])["delivered_turn"]
    assert len(receipts(env.source)) == 1
    assert (await runner.append(row["id"], "第二次尝试"))["ok"]
    assert runner.cancel(row["id"])
    await settle()
    assert tasks.get(row["id"])["status"] == "cancelled"
    assert len(receipts(env.source)) == 2
    version = tasks.get(row["id"])["run_version"]
    assert d.get(row["id"])["delivered_turn"] > first_version
    tasks._DB.close()
    tasks._DB = None
    assert tasks.get(row["id"])["run_version"] == version
    await runner.heal_after_restart()
    await d.drain()
    assert len(receipts(env.source)) == 2


@pytest.mark.anyio
async def test_background_finish_turn_error_keeps_owner_for_terminal_fallback(env, monkeypatch):
    def broken_finish(*args, **kwargs):
        raise RuntimeError("模拟会话数据库锁异常")
    monkeypatch.setattr(session_store, "finish_turn", broken_finish)
    row = dispatch(env)
    await settle()
    assert tasks.get(row["id"])["status"] == "failed"
    assert row["id"] not in runner._running
    assert len(receipts(env.source)) == 1
