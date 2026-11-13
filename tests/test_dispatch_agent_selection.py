"""真实派发入口的身份选择回归；隔离数据库，不执行模型。"""
from types import SimpleNamespace

import pytest

from vococo import config
from vococo.core import dispatch, task_runner, tasks
from vococo.gateway import clarify
from vococo.memory import agents, session_store
from vococo.tools import builtin
from vococo.voice import task_tools


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def env(isolated, monkeypatch):
    monkeypatch.setattr(tasks, "_DB", None)
    monkeypatch.setattr(task_runner, "_maybe_start_next", lambda: None)
    monkeypatch.setattr(task_runner, "_notify_activity", lambda *_: None)
    monkeypatch.setattr(task_tools, "_find_related_candidate", lambda *_: None)
    source, target = agents.create("来源"), agents.create("专门处理")
    session = config.new_session_key()
    session_store.bind_session(session, agent_id=source["id"])
    # 使用统一绑定的新式会话，不借项目目录反推来源身份。
    token = clarify.set_current(session, SimpleNamespace(platform="web"), "test")
    yield session, source, target
    clarify.reset_current(token)
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None


async def invoke(entry, session, args):
    if entry == "text-old":
        return await builtin.dispatch_session.handler(args)
    if entry == "text-new":
        return await dispatch.build_tools(session)[1].handler(args)
    return await task_tools.voice_dispatch_task.handler(args)


@pytest.mark.anyio
@pytest.mark.parametrize("entry", ["text-old", "text-new", "voice-old", "voice-new"])
@pytest.mark.parametrize("selection", [None, "name", "id", "invalid"])
async def test_dispatch_identity_is_not_selected_by_directory(env, monkeypatch, entry, selection):
    session, source, target = env
    monkeypatch.setenv("VOCOCO_TASK_DISPATCH_V2", "1" if entry.endswith("new") else "0")
    args = {"title": "独立任务", "prompt": "完整背景", "cwd": target["workdir"]}
    if selection:
        args["agent"] = target[selection] if selection != "invalid" else "不存在的 Agent"
    out = await invoke(entry, session, args)
    rows = tasks.list_recent(10)
    if selection == "invalid":
        assert "不存在" in out["content"][0]["text"]
        assert not rows
        return
    assert len(rows) == 1
    row = rows[0]
    expected = target if selection else source
    assert row["agent_id"] == expected["id"]
    assert row["cwd"] == target["workdir"]
    assert row["source_session_key"] == session
    assert row["session_key"] not in {session, target["main_conv"], source["main_conv"]}
    assert agents.agent_for_session(row["session_key"])["id"] == expected["id"]
    assert row["relation"] == "independent" and row["parent_execution_id"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("entry", ["text-old", "text-new", "voice-old", "voice-new"])
async def test_explicit_agent_defaults_to_target_workdir(env, monkeypatch, entry):
    session, _, target = env
    monkeypatch.setenv("VOCOCO_TASK_DISPATCH_V2", "1" if entry.endswith("new") else "0")
    await invoke(entry, session, {"title": "目标目录", "prompt": "背景", "agent": target["id"]})
    row = tasks.list_recent(1)[0]
    assert row["agent_id"] == target["id"]
    assert row["cwd"] == target["workdir"]


@pytest.mark.anyio
@pytest.mark.parametrize("entry", ["text-old", "text-new", "voice-old", "voice-new"])
async def test_general_agent_stays_general_when_directory_belongs_to_target(env, monkeypatch, entry):
    _, _, target = env
    monkeypatch.setenv("VOCOCO_TASK_DISPATCH_V2", "1" if entry.endswith("new") else "0")
    session = config.new_session_key()
    session_store.bind_session(session, agent_id=agents.GENERAL_ID)
    token = clarify.set_current(session, SimpleNamespace(platform="web"), "general")
    try:
        await invoke(entry, session, {"title": "总助理派发", "prompt": "背景", "cwd": target["workdir"]})
    finally:
        clarify.reset_current(token)
    assert tasks.list_recent(1)[0]["agent_id"] == agents.GENERAL_ID
