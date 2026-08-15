"""cron 调度器的测试(2026-07-29 随统一后台任务引擎改写)。

_run_job 现在只是"触发"——真正跑一轮走 core/task_runner.py 那套语音/cron/chat
三方共用的引擎(job_id 复用为 task_id),跑完后异步触发 _on_task_terminal 做
回填统计 + 推送,不再像以前那样在 _run_job 里同步等 run_turn 跑完再处理。

创建/管理走 web 接口(见 test_web_cron_sidebar.py);这里测两段:
1. _run_job 触发后,任务真正落进它自己的专属会话,且第二次触发是原地续聊
   (不产生新任务行)。
2. _on_task_terminal 收到终态任务后正确回填 last_status/last_run_at,并按
   "专属会话 + 可选额外目标"的规则推送——独立测,不用等真的跑一轮。
"""
from __future__ import annotations

import asyncio

import pytest

from vococo.core import task_runner
from vococo.core import tasks as bg_tasks
from vococo.core.task_runner import _SUMMARY_TAG_INSTRUCTION
from vococo.core.agent import AgentReply, Done
from vococo.cron import scheduler
from vococo.memory import session_store
from vococo.voice import notify


@pytest.fixture
def cron_env(isolated, monkeypatch):
    data = isolated / "data"
    data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(scheduler.config, "CRON_JOBS_PATH", data / "cron_jobs.json")
    monkeypatch.setattr(bg_tasks, "_DB", None)
    task_runner._running.clear()
    scheduler._script_running.clear()
    yield data
    if bg_tasks._DB is not None:
        bg_tasks._DB.close()
        bg_tasks._DB = None


async def _noop_coro(*_a, **_k) -> None:
    return None


# ── _run_job:触发后落进专属会话,job_id 复用为 task_id ───────────────────────


@pytest.mark.anyio
async def test_run_job_dispatches_and_persists_to_own_conv(cron_env, monkeypatch):
    async def fake_stream_turn(history, prompt, cwd=None, session_key=None, **kw):
        yield Done(AgentReply(text="今天没什么特别的", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(task_runner, "stream_turn", fake_stream_turn)
    monkeypatch.setattr(notify, "on_task_terminal", _noop_coro)

    job = scheduler.create_job(
        name="晨间简报", prompt="汇总今天安排", schedule={"kind": "cron", "expr": "0 8 * * *"}
    )
    scheduler._run_job(job, _noop_coro)
    await task_runner._running[job["id"]]

    row = bg_tasks.get(job["id"])
    assert row["origin"] == "cron"
    assert row["status"] == "done"

    turns = session_store.load_recent(job["conv"])
    assert len(turns) == 1
    assert turns[0].user == "汇总今天安排"
    assert "今天没什么特别的" in turns[0].assistant


@pytest.mark.anyio
async def test_run_job_second_trigger_appends_same_task_no_new_row(cron_env, monkeypatch):
    """cron 到点第二次触发同一个 job:不产生新任务行,是对同一个 task 原地续接一轮
    (job_id 复用为 task_id 的设计,见 core/tasks.py 模块头说明)。"""
    calls = []

    async def fake_stream_turn(history, prompt, cwd=None, session_key=None, **kw):
        calls.append(prompt)
        yield Done(AgentReply(text=f"第{len(calls)}次结果", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(task_runner, "stream_turn", fake_stream_turn)
    monkeypatch.setattr(notify, "on_task_terminal", _noop_coro)

    job = scheduler.create_job(
        name="晨间简报", prompt="汇总今天安排", schedule={"kind": "cron", "expr": "0 8 * * *"}
    )
    scheduler._run_job(job, _noop_coro)
    await task_runner._running[job["id"]]

    # 第二次触发落在"终态续聊"分支,_run_job 内部用 asyncio.create_task 包了一层
    # append()(不阻塞 _tick),要先让出一次事件循环,_running[job_id] 才会被
    # append() 内部的 _start_one 填上。
    scheduler._run_job(job, _noop_coro)
    await asyncio.sleep(0)
    await task_runner._running[job["id"]]

    assert len(bg_tasks.list_recent()) == 1  # 没有产生第二条任务行
    # 实际发给模型的文本会带上摘要标记指令(见 task_runner._SUMMARY_TAG_INSTRUCTION),
    # 落库的 turn 本身仍是干净的原文(见另一个用例的断言)。
    expected = "汇总今天安排" + _SUMMARY_TAG_INSTRUCTION
    assert calls == [expected, expected]


# ── _on_task_terminal:回填统计 + 推送(不依赖真的跑一轮,直接构造终态 task) ──


@pytest.mark.anyio
async def test_on_task_terminal_persists_status_and_pushes_web(cron_env):
    job = scheduler.create_job(
        name="晨间简报", prompt="汇总今天安排", schedule={"kind": "cron", "expr": "0 8 * * *"}
    )
    pushes = []

    async def fake_push(platform, chat_id, text):
        pushes.append((platform, chat_id, text))

    task = {
        "id": job["id"], "status": "done",
        "result_summary": "今天没什么特别的", "result_full": "今天没什么特别的",
    }
    await scheduler._on_task_terminal(task, fake_push)

    updated = next(j for j in scheduler.load_jobs() if j["id"] == job["id"])
    assert updated["last_status"] == "success"
    assert updated["last_run_at"] is not None

    assert pushes == [("web", job["conv"], pushes[0][2])]
    assert "今天没什么特别的" in pushes[0][2]


@pytest.mark.anyio
async def test_on_task_terminal_also_pushes_extra_target(cron_env):
    job = scheduler.create_job(
        name="每周复盘", prompt="回顾这周", schedule={"kind": "cron", "expr": "0 21 * * 0"},
        target={"platform": "web", "chat_id": "conv1"},
    )
    pushes = []

    async def fake_push(platform, chat_id, text):
        pushes.append((platform, chat_id))

    task = {"id": job["id"], "status": "done", "result_summary": "本周进展良好", "result_full": "本周进展良好"}
    await scheduler._on_task_terminal(task, fake_push)

    assert pushes == [("web", job["conv"]), ("web", "conv1")]


@pytest.mark.anyio
async def test_on_task_terminal_marks_failed_status(cron_env):
    job = scheduler.create_job(
        name="任务", prompt="p", schedule={"kind": "cron", "expr": "0 8 * * *"}
    )

    async def fake_push(platform, chat_id, text):
        pass

    task = {"id": job["id"], "status": "failed", "result_summary": "", "result_full": ""}
    await scheduler._on_task_terminal(task, fake_push)

    updated = next(j for j in scheduler.load_jobs() if j["id"] == job["id"])
    assert updated["last_status"] == "error"


@pytest.mark.anyio
async def test_on_task_terminal_skips_deleted_job(cron_env):
    """任务定义已被删除:只是一次孤立的收尾,不报错、不推送(job 已经不存在了)。"""
    async def fake_push(platform, chat_id, text):
        raise AssertionError("job 已删除不该推送")

    task = {"id": "ghost-job", "status": "done", "result_summary": "x", "result_full": "x"}
    await scheduler._on_task_terminal(task, fake_push)  # 不抛异常即通过


# ── load_jobs:老数据迁移 ────────────────────────────────────────────────────


def test_load_jobs_backfills_missing_conv(cron_env):
    scheduler.save_jobs([{"id": "legacy1", "name": "老任务", "prompt": "x",
                           "schedule": {"kind": "cron", "expr": "0 8 * * *"}, "enabled": True}])
    jobs = scheduler.load_jobs()
    assert jobs[0]["conv"] == "task:legacy1"
    # 迁移结果应落盘,下次读取仍然一致
    assert scheduler.load_jobs()[0]["conv"] == "task:legacy1"


def test_load_jobs_migrates_legacy_cron_task_prefix(cron_env):
    """2026-07-29 前缀统一:老库里 conv=cron-task:xxx 的 job 定义要自动迁移成
    task:xxx,不然下次触发会写去一个"新"会话,跟历史对不上号。"""
    scheduler.save_jobs([{"id": "legacy2", "name": "老任务2", "prompt": "x",
                           "conv": "cron-task:legacy2",
                           "schedule": {"kind": "cron", "expr": "0 8 * * *"}, "enabled": True}])
    jobs = scheduler.load_jobs()
    assert jobs[0]["conv"] == "task:legacy2"
    assert scheduler.load_jobs()[0]["conv"] == "task:legacy2"


# ── 脚本任务模式(mode=="script"):直接跑命令,不进 Agent 会话 ────────────────
# 2026-08-15 加:有信号(更新/待确认)才值得花一次 LLM 总结,没信号时原始脚本
# 输出直接当结果——零 LLM 调用。见模块头「脚本任务」模式说明、
# memory/people_scan_cli.py 的 ##CRON_SIGNAL## 约定。


class _FakeProc:
    """假的 asyncio subprocess,communicate() 直接吐预设 stdout,不真的起进程。"""

    def __init__(self, stdout: bytes, returncode: int = 0):
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, b""


def _make_script_job(**overrides) -> dict:
    job = scheduler.create_job(
        name="人脉画像更新", prompt="[脚本任务模式,此 prompt 仅供参考]",
        schedule={"kind": "cron", "expr": "0 7 * * *"},
    )
    job.update(mode="script", command="echo hi", summarize_prompt="把统计总结成一句话")
    job.update(overrides)
    scheduler.save_jobs([job])
    return job


@pytest.mark.anyio
async def test_run_script_job_no_signal_skips_llm(cron_env, monkeypatch):
    """没信号(##CRON_SIGNAL:0##):原始脚本输出直接当结果,绝不调 sidecar_chat。"""
    async def fake_subprocess_shell(cmd, **kw):
        return _FakeProc("没有新/改动的笔记,画像无更新。\n##CRON_SIGNAL:0##\n".encode())

    monkeypatch.setattr(scheduler.asyncio, "create_subprocess_shell", fake_subprocess_shell)

    async def fail_sidecar_chat(*a, **k):
        raise AssertionError("没信号不该调 LLM")

    monkeypatch.setattr(scheduler.providers, "sidecar_chat", fail_sidecar_chat)

    job = _make_script_job()
    pushes = []

    async def fake_push(platform, chat_id, text):
        pushes.append((platform, chat_id, text))

    await scheduler._run_script_job(job, fake_push)

    turns = session_store.load_recent(job["conv"])
    assert len(turns) == 1
    assert "没有新/改动的笔记" in turns[0].assistant
    assert "CRON_SIGNAL" not in turns[0].assistant  # 标记已被剥掉,不该出现在展示文本里

    updated = next(j for j in scheduler.load_jobs() if j["id"] == job["id"])
    assert updated["last_status"] == "success"
    assert updated["last_run_at"] is not None
    assert pushes and "没有新/改动的笔记" in pushes[0][2]


@pytest.mark.anyio
async def test_run_script_job_with_signal_calls_summarizer(cron_env, monkeypatch):
    """有信号(##CRON_SIGNAL:1##):追加一次轻量总结,总结结果替换成展示文本。"""
    raw = "共处理 3 篇笔记:更新 1 / 待确认 0 / 无人物跳过 2 / 错误 0\n  · a: 胜源\n##CRON_SIGNAL:1##\n"

    async def fake_subprocess_shell(cmd, **kw):
        return _FakeProc(raw.encode())

    monkeypatch.setattr(scheduler.asyncio, "create_subprocess_shell", fake_subprocess_shell)

    calls = []

    async def fake_sidecar_chat(prompt, **kw):
        calls.append(prompt)
        return "今天更新了胜源的画像。"

    monkeypatch.setattr(scheduler.providers, "sidecar_chat", fake_sidecar_chat)

    job = _make_script_job()
    await scheduler._run_script_job(job, _noop_coro)

    assert len(calls) == 1
    assert "把统计总结成一句话" in calls[0] and "胜源" in calls[0]
    turns = session_store.load_recent(job["conv"])
    assert turns[0].assistant == "今天更新了胜源的画像。"


@pytest.mark.anyio
async def test_run_script_job_summarizer_failure_falls_back_to_raw(cron_env, monkeypatch):
    """总结调用失败(返回 None,如未配置 DeepSeek/网络错误):原样回退到脚本原文,
    不能把这轮结果丢了。"""
    async def fake_subprocess_shell(cmd, **kw):
        return _FakeProc("共处理 1 篇笔记:更新 1 / 待确认 0 / 无人物跳过 0 / 错误 0\n##CRON_SIGNAL:1##\n".encode())

    monkeypatch.setattr(scheduler.asyncio, "create_subprocess_shell", fake_subprocess_shell)

    async def fake_sidecar_chat(*a, **k):
        return None

    monkeypatch.setattr(scheduler.providers, "sidecar_chat", fake_sidecar_chat)

    job = _make_script_job()
    await scheduler._run_script_job(job, _noop_coro)

    turns = session_store.load_recent(job["conv"])
    assert "共处理 1 篇笔记" in turns[0].assistant


@pytest.mark.anyio
async def test_run_script_job_no_summarize_prompt_skips_llm_even_with_signal(cron_env, monkeypatch):
    """job 没配 summarize_prompt:哪怕有信号也不调 LLM(该字段本来就是可选的)。"""
    async def fake_subprocess_shell(cmd, **kw):
        return _FakeProc("共处理 1 篇笔记:更新 1 / 待确认 0 / 无人物跳过 0 / 错误 0\n##CRON_SIGNAL:1##\n".encode())

    monkeypatch.setattr(scheduler.asyncio, "create_subprocess_shell", fake_subprocess_shell)

    async def fail_sidecar_chat(*a, **k):
        raise AssertionError("没配 summarize_prompt 不该调 LLM")

    monkeypatch.setattr(scheduler.providers, "sidecar_chat", fail_sidecar_chat)

    job = _make_script_job(summarize_prompt="")
    await scheduler._run_script_job(job, _noop_coro)  # 不抛异常即通过


@pytest.mark.anyio
async def test_run_script_job_command_error_marks_failed_no_summarizer(cron_env, monkeypatch):
    """命令本身跑失败(非 0 退出码):标记 error,原样展示报错,不调 LLM 润色。"""
    async def fake_subprocess_shell(cmd, **kw):
        return _FakeProc(b"Traceback...\nModuleNotFoundError", returncode=1)

    monkeypatch.setattr(scheduler.asyncio, "create_subprocess_shell", fake_subprocess_shell)

    async def fail_sidecar_chat(*a, **k):
        raise AssertionError("执行失败不该调 LLM")

    monkeypatch.setattr(scheduler.providers, "sidecar_chat", fail_sidecar_chat)

    job = _make_script_job()
    await scheduler._run_script_job(job, _noop_coro)

    updated = next(j for j in scheduler.load_jobs() if j["id"] == job["id"])
    assert updated["last_status"] == "error"
    turns = session_store.load_recent(job["conv"])
    assert "ModuleNotFoundError" in turns[0].assistant


@pytest.mark.anyio
async def test_run_job_routes_script_mode_to_run_script_job(cron_env, monkeypatch):
    """_run_job 按 mode 路由:script 模式绝不该碰 task_runner/Agent 那条路径。"""
    called = []

    async def fake_run_script_job(job, push):
        called.append(job["id"])

    monkeypatch.setattr(scheduler, "_run_script_job", fake_run_script_job)

    def fail_dispatch(**kw):
        raise AssertionError("script 模式不该走 task_runner.dispatch")

    monkeypatch.setattr(task_runner, "dispatch", fail_dispatch)

    job = _make_script_job()
    scheduler._run_job(job, _noop_coro)
    await asyncio.sleep(0)  # 让 create_task 包的协程有机会跑一次

    assert called == [job["id"]]
