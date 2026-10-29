"""cron / Agent 的持久身份、迁移与权限边界，只使用隔离库。"""
import json

import pytest

from vococo import config
from vococo.core import task_runner, tasks
from vococo.core.agent import AgentReply, Done
from vococo.cron import scheduler
from vococo.memory import agents, session_store
from vococo.voice import notify


@pytest.fixture
def env(isolated, monkeypatch):
    monkeypatch.setattr(config, "CRON_JOBS_PATH", isolated / "cron_jobs.json")
    monkeypatch.setattr(tasks, "_DB", None)
    task_runner._running.clear()
    yield isolated
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("owner_kind", ["general", "agent"])
def test_task_preserves_bound_null_project_and_source_access(env, owner_kind):
    owner = agents.get("general") if owner_kind == "general" else agents.create("业务")
    key = config.new_session_key()
    session_store.bind_session(key, agent_id=owner["id"], project_hash=None)
    cwd = env / "execution"
    cwd.mkdir()
    access = {"session": "web:source", "mode": "standard", "denied": ["Bash"],
              "chain_session": "web:original"}
    row = tasks.create("执行", "巡检", session_key=key, cwd=str(cwd), source_access=access)
    assert row["agent_id"] == owner["id"]
    assert json.loads(row["source_access"]) == access
    assert session_store.session_binding(key) == {"agent_id": owner["id"], "project_hash": None}
    assert tasks.task_for_session(key)["id"] == row["id"]


def test_bound_deleted_agent_cannot_fall_back_to_general(env):
    owner = agents.create("已删除")
    key = owner["main_conv"]
    agents.delete(owner["id"])
    with pytest.raises(ValueError, match="Agent 不存在"):
        tasks.create("拒绝", "巡检", session_key=key)
    assert tasks.task_for_session(key) is None


def test_bound_job_keeps_null_project_after_cwd_edit(env):
    job = scheduler.create_job(name="巡检", prompt="p", schedule={"kind": "webhook"})
    cwd = env / "execution"
    cwd.mkdir()
    updated = scheduler.update_job(job["id"], name=job["name"], prompt=job["prompt"],
                                   schedule=job["schedule"], cwd=str(cwd))
    scheduler._bind_job_session(updated)
    assert session_store.session_binding(job["conv"]) == {"agent_id": "general", "project_hash": None}


@pytest.mark.anyio
@pytest.mark.parametrize("new_owner", [True, False])
async def test_transfer_keeps_old_binding_and_dispatches_under_new_owner(env, monkeypatch, new_owner):
    old = agents.create("旧归属")
    other = agents.create("新归属")
    job = scheduler.create_job(name="巡检", prompt="p", agent_id=old["id"],
                               schedule={"kind": "webhook"})
    original = tasks.create("执行", "p", origin="cron", job_id=job["id"], session_key=job["conv"])
    tasks.set_status(original["id"], "cancelled")
    session_store.append(job["conv"], "p", "旧历史")
    binding = session_store.session_binding(job["conv"])
    updated = scheduler.update_job(job["id"], name=job["name"], prompt=job["prompt"],
                                   schedule=job["schedule"], agent_id=other["id"] if new_owner else None)
    assert updated["conv"] != job["conv"]
    assert session_store.session_binding(job["conv"]) == binding
    assert session_store.load_recent(job["conv"])[0].assistant == "旧历史"
    agents.delete(old["id"])
    assert scheduler.load_jobs()[0]["enabled"]
    seen = []

    async def stream(history, prompt, **kwargs):
        seen.append(agents.agent_for_session(kwargs["session_key"])["id"])
        yield Done(AgentReply(text="完成", tool_calls=[], cost_usd=None, is_error=False))

    async def noop(*args):
        pass

    monkeypatch.setattr(task_runner, "stream_turn", stream)
    monkeypatch.setattr(notify, "on_task_terminal", noop)
    scheduler._run_job(updated, noop)
    task = tasks.task_for_job(job["id"])
    await task_runner._running[task["id"]]
    assert task["id"] != original["id"]
    assert seen == [other["id"] if new_owner else "general"]
    assert not task["source_access"]


def test_new_cron_rejects_missing_owner_without_persisting_rule(env):
    with pytest.raises(ValueError, match="Agent 不存在"):
        scheduler.create_job(name="拒绝", prompt="p", agent_id="dead000001", schedule={"kind": "webhook"})
    assert scheduler.load_jobs() == []


@pytest.mark.anyio
async def test_script_run_anchor_uses_session_without_execution_row(env):
    owner = agents.create("脚本归属")
    job = scheduler.create_job(name="脚本", prompt="p", agent_id=owner["id"],
                               schedule={"kind": "webhook"}, mode="script", command="true")
    session_store.append(job["conv"], "p", "脚本结果")
    agents.record_run(owner["id"], job, "success", "脚本结果", to_main=False)
    run = agents.recent_runs(owner["id"])[0]
    assert tasks.task_for_job(job["id"]) is None
    assert run["target_conv"] == job["conv"]
    assert run["target_turn_id"] is not None
