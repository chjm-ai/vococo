"""cron 调度器执行一次任务(_run_job)的落库 + 推送路由。

创建/管理走 web 接口(见 test_web_cron_sidebar.py);这里只测"跑一次任务"这半段:
结果必须落进任务专属会话(侧栏"定时任务"分组靠这个显示历史),同时默认推给
自己的 web 会话(自动带出系统推送,覆盖 Mac/iPhone 等已订阅设备),再按需推给
额外目标(如 telegram)。
"""
from __future__ import annotations

import pytest

from claude_hermes.core.agent import AgentReply
from claude_hermes.cron import scheduler
from claude_hermes.memory import session_store


@pytest.fixture
def cron_env(isolated, monkeypatch):
    data = isolated / "data"
    data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(scheduler.config, "CRON_JOBS_PATH", data / "cron_jobs.json")
    return data


@pytest.mark.anyio
async def test_run_job_persists_to_own_conv_and_pushes_web(cron_env, monkeypatch):
    async def fake_run_turn(history, prompt, model=None):
        return AgentReply(text="今天没什么特别的", tool_calls=[], cost_usd=None, is_error=False)

    monkeypatch.setattr(scheduler, "run_turn", fake_run_turn)

    job = scheduler.create_job(
        name="晨间简报", prompt="汇总今天安排", schedule={"kind": "cron", "expr": "0 8 * * *"}
    )
    pushes = []

    async def fake_push(platform, chat_id, text):
        pushes.append((platform, chat_id, text))

    await scheduler._run_job(job, fake_push)

    assert job["last_status"] == "success"
    turns = session_store.load_recent(job["conv"])
    assert len(turns) == 1
    assert turns[0].user == "汇总今天安排"
    assert "今天没什么特别的" in turns[0].assistant

    assert pushes == [("web", job["conv"], pushes[0][2])]
    assert "今天没什么特别的" in pushes[0][2]


@pytest.mark.anyio
async def test_run_job_also_pushes_extra_target(cron_env, monkeypatch):
    async def fake_run_turn(history, prompt, model=None):
        return AgentReply(text="本周进展良好", tool_calls=[], cost_usd=None, is_error=False)

    monkeypatch.setattr(scheduler, "run_turn", fake_run_turn)

    job = scheduler.create_job(
        name="每周复盘",
        prompt="回顾这周",
        schedule={"kind": "cron", "expr": "0 21 * * 0"},
        target={"platform": "telegram", "chat_id": 123},
    )
    pushes = []

    async def fake_push(platform, chat_id, text):
        pushes.append((platform, chat_id))

    await scheduler._run_job(job, fake_push)

    assert pushes == [("web", job["conv"]), ("telegram", 123)]


@pytest.mark.anyio
async def test_load_jobs_backfills_missing_conv(cron_env):
    scheduler.save_jobs([{"id": "legacy1", "name": "老任务", "prompt": "x",
                           "schedule": {"kind": "cron", "expr": "0 8 * * *"}, "enabled": True}])
    jobs = scheduler.load_jobs()
    assert jobs[0]["conv"] == "cron-task:legacy1"
    # 迁移结果应落盘,下次读取仍然一致
    assert scheduler.load_jobs()[0]["conv"] == "cron-task:legacy1"
