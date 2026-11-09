"""历史研究兼容：只读、队列拒跑、重启补账与普通任务回归。"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from vococo import config, providers
from vococo.core import research, task_events, task_runner as runner, tasks
from vococo.core.agent import AgentReply, Done
from vococo.memory import session_store

from historical_research import initialize, update


@pytest.fixture
def anyio_backend():
    return "asyncio"



@pytest.fixture
def environment(isolated, monkeypatch):
    monkeypatch.setattr(tasks, "_DB", None)
    monkeypatch.setattr(runner, "_running", {})
    monkeypatch.setattr(task_events, "_started_tasks", set())
    monkeypatch.setattr(task_events, "_subscribers", set())
    monkeypatch.setattr(task_events, "_terminal_handlers", [])
    monkeypatch.setattr(task_events, "_main_event_bridge", None)
    monkeypatch.setattr(config, "TASK_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(config, "BG_DAILY_TOKEN_BUDGET", 0)
    monkeypatch.setattr(config, "MODEL", "fixed-default")
    monkeypatch.setattr(providers, "resolve", lambda *a, **k: ("fixed-default", {}))
    monkeypatch.setattr(runner, "_source_agent_id", lambda key: None)
    yield isolated
    assert not runner._running
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None



async def settle():
    """等待明确可控的本地 asyncio 执行器收尾，不接触网络或 SDK。"""
    for _ in range(100):
        if not runner._running:
            await asyncio.sleep(0)
            if not runner._running:
                return
        await asyncio.sleep(0.001)
    raise AssertionError("执行器没有收尾")



def queued_research(task_id, status="queued", checkpoint=False, research_status="ready"):
    initialize(task_id, "研究问题", "fixed-model")
    update(task_id, lambda state: state.update(
        status=research_status, checkpoints={"plan": "计划"} if checkpoint else {}))
    row = tasks.create("研究", "研究问题", task_id=task_id, kind="research", origin="chat")
    if status != "queued":
        tasks.set_status(task_id, "running")
        if status != "running":
            tasks.finish(task_id, status, "", "旧结果")
    return row



@pytest.mark.anyio
@pytest.mark.parametrize("status,research_status,checkpoint,expired,expected", [
    ("running", "running", True, False, "failed"),
    ("queued", "ready", False, False, "failed"),
    ("done", "done", True, False, "done"),
    ("failed", "interrupted", True, False, "failed"),
    ("running", "running", False, False, "failed"),
    ("running", "running", True, True, "failed"),
    ("queued", "ready", False, True, "failed"),
    ("queued", "interrupted", True, False, "failed"),
    ("cancelled", "running", True, False, "cancelled"),
])
async def test_restart_handles_all_research_states(environment, monkeypatch, status, research_status,
                                                   checkpoint, expired, expected):
    queued_research("restore", status, checkpoint, research_status)
    if expired:
        update("restore", lambda state: state.update(deadline=time.time() - 1))
    before = research.get("restore")
    await runner.heal_after_restart()
    await settle()
    row = tasks.get("restore")
    assert row["status"] == expected
    if status in {"queued", "running"}:
        assert "已移除" in row["progress_note"]
    assert research.get("restore") == before
    assert tasks.list_queued() == []



@pytest.mark.anyio
async def test_restart_clears_all_research_before_notification_can_dispatch(environment, monkeypatch):
    queued_research("old-first")
    queued_research("old-second")
    started, notified = [], []
    async def run(task_id, turn_text=None):
        started.append(task_id)
        assert tasks.get(task_id)["kind"] != "research"
        tasks.finish(task_id, "done", "普通任务结果", "完成")
        runner._running.pop(task_id, None)
    async def notify(task_id):
        notified.append(task_id)
        if len(notified) == 1:
            assert tasks.get("old-second")["status"] == "failed"
            runner.dispatch("通知期间派发的普通任务", "问题", origin="chat")
            await asyncio.sleep(0)
    monkeypatch.setattr(runner, "_run", run)
    monkeypatch.setattr(task_events, "emit_terminal", notify)
    monkeypatch.setattr(config, "resolve_execution_root", lambda **k: str(environment))
    monkeypatch.setattr(runner, "_task_agent_id", lambda *args: None)
    await runner.heal_after_restart()
    await settle()
    assert notified == ["old-first", "old-second"]
    assert len(started) == 1
    assert tasks.get(started[0])["status"] == "done"
    assert tasks.list_queued() == []



@pytest.mark.anyio
async def test_restart_preserves_saved_report_without_reopening_executor(environment, monkeypatch):
    queued_research("saved", "running", True, "done")
    update("saved", lambda state: state.update(report="已保存的报告", deadline=0))
    monkeypatch.setattr(config, "BG_DAILY_TOKEN_BUDGET", 1)
    tasks._conn().execute("UPDATE tasks SET origin='voice' WHERE id='saved'")
    tasks._conn().commit()
    tasks.add_daily_usage(1)
    await runner.heal_after_restart()
    await settle()
    assert tasks.get("saved")["status"] == "failed"
    assert research.get("saved")["report"] == "已保存的报告"



def test_research_accounting_is_idempotent_across_midnight(environment, monkeypatch):
    monkeypatch.setattr(tasks, "_today", lambda: "2026-10-16")
    assert tasks.reconcile_research_usage("research", 90) == 90
    assert tasks.reconcile_research_usage("research", 90) == 0
    monkeypatch.setattr(tasks, "_today", lambda: "2026-10-17")
    assert tasks.reconcile_research_usage("research", 100) == 10
    assert tasks.reconcile_research_usage("research", 100) == 0
    assert tasks.reconcile_research_usage("research", 80) == 0
    assert tasks.daily_usage("2026-10-16") == 90
    assert tasks.daily_usage("2026-10-17") == 10
    # 没有逐日明细的旧检查点，只把未对账差额保守归恢复当天。
    assert tasks.reconcile_research_usage("legacy", 30) == 30
    assert tasks.daily_usage("2026-10-16") == 90
    assert tasks.daily_usage("2026-10-17") == 40



def test_research_accounting_rolls_back_daily_and_checkpoint_together(environment):
    connection = tasks._conn()
    connection.execute(
        "CREATE TRIGGER fail_research_accounting BEFORE INSERT ON bg_research_usage "
        "BEGIN SELECT RAISE(ABORT, '模拟账本失败'); END"
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="模拟账本失败"):
        tasks.reconcile_research_usage("atomic", 90)
    assert tasks.daily_usage() == 0
    assert not connection.execute("SELECT * FROM bg_research_usage").fetchall()
    connection.execute("DROP TRIGGER fail_research_accounting")
    connection.commit()
    assert tasks.reconcile_research_usage("atomic", 90) == 90
    assert tasks.daily_usage() == 90
    assert tasks.reconcile_research_usage("atomic", 90) == 0



@pytest.mark.anyio
@pytest.mark.parametrize("already_accounted", [False, True])
async def test_restart_reconciles_prior_tokens_without_double_counting(environment, monkeypatch, already_accounted):
    queued_research("recover-usage", "running", True, "running")
    tasks._conn().execute("UPDATE tasks SET origin='voice' WHERE id='recover-usage'")
    tasks._conn().commit()
    update("recover-usage", lambda state: state["usage"].update(tokens=90))
    if already_accounted:
        tasks.reconcile_research_usage("recover-usage", 90)
    await runner.heal_after_restart()
    await settle()
    assert tasks.get("recover-usage")["status"] == "failed"
    assert research.get("recover-usage")["usage"]["tokens"] == 90
    assert tasks.daily_usage() == 90
    await runner.heal_after_restart()
    assert tasks.daily_usage() == 90



@pytest.mark.anyio
async def test_restart_reconciles_terminal_task_after_accounting_failure(environment, monkeypatch):
    connection = tasks._conn()
    connection.execute(
        "CREATE TRIGGER fail_research_accounting BEFORE INSERT ON bg_research_usage "
        "BEGIN SELECT RAISE(ABORT, '模拟账本失败'); END"
    )
    connection.commit()
    row = queued_research("failed-accounting", "failed", True, "interrupted")
    tasks._conn().execute("UPDATE tasks SET origin='voice' WHERE id=?", (row["id"],))
    tasks._conn().commit()
    update(row["id"], lambda state: state["usage"].update(tokens=90))
    with pytest.raises(sqlite3.IntegrityError, match="模拟账本失败"):
        runner._reconcile_research_usage(tasks.get(row["id"]))
    assert tasks.get(row["id"])["status"] == "failed"
    assert tasks.daily_usage() == 0
    connection.execute("DROP TRIGGER fail_research_accounting")
    connection.commit()
    await runner.heal_after_restart()
    assert tasks.daily_usage() == 90
    assert tasks.get(row["id"])["status"] == "failed"
    await runner.heal_after_restart()
    assert tasks.daily_usage() == 90



@pytest.mark.anyio
async def test_restart_reconciliation_enforces_daily_limit_before_execution(environment, monkeypatch):
    queued_research("over-cap", "running", True, "running")
    tasks._conn().execute("UPDATE tasks SET origin='voice' WHERE id='over-cap'")
    tasks._conn().commit()
    update("over-cap", lambda state: state["usage"].update(tokens=90))
    monkeypatch.setattr(config, "BG_DAILY_TOKEN_BUDGET", 90)
    await runner.heal_after_restart()
    await settle()
    assert tasks.get("over-cap")["status"] == "failed"
    assert tasks.daily_usage() == 90
    assert "已移除" in tasks.get("over-cap")["result_summary"]



@pytest.mark.anyio
@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled", "done"])
async def test_removed_core_resume_and_start_leave_history_unchanged(environment, status):
    row = queued_research("history", status, True, "interrupted")
    before_task, before_state = tasks.get(row["id"]), research.get(row["id"])
    result = await runner.append(row["id"], "恢复研究")
    assert not result["ok"] and "已移除" in result["message"]
    assert not runner._start_one(row)
    with pytest.raises(RuntimeError, match="已移除"):
        runner._spawn(row["id"])
    assert tasks.get(row["id"]) == before_task
    assert research.get(row["id"]) == before_state
    assert not runner._running



@pytest.mark.anyio
@pytest.mark.parametrize("origin", ["chat", "cron"])
async def test_ordinary_dispatch_before_heal_never_starts_old_research(environment, monkeypatch, origin):
    queued_research("old-before-heal")
    before = research.get("old-before-heal")
    started = []
    async def run(task_id, turn_text=None):
        started.append(task_id)
        assert tasks.get(task_id)["kind"] == "chat"
        tasks.finish(task_id, "done", "普通任务结果", "完成")
        runner._running.pop(task_id, None)
    monkeypatch.setattr(runner, "_run", run)
    monkeypatch.setattr(config, "resolve_execution_root", lambda **k: str(environment))
    monkeypatch.setattr(runner, "_task_agent_id", lambda *args: None)
    row = runner.dispatch("普通任务", "问题", origin=origin)
    await settle()
    assert started == [row["id"]]
    assert tasks.get("old-before-heal")["status"] == "failed"
    assert "已移除" in tasks.get("old-before-heal")["progress_note"]
    assert research.get("old-before-heal") == before
    await runner.heal_after_restart()
    assert started == [row["id"]]
    assert tasks.get(row["id"])["status"] == "done"



@pytest.mark.anyio
async def test_ordinary_task_dispatch_append_and_restart_unchanged(environment, monkeypatch):
    async def execute(history, prompt, **kwargs):
        yield Done(AgentReply(text="普通任务结果", tool_calls=[], cost_usd=None, is_error=False))
    async def cwd(root, task_id):
        return root
    monkeypatch.setattr(runner, "stream_turn", execute)
    monkeypatch.setattr(runner.worktree, "execution_cwd_for_task", cwd)
    monkeypatch.setattr(config, "resolve_execution_root", lambda **k: str(environment))
    monkeypatch.setattr(providers, "sidecar_env", lambda *a, **k: None)
    row = runner.dispatch("普通", "问题", origin="chat")
    await settle()
    assert tasks.get(row["id"])["kind"] == "chat"
    assert tasks.get(row["id"])["status"] == "done"
    assert (await runner.append(row["id"], "追问"))["ok"]
    await settle()
    tasks.create("普通排队", "问题", task_id="orphan")
    await runner.heal_after_restart()
    assert tasks.get("orphan")["status"] == "failed"



def test_removed_execution_apis_are_absent(environment):
    assert not hasattr(runner, "dispatch_research")
    assert not hasattr(runner, "resume_research")
    assert not hasattr(tasks, "requeue_research")
    for name in ("initialize", "execute", "prepare_resume", "recover", "record_source"):
        assert not hasattr(research, name)
