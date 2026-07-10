"""P1 任务板的测试(见 docs/design/voice-companion/02-phase1-task-board.md §5)。

覆盖:tasks CRUD 与状态机(含非法迁移)、执行器(进度节流/终态/超时/取消/并发排队)、
三个工具的 schema 与行为、重启自愈、通知分发(在线 SSE / 离线 web_push)。
"""
from __future__ import annotations

import asyncio

import pytest

from claude_hermes import config
from claude_hermes.core.agent import AgentReply, Done, ToolInput
from claude_hermes.gateway import core as gateway_core
from claude_hermes.memory import session_store
from claude_hermes.voice import executor, notify, session, task_tools, tasks, tts


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def voice_db(isolated, monkeypatch):
    """同 test_voice_p0.py 的 voice_db:独立 db + 清空鉴权 + 重置模块级单例。

    session 模块委托 session_store 存储,重置由 `isolated` fixture 代劳。
    """
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(tasks, "_DB", None)
    executor._running.clear()
    notify._subscribers.clear()
    yield
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None


async def _noop_coro(*_a, **_k) -> None:
    return None


async def _bytes_coro(b: bytes) -> bytes:
    return b


# ── tasks.py:CRUD + 状态机 ───────────────────────────────────────────────
def test_create_defaults_to_queued(voice_db):
    t = tasks.create("标题", "prompt 内容")
    assert t["status"] == "queued"
    assert t["progress_note"] == ""
    assert tasks.get(t["id"])["title"] == "标题"


def test_get_latest_returns_most_recently_created(voice_db):
    tasks.create("A", "p1")
    b = tasks.create("B", "p2")
    assert tasks.get_latest()["id"] == b["id"]


def test_set_status_allows_legal_transition(voice_db):
    t = tasks.create("A", "p")
    assert tasks.set_status(t["id"], "running") is True
    assert tasks.get(t["id"])["status"] == "running"


def test_set_status_rejects_illegal_transition(voice_db):
    t = tasks.create("A", "p")
    assert tasks.set_status(t["id"], "done") is False  # queued 不能跳过 running 直达 done
    assert tasks.get(t["id"])["status"] == "queued"


def test_set_status_rejects_transition_out_of_terminal_state(voice_db):
    t = tasks.create("A", "p")
    tasks.set_status(t["id"], "running")
    tasks.finish(t["id"], "done", "结果", "摘要")
    assert tasks.set_status(t["id"], "running") is False
    assert tasks.get(t["id"])["status"] == "done"


def test_finish_rejects_non_terminal_status(voice_db):
    t = tasks.create("A", "p")
    tasks.set_status(t["id"], "running")
    assert tasks.finish(t["id"], "running", "x", "y") is False


def test_count_running_and_list_queued(voice_db):
    a = tasks.create("A", "p")
    tasks.create("B", "p")
    tasks.set_status(a["id"], "running")
    assert tasks.count_running() == 1
    queued = tasks.list_queued()
    assert len(queued) == 1 and queued[0]["title"] == "B"


def test_snapshot_for_prompt_empty_board(voice_db):
    out = tasks.snapshot_for_prompt()
    assert "任务板是空的" in out


def test_snapshot_for_prompt_lists_active_and_recent_done(voice_db):
    a = tasks.create("查日志", "p")
    tasks.set_status(a["id"], "running", progress_note="正在读文件")
    tasks.create("排队活", "p")
    c = tasks.create("已完活", "p")
    tasks.set_status(c["id"], "running")
    tasks.finish(c["id"], "done", "完整结果", "一句话摘要")
    out = tasks.snapshot_for_prompt()
    assert "「查日志」进行中" in out and "正在读文件" in out
    assert "「排队活」排队中" in out
    assert "「已完活」已完成:一句话摘要" in out


def test_build_prompt_injects_task_snapshot(voice_db):
    from claude_hermes.voice import prompts

    t = tasks.create("查资料", "p")
    tasks.set_status(t["id"], "running", progress_note="正在查")
    out = prompts.build_prompt("那个任务怎么样了")
    assert "【任务板快照】" in out
    assert "「查资料」进行中" in out
    # 防虚构硬规则也要在场
    assert "绝不能宣称" in out


def test_mark_orphans_failed_covers_queued_and_running(voice_db):
    a = tasks.create("A", "p")
    tasks.set_status(a["id"], "running")
    b = tasks.create("B", "p")  # 仍是 queued
    orphans = tasks.mark_orphans_failed()
    assert {o["id"] for o in orphans} == {a["id"], b["id"]}
    assert tasks.get(a["id"])["status"] == "failed"
    assert tasks.get(a["id"])["progress_note"] == "服务重启,任务中断"
    assert tasks.get(b["id"])["status"] == "failed"
    # 幂等:再调一次不该把 done/failed 的任务再翻出来
    assert tasks.mark_orphans_failed() == []


# ── executor.py:进度节流 / 终态 / 超时 / 取消 / 并发排队 ──────────────────
@pytest.mark.anyio
async def test_dispatch_runs_immediately_and_writes_done(voice_db, monkeypatch):
    async def fake_stream_turn(history, prompt, cwd=None, session_key=None, **kw):
        yield Done(AgentReply(text="任务的结果", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(executor, "stream_turn", fake_stream_turn)
    notified = []

    async def fake_notify(task_id):
        notified.append(task_id)

    monkeypatch.setattr(notify, "on_task_terminal", fake_notify)

    task = executor.dispatch("标题", "prompt")
    assert tasks.get(task["id"])["status"] == "running"  # 并发未满,立即起跑
    await executor._running[task["id"]]

    row = tasks.get(task["id"])
    assert row["status"] == "done"
    assert row["result_full"] == "任务的结果"
    assert notified == [task["id"]]


@pytest.mark.anyio
async def test_dispatch_persists_full_conversation_to_shared_session_store(
    voice_db, monkeypatch
):
    """任务的完整对话(不只是 result_full 摘要)要落进跟普通对话共用的
    session_store,session_key=voice-task:{id}——这样侧边栏"语音任务"分组才能
    像回看普通对话一样回看它。"""

    async def fake_stream_turn(history, prompt, cwd=None, session_key=None, **kw):
        yield Done(
            AgentReply(
                text="任务的结果", tool_calls=[], cost_usd=None, is_error=False,
                sdk_session_id="sdk-abc",
            )
        )

    monkeypatch.setattr(executor, "stream_turn", fake_stream_turn)
    monkeypatch.setattr(notify, "on_task_terminal", _noop_coro)

    task = executor.dispatch("标题", "帮我查一下天气")
    await executor._running[task["id"]]

    session_key = f"voice-task:{task['id']}"
    history = session_store.load_recent(session_key)
    assert len(history) == 1
    assert history[0].user == "帮我查一下天气"
    assert history[0].assistant == "任务的结果"
    assert session_store.get_sdk_session_id(session_key) == "sdk-abc"


@pytest.mark.anyio
async def test_task_session_stays_conversable_after_terminal_status(voice_db, monkeypatch):
    """任务跑完(终态)之后,用户还应该能对着同一个 session_key 继续发文字追问——
    续聊完全不碰 tasks.py 的状态机,只是走跟普通聊天一样的 converse()。"""

    async def fake_stream_turn(history, prompt, cwd=None, session_key=None, **kw):
        yield Done(
            AgentReply(
                text="今天晴天", tool_calls=[], cost_usd=None, is_error=False,
                sdk_session_id="sdk-1",
            )
        )

    monkeypatch.setattr(executor, "stream_turn", fake_stream_turn)
    monkeypatch.setattr(notify, "on_task_terminal", _noop_coro)

    task = executor.dispatch("标题", "查天气")
    await executor._running[task["id"]]
    assert tasks.get(task["id"])["status"] == "done"  # 已是终态

    session_key = f"voice-task:{task['id']}"

    async def fake_stream_turn_followup(history, user_text, **kw):
        assert len(history) == 1  # 续聊能读到派发那一轮的历史
        yield Done(
            AgentReply(
                text="明天也是晴天", tool_calls=[], cost_usd=None, is_error=False,
                sdk_session_id="sdk-2",
            )
        )

    monkeypatch.setattr(gateway_core, "stream_turn", fake_stream_turn_followup)
    reply = await gateway_core.converse(session_key, "那明天呢", None, gateway_core.Sink())

    assert reply is not None
    assert reply.text == "明天也是晴天"
    # 终态没被续聊碰过——状态机跟对话能力是两回事
    assert tasks.get(task["id"])["status"] == "done"
    history = session_store.load_recent(session_key)
    assert len(history) == 2
    assert history[-1].user == "那明天呢"
    assert history[-1].assistant == "明天也是晴天"


@pytest.mark.anyio
async def test_progress_note_updates_from_tool_input_throttled(voice_db, monkeypatch):
    async def fake_stream_turn(history, prompt, cwd=None, session_key=None, **kw):
        yield ToolInput(name="Bash", tool_id="t1", tool_input={"command": "grep foo"}, parent_id=None)
        # 子代理内部的工具(parent_id 非空)不算顶层动作,不该覆盖 progress_note
        yield ToolInput(name="Read", tool_id="t2", tool_input={"file_path": "/tmp/x.log"}, parent_id="agent-1")
        # 同一轮内紧跟着的第二次顶层工具调用:5s 节流窗口内,应被跳过
        yield ToolInput(name="Grep", tool_id="t3", tool_input={"pattern": "TODO"}, parent_id=None)
        yield Done(AgentReply(text="done", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(executor, "stream_turn", fake_stream_turn)
    monkeypatch.setattr(notify, "on_task_terminal", _noop_coro)

    task = executor.dispatch("标题", "prompt")
    await executor._running[task["id"]]

    assert tasks.get(task["id"])["progress_note"] == "正在执行:grep foo"


@pytest.mark.anyio
async def test_task_timeout_marks_failed(voice_db, monkeypatch):
    monkeypatch.setattr(config, "VOICE_TASK_TIMEOUT_MIN", 0.001)  # ≈0.06s,秒超时

    async def fake_stream_turn(history, prompt, cwd=None, session_key=None, **kw):
        await asyncio.sleep(1)
        yield Done(AgentReply(text="不会跑到这", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(executor, "stream_turn", fake_stream_turn)
    monkeypatch.setattr(notify, "on_task_terminal", _noop_coro)

    task = executor.dispatch("标题", "prompt")
    await executor._running[task["id"]]

    row = tasks.get(task["id"])
    assert row["status"] == "failed"
    assert "超时" in row["result_summary"]


@pytest.mark.anyio
async def test_cancel_running_task_sets_cancelled(voice_db, monkeypatch):
    started = asyncio.Event()

    async def fake_stream_turn(history, prompt, cwd=None, session_key=None, **kw):
        started.set()
        await asyncio.sleep(10)
        yield Done(AgentReply(text="不会跑到这", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(executor, "stream_turn", fake_stream_turn)
    monkeypatch.setattr(notify, "on_task_terminal", _noop_coro)

    task = executor.dispatch("标题", "prompt")
    await started.wait()
    assert executor.cancel(task["id"]) is True
    await executor._running[task["id"]]

    assert tasks.get(task["id"])["status"] == "cancelled"


def test_cancel_queued_task_directly(voice_db):
    t = tasks.create("A", "p")
    assert executor.cancel(t["id"]) is True
    assert tasks.get(t["id"])["status"] == "cancelled"


@pytest.mark.anyio
async def test_dispatch_queues_beyond_concurrency_limit(voice_db, monkeypatch):
    monkeypatch.setattr(config, "VOICE_TASK_MAX_CONCURRENCY", 1)
    gate = asyncio.Event()

    async def fake_stream_turn(history, prompt, cwd=None, session_key=None, **kw):
        await gate.wait()
        yield Done(AgentReply(text="ok", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(executor, "stream_turn", fake_stream_turn)
    monkeypatch.setattr(notify, "on_task_terminal", _noop_coro)

    a = executor.dispatch("A", "p")
    b = executor.dispatch("B", "p")
    assert tasks.get(a["id"])["status"] == "running"
    assert tasks.get(b["id"])["status"] == "queued"

    handle_a = executor._running[a["id"]]
    gate.set()
    await handle_a
    # gate 已经 set,A 跑完后 _maybe_start_next() 拉起的 B 在同一拍事件循环里就跟着
    # 跑完了(mock 的 gate.wait() 不会真正挂起),所以这里直接断言终态即可,不必
    # 再抓一次 _running 里的 handle。
    assert tasks.get(a["id"])["status"] == "done"
    assert tasks.get(b["id"])["status"] == "done"


@pytest.mark.anyio
async def test_summarize_short_text_passthrough(voice_db):
    assert await executor._summarize("短结果") == "短结果"


@pytest.mark.anyio
async def test_summarize_strips_markdown_before_speaking(voice_db):
    # 后台任务没有 P0 语音人设的"禁止 markdown"规则,模型常带 `代码`/**加粗**;
    # 这段文字最终要被朗读,得先摘掉这些没法读的符号(不止 50 字才走的截断路径也要摘)。
    out = await executor._summarize("完成,共有 **31** 个 `.py` 文件。")
    assert "*" not in out and "`" not in out


@pytest.mark.anyio
async def test_summarize_falls_back_to_truncation_on_llm_failure(voice_db, monkeypatch):
    async def boom(history, user_text, model=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(executor, "run_turn", boom)
    out = await executor._summarize("字" * 100)
    assert out.endswith("…")
    assert len(out) <= 50


@pytest.mark.anyio
async def test_heal_after_restart_notifies_each_orphan(voice_db, monkeypatch):
    t = tasks.create("标题", "p")
    tasks.set_status(t["id"], "running")
    notified = []

    async def fake_notify(task_id):
        notified.append(task_id)

    monkeypatch.setattr(notify, "on_task_terminal", fake_notify)
    await executor.heal_after_restart()

    assert notified == [t["id"]]
    assert tasks.get(t["id"])["status"] == "failed"


# ── task_tools.py:三个工具的 schema 与行为 ─────────────────────────────────
def test_dispatch_tool_schema_requires_title_and_prompt():
    t = task_tools.voice_dispatch_task
    assert t.name == "voice_dispatch_task"
    assert set(t.input_schema["required"]) == {"title", "prompt"}
    assert "cwd" in t.input_schema["properties"]


def test_query_tool_schema_has_optional_task_id():
    t = task_tools.voice_query_task
    assert t.name == "voice_query_task"
    assert "task_id" in t.input_schema["properties"]
    assert not t.input_schema.get("required")


def test_list_tool_schema_takes_no_params():
    t = task_tools.voice_list_tasks
    assert t.name == "voice_list_tasks"
    assert t.input_schema["properties"] == {}


@pytest.mark.anyio
async def test_dispatch_tool_rejects_empty_fields():
    result = await task_tools.voice_dispatch_task.handler({"title": "", "prompt": ""})
    assert "需要" in result["content"][0]["text"]


@pytest.mark.anyio
async def test_dispatch_tool_dispatches_and_reports_task_id(voice_db, monkeypatch):
    async def fake_stream_turn(history, prompt, cwd=None, session_key=None, **kw):
        yield Done(AgentReply(text="ok", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(executor, "stream_turn", fake_stream_turn)
    monkeypatch.setattr(notify, "on_task_terminal", _noop_coro)

    result = await task_tools.voice_dispatch_task.handler({"title": "标题", "prompt": "内容"})
    text = result["content"][0]["text"]
    assert "task_id=" in text and "标题" in text
    assert tasks.get_latest()["title"] == "标题"


@pytest.mark.anyio
async def test_query_tool_reports_latest_when_id_omitted(voice_db):
    tasks.create("查询目标", "prompt")
    result = await task_tools.voice_query_task.handler({})
    assert "查询目标" in result["content"][0]["text"]


@pytest.mark.anyio
async def test_query_tool_reports_missing_task(voice_db):
    result = await task_tools.voice_query_task.handler({"task_id": "no-such-id"})
    assert "没有找到" in result["content"][0]["text"]


@pytest.mark.anyio
async def test_list_tool_lists_recent_tasks(voice_db):
    tasks.create("任务甲", "p1")
    tasks.create("任务乙", "p2")
    result = await task_tools.voice_list_tasks.handler({})
    text = result["content"][0]["text"]
    assert "任务甲" in text and "任务乙" in text


# ── notify.py:在线走 SSE 事件,离线走 web_push ──────────────────────────────
@pytest.mark.anyio
async def test_notify_broadcasts_sse_event_when_online(voice_db, monkeypatch):
    t = tasks.create("标题", "p")
    tasks.set_status(t["id"], "running")
    tasks.finish(t["id"], "done", "完整结果", "一句话摘要")
    monkeypatch.setattr(tts, "synthesize", lambda text, voice: _bytes_coro(b"AUDIO"))

    q = notify.subscribe()
    try:
        await notify.on_task_terminal(t["id"])
        event, payload = q.get_nowait()
    finally:
        notify.unsubscribe(q)

    assert event == "task_done"
    assert payload["id"] == t["id"]
    assert payload["result_summary"] == "一句话摘要"
    assert payload["audio_b64"]


@pytest.mark.anyio
async def test_notify_falls_back_to_web_push_when_offline(voice_db, monkeypatch):
    t = tasks.create("标题", "p")
    tasks.set_status(t["id"], "running")
    tasks.finish(t["id"], "failed", "", "出错了")

    from claude_hermes.gateway.adapters import web_push

    calls = []

    async def fake_notify(**kwargs):
        calls.append(kwargs)
        return 1

    monkeypatch.setattr(web_push.PUSH, "notify", fake_notify)
    assert notify.is_online() is False

    await notify.on_task_terminal(t["id"])

    assert len(calls) == 1
    assert calls[0]["title"] == "任务完成:标题"
    assert calls[0]["body"] == "出错了"
