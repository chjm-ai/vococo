"""对话工具的审批、能力校验和重复保护。"""
import pytest

from vococo.core import permissions
from vococo.gateway import settings_store
from vococo.memory import agents, projects
from vococo.tools import agent_management as tools, danger


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def capabilities(isolated, monkeypatch):
    monkeypatch.setattr(settings_store, "list_skills", lambda: [{"name": "design"}])
    monkeypatch.setattr(settings_store, "list_external", lambda: [
        {"name": "penpot", "enabled": True, "url": "secret"},
        {"name": "disabled", "enabled": False},
        {"name": "legacy"},
    ])


def text(result):
    return result["content"][0]["text"]


@pytest.mark.anyio
async def test_create_approval_and_capabilities(capabilities, monkeypatch):
    args = {"name": "UI 设计", "skills": ["design"], "mcp": ["penpot"]}
    assert "未批准" in text(await tools.create_agent.handler(args))
    assert len(agents.list_agents()) == 1
    notices = []

    async def approve(reason, detail):
        notices.append((reason, detail))
        return True

    monkeypatch.setattr(danger, "require_approval", approve)
    assert "已创建" in text(await tools.create_agent.handler(args))
    agent = agents.list_agents()[1]
    assert agent["skills"] == ["design"] and agent["mcp"] == ["penpot"]
    assert "secret" not in str(notices)
    assert "同名" in text(await tools.create_agent.handler(args))
    assert len(notices) == 1
    assert "创建或配置 Agent" in permissions.KEEP_ASKING


@pytest.mark.anyio
async def test_invalid_and_duplicate_paths_do_not_modify(capabilities, monkeypatch, tmp_path):
    async def approve(*args):
        return True

    monkeypatch.setattr(danger, "require_approval", approve)
    path = tmp_path / "project"
    path.mkdir()
    agent = agents.create("旧名", str(path))
    assert "已绑定" in text(await tools.create_agent.handler({"name": "新名", "path": str(path)}))
    assert agents.get(agent["id"])["name"] == "旧名"
    assert "未找到" in text(await tools.create_agent.handler({"name": "新名", "skills": ["不存在"]}))
    assert "未找到" in text(await tools.create_agent.handler({"name": "新名", "mcp": ["disabled"]}))
    assert len(agents.list_agents()) == 2


@pytest.mark.anyio
async def test_update_tristate_denial_and_concurrent_change(capabilities, monkeypatch):
    agent = agents.create("设计")
    agents.update(agent["id"], skills=["design"], mcp=["penpot"])
    assert "未批准" in text(await tools.update_agent.handler({"id": agent["id"], "mcp": []}))
    assert agents.get(agent["id"])["mcp"] == ["penpot"]

    async def approve(*args):
        return True

    monkeypatch.setattr(danger, "require_approval", approve)
    assert "已配置" in text(await tools.update_agent.handler({"id": agent["id"], "skills": None, "mcp": []}))
    assert agents.get(agent["id"])["skills"] is None
    assert agents.get(agent["id"])["mcp"] == []

    async def change_during_approval(*args):
        agents.update(agent["id"], mcp=["penpot"])
        return True

    monkeypatch.setattr(danger, "require_approval", change_during_approval)
    assert "发生变化" in text(await tools.update_agent.handler({"id": agent["id"], "mcp": None}))
    assert agents.get(agent["id"])["mcp"] == ["penpot"]


@pytest.mark.anyio
async def test_symlink_swap_and_legacy_agent(capabilities, monkeypatch, tmp_path):
    first, second, link = tmp_path / "first", tmp_path / "second", tmp_path / "link"
    first.mkdir()
    second.mkdir()
    link.symlink_to(first, target_is_directory=True)

    async def swap(*args):
        link.unlink()
        link.symlink_to(second, target_is_directory=True)
        return True

    monkeypatch.setattr(danger, "require_approval", swap)
    assert "发生变化" in text(await tools.create_agent.handler({"name": "新建", "path": str(link)}))
    assert len(agents.list_agents()) == 1

    projects.upsert_project(str(first))
    agent_id = projects.project_hash(str(first))
    agents.write_doc(agent_id, "AGENT.md", "# 旧项目\n")

    async def approve(*args):
        return True

    monkeypatch.setattr(danger, "require_approval", approve)
    assert "已配置" in text(await tools.update_agent.handler({"id": agent_id, "mcp": ["legacy"]}))
    assert agents.get(agent_id)["mcp"] == ["legacy"]
