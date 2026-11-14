"""新聊天保存历史，不创建工作树；修改代码交给独立任务。"""
from pathlib import Path
import subprocess

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.core import worktree
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import _db, agents, chats, session_store
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


@pytest.mark.parametrize("stored_mode", ["task", "chat", None])
@pytest.mark.parametrize("legacy_key", [False, True])
def test_legacy_project_main_stays_task_when_reading_chat_history(isolated, stored_mode, legacy_key):
    root = isolated / "project"
    root.mkdir()
    owner = agents.create("开发", path=str(root))
    if legacy_key:
        meta = agents._read_meta(owner["id"])
        meta["main_conv"] = f"p{owner['project_hash']}:main"
        agents._write_meta(owner["id"], meta)
        owner = agents.get(owner["id"])
    key = agents.main_session_key(owner)
    session_store.bind_session(key, agent_id=owner["id"], project_hash=owner["project_hash"])
    session_store.append(key, "旧开发成果", "保留回答")
    with _db.conn() as connection:
        connection.execute("UPDATE session_meta SET conversation_mode=? WHERE session_key=?", (stored_mode, key))
    session_store.set_prefs({f"active_chat:{owner['id']}": key})

    # 未打开聊天历史时也必须兼容已经被旧版本误标的主会话。
    assert chats.mode(key) == "task"
    assert chats.recent(owner["id"]) == []
    chat = chats.current(owner["id"])
    assert chat["conv"] != key
    assert chats.mode(chat["conv"]) == "chat"
    with pytest.raises(ValueError):
        chats.select(key, owner["id"])
    assert chats.mode(key) == "task"
    assert _db.conn().execute("SELECT conversation_mode FROM session_meta WHERE session_key=?", (key,)).fetchone()[0] == "task"
    assert session_store.session_summary(key)["turns"] == 1


def test_legacy_general_main_remains_chat(isolated):
    session_store.append("main", "旧聊天", "保留回答")
    chats.preserve_legacy(agents.get("general"))
    assert chats.mode("main") == "chat"
    assert "main" in {row["conv"] for row in chats.recent("general")}


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
async def test_mislabeled_main_prepares_real_worktree_but_chat_refuses(isolated, monkeypatch):
    from vococo.tools import builtin, danger

    root = isolated / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "初始"], check=True, capture_output=True)
    monkeypatch.setattr(config, "ROOT_DIR", root)
    monkeypatch.setattr(worktree, "_wt_base", lambda _root: isolated / "worktrees")
    owner = agents.create("开发", path=str(root))
    agents.update(owner["id"], permission="full")
    meta = agents._read_meta(owner["id"])
    meta["main_conv"] = f"p{owner['project_hash']}:main"
    agents._write_meta(owner["id"], meta)
    key = agents.main_session_key(agents.get(owner["id"]))
    session_store.bind_session(key, agent_id=owner["id"], project_hash=owner["project_hash"])
    with _db.conn() as connection:
        connection.execute("UPDATE session_meta SET conversation_mode='chat' WHERE session_key=?", (key,))
    monkeypatch.setattr(danger, "current_session_key", lambda: key)
    result = await builtin.prepare_repository.handler({"path": str(root)})
    assert not result.get("isError"), result
    checkout = session_store.get_worktree(key)
    assert checkout and Path(checkout, ".git").is_file()
    assert Path(checkout) != root

    chat = chats.create(owner["id"])["conv"]
    monkeypatch.setattr(danger, "current_session_key", lambda: chat)
    result = await builtin.prepare_repository.handler({"path": str(root)})
    assert result.get("isError")
    assert "聊天模式" in result["content"][0]["text"]
    assert session_store.get_worktree(chat) is None


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
