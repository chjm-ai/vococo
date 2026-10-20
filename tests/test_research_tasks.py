"""研究任务与统一队列集成；真实临时库，只有执行器/模型解析使用替身。"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from vococo import config, providers
from vococo.core import research, research_store, task_events, task_runner as runner, tasks
from vococo.core.agent import AgentReply, Done, TextDelta, ToolInput, ToolStarted
from vococo.memory import session_store


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
    monkeypatch.setattr(providers, "resolve", lambda *a, **k: None)
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


@pytest.mark.anyio
async def test_research_returns_to_source_without_touching_active_reply(environment, monkeypatch):
    source = "web:source"
    active_turn = session_store.start_turn(source, "正在回复的问题")
    events = []
    task_events.register_main_event_bridge(events.append)
    async def execute(task_id, stream_fn=None):
        await stream_fn(TextDelta("不是最终报告的阶段草稿"))
        return await completed(task_id)
    monkeypatch.setattr(research, "execute", execute)
    row = runner.dispatch_research("研究", "问题", context_session_key=source)
    await settle()
    assert row["source_session_key"] == source
    assert session_store.load_history(tasks.session_key(row["id"])) == []
    history = session_store.load_history(source)
    assert history[-1]["assistant"] == "完整研究报告"
    assert "阶段草稿" not in str(history)
    assert all(event["type"] not in {"start", "done", "message"} for event in events)
    assert any(event["type"] == "research_update" for event in events)
    assert any(event["type"] == "research_result" for event in events)
    assert session_store.finish_turn(active_turn, "原回复正常收尾", session_key=source)
    assert session_store.load_history(source)[0]["assistant"] == "原回复正常收尾"


@pytest.mark.anyio
async def test_progress_events_include_checkpoints_without_delta_spam(environment, monkeypatch):
    events = []
    task_events.register_main_event_bridge(events.append)
    async def execute(task_id, stream_fn=None):
        research_store.update(task_id, lambda state: state.update(status="running"))
        for _ in range(30):
            await stream_fn(TextDelta("不能公开的草稿"))
        before = len(events)
        research._checkpoint(task_id, "plan", "公开的已提交计划")
        await stream_fn(research.ProgressChanged())
        await stream_fn(research.ProgressChanged())
        updates = [event for event in events[before:] if event["type"] == "research_update"]
        assert len(updates) == 1
        assert updates[0]["progress"]["checkpoints"] == {"plan": "公开的已提交计划"}
        assert updates[0]["progress"]["title"] == "进度研究"
        return await completed(task_id)
    monkeypatch.setattr(research, "execute", execute)
    row = runner.dispatch_research("进度研究", "问题", context_session_key="web:source")
    await settle()
    updates = [event for event in events if event["type"] == "research_update"]
    assert len(updates) <= 5
    assert all("progress" in event for event in updates)
    assert "不能公开的草稿" not in str(events)
    assert updates[-1]["progress"]["task_status"] == "done"
    assert tasks.get(row["id"])["status"] == "done"


@pytest.mark.anyio
async def test_real_research_cancel_publishes_closed_activity(environment, monkeypatch):
    started = asyncio.Event()
    events = []
    task_events.register_main_event_bridge(events.append)
    async def model(**kwargs):
        if "当前阶段：plan。" in kwargs["user_text"]:
            yield Done(AgentReply(text="已提交计划", tool_calls=[], cost_usd=None, is_error=False))
            return
        args = {"query": "公开搜索词"}
        await kwargs["tool_guard"]({"tool_name": "WebSearch", "tool_input": args}, "q", None)
        yield ToolInput("WebSearch", "q", args)
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(research, "stream_turn", model)
    row = runner.dispatch_research("取消研究", "问题", context_session_key="web:source")
    try:
        await asyncio.wait_for(started.wait(), 2)
        updates = [event["progress"] for event in events if event["type"] == "research_update"]
        assert updates[-1]["activity"][0]["status"] == "running"
        assert updates[-1]["checkpoints"] == {"plan": "已提交计划"}
    finally:
        runner.cancel(row["id"])
        await settle()
    progress = [event["progress"] for event in events if event["type"] == "research_update"][-1]
    assert progress["task_status"] == "cancelled"
    assert progress["activity"][0]["status"] == "failed"
    assert progress["activity"][0]["finished_at"]
    assert progress["activity"] == research.get(row["id"])["activity"]


@pytest.mark.anyio
async def test_partial_executor_output_is_not_completed_report(environment, monkeypatch):
    async def partial(task_id, stream_fn=None):
        return "阶段文本，不是报告"
    monkeypatch.setattr(research, "execute", partial)
    row = runner.dispatch_research("研究", "问题", context_session_key="web:source")
    await settle()
    assert tasks.get(row["id"])["status"] == "failed"
    assert tasks.get(row["id"])["result_full"] == ""
    assert "未产出已完成的报告" in session_store.load_history("web:source")[-1]["assistant"]


@pytest.mark.anyio
async def test_inline_cancel_resume_and_restart_keep_source(environment, monkeypatch):
    started = asyncio.Event()
    async def blocked(task_id, stream_fn=None):
        research_store.update(task_id, lambda state: state.update(
            status="running", checkpoints={"plan": "计划"}))
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(research, "execute", blocked)
    row = runner.dispatch_research("研究", "问题", context_session_key="web:source")
    await started.wait()
    await runner.cancel_and_wait(row["id"])
    assert "已取消" in session_store.load_history("web:source")[-1]["assistant"]
    monkeypatch.setattr(research, "execute", completed)
    assert (await runner.resume_research(row["id"]))["ok"]
    await settle()
    assert session_store.load_history("web:source")[-1]["assistant"] == "完整研究报告"
    count = len(session_store.load_history("web:source"))
    await runner.heal_after_restart()
    assert len(session_store.load_history("web:source")) == count
    assert session_store.load_history(tasks.session_key(row["id"])) == []


@pytest.mark.anyio
async def test_inline_delivery_failure_does_not_block_queue_and_retries_on_restart(environment, monkeypatch):
    original = session_store.append_external
    def broken(*a, **k):
        raise RuntimeError("模拟会话库暂不可写")
    monkeypatch.setattr(session_store, "append_external", broken)
    monkeypatch.setattr(research, "execute", completed)
    first = runner.dispatch_research("研究一", "问题", context_session_key="web:source")
    second = runner.dispatch_research("研究二", "问题", context_session_key="web:source")
    await settle()
    assert tasks.get(first["id"])["status"] == "done"
    assert tasks.get(second["id"])["status"] == "done"
    assert not tasks.get(first["id"])["research_delivered_at"]
    monkeypatch.setattr(session_store, "append_external", original)
    await runner.heal_after_restart()
    assert len(session_store.load_history("web:source")) == 2
    await runner.heal_after_restart()
    assert len(session_store.load_history("web:source")) == 2


@pytest.mark.anyio
async def test_delivery_retries_after_append_succeeded_without_duplicate(environment, monkeypatch):
    original = session_store.set_pending_review
    def broken(*a, **k):
        raise RuntimeError("模拟写完报告后失败")
    monkeypatch.setattr(session_store, "set_pending_review", broken)
    monkeypatch.setattr(research, "execute", completed)
    row = runner.dispatch_research("研究", "问题", context_session_key="web:source")
    await settle()
    assert len(session_store.load_history("web:source")) == 1
    assert not tasks.get(row["id"])["research_delivered_at"]
    monkeypatch.setattr(session_store, "set_pending_review", original)
    await runner.heal_after_restart()
    assert len(session_store.load_history("web:source")) == 1
    assert tasks.get(row["id"])["research_delivered_at"]


@pytest.mark.anyio
async def test_research_notify_links_source_and_sends_full_report(environment, monkeypatch):
    from vococo.voice import notify
    from vococo.gateway.adapters.web_push import PUSH
    pushed, platform, events = [], [], []
    async def push(**kwargs):
        pushed.append(kwargs)
    async def send(*args):
        platform.append(args)
    monkeypatch.setattr(PUSH, "notify", push)
    monkeypatch.setattr(notify, "_platform_push", send)
    task_events.register_main_event_bridge(events.append)
    monkeypatch.setattr(research, "execute", completed)
    row = runner.dispatch_research("研究", "问题", context_session_key="web:source",
                                   dispatch_platform="web", dispatch_chat_id="source")
    await settle()
    await notify.on_task_terminal(row["id"])
    assert pushed[-1]["conv"] == "source"
    assert platform == []  # Web 不可再走会结束主回复的通用 message 推送。
    tasks._conn().execute("UPDATE tasks SET dispatch_platform='telegram' WHERE id=?", (row["id"],))
    tasks._conn().commit()
    await notify.on_task_terminal(row["id"])
    assert platform[-1] == ("telegram", "source", "完整研究报告")
    assert not any(event["type"] in {"start", "done"} for event in events)
    assert session_store.load_history(tasks.session_key(row["id"])) == []


@pytest.mark.anyio
async def test_custom_main_session_research_routes_to_frontend_main(environment, monkeypatch):
    from vococo.voice import notify
    from vococo.gateway.adapters.web_push import PUSH
    monkeypatch.setattr(config, "SESSION_KEY", "shared-session")
    monkeypatch.setattr(research, "execute", completed)
    events, pushes = [], []
    task_events.register_main_event_bridge(events.append)
    async def push(**kwargs):
        pushes.append(kwargs)
    monkeypatch.setattr(PUSH, "notify", push)
    row = runner.dispatch_research("研究", "问题", context_session_key="shared-session")
    await settle()
    await notify.on_task_terminal(row["id"])
    assert events and all(event["conv"] == "main" for event in events)
    assert pushes[-1]["conv"] == "main"
    assert session_store.load_history("shared-session")[-1]["assistant"] == "完整研究报告"
    assert session_store.load_history("web:shared-session") == []


async def completed(task_id, stream_fn=None):
    research_store.update(task_id, lambda state: state.update(status="done", report="完整研究报告"))
    return "完整研究报告"


def queued_research(task_id, status="queued", checkpoint=False, research_status="ready"):
    research.initialize(task_id, "研究问题", "fixed-model")
    research_store.update(task_id, lambda state: state.update(
        status=research_status, checkpoints={"plan": "计划"} if checkpoint else {}))
    row = tasks.create("研究", "研究问题", task_id=task_id, kind="research", origin="chat")
    if status != "queued":
        tasks.set_status(task_id, "running")
        if status != "running":
            tasks.finish(task_id, status, "", "旧结果")
    return row


@pytest.mark.anyio
@pytest.mark.parametrize("explicit,source,expected", [
    ("explicit", "source-model", "explicit"),
    (None, "source-model", "source-model"), (None, "", "fixed-default"),
])
async def test_dispatch_fixed_model_no_sidecar(environment, monkeypatch, explicit, source, expected):
    session_store.set_chosen_model("web:source", source)
    monkeypatch.setattr(providers, "sidecar_env", lambda *a, **k: pytest.fail("研究不能模型回退"))
    observed = []
    async def execute(task_id, stream_fn=None):
        observed.append(research.get(task_id)["model"])
        assert tasks.get(task_id)["status"] == "running"
        return await completed(task_id, stream_fn)
    monkeypatch.setattr(research, "execute", execute)
    row = runner.dispatch_research("研究", "问题", model=explicit, context_session_key="web:source")
    session_store.set_chosen_model("web:source", "changed-later")
    assert row["kind"] == "research"
    assert session_store.get_chosen_model(tasks.session_key(row["id"])) == ""
    assert research.get(row["id"])["model"] == expected
    await settle()
    assert observed == [expected]
    assert tasks.get(row["id"])["result_full"] == "完整研究报告"


@pytest.mark.anyio
async def test_research_queue_and_resume_share_concurrency(environment, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    seen = []
    async def execute(task_id, stream_fn=None):
        seen.append(task_id)
        started.set()
        await release.wait()
        return await completed(task_id, stream_fn)
    monkeypatch.setattr(research, "execute", execute)
    first = runner.dispatch_research("一", "问题")
    await started.wait()
    second = runner.dispatch_research("二", "问题")
    assert second["status"] == "queued"
    assert runner.cancel(second["id"])
    restored = await runner.resume_research(second["id"])
    assert restored["ok"] and restored["task"]["status"] == "queued"
    assert seen == [first["id"]]
    release.set()
    await settle()
    assert seen == [first["id"], second["id"]]
    assert tasks.count_running() == 0


@pytest.mark.anyio
async def test_cancel_running_then_resume_preserves_checkpoint(environment, monkeypatch):
    started = asyncio.Event()
    async def blocked(task_id, stream_fn=None):
        research_store.update(task_id, lambda state: state.update(
            status="running", checkpoints={"plan": "已保存计划"}))
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(research, "execute", blocked)
    row = runner.dispatch_research("研究", "问题")
    await started.wait()
    await runner.cancel_and_wait(row["id"])
    assert tasks.get(row["id"])["status"] == "cancelled"
    assert not (await runner.append(row["id"], "覆盖原问题"))["ok"]
    monkeypatch.setattr(research, "execute", completed)
    assert (await runner.resume_research(row["id"]))["ok"]
    await settle()
    assert research.get(row["id"])["checkpoints"] == {"plan": "已保存计划"}
    assert not (await runner.resume_research(row["id"]))["ok"]


@pytest.mark.anyio
async def test_cancel_before_first_coroutine_step_frees_queue(environment, monkeypatch):
    monkeypatch.setattr(research, "execute", completed)
    first = runner.dispatch_research("一", "问题")
    second = runner.dispatch_research("二", "问题")
    assert runner.cancel(first["id"])
    await settle()
    assert tasks.get(first["id"])["status"] == "cancelled"
    assert tasks.get(second["id"])["status"] == "done"


@pytest.mark.anyio
@pytest.mark.parametrize("already_failed", [False, True])
async def test_executor_database_failure_keeps_traceback(environment, monkeypatch, capsys, already_failed):
    def open_missing_database():
        sqlite3.connect(environment / "missing" / "probe.db")

    async def broken(task_id, turn_text=None):
        if already_failed:
            tasks.fail_research(task_id, "先前失败")
        open_missing_database()

    monkeypatch.setattr(runner, "_run", broken)
    row = runner.dispatch_research("研究", "问题")
    finished = runner._running[row["id"]]
    await asyncio.wait({finished})
    await asyncio.sleep(0)
    if already_failed:
        runner._running.pop(row["id"], None)
    output = capsys.readouterr().out
    assert f"研究执行器 {row['id']} 异常" in output
    assert "Traceback (most recent call last)" in output
    assert "open_missing_database" in output
    assert "sqlite3.OperationalError: unable to open database file" in output
    assert tasks.get(row["id"])["status"] == "failed"
    if not already_failed:
        assert not runner._running
        assert "研究执行器中断" in tasks.get(row["id"])["result_summary"]


@pytest.mark.parametrize("failure", ["initialize", "create"])
def test_dispatch_failure_never_leaves_queued_row(environment, monkeypatch, failure):
    def broken(*a, **k):
        raise ValueError("模拟创建失败")
    target, name = {
        "initialize": (research, "initialize"),
        "create": (tasks, "create"),
    }[failure]
    monkeypatch.setattr(target, name, broken)
    with pytest.raises(ValueError, match="模拟创建失败"):
        runner.dispatch_research("研究", "问题")
    assert tasks.list_recent() == []


@pytest.mark.anyio
async def test_failure_and_progress_metrics_use_common_finish(environment, monkeypatch):
    terminal = []
    async def terminal_handler(task_id):
        terminal.append(tasks.get(task_id))
    task_events.register_terminal_handler(terminal_handler)
    updates = task_events.subscribe()
    async def execute(task_id, stream_fn=None):
        research_store.update(task_id, lambda state: state.update(
            stage="collect", sources=[{"id": "S1"}, {"id": "S2"}],
            usage={"tokens": 37, "searches": 3, "fetches": 2}))
        await stream_fn(ToolStarted("WebSearch", "tool"))
        await stream_fn(ToolInput("WebSearch", "tool", {"query": "研究"}))
        await stream_fn(TextDelta("阶段正文"))
        await stream_fn(Done(AgentReply(text="阶段结果", tool_calls=[], cost_usd=None,
                                       is_error=False, turn_tokens=37)))
        raise RuntimeError("研究阶段失败")
    monkeypatch.setattr(research, "execute", execute)
    row = runner.dispatch_research("研究", "问题", dispatch_platform="telegram",
                                   context_session_key="telegram:source")
    await settle()
    result = tasks.get(row["id"])
    assert result["status"] == "failed"
    assert "研究阶段失败" in result["result_summary"]
    assert result["last_tokens"] == 37 and result["last_tool_calls"] == 1
    assert tasks.daily_usage() == 37
    assert terminal[-1]["status"] == "failed"
    notes = []
    while not updates.empty():
        _, payload = updates.get_nowait()
        notes.append(payload["progress_note"])
        assert payload["kind"] == "research"
    assert "搜集资料 · 2个来源 · 3/12次搜索" in notes
    turns = session_store.load_history(tasks.session_key(row["id"]))
    assert turns == []
    source_turns = session_store.load_history("telegram:source")
    assert "研究阶段失败" in source_turns[-1]["assistant"]
    assert "阶段正文" not in str(source_turns)


@pytest.mark.anyio
@pytest.mark.parametrize("status,research_status,checkpoint,expired,expected", [
    ("running", "running", True, False, "done"),
    ("queued", "ready", False, False, "done"),
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
        research_store.update("restore", lambda state: state.update(deadline=time.time() - 1))
    monkeypatch.setattr(research, "execute", completed)
    await runner.heal_after_restart()
    await settle()
    row = tasks.get("restore")
    assert row["status"] == expected
    if expected == "failed":
        assert row["progress_note"]
    assert tasks.list_queued() == []


@pytest.mark.anyio
async def test_restart_uses_saved_report_even_if_expired_or_daily_full(environment, monkeypatch):
    queued_research("saved", "running", True, "done")
    research_store.update("saved", lambda state: state.update(report="已保存的报告", deadline=0))
    monkeypatch.setattr(config, "BG_DAILY_TOKEN_BUDGET", 1)
    tasks._conn().execute("UPDATE tasks SET origin='voice' WHERE id='saved'")
    tasks._conn().commit()
    tasks.add_daily_usage(1)
    async def forbidden(*a, **k):
        pytest.fail("已保存报告不能重复执行")
    monkeypatch.setattr(research, "execute", forbidden)
    await runner.heal_after_restart()
    await settle()
    assert tasks.get("saved")["status"] == "done"
    assert tasks.get("saved")["result_full"] == "已保存的报告"


@pytest.mark.anyio
async def test_resume_limits_missing_and_append_do_not_change_prompt(environment):
    queued_research("failed", "failed")
    research_store.update("failed", lambda state: state.update(deadline=0))
    assert not (await runner.resume_research("failed"))["ok"]
    assert not (await runner.resume_research("missing"))["ok"]
    queued_research("queued")
    assert not (await runner.append("queued", "更改问题"))["ok"]
    assert tasks.get("queued")["prompt"] == "研究问题"
    assert runner.cancel("queued")


@pytest.mark.anyio
async def test_daily_limit_rejects_before_execute(environment, monkeypatch):
    monkeypatch.setattr(config, "BG_DAILY_TOKEN_BUDGET", 10)
    tasks.add_daily_usage(10)
    async def forbidden(*a, **k):
        pytest.fail("每日预算耗尽不能开始研究")
    monkeypatch.setattr(research, "execute", forbidden)
    row = runner.dispatch_research("研究", "问题", dispatch_platform="telegram")
    await settle()
    assert tasks.get(row["id"])["status"] == "failed"
    assert "总量已达上限" in tasks.get(row["id"])["result_summary"]


@pytest.mark.anyio
async def test_research_bypasses_worktree_and_chat_task_budget(environment, monkeypatch):
    monkeypatch.setattr(config, "CHAT_TASK_TOKEN_BUDGET", 1)
    async def forbidden(*a, **k):
        pytest.fail("研究不能创建项目 worktree")
    monkeypatch.setattr(runner.worktree, "execution_cwd_for_task", forbidden)
    monkeypatch.setattr(research, "execute", completed)
    row = runner.dispatch_research("研究", "问题", budget={"tokens": 999})
    await settle()
    assert research.get(row["id"])["budget"]["tokens"] == 999
    assert tasks.get(row["id"])["status"] == "done"


@pytest.mark.anyio
async def test_old_terminal_callback_cannot_fail_resumed_run(environment, monkeypatch):
    old_notification, release_old = asyncio.Event(), asyncio.Event()
    resumed, release_new = asyncio.Event(), asyncio.Event()
    calls = 0
    async def delayed_terminal(task_id):
        if tasks.get(task_id)["status"] == "failed":
            old_notification.set()
            await release_old.wait()
    task_events.register_terminal_handler(delayed_terminal)
    async def execute(task_id, stream_fn=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("首轮失败")
        resumed.set()
        await release_new.wait()
        return await completed(task_id)
    monkeypatch.setattr(research, "execute", execute)
    row = runner.dispatch_research("研究", "问题")
    old = runner._running[row["id"]]
    await old_notification.wait()
    assert (await runner.resume_research(row["id"]))["ok"]
    new = runner._running[row["id"]]
    await resumed.wait()
    release_old.set()
    await old
    await asyncio.sleep(0)
    assert tasks.get(row["id"])["status"] == "running"
    assert runner._running[row["id"]] is new
    assert runner.cancel(row["id"])
    await settle()
    assert tasks.get(row["id"])["status"] == "cancelled"


@pytest.mark.anyio
async def test_usage_limit_during_research_counts_tokens_and_finishes(environment, monkeypatch):
    monkeypatch.setattr(config, "BG_DAILY_TOKEN_BUDGET", 10)
    async def execute(task_id, stream_fn=None):
        research_store.update(task_id, lambda state: state["usage"].update(tokens=10))
        await stream_fn(TextDelta("到达每日预算"))
        pytest.fail("到达每日预算后不能继续研究")
    monkeypatch.setattr(research, "execute", execute)
    row = runner.dispatch_research("研究", "问题", dispatch_platform="telegram")
    await settle()
    assert tasks.get(row["id"])["status"] == "failed"
    assert tasks.get(row["id"])["last_tokens"] == 10
    assert tasks.daily_usage() == 10


@pytest.mark.anyio
async def test_restart_missing_record_and_exhausted_tokens_fail(environment, monkeypatch):
    tasks.create("缺研究记录", "问题", task_id="missing", kind="research")
    queued_research("exhausted", "running", True, "running")
    research_store.update("exhausted", lambda state: state["usage"].update(tokens=state["budget"]["tokens"]))
    async def forbidden(*a, **k):
        pytest.fail("不可恢复的研究不能运行")
    monkeypatch.setattr(research, "execute", forbidden)
    await runner.heal_after_restart()
    assert tasks.get("missing")["status"] == "failed"
    assert "研究记录缺失" in tasks.get("missing")["progress_note"]
    assert tasks.get("exhausted")["status"] == "failed"
    assert "token 预算" in tasks.get("exhausted")["progress_note"]
    assert tasks.list_queued() == []


@pytest.mark.anyio
async def test_session_initialization_failure_still_frees_slot(environment, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("会话库失败")
    monkeypatch.setattr(session_store, "start_turn", broken)
    monkeypatch.setattr(research, "execute", completed)
    first = queued_research("legacy-one")
    second = queued_research("legacy-two")
    runner._maybe_start_next()
    await settle()
    assert tasks.get(first["id"])["status"] == "failed"
    assert tasks.get(second["id"])["status"] == "failed"
    assert tasks.list_queued() == []


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
    research_store.update("recover-usage", lambda state: state["usage"].update(tokens=90))
    if already_accounted:
        tasks.reconcile_research_usage("recover-usage", 90)
    async def execute(task_id, stream_fn=None):
        assert tasks.daily_usage() == 90  # 启动前必须已经补记崩溃前的消耗。
        research_store.update(task_id, lambda state: state["usage"].update(tokens=100))
        await stream_fn(TextDelta("恢复后的结果"))
        assert tasks.daily_usage() == 100  # 无需等待收尾，流式回调已对账。
        await stream_fn(TextDelta("重复事件"))
        assert tasks.daily_usage() == 100
        return await completed(task_id)
    monkeypatch.setattr(research, "execute", execute)
    await runner.heal_after_restart()
    await settle()
    assert tasks.get("recover-usage")["status"] == "done"
    assert tasks.get("recover-usage")["last_tokens"] == 10
    assert tasks.daily_usage() == 100
    await runner.heal_after_restart()
    assert tasks.daily_usage() == 100


@pytest.mark.anyio
async def test_restart_reconciles_terminal_task_after_accounting_failure(environment, monkeypatch):
    connection = tasks._conn()
    connection.execute(
        "CREATE TRIGGER fail_research_accounting BEFORE INSERT ON bg_research_usage "
        "BEGIN SELECT RAISE(ABORT, '模拟账本失败'); END"
    )
    connection.commit()
    async def execute(task_id, stream_fn=None):
        research_store.update(task_id, lambda state: state["usage"].update(tokens=90))
        await stream_fn(TextDelta("已耗90"))
    monkeypatch.setattr(research, "execute", execute)
    row = runner.dispatch_research("研究", "问题", dispatch_platform="telegram")
    await settle()
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
    research_store.update("over-cap", lambda state: state["usage"].update(tokens=90))
    monkeypatch.setattr(config, "BG_DAILY_TOKEN_BUDGET", 90)
    async def forbidden(*a, **k):
        pytest.fail("补记后已达每日上限，不能启动 SDK")
    monkeypatch.setattr(research, "execute", forbidden)
    await runner.heal_after_restart()
    await settle()
    assert tasks.get("over-cap")["status"] == "failed"
    assert tasks.daily_usage() == 90
    assert "总量已达上限" in tasks.get("over-cap")["result_summary"]


@pytest.mark.anyio
async def test_explicit_resume_reconciles_previous_run_tokens(environment, monkeypatch):
    queued_research("resume-usage", "cancelled", True, "interrupted")
    tasks._conn().execute("UPDATE tasks SET origin='voice' WHERE id='resume-usage'")
    tasks._conn().commit()
    research_store.update("resume-usage", lambda state: state["usage"].update(tokens=90))
    async def execute(task_id, stream_fn=None):
        assert tasks.daily_usage() == 90
        research_store.update(task_id, lambda state: state["usage"].update(tokens=100))
        # 没有新事件也必须在 finally 对账。
        return await completed(task_id)
    monkeypatch.setattr(research, "execute", execute)
    assert (await runner.resume_research("resume-usage"))["ok"]
    assert tasks.daily_usage() == 90
    await settle()
    assert tasks.daily_usage() == 100
    assert tasks.get("resume-usage")["last_tokens"] == 10


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
