"""外围会话映射回归；只使用临时数据库，不启动模型。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from vococo import config
from vococo.core import delegation, task_events, task_runner, tasks
from vococo.cron import scheduler
from vococo.gateway import brain_routes, library_api, notice_actions, task_routes
from vococo.memory import agents, session_store
from vococo.voice import notify, task_tools


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def links(isolated, monkeypatch):
    monkeypatch.setattr(tasks, "_DB", None)
    monkeypatch.setattr(config, "CRON_JOBS_PATH", isolated / "cron.json")
    monkeypatch.setattr(task_runner, "_maybe_start_next", Mock())
    for aid in ("abcdef1234", "1234abcdef"):
        workdir = isolated / aid
        workdir.mkdir()
        session_store.upsert_project(str(workdir))
        agents._write_meta(aid, {"name": aid, "workdir": str(workdir),
                                 "disallowed_tools": ["WebFetch"]})
    yield isolated
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None


def job(**options):
    return scheduler.create_job(name="巡检", prompt="检查", schedule={"kind": "interval", "minutes": 60},
                                agent_id="abcdef1234", **options)


def bound_session(aid="abcdef1234"):
    key = config.new_session_key()
    session_store.bind_session(key, agent_id=aid, project_hash=agents.get(aid)["project_hash"])
    return key


def test_new_jobs_bind_without_execution_and_legacy_cron_keys_migrate_idempotently(links):
    created = job(mode="script", command="printf 'ok'")
    assert created["conv"].startswith("session:")
    assert session_store.session_binding(created["conv"])["agent_id"] == "abcdef1234"
    assert tasks.task_for_job(created["id"]) is None
    scheduler.save_jobs([{"id": "old", "conv": "task:old"},
                         {"id": "older", "conv": "cron-task:older"}])
    assert [row["conv"] for row in scheduler.load_jobs()] == ["task:old", "task:older"]
    assert [row["conv"] for row in scheduler.load_jobs()] == ["task:old", "task:older"]


@pytest.mark.anyio
async def test_scheduler_dispatches_by_job_and_reuses_execution_mapping(links, monkeypatch):
    created = job(model="fixture-model")
    dispatch = Mock(side_effect=lambda **kw: tasks.create(**{key: value for key, value in kw.items()
                                                            if key != "model"}))
    append = AsyncMock()
    monkeypatch.setattr(task_runner, "dispatch", dispatch)
    monkeypatch.setattr(task_runner, "append", append)
    scheduler._run_job(created, AsyncMock())
    task = tasks.task_for_job(created["id"])
    assert task["id"] != created["id"]
    assert task["session_key"] == created["conv"]
    assert "task_id" not in dispatch.call_args.kwargs
    session_store.set_sdk_session_id(created["conv"], "old-sdk")
    scheduler._run_job(created, AsyncMock())
    await asyncio.sleep(0)
    append.assert_awaited_once_with(task["id"], created["prompt"], fresh=True)
    assert dispatch.call_count == 1
    assert session_store.get_sdk_session_id(created["conv"]) is None
    assert session_store.get_chosen_model(created["conv"]) == "fixture-model"


@pytest.mark.anyio
async def test_terminal_updates_job_not_execution_id(links):
    created = job()
    task = tasks.create("巡检", "检查", origin="cron", job_id=created["id"],
                        session_key=created["conv"], agent_id="abcdef1234")
    push = AsyncMock()
    await scheduler._on_task_terminal({**task, "status": "done", "result_summary": "成功"}, push)
    assert scheduler.load_jobs()[0]["last_status"] == "success"
    assert push.call_args.args[1] == created["conv"]


@pytest.mark.anyio
async def test_script_result_stays_in_bound_conversation(links, monkeypatch):
    created = job(mode="script", command="printf 'done ##CRON_SIGNAL:0##'")
    monkeypatch.setattr(scheduler, "_record_agent_run", Mock())
    events = []
    monkeypatch.setattr(task_events, "_main_event_bridge", events.append)
    await scheduler._run_script_job(created, AsyncMock())
    assert [event["type"] for event in events] == ["start", "done"]
    assert all(event["job_id"] == created["id"] and event["conversation_id"] == created["conv"]
               and event["task_id"] is None for event in events)
    assert tasks.task_for_job(created["id"]) is None
    assert session_store.load_recent(created["conv"])[0].assistant.strip() == "done"
    assert session_store.session_binding(created["conv"])["agent_id"] == "abcdef1234"


def test_changed_owner_gets_new_session_and_preserves_old_history(links):
    created = job()
    old_key = created["conv"]
    session_store.append(old_key, "旧归属的问题", "旧归属的回答")
    updated = scheduler.update_job(created["id"], name=created["name"], prompt=created["prompt"],
                                   schedule=created["schedule"], agent_id="1234abcdef")
    assert updated["conv"].startswith("session:") and updated["conv"] != old_key
    assert session_store.session_binding(updated["conv"])["agent_id"] == "1234abcdef"
    assert session_store.session_binding(old_key)["agent_id"] == "abcdef1234"
    assert session_store.load_recent(old_key)[0].assistant == "旧归属的回答"
    assert session_store.load_recent(updated["conv"]) == []
    assert scheduler.load_jobs()[0]["conv"] == updated["conv"]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("new_owner", [None, "1234abcdef"])
def test_deleted_owner_rule_can_transfer_without_reviving_old_agent(links, legacy, new_owner):
    created = job()
    if legacy:
        created["conv"] = f"task:{created['id']}"
        scheduler.save_jobs([created])
    session_store.append(created["conv"], "旧问题", "保留历史")
    agents.delete("abcdef1234")
    updated = scheduler.update_job(created["id"], name=created["name"], prompt=created["prompt"],
                                   schedule=created["schedule"], agent_id=new_owner)
    assert updated["conv"] != created["conv"]
    assert session_store.session_binding(created["conv"])["agent_id"] == "abcdef1234"
    assert session_store.load_recent(created["conv"])[0].assistant == "保留历史"
    assert session_store.session_binding(updated["conv"])["agent_id"] == (new_owner or agents.GENERAL_ID)
    assert agents.get("abcdef1234") is None
    with pytest.raises(ValueError, match="已删除|不存在"):
        scheduler._run_job(created, AsyncMock())


def test_changed_directory_gets_new_session_and_execution_root(links, monkeypatch):
    created = job()
    task = tasks.create("旧运行", "检查", origin="cron", job_id=created["id"],
                        session_key=created["conv"], agent_id="abcdef1234")
    directory = links / "new-root"
    directory.mkdir()
    updated = scheduler.update_job(created["id"], name=created["name"], prompt=created["prompt"],
                                   schedule=created["schedule"], cwd=str(directory))
    assert updated["conv"] != created["conv"]
    assert session_store.session_binding(updated["conv"])["project_hash"] == session_store.upsert_project(str(directory))["hash"]
    dispatch = Mock()
    monkeypatch.setattr(task_runner, "dispatch", dispatch)
    scheduler._run_job(updated, AsyncMock())
    assert dispatch.call_args.kwargs["cwd"] == str(directory)
    assert dispatch.call_args.kwargs["session_key"] == updated["conv"]
    assert tasks.get(task["id"])["session_key"] == created["conv"]


@pytest.mark.anyio
@pytest.mark.parametrize("legacy", [False, True])
async def test_old_execution_cannot_notify_new_owner_after_transfer(links, monkeypatch, legacy):
    created = job()
    if legacy:
        created["conv"] = f"task:{created['id']}"
        scheduler.save_jobs([created])
    task = tasks.create("旧运行", "检查", origin="cron", job_id=created["id"],
                        session_key=created["conv"], agent_id="abcdef1234")
    updated = scheduler.update_job(created["id"], name="新归属", prompt=created["prompt"],
                                   schedule=created["schedule"], agent_id="1234abcdef")
    push = AsyncMock()
    record = Mock()
    monkeypatch.setattr(scheduler, "_record_agent_run", record)
    await scheduler._on_task_terminal({**task, "status": "done", "result_summary": "旧结果"}, push)
    assert scheduler.load_jobs()[0] == updated
    push.assert_not_awaited()
    record.assert_not_called()
    # 脚本同样使用启动时的会话，不能拿最新归属投递旧结果。
    assert await scheduler._push_job_result(created["id"], "success", "旧脚本", push,
                                             session_key=created["conv"]) is None
    push.assert_not_awaited()
    assert updated["conv"] != created["conv"]
    assert task_runner.task_owner_id(tasks.get(task["id"])) == "abcdef1234"
    assert agents.agent_for_session(created["conv"])["id"] == "abcdef1234"
    agents._cancel_agent_work("1234abcdef")
    assert tasks.get(task["id"])["status"] == "queued"
    agents._cancel_agent_work("abcdef1234")
    assert tasks.get(task["id"])["status"] == "cancelled"


@pytest.mark.anyio
async def test_script_busy_and_cancellation_follow_start_session_after_transfer(links, monkeypatch):
    created = job(mode="script", command="true")
    entered = asyncio.Event()

    async def script(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduler, "_run_script_job", script)
    scheduler._run_job(created, AsyncMock())
    running = scheduler._script_tasks[created["id"]]
    await entered.wait()
    try:
        updated = scheduler.update_job(created["id"], name=created["name"], prompt=created["prompt"],
                                       schedule=created["schedule"], agent_id="1234abcdef")
        assert task_runner.session_busy(created["conv"])
        assert not task_runner.session_busy(updated["conv"])
        assert scheduler.script_owner_id(created["id"]) == "abcdef1234"
        assert agents._cancel_agent_work("1234abcdef") == set()
        assert not running.cancelling()
        assert agents._cancel_agent_work("abcdef1234") == {running}
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    assert created["id"] not in scheduler._script_sessions


def test_deleted_owner_is_not_silently_general(links):
    created = job()
    with pytest.raises(ValueError, match="已删除"):
        scheduler._run_job({**created, "agent_id": "deadbeef00"}, AsyncMock())


@pytest.mark.parametrize("key,conv", [("session:abc", "session:abc"), ("web:legacy", "legacy"),
                                      ("task:old", "task:old"), ("unknown:route", "")])
def test_notice_and_library_links_accept_new_keys_without_opening_unknown_routes(links, key, conv):
    assert library_api._source_conv(key) == conv
    assert notice_actions.conv_of(key) == (conv or None)
    if conv:
        assert task_routes._dispatch_chat_id(SimpleNamespace(query={"session_key": key})) == conv


@pytest.mark.anyio
async def test_notice_resume_uses_mapped_task_id(links, monkeypatch):
    task = tasks.create("任务", "内容", agent_id="abcdef1234")
    append = AsyncMock(return_value={"message": "已续接"})
    monkeypatch.setattr(task_runner, "append", append)
    assert await notice_actions.resume(task["session_key"], "继续") == "已续接"
    append.assert_awaited_once_with(task["id"], "继续")


def test_task_activity_sends_execution_job_and_conversation_ids(links, monkeypatch):
    created = job()
    task = tasks.create("任务", "内容", origin="cron", job_id=created["id"],
                        session_key=created["conv"], agent_id="abcdef1234")
    queue, events = asyncio.Queue(), []
    monkeypatch.setattr(task_events, "_subscribers", {queue})
    monkeypatch.setattr(task_events, "_main_event_bridge", events.append)
    monkeypatch.setattr(task_events, "_started_tasks", set())
    task_events.on_task_activity({**task, "status": "running"})
    payload = queue.get_nowait()[1]
    for value in (payload, events[0]):
        assert value["task_id"] == task["id"]
        assert value["job_id"] == created["id"]
        assert value["conversation_id"] == created["conv"]


@pytest.mark.anyio
async def test_terminal_notification_sends_mapped_conversation(links, monkeypatch):
    from vococo.gateway.adapters.web_push import PUSH

    task = tasks.create("任务", "内容", origin="chat", agent_id="abcdef1234")
    tasks.set_status(task["id"], "running")
    tasks.finish(task["id"], "done", "成功", "成功")
    events, push = [], AsyncMock()
    monkeypatch.setattr(notify, "_broadcast", lambda event, payload: events.append(payload))
    monkeypatch.setattr(PUSH, "notify", push)
    monkeypatch.setattr(notify, "_platform_push", None)
    await notify.on_task_terminal(task["id"])
    assert events[0]["task_id"] == task["id"]
    assert events[0]["conversation_id"] == task["session_key"]
    assert events[0]["job_id"] is None
    assert push.call_args.kwargs["conv"] == task["session_key"]


def test_brain_owner_uses_binding_and_rejects_unknown_new_session(links):
    key = bound_session()
    assert brain_routes._fast_owner(key, {}, {}, brain_routes._history_owner)["id"] == "abcdef1234"
    assert brain_routes._owns_session(key, "abcdef1234")
    assert not brain_routes._owns_session(key, "1234abcdef")
    assert brain_routes._history_owner(config.new_session_key()) is None


def test_delegation_new_chain_uses_real_session_and_preserves_denials(links):
    source = bound_session()
    session_store.start_turn(source, "委托处理")
    task = delegation.delegate(source, ["Bash"], "1234abcdef", "整理", "完整背景")
    access = json.loads(task["source_access"])
    assert access["chain_session"] == task["session_key"]
    assert access["chain_session"].startswith("session:")
    assert {"Bash", "WebFetch"} <= set(access["denied"])
    assert delegation.get(task["id"])["source_session"] == source
    assert delegation.query(source, task["id"])["id"] == task["id"]
    with pytest.raises(ValueError, match="不属于"):
        delegation.query(bound_session("1234abcdef"), task["id"])
    with pytest.raises(ValueError):
        delegation._source(config.new_session_key())


@pytest.mark.anyio
async def test_voice_resolves_session_to_execution_for_query_cancel_and_continue(links, monkeypatch):
    task = tasks.create("任务", "内容", agent_id="abcdef1234")
    key = task["session_key"]
    assert task_tools._find_task(key)["id"] == task["id"]
    assert task_tools._find_task(task["id"])["id"] == task["id"]
    result = await task_tools.voice_query_session.handler({"session_id": key})
    assert key in result["content"][0]["text"]
    cancel = Mock(return_value=True)
    monkeypatch.setattr(task_runner, "cancel", cancel)
    await task_tools.voice_cancel_task.handler({"session_id": key})
    cancel.assert_called_once_with(task["id"])
    append = AsyncMock(return_value={"ok": True, "message": "继续", "task": task})
    monkeypatch.setattr(task_runner, "append", append)
    await task_tools.voice_continue_session.handler({"session_id": key, "instruction": "继续"})
    append.assert_awaited_once_with(task["id"], "继续")
