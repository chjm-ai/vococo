"""Agent 删除和历史空壳的边界回归,仅使用临时数据目录。"""
from __future__ import annotations

import pytest

from vococo.memory import agents, projects


@pytest.mark.parametrize("named", [False, True])
def test_list_skips_materialized_shell_but_keeps_configured_agent(isolated, named):
    project = isolated / "project"
    project.mkdir()
    p = projects.upsert_project(str(project))
    agent = agents.by_project_hash(p["hash"])
    assert agents.home(agent["id"]).is_dir()
    assert agent["id"] not in {a["id"] for a in agents.list_agents()}
    if named:
        agents.update(agent["id"], name="有名字")
    else:
        agents.write_doc(agent["id"], "AGENT.md", "# 已配置\n")
    assert agent["id"] in {a["id"] for a in agents.list_agents()}


def test_list_preserves_metadata_when_materializing_named_agent(isolated):
    project = isolated / "project"
    project.mkdir()
    p = projects.upsert_project(str(project))
    agents._write_meta(p["hash"], {"name": "保留名称", "skills": ["pdf"]})
    listed = next(a for a in agents.list_agents() if a["id"] == p["hash"])
    assert listed["name"] == "保留名称" and listed["skills"] == ["pdf"]


def test_delete_hides_project_even_when_workdir_missing(isolated):
    project = isolated / "project"
    project.mkdir()
    agent = agents.create("已丢工作目录", str(project))
    project.rmdir()
    agents.delete(agent["id"])
    assert agent["project_hash"] not in {p["hash"] for p in projects.list_projects()}
    assert agents.by_project_hash(agent["project_hash"]) is None


def test_delete_rejects_project_hash_alias_without_materializing(isolated, monkeypatch):
    from vococo import config
    from vococo.cron import scheduler

    monkeypatch.setattr(config, "CRON_JOBS_PATH", isolated / "jobs.json")
    agent = agents.create("真实 Agent")
    job = scheduler.create_job(name="保留任务", prompt="p", schedule={"kind": "cron", "expr": "0 9 * * *"},
                               agent_id=agent["id"])
    with pytest.raises(ValueError, match="不存在"):
        agents.delete(agent["project_hash"])
    assert not agents.home(agent["project_hash"]).exists()
    assert agents.get(agent["id"]) is not None
    assert next(j for j in scheduler.load_jobs() if j["id"] == job["id"])["enabled"]


def test_delete_keeps_legacy_workspace_in_place(isolated, monkeypatch):
    from vococo import config

    monkeypatch.setattr(config, "CRON_JOBS_PATH", isolated / "jobs.json")
    aid = "abcdef0123"
    workdir = agents.home(aid) / "workspace"
    workdir.mkdir(parents=True)
    workfile = workdir / "keep.txt"
    workfile.write_text("不能移动的工作数据", encoding="utf-8")
    agents._write_meta(aid, {"name": "旧 Agent", "workdir": str(workdir)})
    agents.write_doc(aid, "AGENT.md", "# 旧 Agent")
    projects.upsert_project(str(workdir))
    agents.delete(aid)
    assert workfile.read_text(encoding="utf-8") == "不能移动的工作数据"
    assert not (agents.home(aid) / "agent.json").exists()
    archived = list((agents.root_dir() / agents.DELETED_DIR).glob(f"{aid}-*"))
    assert len(archived) == 1 and (archived[0] / "agent.json").is_file()
    assert not (archived[0] / "workspace").exists()
    assert agents.get(aid) is None
    assert aid not in {a["id"] for a in agents.list_agents()}


def test_delete_waits_for_update_and_does_not_resurrect(isolated, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from vococo import config

    monkeypatch.setattr(config, "CRON_JOBS_PATH", isolated / "jobs.json")
    agent = agents.create("并发保存")
    saving, proceed, deleting = Event(), Event(), Event()
    write_meta = agents._write_meta

    def pause_write(aid, meta):
        saving.set()
        assert proceed.wait(5)
        write_meta(aid, meta)

    def delete():
        deleting.set()
        return agents.delete(agent["id"])

    monkeypatch.setattr(agents, "_write_meta", pause_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        update = pool.submit(agents.update, agent["id"], name="新名称")
        try:
            assert saving.wait(5)
            removed = pool.submit(delete)
            assert deleting.wait(5)
            assert not removed.done()
        finally:
            proceed.set()
        update.result(timeout=5)
        assert removed.result(timeout=5)["name"] == "新名称"
    assert agents.get(agent["id"]) is None
    assert not agents.home(agent["id"]).exists()
