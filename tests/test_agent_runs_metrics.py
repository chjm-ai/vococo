"""Agent 运行记录可度量:token / 耗时 / 工具次数落库,派出的后台任务归属 Agent,复盘带统计。"""
from __future__ import annotations

import pytest

from vococo import config
from vococo.core import task_runner
from vococo.core import tasks as bg_tasks
from vococo.core.agent import AgentReply, Done, ToolStarted
from vococo.cron import scheduler
from vococo.memory import agents, projects, session_store
from vococo.voice import notify


@pytest.fixture
def env(isolated, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CRON_JOBS_PATH", tmp_path / "data" / "cron_jobs.json")
    monkeypatch.setattr(bg_tasks, "_DB", None)
    task_runner._running.clear()
    proj = tmp_path / "proj"
    proj.mkdir()
    projects.upsert_project(str(proj))
    yield agents.by_project_hash(projects.project_hash(str(proj)))
    if bg_tasks._DB is not None:
        bg_tasks._DB.close()
        bg_tasks._DB = None


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _noop(*_a, **_k) -> None:
    return None


def _fake_turn(seen: list, *, is_error: bool = False):
    async def fake_stream_turn(history, prompt, cwd=None, session_key=None, **kw):
        seen.append({"prompt": prompt, "extra": kw.get("system_prompt_extra", "")})
        yield ToolStarted(name="Read", tool_id="t1")
        yield ToolStarted(name="Grep", tool_id="t2")
        yield ToolStarted(name="Read", tool_id="t3", parent_id="t2")  # 子代理内部的不算
        yield Done(AgentReply(text="查完了", tool_calls=[], cost_usd=None, is_error=is_error,
                              error="炸了" if is_error else "", stream_tokens=12345))
    return fake_stream_turn


@pytest.mark.anyio
async def test_task_from_agent_session_is_owned_and_recorded(env, monkeypatch):
    seen: list = []
    monkeypatch.setattr(task_runner, "stream_turn", _fake_turn(seen))
    monkeypatch.setattr(notify, "on_task_terminal", _noop)
    agents.write_doc(env["id"], "AGENT.md", "# proj\n\n## 职责与人格\n\n管询盘")
    src = f"web:p{env['project_hash']}:c1"
    task = task_runner.dispatch(title="查询盘", prompt="查一下", origin="chat", context_session_key=src)
    assert task["agent_id"] == env["id"]
    # 任务会话认得自己的 Agent → 拿到 Agent 设定
    assert agents.agent_for_session(f"task:{task['id']}")["id"] == env["id"]
    await task_runner._running[task["id"]]
    assert "管询盘" in seen[0]["extra"]

    row = bg_tasks.get(task["id"])
    assert (row["last_tokens"], row["last_tool_calls"]) == (12345, 2)
    assert row["last_duration"] >= 0
    runs = agents.recent_runs(env["id"])
    assert len(runs) == 1
    r = runs[0]
    assert (r["job_name"], r["status"], r["tokens"], r["tool_calls"]) == ("查询盘", "success", 12345, 2)
    assert r["turn_id"] is None  # 派出的任务不复制进主会话
    assert session_store.load_recent(agents.main_session_key(env)) == []
    log = next((agents.home(env["id"]) / "runs").glob("*.md")).read_text(encoding="utf-8")
    assert "查询盘 · success · 1.2 万 token" in log and "2 次工具" in log


@pytest.mark.anyio
async def test_task_from_plain_session_has_no_agent(env, monkeypatch):
    monkeypatch.setattr(task_runner, "stream_turn", _fake_turn([], is_error=True))
    monkeypatch.setattr(notify, "on_task_terminal", _noop)
    # 普通会话 / 全局主会话(通用 Agent)派的活都不归属
    for src in ("web:abc", config.SESSION_KEY, None):
        t = task_runner.dispatch(title="x", prompt="y", origin="chat", context_session_key=src)
        assert t["agent_id"] is None
        await task_runner._running[t["id"]]
    assert agents.recent_runs(env["id"]) == []


def test_run_stats_and_text(env):
    assert agents.run_stats(env["id"])["success_rate"] is None
    assert "没有任何运行记录" in agents.stats_text(env["id"])
    m = {"tokens": 30000, "duration": 90, "tool_calls": 5}
    agents.record_run(env["id"], {"id": "j1", "name": "发信"}, "success", "ok", metrics=m)
    agents.record_run(env["id"], {"id": "j1", "name": "发信"}, "error", "挂了", metrics=m)
    agents.record_run(env["id"], {"id": "j2", "name": "巡检"}, "success", "ok", to_main=False)
    st = agents.run_stats(env["id"])
    assert (st["runs"], st["ok"], st["tokens"]) == (3, 2, 60000)
    assert st["avg_duration"] == 90  # 没指标的那次不拉低平均
    assert st["success_rate"] == pytest.approx(0.667, abs=0.001)
    assert st["jobs"][0]["name"] == "发信"  # 按 token 花费排前面
    text = agents.stats_text(env["id"])
    assert "共 3 次,成功 2 次(67%)" in text and "- 发信:2 次,成功 1,6.0 万 token" in text
    # 旧数据没有指标:标题里不带指标段
    assert agents._metrics_line({}) == "" and agents._metrics_line(None) == ""


@pytest.mark.anyio
async def test_goal_review_trigger_gets_stats_and_cron_metrics(env, monkeypatch):
    seen: list = []
    monkeypatch.setattr(task_runner, "stream_turn", _fake_turn(seen))
    monkeypatch.setattr(notify, "on_task_terminal", _noop)
    agents.write_doc(env["id"], "GOAL.md", "# 目标\n\n每月 20 个询盘\n")
    job = agents.ensure_goal_review(env["id"])
    scheduler._run_job(scheduler.load_jobs()[0], _noop)
    await task_runner._running[job["id"]]
    assert seen[0]["prompt"].startswith("【运行统计·近 7 天】")
    assert "【目标复盘】" in seen[0]["prompt"]

    # cron 终态钩子把任务行上的指标带进运行记录
    await scheduler._on_task_terminal(bg_tasks.get(job["id"]), _noop)
    r = agents.recent_runs(env["id"])[0]
    assert (r["job_name"], r["tokens"], r["tool_calls"]) == ("目标复盘", 12345, 2)
    assert r["turn_id"] is not None  # 定时任务照旧复制进主会话
