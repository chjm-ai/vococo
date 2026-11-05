"""执行完成只走统一通知入口，不由会话展示或来源类型决定。"""
import asyncio

import pytest

from vococo import config
from vococo.core import completion, task_events, task_runner, tasks
from vococo.core.agent import AgentReply, Done, TextDelta
from vococo.cron import scheduler
from vococo.gateway import core
from vococo.gateway.adapters.web import WebAdapter, _WebSink, _conv_id_for_key
from vococo.gateway.adapters.web_push import PUSH
from vococo.gateway.run import GatewayRunner
from vococo.memory import session_store
from vococo.voice import notify


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def outbox(isolated, monkeypatch):
    sent = []

    async def send(**payload):
        sent.append(payload)
        return 1

    monkeypatch.setattr(PUSH, "notify", send)
    monkeypatch.setattr(config, "PUSH_ON_DONE", True)
    monkeypatch.setattr(config, "CRON_JOBS_PATH", isolated / "jobs.json")
    monkeypatch.setattr(completion, "_policy", scheduler._prepare_completion)
    monkeypatch.setattr(completion, "_platform_push", None)
    monkeypatch.setattr(task_events, "_terminal_handlers", [notify._terminal_event_handler])
    monkeypatch.setattr(task_events, "_subscribers", set())
    monkeypatch.setattr(task_events, "_main_event_bridge", None)
    monkeypatch.setattr(task_events, "_stream_sink_factory", None)
    monkeypatch.setattr(task_runner, "_running", {})
    monkeypatch.setattr(tasks, "_DB", None)
    yield sent
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None


@pytest.mark.anyio
async def test_sink_only_renders_without_looking_up_execution_type(outbox, monkeypatch):
    key = "session:pure-view"
    turn_id = session_store.append(key, "问题", "结果")
    adapter = WebAdapter()
    events = []
    monkeypatch.setattr(adapter, "_emit", events.append)

    def unexpected(*args, **kwargs):
        pytest.fail("展示层不应查询执行类型或发送完成推送")

    monkeypatch.setattr(tasks, "task_id_from_session_key", unexpected)
    monkeypatch.setattr(adapter, "_push_notify", unexpected)
    await _WebSink(adapter, key).done(AgentReply("结果", [], None, False), turn_id)
    assert [event["type"] for event in events] == ["start", "done"]
    assert outbox == []


@pytest.mark.anyio
@pytest.mark.parametrize("origin", [None, "chat", "voice", "cron"])
async def test_foreground_cancellation_notifies_once_without_schedule_side_effects(outbox, isolated, monkeypatch, origin):
    row = tasks.create("取消", "问题", cwd=str(isolated), origin=origin, job_id="old-rule") if origin else None
    key = tasks.session_key(row["id"]) if row else "session:ordinary"
    if row is None:
        session_store.bind_session(key, agent_id="general")
    adapter = WebAdapter()

    async def stream(*args, **kwargs):
        yield TextDelta("部分回答")
        raise asyncio.CancelledError

    monkeypatch.setattr(core, "stream_turn", stream)
    with pytest.raises(asyncio.CancelledError):
        await core.converse(key, "继续", None, _WebSink(adapter, key), cwd_override=str(isolated))
    assert len(outbox) == 1
    assert outbox[0]["kind"] == "cancelled"
    assert session_store.load_history(key)[-1]["assistant"] == "部分回答"
    assert not next(row for row in session_store.list_sessions("") if row["key"] == key)["pending_review"]


@pytest.mark.anyio
@pytest.mark.parametrize("origin", ["chat", "voice", "cron"])
@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
async def test_dispatched_execution_uses_same_completion_after_saving_result(outbox, isolated, monkeypatch, origin, status):
    adapter = WebAdapter()
    events = []
    monkeypatch.setattr(adapter, "_emit", events.append)
    legacy_pushes = []
    monkeypatch.setattr(adapter, "_push_notify", lambda **kw: legacy_pushes.append(kw))

    async def stream(*args, **kwargs):
        yield TextDelta("运行结果")
        if status == "cancelled":
            raise asyncio.CancelledError
        yield Done(AgentReply("运行结果", [], None, status == "failed", error="测试错误" if status == "failed" else ""))

    monkeypatch.setattr(task_runner, "stream_turn", stream)
    job = scheduler.create_job(name="运行", prompt="检查", schedule={"kind": "cron", "expr": "0 8 * * *"}) if origin == "cron" else None
    row = task_runner.dispatch(
        title="运行", prompt="检查", cwd=str(isolated), origin=origin,
        job_id=job["id"] if job else None, session_key=job["conv"] if job else None,
    )
    await task_runner._running[row["id"]]
    key = tasks.session_key(row["id"])
    assert tasks.get(row["id"])["status"] == status
    assert session_store.load_history(key)[-1]["assistant"]
    assert len(outbox) == 1
    assert outbox[0]["conv"] == _conv_id_for_key(key)
    assert outbox[0]["kind"] == {"done": "done", "failed": "error", "cancelled": "cancelled"}[status]
    assert len([event for event in events if event["type"] == "done"]) == 1
    assert legacy_pushes == []


@pytest.mark.anyio
async def test_result_copies_cannot_generate_second_system_notification(outbox, monkeypatch):
    adapter = WebAdapter()
    events, legacy_pushes = [], []
    monkeypatch.setattr(adapter, "_emit", events.append)
    monkeypatch.setattr(adapter, "_push_notify", lambda **kw: legacy_pushes.append(kw))
    runner = GatewayRunner([adapter])
    monkeypatch.setattr(completion, "_platform_push", runner.deliver_completion_copy)
    event = completion.Completion("session:source", "done", "已完成", copies=[
        ("web", "session:source", "同一会话"),
        ("web", "session:target", "结果副本"),
        ("web", "session:target", "结果副本"),
    ])
    assert await completion.publish(event)
    assert len(outbox) == 1
    assert events == [{"conv": "session:target", "type": "message", "text": "结果副本"}]
    assert legacy_pushes == []


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
async def test_notification_switch_applies_to_every_completion(outbox, monkeypatch, status):
    monkeypatch.setattr(config, "PUSH_ON_DONE", False)
    assert not await completion.publish(completion.Completion("session:muted", status, "结果"))
    assert outbox == []


@pytest.mark.anyio
@pytest.mark.parametrize("is_error", [False, True])
async def test_background_followup_does_not_replay_old_schedule(outbox, isolated, monkeypatch, is_error):
    row = tasks.create("旧定时执行", "检查", cwd=str(isolated), origin="cron", job_id="old-rule")
    tasks.set_status(row["id"], "running")
    tasks.finish(row["id"], "done", "上次结果", "上次结果")
    policies = []
    monkeypatch.setattr(completion, "_policy", policies.append)

    async def stream(*args, **kwargs):
        yield Done(AgentReply("续聊结果", [], None, is_error, error="失败" if is_error else ""))

    monkeypatch.setattr(task_runner, "stream_turn", stream)
    assert await task_runner.append(row["id"], "继续")
    await task_runner._running[row["id"]]
    assert policies == []
    assert len(outbox) == 1
    assert outbox[0]["kind"] == ("error" if is_error else "done")


@pytest.mark.anyio
async def test_background_followup_initialization_failure_does_not_replay_schedule(outbox, monkeypatch):
    row = tasks.create("旧执行", "检查", origin="cron", job_id="old-rule")
    policies = []
    monkeypatch.setattr(completion, "_policy", policies.append)

    async def fail(*args, **kwargs):
        raise RuntimeError("初始化失败")

    monkeypatch.setattr(task_runner, "_run", fail)
    running = task_runner._spawn(row["id"], "人工续聊")
    tasks.set_status(row["id"], "running")
    task_runner._running[row["id"]] = running
    with pytest.raises(RuntimeError, match="初始化失败"):
        await running
    await asyncio.sleep(.05)
    assert policies == []
    assert len(outbox) == 1
    assert outbox[0]["kind"] == "error"


@pytest.mark.anyio
async def test_delivery_failure_keeps_result_and_reports_unconfirmed(outbox, monkeypatch):
    key = "session:delivery-failed"
    tid = session_store.append(key, "问题", "已保存结果")

    async def fail(**payload):
        raise RuntimeError("通知通道故障")

    monkeypatch.setattr(PUSH, "notify", fail)
    assert not await completion.publish(completion.Completion(key, "done", "已保存结果", tid))
    assert session_store.load_history(key)[-1]["assistant"] == "已保存结果"


@pytest.mark.anyio
async def test_unread_write_failure_does_not_swallow_notification(outbox, monkeypatch):
    def fail(*args):
        raise RuntimeError("未读标记不可写")

    monkeypatch.setattr(session_store, "set_pending_review", fail)
    assert await completion.publish(completion.Completion("session:unread-failed", "done", "结果"))
    assert len(outbox) == 1


@pytest.mark.anyio
async def test_no_recipient_is_not_confirmed_delivery(outbox, monkeypatch):
    async def no_recipient(**payload):
        return 0

    monkeypatch.setattr(PUSH, "notify", no_recipient)
    assert not await completion.publish(completion.Completion("session:no-device", "done", "结果"))


@pytest.mark.anyio
async def test_push_failure_does_not_block_other_result_targets(outbox, monkeypatch):
    copies = []

    async def fail(**payload):
        raise RuntimeError("Web Push 不可用")

    async def copy(platform, chat_id, text):
        copies.append((platform, chat_id, text))

    monkeypatch.setattr(PUSH, "notify", fail)
    monkeypatch.setattr(completion, "_platform_push", copy)
    event = completion.Completion("session:fallback", "done", "结果", copies=[("telegram", "123", "结果")])
    assert not await completion.publish(event)
    assert copies == [("telegram", "123", "结果")]


@pytest.mark.anyio
async def test_foreground_initialization_failure_does_not_replay_old_schedule(outbox, monkeypatch):
    row = tasks.create("旧定时执行", "检查", origin="cron", job_id="old-rule",
                       dispatch_platform="web", dispatch_chat_id="session:old-target")
    policies = []
    monkeypatch.setattr(completion, "_policy", policies.append)
    with pytest.raises(RuntimeError, match="初始化失败"):
        async with task_runner.foreground_execution(row["id"]):
            raise RuntimeError("初始化失败")
    assert tasks.get(row["id"])["status"] == "failed"
    assert policies == []
    assert len(outbox) == 1
    assert outbox[0]["kind"] == "error"


@pytest.mark.anyio
@pytest.mark.parametrize("outcomes,accepted", [
    (["ok"], 1), (["timeout"], 0), (["503"], 0), (["410"], 0), (["timeout", "ok"], 1),
])
async def test_real_push_receipt_counts_only_gateway_acceptance(outbox, isolated, monkeypatch, outcomes, accepted):
    from types import SimpleNamespace
    from vococo.gateway.adapters import web_push

    class PushError(Exception):
        def __init__(self, status):
            self.response = SimpleNamespace(status_code=int(status))

    def send(**kwargs):
        outcome = kwargs["subscription_info"]["endpoint"]
        if outcome == "timeout":
            raise TimeoutError("网关超时")
        if outcome != "ok":
            raise PushError(outcome)

    manager = web_push.PushManager()
    manager._path = isolated / "subscriptions.json"
    manager._subs = [{"endpoint": outcome} for outcome in outcomes]
    monkeypatch.setattr(manager, "is_configured", lambda: True)
    monkeypatch.setattr(web_push, "_cfg", lambda: SimpleNamespace(VAPID_PRIVATE_KEY="test", VAPID_SUBJECT="test"))
    monkeypatch.setattr(web_push, "WebPushException", PushError)
    monkeypatch.setattr(web_push, "webpush", send)
    monkeypatch.setattr(PUSH, "notify", manager.notify)
    assert await completion.publish(completion.Completion("session:receipt", "done", "结果")) == bool(accepted)
    assert all(sub["endpoint"] != "410" for sub in manager._subs)


@pytest.mark.anyio
async def test_muted_script_is_not_acknowledged_without_delivery(outbox, monkeypatch):
    monkeypatch.setattr(config, "PUSH_ON_DONE", False)
    job = scheduler.create_job(name="巡检", prompt="检查", schedule={"kind": "interval", "minutes": 60})
    assert await scheduler._push_job_result(job["id"], "success", "待补播结果", None, run_id="pending") is None
    assert outbox == []
