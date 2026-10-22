"""真实执行器→stream_turn→SDK/MCP 路径；仅模型传输为本地夹具，所有存储在 tmp_path。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, StreamEvent
from mcp.types import CallToolRequest, CallToolRequestParams

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
            tid = Path(self.options.cwd or "fixture-source").name
            row = tasks.get(tid)
            if row:
                self.row = row
                callback = state.behavior.get(row["title"])
                if callback:
                    await callback(self)
            else:
                self.row = None
                state.summary_calls += 1
                self.text = "自动汇总"
                if self.options.tools is None:
                    self.context.append(str(prompt))
                    state.sdk_transcripts["fixture-session"] = list(self.context)
                if self.options.tools is None and getattr(state, "source_behavior", None):
                    callback, state.source_behavior = state.source_behavior, None
                    await callback(self)

        async def get_context_usage(self):
            return {"totalTokens": 100, "rawMaxTokens": 200_000}

        async def receive_messages(self):
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
    state.source_options = ClaudeAgentOptions(mcp_servers={"vococo": {}}, skills=[],
                                              permission_mode=config.PERMISSION_MODE)
    def delegate(session, denied, *args, **kw):
        parent = next((c for c in state.clients if getattr(c, "row", None)
                       and tasks.session_key(c.row["id"]) == session), None)
        policy = d.snapshot(parent.options if parent else state.source_options)
        return d.delegate(session, denied, *args, policy=policy, **kw)
    state.delegate = delegate
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
    for matcher in client.options.hooks["PreToolUse"]:
        for hook in matcher.hooks:
            result = await hook({"tool_name": full_name, "tool_input": args}, "call", {})
            if result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
                return {"error": "工具拒绝"}
    server = client.options.mcp_servers["vococo"]["instance"]
    result = await server.request_handlers[CallToolRequest](CallToolRequest(
        method="tools/call", params=CallToolRequestParams(name=name, arguments=args)))
    return json.loads(result.root.content[0].text)


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
async def test_real_a_b_c_execution_and_isolated_summary(env):
    session_store.append(f"web:p{env.b['project_hash']}:main", "目标主会话秘密", "不得读取")

    async def forward(client):
        result = await call(client, "delegate_to_agent", {
            "agent": env.c["id"], "title": "C任务", "prompt": "给 C 的完整背景", "token_budget": 5000,
            "timeout_seconds": 30, "source_agent": "伪造身份"})
        assert "task_id" in result, result
        env.children.append(result["task_id"])
    env.behavior["B任务"] = forward
    root = dispatch(env, tokens=60_000, seconds=300)
    await settle()
    child = d.get(env.children[0])
    assert child["source_agent"] == env.b["id"] and child["target_agent"] == env.c["id"]
    assert child["parent_id"] == root["id"] and child["depth"] == 2
    assert child["chain_id"] == d.get(root["id"])["chain_id"]
    assert tasks.get(root["id"])["status"] == "done"
    assert len(receipts(env.source)) == len(receipts(tasks.session_key(root["id"]))) == 1
    assert any(e["type"] == "agent_run" and e["conv"] == env.source.removeprefix("web:") for e in env.events)
    assert env.summary_calls == 2 and d.get(root["id"])["summary_used"] == 110
    assert d.query(env.source, env.children[0])["status"] == "done"
    summaries = [c for c in env.clients if c.row is None]
    assert "Agent 回执" in str(summaries[-1].prompt) and env.children[0] in str(summaries[-1].prompt)
    for client in env.clients:
        options = client.options
        assert options.tools == (["WebSearch", "WebFetch"] if client.row is None
                                 else sorted(d._NATIVE - {"Skill"}))
        assert options.setting_sources == [] and options.strict_mcp_config
        assert not options.skills and not options.plugins and options.agents is None
        assert options.resume is None and "目标主会话秘密" not in str(client.prompt)
        if client.row is None:
            assert options.mcp_servers == {}
            for matcher in options.hooks["PreToolUse"]:
                result = await matcher.hooks[0]({"tool_name": "mcp__vococo__delegate_to_agent"}, "x", {})
                assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.anyio
async def test_cycle_depth_and_reserved_budget_rejections(env):
    async def forward(client):
        source = tasks.session_key(client.row["id"])
        for ref in [env.a["id"], env.b["id"]]:
            with pytest.raises(ValueError, match="祖先"):
                env.delegate(source, [], ref, "循环", "背景", 1000, 5)
        with pytest.raises(ValueError, match="token"):
            env.delegate(source, [], env.c["id"], "过量", "背景", 60_000, 5)
        with pytest.raises(ValueError, match="时长"):
            env.delegate(source, [], env.c["id"], "过量", "背景", 1000, 300)
        child = env.delegate(source, [], env.c["id"], "C任务", "背景", 1000, 5)
        env.children.append(child["id"])

    async def too_deep(client):
        with pytest.raises(ValueError, match="两层"):
            env.delegate(tasks.session_key(client.row["id"]), [], agents.GENERAL_ID, "三层", "背景", 1000, 5)
    env.behavior.update({"B任务": forward, "C任务": too_deep})
    root = dispatch(env, tokens=60_000, seconds=300)
    await settle()
    assert tasks.get(root["id"])["status"] == "done"
    assert d.get(root["id"])["reserved_tokens"] == 9000


@pytest.mark.anyio
async def test_five_tasks_and_root_budgets_cannot_be_reset(env, monkeypatch):
    monkeypatch.setattr(config, "TASK_MAX_CONCURRENCY", 0)
    for _ in range(5):
        dispatch(env, tokens=1000, seconds=5)
    with pytest.raises(ValueError, match="五"):
        dispatch(env, tokens=1000, seconds=5)
    session_store.finish_turn(env.turn, "结束", session_key=env.source)
    with pytest.raises(ValueError, match="用户轮次"):
        dispatch(env)
    session_store.start_turn(env.source, "新的用户请求")
    dispatch(env, tokens=100_000, seconds=800)
    with pytest.raises(ValueError, match="token"):
        dispatch(env, tokens=20_000, seconds=5)
    with pytest.raises(ValueError, match="时长"):
        dispatch(env, tokens=1000, seconds=60)


@pytest.mark.anyio
async def test_permissions_full_target_cannot_widen_source(env):
    agents.update(env.b["id"], permission="full", disallowed_tools=["WebFetch"])
    root = env.delegate(env.source, ["WebSearch", "mcp__vococo__get_agent_delegation"],
                      env.b["id"], "B任务", "背景")
    await settle()
    client = next(c for c in env.clients if c.row)
    hook = client.options.hooks["PreToolUse"][0].hooks[0]
    for name in ["Bash", "Write", "Skill", "Agent", "Task", "WebFetch", "WebSearch",
                 "mcp__vococo__save_memory", "mcp__vococo__ask_user", "mcp__vococo__dispatch_session",
                 "mcp__vococo__get_agent_delegation", "mcp__other__delegate_to_agent"]:
        result = await hook({"tool_name": name, "tool_input": {}}, "x", {})
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny", name
    assert tasks.get(root["id"])["status"] == "done"
    with pytest.raises(ValueError, match="禁止"):
        env.delegate(env.source, ["mcp__vococo__delegate*"], env.b["id"], "拒绝", "背景")


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
async def test_terminal_receipts_idempotent_and_restart_redelivery(env, monkeypatch, status):
    if status == "failed":
        env.errors.add("B任务")
    if status == "cancelled":
        monkeypatch.setattr(config, "TASK_MAX_CONCURRENCY", 0)
    row = dispatch(env)
    if status == "cancelled":
        assert runner.cancel(row["id"])
    await settle()
    assert tasks.get(row["id"])["status"] == status
    assert len(receipts(env.source)) == 1
    await asyncio.gather(task_events.emit_terminal(row["id"]), task_events.emit_terminal(row["id"]))
    await asyncio.gather(d.drain(), d.drain())
    assert len(receipts(env.source)) == 1
    count = env.summary_calls
    # 模拟消息已提交、确认标记尚未写入时崩溃；重新打开真实 sqlite 补投。
    tasks._conn().execute("UPDATE agent_delegations SET delivered=0 WHERE task_id=?", (row["id"],))
    tasks._conn().commit()
    tasks._DB.close()
    tasks._DB = None
    await runner.heal_after_restart()
    await d.drain()
    assert d.get(row["id"])["delivered"] and len(receipts(env.source)) == 1
    assert env.summary_calls == count


@pytest.mark.anyio
async def test_busy_source_and_pending_question_never_interrupted_or_answered(env, monkeypatch):
    clarify.mark_active(env.source)
    pending = clarify.register(env.source, [])
    monkeypatch.setattr(runner, "append", AsyncMock(side_effect=AssertionError("不能中断来源")))
    row = dispatch(env)
    await settle()
    assert tasks.get(row["id"])["status"] == "done"
    assert not receipts(env.source) and not pending.response
    clarify.mark_inactive(env.source)
    await d.drain()
    assert not receipts(env.source) and clarify.has_pending(env.source)
    clarify.abandon(pending.clarify_id)
    await d.drain()
    assert len(receipts(env.source)) == 1
    assert session_store._conn().execute("SELECT assistant_text FROM turns WHERE id=?", (env.turn,)).fetchone()[0] == ""


@pytest.mark.anyio
async def test_running_cancel_and_cancel_before_first_schedule(env):
    started = asyncio.Event()
    async def hang(_client):
        started.set()
        await asyncio.Event().wait()
    env.behavior["B任务"] = hang
    row = dispatch(env)
    await asyncio.wait_for(started.wait(), 2)
    await runner.cancel_and_wait(row["id"])
    await settle()
    assert tasks.get(row["id"])["status"] == "cancelled"
    early = dispatch(env, title="尚未调度")
    assert runner.cancel(early["id"])
    await settle()
    assert tasks.get(early["id"])["status"] == "cancelled"
    assert len(receipts(env.source)) == 2


@pytest.mark.anyio
async def test_unknown_duplicate_identity_and_query_access(env, monkeypatch):
    agents.update(env.c["id"], name="B")
    with pytest.raises(ValueError, match="重名"):
        d.resolve("B")
    with pytest.raises(ValueError):
        d.resolve("../outside")
    monkeypatch.setattr(config, "TASK_MAX_CONCURRENCY", 0)
    row = dispatch(env)
    assert d.query(env.source, row["id"])["status"] == "queued"
    with pytest.raises(ValueError, match="不属于"):
        d.query("web:other", row["id"])
    result = await runner.append(row["id"], "重新获得预算")
    assert not result["ok"]
    ordinary = tasks.create("旧任务", "背景", cwd=str(config.DATA_DIR))
    with pytest.raises(ValueError, match="普通后台"):
        env.delegate(tasks.session_key(ordinary["id"]), [], env.b["id"], "绕过", "背景")


@pytest.mark.anyio
async def test_token_limit_is_enforced_on_actual_result_usage(env):
    env.usage = 2000
    row = dispatch(env, tokens=1000, seconds=5)
    await settle()
    assert tasks.get(row["id"])["status"] == "failed"
    assert d.get(row["id"])["used"] == 2010
    assert "预算" in tasks.get(row["id"])["result_summary"]


@pytest.mark.anyio
async def test_restart_orphan_and_failed_delivery_retry(env, monkeypatch):
    monkeypatch.setattr(config, "TASK_MAX_CONCURRENCY", 0)
    row = dispatch(env)
    original = session_store.append_external
    monkeypatch.setattr(session_store, "append_external", lambda *a: (_ for _ in ()).throw(OSError("暂时不可写")))
    await runner.heal_after_restart()
    await d.drain()
    assert tasks.get(row["id"])["status"] == "failed" and not d.get(row["id"])["delivered"]
    monkeypatch.setattr(session_store, "append_external", original)
    await d.drain()
    assert len(receipts(env.source)) == 1


@pytest.mark.anyio
async def test_web_followup_and_direct_converse_cannot_escape_isolation(env, monkeypatch):
    from vococo.gateway import core
    from vococo.gateway.adapters.base import Incoming
    from vococo.gateway.run import GatewayRunner

    monkeypatch.setattr(config, "TASK_MAX_CONCURRENCY", 0)
    row = dispatch(env)
    key = tasks.session_key(row["id"])
    sink = SimpleNamespace()
    with pytest.raises(ValueError, match="不支持续聊"):
        await core.converse(key, "写入目标项目", None, sink)
    adapter = SimpleNamespace(platform="web", send=AsyncMock(),
                              make_sink=lambda *_: (_ for _ in ()).throw(AssertionError("不能创建模型轮次")))
    inc = Incoming(platform="web", chat_id=key, text="写入目标项目")
    assert inc.session_key == key
    await GatewayRunner([adapter])._dispatch(adapter, inc)
    assert "不能续聊" in adapter.send.await_args.args[1]
    assert not env.clients and tasks.get(row["id"])["status"] == "queued"


@pytest.mark.anyio
async def test_delayed_child_is_included_before_parent_receipt(env):
    started, release = asyncio.Event(), asyncio.Event()
    async def forward(client):
        response = await call(client, "delegate_to_agent", {
            "agent": env.c["id"], "title": "C任务", "prompt": "背景", "token_budget": 1000, "timeout_seconds": 5})
        env.children.append(response["task_id"])
    async def child(client):
        started.set()
        await release.wait()
        client.text = "下级独有结果"
    env.behavior.update({"B任务": forward, "C任务": child})
    root = dispatch(env, tokens=60_000, seconds=300)
    await asyncio.wait_for(started.wait(), 2)
    for _ in range(100):
        if tasks.get(root["id"])["status"] == "done":
            break
        await asyncio.sleep(.01)
    await d.drain()
    assert not receipts(env.source)
    assert not d.get(root["id"])["summary_started"]
    release.set()
    await settle()
    assert len(receipts(env.source)) == 1
    assert d.query(env.source, env.children[0])["result_full"] == "下级独有结果", d.query(env.source, env.children[0])
    assert any("下级独有结果" in str(c.prompt) for c in env.clients if c.row is None)


@pytest.mark.anyio
async def test_dynamic_parent_deadline_interrupts_idle_model(env):
    started = asyncio.Event()
    async def hang(client):
        tid = client.row["id"]
        started.set()
        c = tasks._conn()
        c.execute("UPDATE agent_delegations SET deadline=? WHERE task_id=?", (d.time.time() + .05, tid))
        c.commit()
        await asyncio.Event().wait()
    env.behavior["B任务"] = hang
    row = dispatch(env)
    await asyncio.wait_for(started.wait(), 2)
    await settle()
    assert tasks.get(row["id"])["status"] == "failed"
    assert "时长预算" in tasks.get(row["id"])["result_summary"]
    assert env.clients[0].interrupt.await_count == 1


@pytest.mark.anyio
async def test_expired_queued_task_never_queries_model(env, monkeypatch):
    monkeypatch.setattr(config, "TASK_MAX_CONCURRENCY", 0)
    row = dispatch(env)
    c = tasks._conn()
    c.execute("UPDATE agent_delegations SET deadline=0 WHERE task_id=?", (row["id"],))
    c.commit()
    monkeypatch.setattr(config, "TASK_MAX_CONCURRENCY", 1)
    runner._maybe_start_next()
    await settle()
    assert tasks.get(row["id"])["status"] == "failed" and not env.clients
    assert len(receipts(env.source)) == 1


@pytest.mark.anyio
async def test_transaction_rolls_back_task_and_reserved_budget(env, monkeypatch):
    monkeypatch.setattr(config, "TASK_MAX_CONCURRENCY", 0)
    create = tasks.create
    def fail_after_insert(*args, **kwargs):
        create(*args, **kwargs)
        raise OSError("模拟落库中断")
    monkeypatch.setattr(tasks, "create", fail_after_insert)
    with pytest.raises(OSError):
        dispatch(env)
    assert tasks._conn().execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert tasks._conn().execute("SELECT COUNT(*) FROM agent_delegations").fetchone()[0] == 0


@pytest.mark.anyio
async def test_summary_crash_marker_uses_fallback_without_second_paid_run(env):
    clarify.mark_active(env.source)
    row = dispatch(env)
    await settle()
    c = tasks._conn()
    c.execute("UPDATE agent_delegations SET summary_started=1 WHERE task_id=?", (row["id"],))
    c.commit()
    clarify.mark_inactive(env.source)
    await d.drain()
    assert len(receipts(env.source)) == 1 and env.summary_calls == 0
    assert d.get(row["id"])["delivered"]


async def guard_tool(client, name, args=None):
    result = {}
    for matcher in client.options.hooks["PreToolUse"]:
        for callback in matcher.hooks:
            result = await callback({"tool_name": name, "tool_input": args or {}}, "test", {})
            if result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
                return False
    return True


@pytest.mark.anyio
async def test_actual_source_snapshot_target_files_business_mcp_and_standard_cap(env, monkeypatch):
    import subprocess
    from claude_agent_sdk import create_sdk_mcp_server, tool
    from vococo.core import permissions

    # 真实临时 Git 项目，不操作生产仓库；执行器必须创建并绑定独立 worktree。
    root = Path(env.b["workdir"])
    (root / "客户资料.txt").write_text("目标客户资料")
    for args in [("init",), ("add", "."),
                 ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")]:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    agents.write_doc(env.b["id"], "AGENT.md", "B 的业务职责")
    agents.update(env.a["id"], mcp=["business", "source_only"], skills=["shared", "source_skill"],
                  disallowed_tools=["WebSearch"], memory_sections=["用户偏好"])
    agents.update(env.b["id"], mcp=["business", "target_only"], skills=["shared", "target_skill"],
                  disallowed_tools=["WebFetch"], permission="full", memory_sections=["用户偏好", "技术决策"])
    config.AI_BRAIN_DIR.mkdir(parents=True, exist_ok=True)
    (config.AI_BRAIN_DIR / "MEMORY.md").write_text(
        "## 用户偏好\n共同可见经验\n## 技术决策\n不可扩大的全局分区\n")
    from vococo.memory import deposit
    own = deposit.agent_memory_dir(env.b)
    own.mkdir(parents=True, exist_ok=True)
    (own / "INDEX.md").write_text("目标独有经验")
    calls = []
    @tool("lookup_customer", "读取本地业务夹具", {})
    async def lookup(_args):
        calls.append("business")
        return {"content": [{"type": "text", "text": "业务客户结果"}]}
    server = create_sdk_mcp_server("business", tools=[lookup])
    monkeypatch.setattr(settings_store, "effective_external_mcp",
                        lambda names: {name: server for name in names})
    monkeypatch.setattr(settings_store, "vococo_enabled", lambda: True)
    checks = []
    async def execute(client):
        assert Path(client.options.cwd) != root
        assert session_store.get_worktree(tasks.session_key(client.row["id"])) == client.options.cwd
        path = Path(client.options.cwd) / "客户资料.txt"
        assert await guard_tool(client, "Read", {"file_path": str(path)})
        assert path.read_text() == "目标客户资料"
        assert client.options.skills == ["shared"]
        assert set(client.options.mcp_servers) == {"business", "vococo"}
        assert "B 的业务职责" in str(client.options.system_prompt)
        assert "目标独有经验" in str(client.options.system_prompt)
        assert "共同可见经验" in str(client.options.system_prompt)
        assert "不可扩大的全局分区" not in str(client.options.system_prompt)
        assert not permissions.is_full(tasks.session_key(client.row["id"]))
        assert await guard_tool(client, "mcp__business__lookup_customer")
        response = await server["instance"].request_handlers[CallToolRequest](CallToolRequest(
            method="tools/call", params=CallToolRequestParams(name="lookup_customer", arguments={})))
        assert response.root.content[0].text == "业务客户结果"
        for name in ["WebSearch", "WebFetch", "Bash", "Write", "Agent", "Task", "Workflow",
                     "mcp__target_only__lookup", "mcp__source_only__lookup", "mcp__business__dispatch_session",
                     "mcp__vococo__run_subagent", "mcp__vococo__dispatch_session",
                     "mcp__vococo__voice_dispatch_task", "mcp__vococo__add_cron_job",
                     "mcp__vococo__create_workbench_task", "mcp__vococo__set_external_mcp"]:
            assert not await guard_tool(client, name), name
        checks.append(True)
    env.behavior["真实权限任务"] = execute
    async def source(client):
        entry = client.options.hooks["PreToolUse"][-1].hooks[0]
        blocked = await entry({"tool_name": "mcp__vococo__delegate_to_agent", "agent_id": "native-child"}, "x", {})
        assert blocked["hookSpecificOutput"]["permissionDecision"] == "deny"
        response = await call(client, "delegate_to_agent", {
            "agent": env.b["id"], "title": "真实权限任务", "prompt": "完整背景",
            "token_budget": 24000, "timeout_seconds": 180})
        env.children.append(response["task_id"])
    env.source_behavior = source
    async for _ in agent.stream_turn([], "给 B 派任务", session_key=env.source, cwd=env.a["workdir"]):
        pass
    await settle()
    assert checks == [True] and calls == ["business"], tasks.get(env.children[0])["result_summary"]
    assert tasks.get(env.children[0])["status"] == "done"
    policy = json.loads(d.get(env.children[0])["policy"])
    assert "WebSearch" in policy["denied"] and "source_only" in policy["mcp"]
    assert policy["source_permission"] == "standard"


@pytest.mark.anyio
async def test_delegation_danger_gate_still_denies_without_background_approval(env, monkeypatch):
    from vococo.tools import danger
    from vococo.core import permissions

    agents.update(env.b["id"], permission="full")
    env.source_options.mcp_servers["business"] = {}
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
async def test_receipt_next_real_turn_incrementally_syncs_warm_and_resume_once(env, warm):
    async for event in agent.stream_turn([], "初始轮次", session_key=env.source):
        if isinstance(event, agent.Done):
            session_store.set_sdk_session_id(env.source, event.reply.sdk_session_id)
    first = env.clients[-1]
    row = dispatch(env)
    await settle()
    receipt = d.get(row["id"])["receipt"]
    session_store.append_external(env.source, receipt, f"agent-receipt:{row['id']}")
    if not warm:
        agent.client_pool._pool.clear()  # 模拟重启后仅剩 SDK transcript。
    async def follow(text):
        async for event in agent.stream_turn([], text, session_key=env.source,
                                             resume=session_store.get_sdk_session_id(env.source)):
            if isinstance(event, agent.Done):
                session_store.set_sdk_session_id(env.source, event.reply.sdk_session_id)
    clients_before = len(env.clients)
    await follow("刚才的结果是什么")
    latest = agent.client_pool._pool[env.source].client
    assert (latest is first) == warm
    assert len(env.clients) == clients_before + (not warm)
    if not warm:
        assert latest.options.resume == "fixture-session"
    encoded_receipt = json.dumps(receipt, ensure_ascii=False)
    assert str(latest.prompt).count(encoded_receipt) == 1
    assert "不可信数据" in str(latest.prompt) and "```json" in str(latest.prompt)
    assert len(receipts(env.source)) == 1
    assert "初始轮次" in str(latest.context)
    await follow("继续")
    assert agent.client_pool._pool[env.source].client is latest
    assert encoded_receipt not in str(latest.prompt)
    assert "外部回执增量" not in str(latest.prompt)
    assert "继续" in str(latest.prompt)


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


@pytest.mark.anyio
async def test_local_denials_and_unknown_hooks_fail_closed(env, monkeypatch):
    import os
    settings = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"permissions": {"deny": ["Read(/private/*)"],
                                                   "ask": ["mcp__vococo__get_agent_delegation"]}}))
    async def execute(client):
        assert not await guard_tool(client, "Read", {"file_path": "/tmp/test"})
        assert not await guard_tool(client, "mcp__vococo__get_agent_delegation")
    env.behavior["B任务"] = execute
    row = dispatch(env)
    await settle()
    assert tasks.get(row["id"])["status"] == "done"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{"command": "unknown"}]}}))
    with pytest.raises(ValueError, match="交集"):
        dispatch(env)


@pytest.mark.anyio
async def test_same_name_mcp_with_different_configuration_is_not_mounted(env, monkeypatch):
    env.source_options.mcp_servers["business"] = {"type": "http", "url": "https://source.invalid"}
    agents.update(env.b["id"], mcp=["business"])
    monkeypatch.setattr(settings_store, "effective_external_mcp", lambda names:
                        {name: {"type": "http", "url": "https://target.invalid"} for name in names})
    checks = []
    async def execute(client):
        assert "business" not in client.options.mcp_servers
        assert not await guard_tool(client, "mcp__business__lookup_customer")
        checks.append(True)
    env.behavior["B任务"] = execute
    row = dispatch(env)
    await settle()
    assert checks == [True] and tasks.get(row["id"])["status"] == "done"


@pytest.mark.anyio
async def test_many_external_receipts_and_mid_turn_delivery_are_not_lost(env):
    text = "轮次执行中收到的外部结果"
    async def source(client):
        session_store.append_external(env.source, text, "mid-turn")
    env.source_behavior = source
    async for event in agent.stream_turn([], "真实用户请求", session_key=env.source):
        if isinstance(event, agent.Done):
            session_store.set_sdk_session_id(env.source, event.reply.sdk_session_id)
    old = env.clients[-1]
    for index in range(45):
        session_store.append_external(env.source, f"回执{index}", f"event-{index}")
    async for _ in agent.stream_turn([], "追问", session_key=env.source,
                                     resume=session_store.get_sdk_session_id(env.source)):
        pass
    latest = agent.client_pool._pool[env.source].client
    assert latest is old and str(latest.prompt).count(text) == 1
    assert '"text": "回执0"' in str(latest.prompt) and '"text": "回执44"' in str(latest.prompt)
    revision, pending = session_store.external_context_state(env.source, "fixture-session")
    assert revision and not pending


@pytest.mark.anyio
async def test_sdk_permission_mode_mismatch_fails_before_model_execution(env):
    env.source_options.permission_mode = "plan"
    row = dispatch(env)
    await settle()
    assert tasks.get(row["id"])["status"] == "failed"
    assert "权限模式" in tasks.get(row["id"])["result_summary"]
    assert not any(getattr(client, "row", None) for client in env.clients)


@pytest.mark.anyio
async def test_missing_or_unrepresentable_source_policy_fails_closed(env):
    with pytest.raises(ValueError, match="快照"):
        d.delegate(env.source, [], env.b["id"], "无快照", "背景")
    env.source_options.can_use_tool = AsyncMock()
    with pytest.raises(ValueError, match="交集"):
        env.delegate(env.source, [], env.b["id"], "不支持的权限", "背景")
    assert not tasks._conn().execute("SELECT 1 FROM tasks").fetchone()


@pytest.mark.anyio
async def test_source_read_denial_survives_target_and_nested_delegation(env):
    env.source_options.disallowed_tools = ["Read(/private/*)", "mcp__vococo__run_subagent"]
    checks = []
    async def execute(client):
        assert not await guard_tool(client, "Read", {"file_path": "/tmp/anywhere"})
        checks.append(client.row["title"])
        if client.row["title"] == "B任务":
            response = await call(client, "delegate_to_agent", {
                "agent": env.c["id"], "title": "C任务", "prompt": "背景",
                "token_budget": 1000, "timeout_seconds": 5})
            assert "task_id" in response
    env.behavior.update({"B任务": execute, "C任务": execute})
    row = dispatch(env, tokens=60000, seconds=300)
    await settle()
    assert checks == ["B任务", "C任务"] and tasks.get(row["id"])["status"] == "done"


@pytest.mark.anyio
@pytest.mark.parametrize("tokens,seconds", [(True, 5), (0, 5), (1000, 0), (1000, True), (120000, 5)])
async def test_bad_budgets_are_rejected_without_tasks(env, tokens, seconds):
    with pytest.raises(ValueError):
        dispatch(env, tokens=tokens, seconds=seconds)
    assert tasks._conn().execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
