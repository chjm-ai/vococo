"""P1 任务板的测试(见 docs/design/voice-companion/02-phase1-task-board.md §5)。

覆盖:tasks CRUD 与状态机(含非法迁移)、执行器(进度节流/终态/超时/取消/并发排队)、
三个工具的 schema 与行为、重启自愈、通知分发(在线 SSE / 离线 web_push)。
"""
from __future__ import annotations

import asyncio

import pytest

from claude_hermes import config
from claude_hermes.core import task_runner as executor
from claude_hermes.core import tasks
from claude_hermes.core.agent import AgentReply, Done, SessionStarted, ToolInput
from claude_hermes.gateway import core as gateway_core
from claude_hermes.memory import session_store
from claude_hermes.voice import notify, session, task_tools, tts


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


def test_set_status_allows_reopen_from_terminal_state(voice_db):
    """终态 → running 是追问重开通道(见 executor.append),但状态机仍然不许瞎跳
    (如 queued 直接到 done,跳过 running)。"""
    t = tasks.create("A", "p")
    tasks.set_status(t["id"], "running")
    tasks.finish(t["id"], "done", "结果", "摘要")
    assert tasks.set_status(t["id"], "running") is True
    assert tasks.get(t["id"])["status"] == "running"

    t2 = tasks.create("B", "p")
    assert tasks.set_status(t2["id"], "done") is False


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


def test_mark_orphans_failed_skips_excluded_ids(voice_db):
    """exclude_ids 里的活任务绝不能标死(2026-07-12 假失败事故的防线)。"""
    alive = tasks.create("活任务", "p")
    tasks.set_status(alive["id"], "running")
    dead = tasks.create("真孤儿", "p")
    tasks.set_status(dead["id"], "running")
    orphans = tasks.mark_orphans_failed(exclude_ids={alive["id"]})
    assert {o["id"] for o in orphans} == {dead["id"]}
    assert tasks.get(alive["id"])["status"] == "running"
    assert tasks.get(dead["id"])["status"] == "failed"


def test_finish_corrects_false_failed_back_to_done(voice_db):
    """failed → done 纠错通道:任务被外部误标失败后,executor 如实收尾仍能写回真结局
    (2026-07-12 事故:活干完了、代码提交了,任务板却永远停在"失败")。"""
    t = tasks.create("被误标", "p")
    tasks.set_status(t["id"], "running")
    tasks.mark_orphans_failed()  # 模拟另一进程的孤儿回收误标
    assert tasks.get(t["id"])["status"] == "failed"
    assert tasks.finish(t["id"], "done", "完整成果", "一句话摘要") is True
    row = tasks.get(t["id"])
    assert row["status"] == "done"
    assert row["result_summary"] == "一句话摘要"
    # 纠错只开 failed→done 这一条:done 之后不许再翻回失败
    assert tasks.finish(t["id"], "failed", "", "又失败") is False


def test_set_progress_keeps_terminal_evidence(voice_db):
    """终态任务的 progress_note 是失败原因/最后现场,迟到的进度更新不许覆盖。"""
    t = tasks.create("已死任务", "p")
    tasks.set_status(t["id"], "running")
    tasks.mark_orphans_failed()
    assert tasks.get(t["id"])["progress_note"] == "服务重启,任务中断"
    tasks.set_progress(t["id"], "正在执行:git commit")  # 迟到的进度心跳
    assert tasks.get(t["id"])["progress_note"] == "服务重启,任务中断"


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
    session_store,session_key=task:{id}——这样侧边栏"语音任务"分组才能
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

    session_key = f"task:{task['id']}"
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

    session_key = f"task:{task['id']}"

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
    monkeypatch.setattr(config, "TASK_TIMEOUT_MIN", 0.001)  # ≈0.06s,秒超时

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


# ── voice_append_task:原地续在同一个任务上,不产生新任务 ────────────────────


@pytest.mark.anyio
async def test_append_on_terminal_task_resumes_same_task_no_new_row(voice_db, monkeypatch):
    """追问撞上已经跑完的任务:原地续聊,resume 回同一条 SDK 会话,只发这一轮的话
    (不重新拼全部历史),且自始至终只有一条任务行。"""
    seen = []

    async def fake_first(history, prompt, cwd=None, session_key=None, **kw):
        seen.append({"prompt": prompt, "resume": kw.get("resume")})
        yield Done(AgentReply(text="结果", tool_calls=[], cost_usd=None, is_error=False,
                               sdk_session_id="sdk-first"))

    monkeypatch.setattr(executor, "stream_turn", fake_first)
    monkeypatch.setattr(notify, "on_task_terminal", _noop_coro)

    task = executor.dispatch("标题", "先做 A")
    await executor._running[task["id"]]
    assert tasks.get(task["id"])["status"] == "done"

    async def fake_followup(history, prompt, cwd=None, session_key=None, **kw):
        seen.append({"prompt": prompt, "resume": kw.get("resume")})
        yield Done(AgentReply(text="结果2", tool_calls=[], cost_usd=None, is_error=False,
                               sdk_session_id="sdk-second"))

    monkeypatch.setattr(executor, "stream_turn", fake_followup)
    result = await executor.append(task["id"], "再做 B")
    assert result["ok"] is True
    assert result["task"]["id"] == task["id"]  # 同一个 task_id,没派生新任务
    await executor._running[task["id"]]

    assert tasks.get(task["id"])["status"] == "done"
    assert len(tasks.list_recent(10)) == 1
    # 这一轮只发追加指令本身(不重新拼全部历史),外加固定的摘要标记指令(见
    # _SUMMARY_TAG_INSTRUCTION)——不是历史被重新拼进去了。
    assert seen[1]["prompt"] == "再做 B" + executor._SUMMARY_TAG_INSTRUCTION
    assert seen[1]["resume"] == "sdk-first"  # resume 回第一轮的 SDK 会话


@pytest.mark.anyio
async def test_append_on_running_task_interrupts_and_resumes_same_session(voice_db, monkeypatch):
    """追问撞上还在跑的任务:打断当前这轮,重开时 resume 回同一条 SDK 会话——
    session_id 在 SessionStarted 阶段就已提前存回 session_store(不用等 Done),
    所以哪怕这一轮被打断,下一轮依然接得上,不会丢上下文重开对话。"""
    started = asyncio.Event()

    async def fake_hangs(history, prompt, cwd=None, session_key=None, **kw):
        yield SessionStarted(session_id="sdk-mid-flight")
        started.set()
        await asyncio.sleep(10)
        yield Done(AgentReply(text="不会跑到这", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(executor, "stream_turn", fake_hangs)
    monkeypatch.setattr(notify, "on_task_terminal", _noop_coro)

    task = executor.dispatch("标题", "先做 A")
    await started.wait()

    session_key = f"task:{task['id']}"
    assert session_store.get_sdk_session_id(session_key) == "sdk-mid-flight"

    seen_resume = []

    async def fake_followup(history, prompt, cwd=None, session_key=None, **kw):
        seen_resume.append(kw.get("resume"))
        yield Done(AgentReply(text="接上了", tool_calls=[], cost_usd=None, is_error=False,
                               sdk_session_id="sdk-mid-flight"))

    monkeypatch.setattr(executor, "stream_turn", fake_followup)
    result = await executor.append(task["id"], "改成 B")
    assert result["ok"] is True
    assert result["task"]["id"] == task["id"]
    await executor._running[task["id"]]

    assert tasks.get(task["id"])["status"] == "done"
    assert len(tasks.list_recent(10)) == 1  # 打断没有产生新任务
    assert seen_resume == ["sdk-mid-flight"]


@pytest.mark.anyio
async def test_append_on_queued_task_merges_prompt(voice_db):
    """追问撞上还没轮到的排队任务:还没起跑,直接把新指令并进待执行 prompt。"""
    t = tasks.create("A", "先做 A")
    assert tasks.get(t["id"])["status"] == "queued"

    result = await executor.append(t["id"], "再做 B")
    assert result["ok"] is True
    assert result["task"]["id"] == t["id"]
    row = tasks.get(t["id"])
    assert row["status"] == "queued"
    assert "先做 A" in row["prompt"] and "再做 B" in row["prompt"]
    assert len(tasks.list_recent(10)) == 1


@pytest.mark.anyio
async def test_dispatch_queues_beyond_concurrency_limit(voice_db, monkeypatch):
    monkeypatch.setattr(config, "TASK_MAX_CONCURRENCY", 1)
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


def test_summarize_short_text_passthrough(voice_db):
    assert executor._summarize("短结果") == "短结果"


def test_summarize_strips_markdown_before_speaking(voice_db):
    # 后台任务没有 P0 语音人设的"禁止 markdown"规则,模型常带 `代码`/**加粗**;
    # 这段文字最终要被朗读,得先摘掉这些没法读的符号(不止 50 字才走的截断路径也要摘)。
    out = executor._summarize("完成,共有 **31** 个 `.py` 文件。")
    assert "*" not in out and "`" not in out


def test_summarize_truncates_long_text(voice_db):
    # 摘要不再单开一次 LLM 会话压缩(见 _split_summary_tag),纯截断兜底。
    out = executor._summarize("字" * 100)
    assert out.endswith("…")
    assert len(out) <= 50


def test_split_summary_tag_extracts_and_strips(voice_db):
    text = "这是正文内容。\n\n[[SUMMARY: 一句口语总结]]"
    clean, summary = executor._split_summary_tag(text)
    assert summary == "一句口语总结"
    assert "[[SUMMARY" not in clean
    assert clean.startswith("这是正文内容。")


def test_split_summary_tag_missing_returns_none(voice_db):
    clean, summary = executor._split_summary_tag("没有标记的正文")
    assert summary is None
    assert clean == "没有标记的正文"


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


@pytest.mark.anyio
async def test_heal_after_restart_spares_tasks_alive_in_this_process(voice_db, monkeypatch):
    """heal 只回收没有执行器在管的孤儿;_running 里的活任务无论如何不能被标死。"""
    t = tasks.create("活着呢", "p")
    tasks.set_status(t["id"], "running")
    executor._running[t["id"]] = object()  # 模拟本进程正跑着它
    notified = []

    async def fake_notify(task_id):
        notified.append(task_id)

    monkeypatch.setattr(notify, "on_task_terminal", fake_notify)
    try:
        await executor.heal_after_restart()
    finally:
        executor._running.pop(t["id"], None)

    assert notified == []
    assert tasks.get(t["id"])["status"] == "running"


# ── task_tools.py:工具的 schema 与行为(voice_query_task/voice_list_tasks 已于
# 2026-07-29 合并进 voice_query_session/voice_list_sessions,同时服务后台任务和
# 网页对话两种来源,见 test_voice_web_bridge.py 里网页对话分支的用例)──────────
def test_dispatch_tool_schema_requires_title_and_prompt():
    t = task_tools.voice_dispatch_task
    assert t.name == "voice_dispatch_task"
    assert set(t.input_schema["required"]) == {"title", "prompt"}
    assert "cwd" in t.input_schema["properties"]


def test_query_tool_schema_has_optional_session_id():
    t = task_tools.voice_query_session
    assert t.name == "voice_query_session"
    assert "session_id" in t.input_schema["properties"]
    assert not t.input_schema.get("required")


def test_list_tool_schema_requires_origin():
    t = task_tools.voice_list_sessions
    assert t.name == "voice_list_sessions"
    assert set(t.input_schema["required"]) == {"origin"}


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
    # 默认 cwd 落到本项目根后,_run 会真去建 worktree——测试里挡掉,不碰真实仓库
    monkeypatch.setattr(executor.worktree, "ensure_worktree_for_task", _noop_coro)

    result = await task_tools.voice_dispatch_task.handler({"title": "标题", "prompt": "内容"})
    text = result["content"][0]["text"]
    assert "session_id=" in text and "标题" in text
    assert tasks.get_latest()["title"] == "标题"


@pytest.mark.anyio
async def test_dispatch_tool_defaults_cwd_to_project_root(voice_db, monkeypatch):
    """不传 cwd 也必须有项目目录兜底(2026-07-12:模型从不传 cwd,worktree 隔离
    形同虚设,子代理直接在主检出目录上改代码)。"""
    async def fake_stream_turn(history, prompt, cwd=None, session_key=None, **kw):
        yield Done(AgentReply(text="ok", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(executor, "stream_turn", fake_stream_turn)
    monkeypatch.setattr(notify, "on_task_terminal", _noop_coro)
    wt_calls = []

    async def fake_ensure(root, task_id):
        wt_calls.append(root)
        return None

    monkeypatch.setattr(executor.worktree, "ensure_worktree_for_task", fake_ensure)

    await task_tools.voice_dispatch_task.handler({"title": "标题", "prompt": "内容"})
    row = tasks.get_latest()
    assert row["cwd"] == str(config.ROOT_DIR)  # 落库的就是默认项目根
    running = executor._running.get(row["id"])
    if running is not None:
        await running
    assert wt_calls == [str(config.ROOT_DIR)]  # worktree 隔离确实被触发

    # 显式传 cwd 则原样保留,不被默认值覆盖
    await task_tools.voice_dispatch_task.handler(
        {"title": "标题2", "prompt": "内容", "cwd": "/tmp/other-proj"}
    )
    assert tasks.get_latest()["cwd"] == "/tmp/other-proj"


@pytest.mark.anyio
async def test_query_tool_reports_latest_when_id_omitted(voice_db):
    tasks.create("查询目标", "prompt")
    result = await task_tools.voice_query_session.handler({})
    assert "查询目标" in result["content"][0]["text"]


@pytest.mark.anyio
async def test_query_tool_reports_missing_session(voice_db):
    result = await task_tools.voice_query_session.handler({"session_id": "no-such-id"})
    assert "没有找到" in result["content"][0]["text"]


@pytest.mark.anyio
async def test_list_tool_lists_recent_tasks(voice_db):
    tasks.create("任务甲", "p1")
    tasks.create("任务乙", "p2")
    result = await task_tools.voice_list_sessions.handler({"origin": "task"})
    text = result["content"][0]["text"]
    assert "任务甲" in text and "任务乙" in text


# ── notify.py:在线走 SSE 事件,离线走 web_push ──────────────────────────────
@pytest.mark.anyio
async def test_notify_broadcasts_sse_event_when_online(voice_db, monkeypatch):
    monkeypatch.setattr(config, "VOICE_OMNI_ENABLED", False)
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
async def test_notify_terminal_marks_pending_review_for_done_not_cancelled(voice_db, monkeypatch):
    """2026-07-29:语音任务终态未读标记跟 web/cron-task 侧栏对齐——done/failed 才标,
    用户主动取消的不标(跟 _WebSink.done() 只在真正出结果时才 set_pending_review
    是同一套语义)。"""
    monkeypatch.setattr(config, "VOICE_OMNI_ENABLED", True)  # 跳过 TTS 合成,不是本用例重点
    done_task = tasks.create("done", "p")
    session_store.append(tasks.session_key(done_task["id"]), "p", "结果")  # 真实任务必有 turn 行
    tasks.set_status(done_task["id"], "running")
    tasks.finish(done_task["id"], "done", "结果", "摘要")
    await notify.on_task_terminal(done_task["id"])
    rows = {r["key"]: r for r in session_store.list_sessions(tasks.SESSION_KEY_PREFIX)}
    assert rows[tasks.session_key(done_task["id"])]["pending_review"] is True

    cancelled_task = tasks.create("cancelled", "p")
    session_store.append(tasks.session_key(cancelled_task["id"]), "p", "(任务已取消)")
    tasks.set_status(cancelled_task["id"], "running")
    tasks.finish(cancelled_task["id"], "cancelled", "", "已取消")
    await notify.on_task_terminal(cancelled_task["id"])
    rows = {r["key"]: r for r in session_store.list_sessions(tasks.SESSION_KEY_PREFIX)}
    assert rows[tasks.session_key(cancelled_task["id"])]["pending_review"] is False


@pytest.mark.anyio
async def test_notify_skips_legacy_tts_when_omni_enabled(voice_db, monkeypatch):
    """Omni 出声模式:播报由前端交给 Omni 念,服务端不该再合成旧 TTS(两套声音并存
    =语气割裂+自回声风险);事件照发,announce_text 在,audio_b64 为空。"""
    monkeypatch.setattr(config, "VOICE_OMNI_ENABLED", True)
    t = tasks.create("标题", "p")
    tasks.set_status(t["id"], "running")
    tasks.finish(t["id"], "done", "完整结果", "一句话摘要")

    def _must_not_call(text, voice):
        raise AssertionError("Omni 模式不该调用旧 TTS 合成")

    monkeypatch.setattr(tts, "synthesize", _must_not_call)

    q = notify.subscribe()
    try:
        await notify.on_task_terminal(t["id"])
        event, payload = q.get_nowait()
    finally:
        notify.unsubscribe(q)

    assert event == "task_done"
    assert payload["announce_text"]
    assert payload["audio_b64"] is None


@pytest.mark.anyio
async def test_dispatch_broadcasts_task_update_to_online_subscribers(voice_db, monkeypatch):
    """派发瞬间在线页面就该收到 task_update——通话视图的任务状态条靠它实时出现。"""
    async def fake_stream_turn(history, prompt, cwd=None, session_key=None, **kw):
        yield Done(AgentReply(text="结果", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(executor, "stream_turn", fake_stream_turn)
    monkeypatch.setattr(notify, "on_task_terminal", _noop_coro)

    q = notify.subscribe()
    try:
        task = executor.dispatch("标题", "prompt")
        await executor._running[task["id"]]
        events = []
        while not q.empty():
            events.append(q.get_nowait())
    finally:
        notify.unsubscribe(q)

    updates = [p for e, p in events if e == "task_update"]
    assert updates and updates[-1]["id"] == task["id"]
    assert updates[-1]["status"] == "running"  # 并发未满,广播时已起跑


def test_progress_text_maps_task_tools_to_human_words():
    """MCP 内部工具名(mcp__xxx__yyy)不能原样念给用户听,单独映射成人话。"""
    assert executor.progress_text("mcp__voice_tasks__voice_dispatch_task", {}) == "正在安排后台任务"
    assert executor.progress_text("mcp__voice_tasks__voice_query_session", {}) == "正在查会话状态"
    assert executor.progress_text("mcp__other__thing", {}) == "正在使用工具"
    assert executor.progress_text("Bash", {"command": "ls -la"}) == "正在执行:ls -la"


def test_progress_text_carries_tool_specifics():
    """动作行要带具体信息("正在查资料"这种笼统话没信息量,2026-07-12 用户反馈)。"""
    assert executor.progress_text("WebSearch", {"query": "上海 明天 天气"}) == "正在搜索:上海 明天 天气"
    assert executor.progress_text("WebFetch", {"url": "https://webkit.org/blog/13878/x"}) == "正在读网页:webkit.org"
    assert executor.progress_text("Agent", {"description": "梳理项目结构"}) == "正在派子任务:梳理项目结构"
    assert executor.progress_text("WebSearch", {}) == "正在搜网页"


def test_cancel_queued_broadcasts_task_update(voice_db):
    """排队中取消不走 _run 的终态收尾(没有 task_done),得单独广播一次
    让状态条把这条摘掉。"""
    t = tasks.create("排队任务", "p")
    q = notify.subscribe()
    try:
        assert executor.cancel(t["id"]) is True
        event, payload = q.get_nowait()
    finally:
        notify.unsubscribe(q)
    assert event == "task_update"
    assert payload["id"] == t["id"]
    assert payload["status"] == "cancelled"


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
