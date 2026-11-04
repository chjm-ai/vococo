"""侧栏读取不能重复扫描 Agent，也不能阻塞消息所在的事件循环。"""
from __future__ import annotations

import asyncio
import threading

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.core import tasks
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import agents, session_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def sidebar_app(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(config, "CRON_JOBS_PATH", config.DATA_DIR / "cron_jobs.json")
    monkeypatch.setattr(tasks, "_DB", None)
    tasks._conn()
    session_store._conn()
    adapter = WebAdapter()
    async def ping(request):
        return web.json_response({"ok": True})

    app = web.Application()
    app.add_routes([
        web.get("/conversations", adapter._handle_conversations),
        web.get("/voice/sidebar", adapter._handle_voice_sidebar),
        web.get("/cron/sidebar", adapter._handle_cron_sidebar),
        web.get("/conv/search", adapter._handle_conv_search),
        web.get("/ping", ping),
    ])
    yield app
    tasks._DB.close()
    tasks._DB = None


@pytest.mark.anyio
async def test_conversation_list_reads_each_agent_once_per_request(sidebar_app, monkeypatch):
    owner = agents.create("项目")
    for i in range(20):
        key = f"web:p{owner['project_hash']}:c{i}"
        session_store.set_title(key, f"对话{i}")
    reads = []
    original = agents._read_meta

    def counted(agent_id):
        reads.append(agent_id)
        return original(agent_id)

    monkeypatch.setattr(agents, "_read_meta", counted)
    async with TestClient(TestServer(sidebar_app)) as client:
        response = await client.get("/conversations")
        data = await response.json()
        assert response.status == 200
        assert len([row for row in data["conversations"] if row["key"].startswith("web:")]) == 20
        assert {row["agent_id"] for row in data["conversations"]} == {owner["id"]}
        assert reads.count(owner["id"]) <= 2
        # 下一次请求必须重新读取，不能让权限或 Agent 修改滞留在全局缓存中。
        agents.update(owner["id"], name="改名")
        reads.clear()
        response = await client.get("/conversations")
        assert response.status == 200
        assert 1 <= reads.count(owner["id"]) <= 2


@pytest.mark.anyio
async def test_project_sidebar_does_not_build_execution_policy(sidebar_app, monkeypatch):
    from vococo.core import execution

    owner = agents.create("项目")
    key = f"web:p{owner['project_hash']}:legacy"
    session_store.set_title(key, "旧项目对话")

    def unexpected(*args, **kwargs):
        raise AssertionError("显示归属不应探测 Git 或读取完整执行策略")

    monkeypatch.setattr(execution, "execution_mode", unexpected)
    async with TestClient(TestServer(sidebar_app)) as client:
        response = await client.get("/conversations")
        assert response.status == 200
        rows = (await response.json())["conversations"]
    assert next(row for row in rows if row["key"] == key)["agent_id"] == owner["id"]


@pytest.mark.anyio
async def test_cron_sidebar_does_not_aggregate_unrelated_history(sidebar_app, monkeypatch):
    from vococo.cron import scheduler

    job = scheduler.create_job(name="巡检", prompt="检查", schedule={"kind": "cron", "expr": "0 9 * * *"})
    session_store.set_pending_review(job["conv"], True)

    def unexpected(*args, **kwargs):
        raise AssertionError("定时侧栏不应为未读标记统计全部历史")

    monkeypatch.setattr(session_store, "list_sessions", unexpected)
    async with TestClient(TestServer(sidebar_app)) as client:
        response = await client.get("/cron/sidebar")
        assert response.status == 200
        row = (await response.json())["jobs"][0]
    assert row["conv"] == job["conv"]
    assert row["pending_review"] is True


def test_metadata_looks_up_task_only_once(sidebar_app, monkeypatch):
    from vococo.gateway.adapters.web import _session_metadata

    calls = []
    original = tasks.task_for_session

    def counted(key):
        calls.append(key)
        return original(key)

    monkeypatch.setattr(tasks, "task_for_session", counted)
    row = _session_metadata({"key": "task:missing", "agent_id": None, "project_hash": None})
    assert row["title"] == "新对话"
    assert calls == ["task:missing"]


@pytest.mark.anyio
async def test_sidebar_uses_private_readonly_database_connections(sidebar_app, monkeypatch):
    import sqlite3
    from vococo.memory import _db

    state_writer, task_writer = _db.conn(), tasks._conn()
    original = session_store.list_sessions

    def checked(*args, **kwargs):
        assert _db.conn() is not state_writer
        assert tasks._conn() is not task_writer
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            _db.conn().execute("DELETE FROM session_meta")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            tasks._conn().execute("DELETE FROM tasks")
        return original(*args, **kwargs)

    monkeypatch.setattr(session_store, "list_sessions", checked)
    async with TestClient(TestServer(sidebar_app)) as client:
        response = await client.get("/conversations")
        assert response.status == 200
        await response.read()
    assert _db.conn() is state_writer
    assert tasks._conn() is task_writer
    assert _db._READ_DB.get() is None
    assert tasks._READ_DB.get() is None


@pytest.mark.anyio
async def test_parallel_sidebars_materialize_legacy_agent_once(sidebar_app, monkeypatch, tmp_path):
    from vococo.memory import projects

    project = tmp_path / "legacy"
    project.mkdir()
    projects.upsert_project(str(project))
    phash = projects.project_hash(str(project))
    session_store.set_title(f"web:p{phash}:old", "旧会话")
    entered, writing = threading.Barrier(2), threading.Barrier(2)
    materialize, write_meta = agents._materialize, agents._write_meta
    writes = []

    def synchronized_materialize(agent_id):
        entered.wait(timeout=3)
        return materialize(agent_id)

    def counted_write(agent_id, meta):
        writes.append(agent_id)
        # 未串行保护时两次补建会一起到这里；正确实现只有一次，短暂等待后继续。
        try:
            writing.wait(timeout=0.2)
        except threading.BrokenBarrierError:
            pass
        write_meta(agent_id, meta)

    monkeypatch.setattr(agents, "_materialize", synchronized_materialize)
    monkeypatch.setattr(agents, "_write_meta", counted_write)
    async with TestClient(TestServer(sidebar_app)) as client:
        responses = await asyncio.gather(client.get("/conversations"), client.get("/conversations"))
        assert [response.status for response in responses] == [200, 200]
        for response in responses:
            await response.read()
    assert writes == [phash]
    assert agents.get(phash)["workdir"] == str(project.resolve())


def test_agent_read_snapshot_is_discarded_after_exception(sidebar_app):
    owner = agents.create("项目")
    with pytest.raises(RuntimeError), agents.read_snapshot():
        cached = agents.get(agent_id=owner["id"])
        cached["permission"] = "full"
        cached["links"].append({"path": "/tmp/不可传播"})
        assert agents.get(agent_id=owner["id"])["permission"] == "standard"
        assert agents.get(agent_id=owner["id"])["links"] == []
        raise RuntimeError("模拟读取失败")
    agents.update(owner["id"], name="新名字")
    assert agents.get(owner["id"])["name"] == "新名字"
    assert agents._READ_SNAPSHOT.get() is None


def test_agent_read_snapshots_are_isolated_between_threads(sidebar_app):
    from concurrent.futures import ThreadPoolExecutor

    ready = threading.Barrier(2)

    def read():
        with agents.read_snapshot():
            cache = agents._READ_SNAPSHOT.get()
            ready.wait(timeout=3)
            return cache

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: read(), range(2)))
    assert first is not second
    assert agents._READ_SNAPSHOT.get() is None


@pytest.mark.anyio
@pytest.mark.parametrize("route", ["/conversations", "/voice/sidebar", "/cron/sidebar", "/conv/search?q=测试"])
async def test_slow_sidebar_storage_does_not_block_other_requests(sidebar_app, monkeypatch, route):
    entered, release = threading.Event(), threading.Event()
    name = ("search_sessions" if route.startswith("/conv/search") else
            "pending_reviews" if route == "/cron/sidebar" else "list_sessions")
    original = getattr(session_store, name)

    def slow(*args, **kwargs):
        entered.set()
        release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(session_store, name, slow)
    async with TestClient(TestServer(sidebar_app)) as client:
        pending = asyncio.create_task(client.get(route))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            response = await client.get("/ping")
            assert response.status == 200
            assert not pending.done(), "读取侧栏时整个服务被卡住了"
        finally:
            release.set()
            response = await pending
            await response.read()
        assert response.status == 200
