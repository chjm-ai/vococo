"""任务只能向原派发会话回传，真实入队与网关执行不得丢失权限。"""
import json

import pytest

from vococo import config
from vococo.core import permissions, tasks, worktree
from vococo.core.agent import AgentReply, Done
from vococo.gateway import core, web_bridge
from vococo.gateway.adapters.web import WebAdapter
from vococo.gateway.run import GatewayRunner
from vococo.memory import agents, session_store
from vococo.tools import sessions


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def task_chat(isolated, monkeypatch):
    monkeypatch.setattr(tasks, "_DB", None)
    monkeypatch.setattr(sessions, "_queued", {})
    monkeypatch.setattr(web_bridge, "_dispatch_fn", None)
    monkeypatch.setattr(web_bridge, "_inject_key_fn", None)
    monkeypatch.setattr(web_bridge, "_cancel_fn", None)
    agent = agents.create("回传测试")
    parent = f"web:p{agent['project_hash']}:parent"
    session_store.append(parent, "研究后回来汇总", "已派发")
    access = {"session": parent, "denied": ["Bash"], "mode": "standard"}
    task = tasks.create("研究", "调研", source_session_key=parent,
                        source_access=access, agent_id=agent["id"], origin="chat")
    source = tasks.session_key(task["id"])
    session_store.start_turn(source, "调研", source_access=access)
    adapter = WebAdapter()
    monkeypatch.setattr(adapter, "_emit", lambda event: None)
    web_bridge.register(adapter.inject, lambda _: pytest.fail("不能打断目标"), adapter.inject_by_key)
    yield source, parent, adapter
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None


async def send(source, target, text="研究部分完成，请核对报告后汇总", denied=()):
    tool = next(t for t in sessions.build_tools(source, list(denied)) if t.name == "continue_session")
    result = await tool.handler({"session_id": target, "instruction": text})
    if isinstance(result, list):  # SDK 新版 @tool 的 handler 返回的是 content 块列表
        result = result[0]
    return json.loads(result["content"][0]["text"])


@pytest.mark.anyio
async def test_task_return_preserves_access_through_real_gateway(task_chat, monkeypatch):
    source, parent, adapter = task_chat
    session_store.set_permission(parent, "full")
    result = await send(source, parent, denied=["Write"])
    assert result.get("status") == "queued", result
    incoming = adapter._inbox.get_nowait()
    assert incoming.session_key == parent
    assert "任务结果回传" in incoming.text and "用户完整指令" not in incoming.text
    seen = {}

    async def cwd(key):
        return str(config.ROOT_DIR)

    async def stream(*args, **kwargs):
        seen.update(access=session_store.active_source_access(parent),
                    mode=permissions.state(parent)["mode"])
        # 自动回传不得继续转发，避免两个会话互相唤醒。
        assert "error" in await send(parent, source)
        yield Done(AgentReply(text="已核对", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(worktree, "execution_cwd", cwd)
    monkeypatch.setattr(core, "stream_turn", stream)
    await GatewayRunner([])._dispatch(adapter, incoming)
    assert seen["access"]["session"] == source
    assert set(seen["access"]["denied"]) == {"Bash", "Write"}
    assert seen["mode"] == "standard"
    assert parent not in sessions._queued
    session_store.start_turn(parent, "真人接着问")
    assert session_store.active_source_access(parent) == {}
    assert permissions.state(parent)["mode"] == "full"


@pytest.mark.anyio
async def test_human_new_turn_in_task_session_can_send_through_real_gateway(task_chat, monkeypatch):
    from vococo.gateway.adapters.base import Incoming

    source, parent, adapter = task_chat
    task = tasks.task_for_session(source)
    assert task["source_access"]
    assert session_store.active_source_access(source)["session"] == parent
    seen = {}

    async def cwd(*args):
        return str(config.ROOT_DIR)

    async def stream(*args, **kwargs):
        seen["access"] = session_store.active_source_access(source)
        seen["result"] = await send(source, parent)
        yield Done(AgentReply(text="已发送", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(worktree, "execution_cwd_for_task", cwd)
    monkeypatch.setattr(core, "stream_turn", stream)
    incoming = Incoming(platform="web", chat_id=source, text="真人接着问，请发回原会话")
    assert incoming.source_access is None
    await GatewayRunner([])._dispatch(adapter, incoming)
    assert seen["access"] == {}
    assert seen["result"]["status"] == "queued"
    assert seen["result"]["source_session"] == source
    assert seen["result"]["session_id"] == parent.removeprefix("web:")
    forwarded = adapter._inbox.get_nowait()
    assert forwarded.session_key == parent
    assert forwarded.source_access is None
    assert "用户完整指令" in forwarded.text and "任务结果回传" not in forwarded.text
    assert session_store.load_recent(source)[-1].assistant == "已发送"
    assert tasks.get(task["id"])["source_access"] == task["source_access"]


@pytest.mark.anyio
@pytest.mark.parametrize("invalid", ["other_target", "missing_task", "missing_origin", "wrong_origin"])
async def test_task_return_cannot_forge_parent(task_chat, invalid):
    source, parent, adapter = task_chat
    if invalid == "other_target":
        parent += "other"
        session_store.append(parent, "问", "答")
    elif invalid == "missing_task":
        tasks._conn().execute("DELETE FROM tasks WHERE id=?", (tasks.task_id_from_session_key(source),))
        tasks._conn().commit()
    elif invalid == "missing_origin":
        session_store.start_turn(source, "自动来源缺少会话", source_access={"denied": ["Bash"]})
    else:
        session_store.start_turn(source, "伪来源", source_access={"session": "web:other"})
    result = await send(source, parent)
    assert "error" in result, result
    assert adapter._inbox.empty()


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["busy", "pending", "archived", "unavailable"])
async def test_task_return_obeys_target_guards(task_chat, monkeypatch, state):
    from vococo.gateway import clarify

    source, parent, adapter = task_chat
    if state == "busy":
        monkeypatch.setattr(clarify, "_active_sessions", {parent: "执行中"})
    elif state == "pending":
        monkeypatch.setattr(clarify, "has_pending", lambda key: key == parent)
    elif state == "archived":
        session_store.set_conv_archived(parent, True)
    else:
        monkeypatch.setattr(web_bridge, "_inject_key_fn", None)
    monkeypatch.setattr(web_bridge, "_cancel_fn", None)
    assert "error" in await send(source, parent)
    assert adapter._inbox.empty()
