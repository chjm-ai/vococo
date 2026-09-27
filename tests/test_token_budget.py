"""后台任务 token 预算:stream_turn 流式计数超限即 interrupt;task_runner 取预算 + 每日总量。"""
from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk import ResultMessage, StreamEvent

from vococo import config
from vococo.core import agent, task_runner, tasks
from vococo.core.agent import Done


def _ev(event: dict) -> StreamEvent:
    return StreamEvent(uuid="u", session_id="sid", event=event)


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, session_id="sid",
        is_error=False, num_turns=1, usage={"input_tokens": 5, "output_tokens": 1},
    )


class BudgetFakeClient:
    """每次 query 模拟 3 次模型调用,每次 输入 1000 + 缓存写 500 + 输出 200 = 1700 新鲜 token;
    缓存复读 99999 不应计入。interrupt 后直接给 ResultMessage。"""

    def __init__(self, options=None, *, registry: list):
        self.pending: asyncio.Queue = asyncio.Queue()
        self.interrupted = False
        registry.append(self)

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def interrupt(self):
        self.interrupted = True
        # 真机:interrupt 后 CLI 收尾吐 ResultMessage;把队列里没读的都清掉
        while not self.pending.empty():
            self.pending.get_nowait()
        self.pending.put_nowait(_result())

    async def query(self, prompt=None, *a, **kw):
        for _ in range(3):
            self.pending.put_nowait(_ev({"type": "message_start", "message": {"usage": {
                "input_tokens": 1000, "cache_creation_input_tokens": 500,
                "cache_read_input_tokens": 99999, "output_tokens": 1}}}))
            self.pending.put_nowait(_ev({"type": "content_block_delta",
                                         "delta": {"type": "text_delta", "text": "字"}}))
            self.pending.put_nowait(_ev({"type": "message_delta", "usage": {"output_tokens": 200}}))
        self.pending.put_nowait(_result())

    def receive_messages(self):
        async def gen():
            while True:
                yield await self.pending.get()

        return gen()

    async def get_context_usage(self):
        return {"totalTokens": 100, "rawMaxTokens": 200_000}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake_clients(monkeypatch):
    made: list[BudgetFakeClient] = []
    monkeypatch.setattr(agent, "ClaudeSDKClient", lambda options=None: BudgetFakeClient(options, registry=made))
    monkeypatch.setattr(agent.providers, "resolve", lambda *a: ("claude-sonnet-4", {}))
    monkeypatch.setattr(agent.settings_store, "vococo_enabled", lambda: False)
    monkeypatch.setattr(agent.settings_store, "effective_external_mcp", lambda: {})
    monkeypatch.setattr(agent.settings_store, "effective_skills", lambda cwd=None: None)
    monkeypatch.setattr(agent, "build_system_prompt", lambda cwd=None, cache_key=None: {"append": "p"})
    monkeypatch.setattr(agent, "build_mcp_servers", lambda: {})
    monkeypatch.setattr(agent, "build_hooks", lambda: {})
    return made


async def _run(budget: int) -> agent.AgentReply:
    done = None
    async for ev in agent.stream_turn([], "干活", session_key="task:budget-test", token_budget=budget):
        if isinstance(ev, Done):
            done = ev
    return done.reply


@pytest.mark.anyio
async def test_under_budget_runs_to_end(fake_clients):
    reply = await _run(100_000)
    assert not reply.is_error and not reply.budget_exceeded
    assert reply.stream_tokens == 3 * 1700  # 缓存复读不计
    assert reply.text == "字字字"
    assert not fake_clients[0].interrupted


@pytest.mark.anyio
async def test_over_budget_interrupts(fake_clients):
    reply = await _run(2000)  # 第二次模型调用开头就超
    assert fake_clients[0].interrupted
    assert reply.is_error and reply.budget_exceeded
    assert "token 预算" in reply.error


@pytest.mark.anyio
async def test_zero_budget_means_unlimited(fake_clients):
    reply = await _run(0)
    assert not reply.budget_exceeded and not fake_clients[0].interrupted


# ── task_runner 取预算 ──────────────────────────────────────────────────
@pytest.fixture
def task_db(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "_db_path", lambda: tmp_path / "voice.db")
    monkeypatch.setattr(tasks, "_DB", None)
    monkeypatch.setattr(task_runner, "_cron_job_budget", lambda tid: None)
    yield
    if tasks._DB is not None:
        tasks._DB.close()
    tasks._DB = None


def test_budget_by_origin(task_db, monkeypatch):
    monkeypatch.setattr(config, "TASK_TOKEN_BUDGET", 600_000)
    monkeypatch.setattr(config, "CHAT_TASK_TOKEN_BUDGET", 0)
    monkeypatch.setattr(config, "BG_DAILY_TOKEN_BUDGET", 5_000_000)
    assert task_runner._token_budget({"id": "a", "origin": "cron"}) == (600_000, "")
    assert task_runner._token_budget({"id": "a", "origin": "chat"}) == (0, "")
    monkeypatch.setattr(task_runner, "_cron_job_budget", lambda tid: 50_000)
    assert task_runner._token_budget({"id": "a", "origin": "cron"}) == (50_000, "")


def test_daily_cap_shrinks_then_blocks(task_db, monkeypatch):
    monkeypatch.setattr(config, "TASK_TOKEN_BUDGET", 600_000)
    monkeypatch.setattr(config, "BG_DAILY_TOKEN_BUDGET", 1_000_000)
    tasks.add_daily_usage(700_000)
    assert task_runner._token_budget({"id": "a", "origin": "voice"}) == (300_000, "")
    tasks.add_daily_usage(300_000)
    budget, why = task_runner._token_budget({"id": "a", "origin": "cron"})
    assert budget == 0 and "上限" in why
    # 网页独立会话不受每日总量影响
    assert task_runner._token_budget({"id": "a", "origin": "chat"})[1] == ""
    assert tasks.daily_usage() == 1_000_000


def test_cron_job_budget_reads_job(tmp_path, monkeypatch):
    from vococo.cron import scheduler

    monkeypatch.setattr(scheduler, "load_jobs", lambda: [{"id": "j1", "budget_tokens": 1234}, {"id": "j2"}])
    assert task_runner._cron_job_budget("j1") == 1234
    assert task_runner._cron_job_budget("j2") is None
