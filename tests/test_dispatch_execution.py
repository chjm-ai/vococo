"""真实网关、任务执行器和 SDK MCP 桥接闭环，仅替换模型传输。"""
from unittest.mock import AsyncMock

import pytest

from test_delegation import anyio_backend, env, call, settle
from vococo import config
from vococo.core import dispatch as d, tasks
from vococo.gateway.adapters.base import Incoming
from vococo.gateway.run import GatewayRunner
from vococo.tools import sessions


@pytest.mark.anyio
async def test_nested_dispatch_does_not_inherit_synthetic_native_denials(env, monkeypatch):
    monkeypatch.setenv("VOCOCO_TASK_DISPATCH_V2", "1")
    monkeypatch.setattr(d, "wake", lambda: None)
    d.begin(env.source, env.turn)
    children = []
    async def nested(client):
        result = await call(client, "dispatch_task", {"title": "第二层", "prompt": "背景"})
        assert "task_id" in result, result
        children.append(result["task_id"])
    env.behavior["第一层"] = nested
    d.dispatch(env.source, "child", "第一层", "背景")
    await settle()
    assert len(children) == 1
    assert tasks.get(children[0])["status"] == "done"


@pytest.mark.anyio
async def test_resume_startup_failure_releases_claim(env, monkeypatch):
    monkeypatch.setenv("VOCOCO_TASK_DISPATCH_V2", "1")
    monkeypatch.setattr(d, "wake", lambda: None)
    identifier = d.begin(env.source, env.turn)
    connection = d.connection()
    connection.execute("UPDATE dispatch_executions SET state='claimed',claim='claim',resumes=1 WHERE id=?", (identifier,))
    connection.commit()
    access = d._resume_access(d.current(env.source), "claim")
    adapter = Adapter()
    adapter.send = AsyncMock()
    gateway = GatewayRunner([adapter])
    monkeypatch.setattr(gateway, "_handle", AsyncMock(side_effect=RuntimeError("worktree 创建失败")))
    await gateway._dispatch(adapter, Incoming(platform="web", chat_id=env.source.removeprefix("web:"), text="自动继续", source_access=access))
    assert d.current(env.source)["state"] == "failed"
    assert d.running_resumes() == 0


@pytest.mark.anyio
@pytest.mark.parametrize("exhausted", [False, True])
async def test_voice_children_and_automatic_resume_keep_daily_budget(env, monkeypatch, exhausted):
    monkeypatch.setenv("VOCOCO_TASK_DISPATCH_V2", "1")
    monkeypatch.setattr(config, "BG_DAILY_TOKEN_BUDGET", 10000)
    monkeypatch.setattr(d, "wake", lambda: None)
    from vococo.gateway import web_bridge
    monkeypatch.setattr(web_bridge, "keyed_available", lambda: True)
    monkeypatch.setattr(web_bridge, "session_busy", lambda *_: False)
    queued, children = [], []
    monkeypatch.setattr(sessions, "_enqueue", lambda *args: queued.append(args))
    async def spawn(client):
        result = await call(client, "dispatch_task", {"title": "语音子", "prompt": "背景"})
        assert "task_id" in result, result
        children.append(result["task_id"])
    env.behavior["语音父"] = spawn
    row = d.dispatch(env.source, "independent", "语音父", "完整背景", origin="voice")
    await settle()
    assert tasks.get(children[0])["origin"] == "voice"
    assert tasks.daily_usage() == 220
    del env.behavior["语音父"]
    await d.drain()
    assert len(queued) == 1
    if exhausted:
        tasks.add_daily_usage(10000)
    before, clients = tasks.daily_usage(), len(env.clients)
    session, text, access = queued[0]
    adapter = Adapter()
    await GatewayRunner([adapter])._dispatch(adapter, Incoming(platform="web", chat_id=session, text=text, source_access=access))
    assert tasks.daily_usage() == before + (0 if exhausted else 110)
    assert len(env.clients) == clients + (0 if exhausted else 1)
    assert d.current(row["session_key"])["state"] == ("failed" if exhausted else "completed")


class Sink:
    def __getattr__(self, _):
        return AsyncMock()


class Adapter:
    platform = "web"

    async def send(self, *args, **kwargs):
        pytest.fail(str(args))

    def make_sink(self, _):
        return Sink()


@pytest.mark.anyio
async def test_parent_child_resume_at_single_concurrency(env, monkeypatch):
    monkeypatch.setenv("VOCOCO_TASK_DISPATCH_V2", "1")
    monkeypatch.setattr(config, "TASK_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(d, "wake", lambda: None)
    queued, dispatched = [], []
    monkeypatch.setattr(sessions, "_enqueue", lambda *args: queued.append(args))
    from vococo.gateway import web_bridge
    monkeypatch.setattr(web_bridge, "keyed_available", lambda: True)
    monkeypatch.setattr(web_bridge, "session_busy", lambda *_: False)

    async def spawn(client):
        result = await call(client, "dispatch_task", {"title": "新子任务", "prompt": "完整背景", "agent": env.b["id"]})
        assert "task_id" in result, result
        dispatched.append(result["task_id"])
        client.text = "已安排分工"
    env.source_behavior = spawn
    adapter = Adapter()
    gateway = GatewayRunner([adapter])
    await gateway._dispatch(adapter, Incoming(platform="web", chat_id=env.source.removeprefix("web:"), text="开始工作"))
    await settle()
    assert tasks.get(dispatched[0])["status"] == "done"
    assert d.current(env.source)["state"] == "waiting"
    await d.drain()
    assert len(queued) == 1
    assert d.running_resumes() == 1
    session, text, access = queued[0]
    await gateway._dispatch(adapter, Incoming(platform="web", chat_id=session.removeprefix("web:"), text=text, source_access=access))
    assert d.current(env.source)["state"] == "completed"
    assert d.running_resumes() == 0
    assert d.tree(env.source)["children"][0]["receipt_state"] == "processed"
    assert "执行结果" in str(env.clients[-1].prompt)

    # 自动父轮再次派单时，释放的并发位必须立即调度新子任务。
    root = d.current(env.source)
    connection = d.connection()
    connection.execute("UPDATE dispatch_executions SET state='waiting' WHERE id=?", (root["id"],))
    connection.execute("UPDATE dispatch_receipts SET processed=0 WHERE execution_id=?", (root["id"],))
    connection.commit()
    await d.drain()
    env.source_behavior = spawn
    session, text, access = queued[-1]
    await gateway._dispatch(adapter, Incoming(platform="web", chat_id=session.removeprefix("web:"), text=text, source_access=access))
    await settle()
    assert len(dispatched) == 2
    assert tasks.get(dispatched[-1])["status"] == "done"


@pytest.mark.anyio
async def test_review_profile_remains_read_only_when_switch_is_off(env, monkeypatch):
    monkeypatch.setenv("VOCOCO_TASK_DISPATCH_V2", "1")
    monkeypatch.setattr(d, "wake", lambda: None)
    d.begin(env.source, env.turn)
    row = d.dispatch(env.source, "child", "只读评审", "检查代码", review_cwd=str(config.DATA_DIR.parent))
    await settle()
    client = next(c for c in env.clients if getattr(c, "row", None) and c.row["id"] == row["id"])
    assert client.options.tools == ["Read", "Grep", "Glob"]
    assert not client.options.mcp_servers
    assert client.options.plugins == []
    monkeypatch.delenv("VOCOCO_TASK_DISPATCH_V2")
    adapter = Adapter()
    await GatewayRunner([adapter])._dispatch(adapter, Incoming(platform="web", chat_id=row["session_key"], text="继续评审"))
    assert env.clients[-1].options.tools == ["Read", "Grep", "Glob"]
    assert not env.clients[-1].options.mcp_servers
