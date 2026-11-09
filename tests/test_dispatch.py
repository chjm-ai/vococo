"""统一派发的存储、回执及领取测试；执行调度和模型传输不连接真实服务。"""
from __future__ import annotations

import json

import pytest

from vococo import config
from vococo.core import dispatch as d, task_runner, tasks
from vococo.gateway import clarify, web_bridge
from vococo.memory import agents, session_store
from vococo.tools import sessions


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def env(isolated, monkeypatch):
    monkeypatch.setenv("VOCOCO_TASK_DISPATCH_V2", "1")
    monkeypatch.setattr(tasks, "_DB", None)
    monkeypatch.setattr(d, "wake", lambda: None)
    monkeypatch.setattr(task_runner, "_maybe_start_next", lambda: None)
    monkeypatch.setattr(task_runner, "_notify_activity", lambda *_: None)
    monkeypatch.setattr(task_runner, "session_busy", lambda *_: False)
    monkeypatch.setattr(clarify, "_active_sessions", set())
    monkeypatch.setattr(clarify, "has_pending", lambda *_: False)
    monkeypatch.setattr(sessions, "claim_pending", lambda *_: False)
    monkeypatch.setattr(web_bridge, "session_busy", lambda *_: False)
    monkeypatch.setattr(web_bridge, "keyed_available", lambda: True)
    owner = agents.create("派发测试")
    source = f"web:p{owner['project_hash']}:main"
    turn = session_store.start_turn(source, "原始需求")
    session_store.set_sdk_session_id(source, "fixture-sdk")
    d.begin(source, turn)
    yield source, turn
    tasks._conn().close()
    tasks._DB = None


def child(source, title="子任务"):
    return d.dispatch(source, "child", title, "完整任务背景")


def complete(row, text="完整结果", summary="摘要"):
    tasks.set_status(row["id"], "running")
    tasks.finish(row["id"], "done", text, summary)


def claim(source):
    row = d.current(source)
    connection = d.connection()
    connection.execute("UPDATE dispatch_executions SET state='claimed',claim='claim' WHERE id=?", (row["id"],))
    connection.commit()
    return d._resume_access(row, "claim")


def test_relation_tree_and_legacy_defaults(env):
    source, _ = env
    row = child(source)
    independent = d.dispatch(source, "independent", "新主题", "独立背景")
    assert row["parent_execution_id"] == d.current(source)["id"]
    assert independent["parent_execution_id"] is None
    assert independent["relation"] == "independent"
    tree = d.tree(source)
    assert [node["id"] for node in tree["children"]] == [row["id"]]
    assert d.tree(row["session_key"])["parent_session_key"] == source
    legacy = tasks.create("历史任务", "背景", agent_id=row["agent_id"])
    assert legacy["relation"] == "independent"


@pytest.mark.parametrize("relation,denied", [
    ("child", ["mcp__vococo__run_subagent"]),
    ("child", ["mcp__vococo__delegate_to_agent"]),
    ("child", ["mcp__vococo__dispatch_task"]),
    ("independent", ["mcp__vococo__dispatch_session"]),
    ("child", ["mcp__vococo__*"]),
    ("child", ["Agent"]),
    ("child", ["Task"]),
])
def test_old_and_new_deny_names_cannot_bypass(env, relation, denied):
    with pytest.raises(ValueError, match="禁止"):
        d.dispatch(env[0], relation, "任务", "背景", denied=denied)
    assert not d.tree(env[0])["children"]


def test_chain_limit_also_counts_independent_from_child(env):
    source, _ = env
    row = child(source)
    nested = row["session_key"]
    turn = session_store.start_turn(nested, "执行")
    d.begin(nested, turn)
    for number in range(4):
        d.dispatch(nested, "independent", f"独立{number}", "背景")
    with pytest.raises(ValueError, match="五个"):
        child(nested)


def test_batch_receipts_include_full_result_and_are_idempotent(env):
    source, _ = env
    first, second = child(source, "一"), child(source, "二")
    assert d.finish(source, "done")
    complete(first, "长结果的正文", "短摘要")
    parent = d.current(source)
    assert not d._store_receipts(parent)
    complete(second)
    assert d._store_receipts(parent)
    assert d._store_receipts(parent)
    receipts = d.connection().execute("SELECT * FROM dispatch_receipts").fetchall()
    assert len(receipts) == 2
    assert "长结果的正文" in receipts[0]["text"]
    assert all(row["processed"] == 0 for row in receipts)


def test_human_input_invalidates_claim_even_before_begin(env):
    source, _ = env
    access = claim(source)
    assert d.valid_resume(source, access)
    session_store.start_turn(source, "用户改变需求")
    assert not d.valid_resume(source, access)
    assert not d.valid_resume(source, {"dispatch_resume": "错误结构"})


def test_claim_can_begin_only_the_next_turn(env):
    source, _ = env
    access = claim(source)
    identifier = d.current(source)["id"]
    new_turn = session_store.start_turn(source, "自动续跑")
    assert d.begin(source, new_turn, access) == identifier
    assert d.current(source)["state"] == "active"
    assert not d.valid_resume(source, access)


@pytest.mark.anyio
async def test_drain_batches_once_then_acknowledges_success(env, monkeypatch):
    source, _ = env
    row = child(source)
    complete(row)
    assert d.finish(source, "done")
    queued = []
    monkeypatch.setattr(sessions, "_enqueue", lambda *args: queued.append(args))
    await d.drain()
    await d.drain()
    assert len(queued) == 1
    access = queued[0][2]
    assert d.valid_resume(source, access)
    turn = session_store.start_turn(source, queued[0][1])
    d.begin(source, turn, access)
    assert not d.finish(source, "done")
    assert d.current(source)["state"] == "completed"
    assert d.tree(source)["children"][0]["receipt_state"] == "processed"


@pytest.mark.anyio
async def test_enqueue_failure_preserves_receipts_for_retry(env, monkeypatch):
    source, _ = env
    complete(child(source))
    d.finish(source, "done")
    def fail(*_):
        raise RuntimeError("桥接暂时不可用")
    monkeypatch.setattr(sessions, "_enqueue", fail)
    await d.drain()
    assert d.current(source)["state"] == "waiting"
    assert d.connection().execute("SELECT processed FROM dispatch_receipts").fetchone()[0] == 0


def test_usage_rollup_is_idempotent_and_chain_budget_is_enforced(env, monkeypatch):
    source, turn = env
    row = child(source)
    child_turn = session_store.start_turn(row["session_key"], "子任务")
    d.begin(row["session_key"], child_turn)
    d.record_usage(source, turn, 100)
    d.record_usage(source, turn, 100)
    d.record_usage(row["session_key"], child_turn, 200)
    assert d.tree(source)["usage_tokens"] == 300
    monkeypatch.setenv("VOCOCO_TASK_CHAIN_TOKEN_BUDGET", "400")
    assert d.limit_budget(source, 500, "chat") == (100, "")
    d.record_usage(source, turn, 200)
    budget, refused = d.limit_budget(source, 500, "chat")
    assert budget == 0 and "用尽" in refused
    monkeypatch.setenv("VOCOCO_TASK_CHAIN_TOKEN_BUDGET", "0")
    assert d.limit_budget(source, 500, "chat") == (500, "")


def test_cancel_waiting_tree_cancels_queued_descendants(env):
    source, _ = env
    row = child(source)
    assert d.finish(source, "done")
    assert d.cancel_session(source)
    assert d.current(source)["state"] == "cancelled"
    assert tasks.get(row["id"])["status"] == "cancelled"
    assert not d.cancel_session(source)


@pytest.mark.anyio
async def test_pending_question_blocks_automatic_dispatch(env, monkeypatch):
    source, _ = env
    complete(child(source))
    d.finish(source, "done")
    queued = []
    monkeypatch.setattr(sessions, "_enqueue", lambda *args: queued.append(args))
    monkeypatch.setattr(clarify, "has_pending", lambda *_: True)
    await d.drain()
    assert not queued
    assert d.current(source)["state"] == "waiting"


def test_cannot_dispatch_back_to_a_different_ancestor_agent(env):
    source, _ = env
    owner = agents.agent_for_session(source)
    target = agents.create("另一执行者")
    row = d.dispatch(source, "child", "跨 Agent 子任务", "背景", agent=target["id"])
    nested = row["session_key"]
    turn = session_store.start_turn(nested, "执行")
    d.begin(nested, turn)
    with pytest.raises(ValueError, match="祖先"):
        d.dispatch(nested, "child", "回派", "背景", agent=owner["id"])
    assert child(nested)["agent_id"] == target["id"]


def test_model_configuration_failure_rolls_back_queued_task(env, monkeypatch):
    def fail(*_):
        raise RuntimeError("模型配置写入失败")
    monkeypatch.setattr(session_store, "set_chosen_model", fail)
    with pytest.raises(RuntimeError, match="写入失败"):
        child(env[0])
    assert not d.connection().execute("SELECT 1 FROM tasks").fetchone()


@pytest.mark.anyio
async def test_disabled_feature_does_not_claim_waiting_execution(env, monkeypatch):
    source, _ = env
    complete(child(source))
    d.finish(source, "done")
    queued = []
    monkeypatch.setattr(sessions, "_enqueue", lambda *args: queued.append(args))
    monkeypatch.setenv("VOCOCO_TASK_DISPATCH_V2", "0")
    await d.drain()
    assert not queued
    assert d.current(source)["state"] == "waiting"


def test_reused_turn_id_does_not_charge_the_old_execution(env):
    source, turn = env
    old = d.current(source)["id"]
    d.record_usage(source, turn, 100)
    session_store.delete_session(source)
    reused = session_store.start_turn(source, "新的任务链")
    assert reused == turn
    new = d.begin(source, reused)
    d.record_usage(source, reused, 200)
    assert d.usage_tokens(new) == 200
    assert d.usage_tokens(old) == 100
    # 旧流迟到的指标仍归原执行，不能污染编号恰好相同的新轮次。
    d.record_usage(source, turn, 150, execution_id=old)
    assert d.usage_tokens(old) == 150
    assert d.usage_tokens(new) == 200


def test_old_usage_schema_migrates_without_losing_usage(env):
    source, turn = env
    identifier = d.current(source)["id"]
    c = d.connection()
    c.execute("DROP TABLE dispatch_usage")
    c.execute("CREATE TABLE dispatch_usage(turn_id INTEGER PRIMARY KEY, execution_id TEXT NOT NULL, tokens INTEGER NOT NULL)")
    c.execute("INSERT INTO dispatch_usage VALUES(?,?,?)", (turn, identifier, 100))
    c.commit()
    d.connection()
    d.connection()
    assert d.usage_tokens(identifier) == 100
    session_store.delete_session(source)
    reused = session_store.start_turn(source, "迁移后的新任务")
    new = d.begin(source, reused)
    d.record_usage(source, reused, 200)
    assert d.usage_tokens(new) == 200
    assert d.usage_tokens(identifier) == 100


@pytest.mark.anyio
async def test_voice_parent_is_not_announced_done_while_waiting_for_children(env, monkeypatch):
    from unittest.mock import AsyncMock
    from vococo.core import completion, task_events
    from vococo.gateway.core import _finish_execution
    from vococo.voice import notify

    parent = d.dispatch(env[0], "independent", "语音父任务", "背景", origin="voice")
    source = parent["session_key"]
    turn = session_store.start_turn(source, "自动续跑后再派子任务")
    d.begin(source, turn)
    nested = child(source)
    tasks.set_status(parent["id"], "running")
    events = []
    monkeypatch.setattr(task_events, "_terminal_handlers", [notify.on_task_terminal])
    monkeypatch.setattr(notify, "_broadcast", lambda kind, payload: events.append(kind))
    monkeypatch.setattr(notify, "is_online", lambda: False)
    publish = AsyncMock()
    monkeypatch.setattr(completion, "publish", publish)
    await _finish_execution(source, parent["id"], "done", "等待子任务", turn)
    assert d.current(source)["state"] == "waiting"
    assert not events
    assert publish.await_count == 0
    complete(nested)
    row = d.current(source)
    assert d._store_receipts(row)
    access = claim(source)
    batch = [[nested["id"], tasks.get(nested["id"])["run_version"]]]
    d.connection().execute("UPDATE dispatch_executions SET batch=? WHERE id=?", (json.dumps(batch), row["id"]))
    d.connection().commit()
    turn = session_store.start_turn(source, "汇总完成")
    d.begin(source, turn, access)
    tasks.set_status(parent["id"], "running")
    await _finish_execution(source, parent["id"], "done", "最终结果", turn)
    assert d.current(source)["state"] == "completed"
    assert events == ["task_done"]
    assert publish.await_count == 1


def test_recovery_does_not_repeat_started_operations(env):
    source, _ = env
    other = "web:other"
    other_turn = session_store.start_turn(other, "已经开始")
    d.begin(other, other_turn)
    claim(source)
    d.recover()
    assert d.current(source)["state"] == "waiting"
    assert d.current(other)["state"] == "failed"
