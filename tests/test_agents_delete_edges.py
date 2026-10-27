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
