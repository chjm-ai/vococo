"""新聊天保存历史，不创建工作树；修改代码交给独立任务。"""
from pathlib import Path
import subprocess

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.core import worktree
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import agents, chats, session_store
from vococo.voice import session


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_new_chat_preserves_history_and_owner(isolated):
    first = chats.create("general")
    token = session.activate(first["conv"])
    try:
        session.append("保留这段历史", "第一段回答")
        session.set_resume("old-resume")
    finally:
        session._active_key.reset(token)
    second = chats.create("general")
    assert first["conv"] != second["conv"]
    assert chats.current("general")["conv"] == second["conv"]
    assert session_store.get_sdk_session_id(second["conv"]) is None
    rows = chats.recent("general")
    assert [row["conv"] for row in rows] == [first["conv"]]
    assert rows[0]["title"] == "保留这段历史"
    chats.select(first["conv"], "general")
    assert chats.current("general")["conv"] == first["conv"]
    owner = agents.create("别的 Agent")
    assert chats.recent(owner["id"]) == []
    with pytest.raises(ValueError):
        chats.select(first["conv"], owner["id"])


@pytest.mark.parametrize("saved", ["missing", "deleted", "foreign"])
def test_chat_mode_falls_back_to_recent_history(isolated, saved):
    first = chats.create("general")["conv"]
    session_store.append(first, "旧聊天", "旧回答")
    latest = chats.create("general")["conv"]
    session_store.append(latest, "最近聊天", "最近回答")
    owner = agents.create("其他归属")
    foreign = chats.create(owner["id"])["conv"]
    key = {"missing": None, "deleted": "session:missing", "foreign": foreign}[saved]
    session_store.set_prefs({"active_chat:general": key})
    assert chats.current("general")["conv"] == latest
    assert session_store.get_prefs()["active_chat:general"] == latest
    assert {row["conv"] for row in chats.recent("general")} == {first, latest}
    chats.select(first, "general")
    assert chats.current("general")["conv"] == first  # 上次选择优先于最近消息


def test_chat_mode_without_history_creates_only_one_empty_chat(isolated):
    first = chats.current("general")
    assert not first["turns"]
    assert chats.current("general")["conv"] == first["conv"]
    assert chats.recent("general") == []
    with pytest.raises(ValueError):
        chats.current("nonexistent-agent")


@pytest.mark.anyio
async def test_chat_and_readonly_task_do_not_create_worktree(isolated, monkeypatch):
    root = isolated / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "初始"], check=True, capture_output=True)
    monkeypatch.setattr(config, "ROOT_DIR", root)
    monkeypatch.setattr(config, "AGENT_WORKSPACES_DIR", isolated / "spaces")
    chat = chats.create("general")["conv"]
    assert await worktree.ensure_worktree(chat) is None
    assert not Path(await worktree.execution_cwd(chat), ".git").exists()
    task = config.new_session_key()
    session_store.bind_session(task, agent_id="general", project_hash=None)
    assert not Path(await worktree.execution_cwd(task), ".git").exists()
    assert not (root / "data/worktrees").exists()
    checkout = await worktree.ensure_worktree(task)
    assert Path(checkout, ".git").is_file()
    assert await worktree.execution_cwd(task) == checkout


@pytest.mark.anyio
async def test_chat_api_rejects_invalid_selection(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([web.post("/chats/open", adapter._handle_chat_open),
                    web.get("/chats/history", adapter._handle_chat_history)])
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/chats/open", json={"new": True})
        assert response.status == 200
        chat = await response.json()
        assert chats.mode(chat["conv"]) == "chat"
        for body in ({"agent_id": []}, {"agent_id": ["general"]}, {"conv": "session:missing"}, {"agent_id": "missing"}):
            response = await client.post("/chats/open", json=body)
            assert response.status == 400
        assert (await client.get("/chats/history?agent_id=missing")).status == 400
