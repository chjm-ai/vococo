"""真实 SDK 回执样本回归；不访问网络、不接触生产任务表。"""
import asyncio

import pytest

from vococo.core.checklist import ChecklistTracker, ChecklistUpdated
from vococo.core.timeline import Timeline


def apply(tracker, name, data, response, *, error=False, parent=None):
    tracker.remember("call", name, data, parent)
    return tracker.finish("call", response, error)


def created(tracker):
    return apply(tracker, "TaskCreate", {"subject": "协议测试甲"},
                 "Task #1 created successfully: 协议测试甲")


def test_real_sdk_task_id_camelcase_lifecycle():
    tracker = ChecklistTracker()
    assert created(tracker)[0]["status"] == "pending"
    for status in ("in_progress", "completed"):
        items = apply(tracker, "TaskUpdate", {"taskId": "1", "status": status}, "Updated task #1 status")
        assert items[0]["status"] == status
    assert apply(tracker, "TaskUpdate", {"taskId": "1", "status": "deleted"}, "Updated task #1 deleted") == []


def test_only_successful_results_commit_and_snapshots_are_immutable():
    tracker = ChecklistTracker()
    first = created(tracker)
    tracker.remember("update", "TaskUpdate", {"taskId": "1", "status": "completed"})
    assert tracker.items["1"]["status"] == "pending"
    assert tracker.finish("update", "failure", True) is None
    assert tracker.items["1"]["status"] == "pending"
    assert tracker.finish("update", "Updated task #1 status", False) is None
    apply(tracker, "TaskUpdate", {"taskId": "1", "status": "completed"}, "Updated task #1 status")
    assert first[0]["status"] == "pending"


@pytest.mark.parametrize("data,response", [
    ({"task_id": "1", "status": "completed"}, "Updated task #1 status"),
    ({"taskId": "1", "status": "completed"}, "Task not found"),
    ({"taskId": "1", "status": "completed"}, "Updated task #10 status"),
    ({"taskId": "1", "status": "unknown"}, "Updated task #1 status"),
    ({"taskId": "999", "status": "completed"}, "Updated task #999 status"),
])
def test_unknown_protocol_does_not_fabricate_state(data, response):
    tracker = ChecklistTracker()
    created(tracker)
    assert apply(tracker, "TaskUpdate", data, response) is None
    assert tracker.items["1"]["status"] == "pending"


def test_reads_reconcile_state_and_reject_partial_lists():
    tracker = ChecklistTracker()
    items = apply(tracker, "TaskList", {}, "#1 [in_progress] 协议测试甲\n#2 [pending] 任务乙")
    assert len(items) == 2
    assert apply(tracker, "TaskList", {}, "#1 [completed] 协议测试甲\nunknown format") is None
    assert tracker.items["1"]["status"] == "in_progress"
    items = apply(tracker, "TaskGet", {"taskId": "1"},
                  "Task #1: 协议测试甲\nStatus: completed\nDescription: 测试状态同步")
    assert items[0]["status"] == "completed"
    assert apply(tracker, "TaskList", {}, "No tasks found") == []


def test_parallel_calls_and_child_checklists_do_not_mix():
    tracker = ChecklistTracker()
    tracker.remember("a", "TaskCreate", {"subject": "甲"})
    tracker.remember("b", "TaskCreate", {"subject": "乙"})
    tracker.remember("child", "TaskCreate", {"subject": "子任务"}, "agent-one")
    assert tracker.finish("child", "Task #1 created successfully: 子任务", False) is None
    tracker.finish("b", "Task #2 created successfully: 乙", False)
    tracker.finish("a", "Task #1 created successfully: 甲", False)
    assert {i["subject"] for i in tracker.items.values()} == {"甲", "乙"}


def test_todowrite_full_snapshots_clear_and_validate():
    tracker = ChecklistTracker()
    items = apply(tracker, "TodoWrite", {"todos": [{"content": "旧版清单", "status": "in_progress"}]}, "ok")
    assert items[0]["subject"] == "旧版清单"
    assert apply(tracker, "TodoWrite", {"todos": [{"content": "bad", "status": "unknown"}]}, "ok") is None
    assert apply(tracker, "TodoWrite", {"todos": []}, "ok") == []


def test_timeline_keeps_one_snapshot_even_after_tool_cap():
    timeline = Timeline()
    for index in range(Timeline.MAX_BLOCKS):
        timeline.tool_started("Bash", str(index), None)
    timeline.checklist([{"subject": "未完成", "status": "in_progress", "id": "1"}], "sdk")
    timeline.checklist([], "sdk")
    assert [b for b in timeline.blocks if b["type"] == "checklist"] == [
        {"type": "checklist", "items": [], "session_id": "sdk"},
    ]


def test_checkpoint_persists_refresh_cancel_and_cross_turn_resume(isolated):
    from vococo.core.session_execution import SharedTurn
    from vococo.gateway.adapters.web import _WebSink
    from vococo.memory import session_store

    class Adapter:
        def __init__(self):
            self.sent = []

        def _emit(self, event):
            self.sent.append(event)

    adapter = Adapter()
    session = "web:checklist-test"
    turn_id = session_store.start_turn(session, "开始")
    turn = SharedTurn(session, turn_id, _WebSink(adapter, "checklist-test"))
    items = [{"id": "1", "subject": "未完成步骤", "status": "in_progress"}]
    asyncio.run(turn.observe(ChecklistUpdated(items, "sdk-one")))
    assert adapter.sent[-1] == {"type": "checklist", "conv": "checklist-test", "items": items}
    assert session_store.load_history(session)[0]["events"][-1]["items"] == items
    assert session_store.load_checklist("web:other", "sdk-one") == []
    assert session_store.load_checklist(session, "sdk-other") == []
    session_store.finish_turn(turn_id, "已中断", events=turn.timeline.blocks, session_key=session)
    session_store.start_turn(session, "继续")
    tracker = ChecklistTracker(session_store.load_checklist(session, "sdk-one"))
    assert tracker.items["1"]["status"] == "in_progress"
    assert apply(tracker, "TaskUpdate", {"taskId": "1", "status": "completed"}, "Updated task #1 status")[0]["status"] == "completed"
    # 空清单也必须覆盖旧快照，不能复活已经删除的条目。
    latest = session_store.start_turn(session, "删除")
    session_store.flush_events(session, latest, [{"type": "checklist", "items": [], "session_id": "sdk-one"}])
    assert session_store.load_checklist(session, "sdk-one") == []


def test_restart_and_regenerate_preserve_sdk_state(isolated):
    from vococo.memory import session_store, _db

    session = "web:restart-checklist"
    turn_id = session_store.start_turn(session, "执行")
    snapshot = {"type": "checklist", "session_id": "sdk", "items": [
        {"id": "1", "subject": "仍在 SDK 内的任务", "status": "in_progress"},
    ]}
    session_store.flush_events(session, turn_id, [snapshot])
    _db.reset()  # 模拟进程重开数据库连接。
    recovered = session_store.recover_interrupted_turns()
    assert recovered[0]["turn_id"] == turn_id
    assert session_store.load_history(session)[0]["events"] == [snapshot, {"type": "interrupted"}]
    assert session_store.load_checklist(session, "sdk") == snapshot["items"]
    assert session_store.delete_last_turn(session, turn_id) == "执行"
    assert session_store.load_history(session) == []
    tracker = ChecklistTracker(session_store.load_checklist(session, "sdk"))
    completed = apply(tracker, "TaskUpdate", {"taskId": "1", "status": "completed"}, "Updated task #1 status")
    assert completed[0]["status"] == "completed"
    session_store.clear(session)
    assert session_store.load_checklist(session, "sdk") == []


def test_live_reconnect_keeps_only_latest_snapshot():
    from vococo.gateway.adapters.web import WebAdapter

    adapter = WebAdapter.__new__(WebAdapter)
    adapter._live = {}
    adapter._track_live({"conv": "one", "type": "start"}, 1)
    for seq, status in enumerate(("pending", "in_progress", "completed"), 2):
        adapter._track_live({"conv": "one", "type": "checklist", "items": [
            {"id": "1", "subject": "步骤", "status": status},
        ]}, seq)
    frames = adapter._live["one"]["frames"]
    assert len(frames) == 2
    assert frames[-1][1]["items"][0]["status"] == "completed"
    adapter._track_live({"conv": "one", "type": "done"}, 5)
    assert adapter._live == {}


def test_late_checkpoint_cannot_resurrect_deleted_turn(isolated):
    from vococo.memory import session_store

    session = "web:deleted-checklist"
    turn_id = session_store.start_turn(session, "执行")
    session_store.cancel_turn(turn_id, session_key=session)
    snapshot = {"type": "checklist", "session_id": "sdk", "items": [
        {"id": "1", "subject": "过期结果", "status": "completed"},
    ]}
    session_store.flush_events(session, turn_id, [snapshot])
    assert session_store.load_checklist(session, "sdk") == []
