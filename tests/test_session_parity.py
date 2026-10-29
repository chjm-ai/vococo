"""普通与任务会话的名称、模型和运行中用量保持一致。"""
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from test_unified_session_web import setup_web
from vococo import config
from vococo.core import task_events, task_runner, tasks
from vococo.core.agent import AgentReply, Done
from vococo.gateway.adapters.web import WebAdapter, _WebSink
from vococo.memory import session_store
from vococo.tools import sessions


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("legacy", [False, True])
async def test_task_title_search_rename_and_duplicate_use_same_name(setup_web, legacy):
    adapter, app, _ = setup_web
    app.router.add_post("/rename", adapter._handle_rename)
    task = tasks.create("运维收尾重派", "检查", origin="chat", agent_id="general")
    key = task["session_key"]
    if legacy:
        session_store._conn().execute("UPDATE session_meta SET title=NULL WHERE session_key=?", (key,))
        session_store._conn().commit()
    source = config.new_session_key()
    session_store.bind_session(source, agent_id="general")
    session_store.append(source, "问题", "答复")
    assert sessions._list(source, "运维收尾重派", 20)["total"] == 1
    async with TestClient(TestServer(app)) as client:
        results = (await (await client.get("/search?q=运维收尾重派")).json())["results"]
        assert next(r for r in results if r["conv"] == key)["title"] == "运维收尾重派"
        response = await client.post("/duplicate", json={"conv": key})
        assert (await response.json())["title"] == "运维收尾重派副本"
        assert (await client.post("/rename", json={"conv": key, "title": "新的会话名"})).status == 200
        rows = (await (await client.get("/voice/sidebar")).json())["tasks"]
        assert next(r for r in rows if r["conv"] == key)["title"] == "新的会话名"
        results = (await (await client.get("/search?q=新的会话名")).json())["results"]
        assert next(r for r in results if r["conv"] == key)["title"] == "新的会话名"
    assert sessions._list(source, "新的会话名", 20)["total"] == 1


def test_legacy_title_search_preserves_recency_archive_and_limit(setup_web):
    from vococo.gateway.adapters.web import _search_session_rows

    for _ in range(50):
        key = config.new_session_key()
        session_store.bind_session(key, agent_id="general")
        session_store.set_title(key, "同名关键字")
        session_store.append(key, "问题", "答复")
    task = tasks.create("同名关键字", "检查", origin="chat", agent_id="general")
    key = task["session_key"]
    session_store._conn().execute("UPDATE session_meta SET title=NULL WHERE session_key=?", (key,))
    session_store._conn().commit()
    session_store.append(key, "最近更新", "完成")
    session_store.set_conv_archived(key, True)
    rows = _search_session_rows("同名关键字")
    assert len(rows) == 50
    assert rows[0]["key"] == key and rows[0]["archived"] is True
    assert rows[0]["last_ts"] is not None
    session_store.set_title(key, "已经改名")
    assert key not in {row["key"] for row in _search_session_rows("同名关键字")}
    session_store.delete_session(key)
    assert key not in {row["key"] for row in _search_session_rows("已经改名")}


@pytest.mark.anyio
@pytest.mark.parametrize("chosen", ["", "explicit-model"])
async def test_task_uses_shared_default_and_keeps_live_context_on_interruption(isolated, monkeypatch, chosen):
    from vococo.core.agent import ContextUsage

    monkeypatch.setattr(tasks, "_DB", None)
    monkeypatch.setattr(task_runner, "_running", {})
    monkeypatch.setattr(task_events, "_terminal_handlers", [])
    monkeypatch.setattr(task_events, "_started_tasks", set())
    monkeypatch.setattr(task_events, "_main_event_bridge", None)
    monkeypatch.setattr(task_events, "_stream_sink_factory", None)
    monkeypatch.setattr(config, "MODEL", "default-model")
    monkeypatch.setattr(task_runner.providers, "resolve", lambda model, *_: (model, {}))
    monkeypatch.setattr(task_runner.providers, "sidecar_env", lambda *a, **kw: ("wrong-model", {}))
    adapter, events = WebAdapter(), []
    monkeypatch.setattr(adapter, "_emit", events.append)
    monkeypatch.setattr(adapter, "_push_notify", lambda **kw: None)

    async def stream(*args, session_key=None, model=None, **kwargs):
        assert model == (chosen or config.MODEL)
        yield ContextUsage(12000, 100000, model)
        summary = session_store.session_summary(session_key)
        assert summary["ctx_tokens"] == 12000
        assert summary["total_tokens"] == 0
        assert next(e for e in events if e["type"] == "usage")["ctx_tokens"] == 12000
        raise RuntimeError("中途失败")
        yield Done(AgentReply("", [], None, False))

    monkeypatch.setattr(task_runner, "stream_turn", stream)
    row = task_runner.dispatch(title="任务", prompt="执行", model=chosen or None, origin="chat", cwd=str(isolated))
    try:
        await task_runner._running[row["id"]]
        assert session_store.session_summary(row["session_key"])["ctx_tokens"] == 12000
        assert tasks.get(row["id"])["status"] == "failed"
    finally:
        if tasks._DB is not None:
            tasks._DB.close()
            tasks._DB = None


@pytest.mark.anyio
async def test_shared_live_context_preserves_accounting_and_rejects_deleted_turn(isolated, monkeypatch):
    from vococo.core.agent import ContextUsage
    from vococo.core.session_execution import SharedTurn

    key = config.new_session_key()
    session_store.bind_session(key, agent_id="general")
    tid = session_store.start_turn(key, "问题")
    session_store.record_usage(key, 100, 50, window=1000, last_in=20, last_cache=30, last_out=10)
    adapter, events = WebAdapter(), []
    monkeypatch.setattr(adapter, "_emit", events.append)
    shared = SharedTurn(key, tid, _WebSink(adapter, key))
    await shared.observe(ContextUsage(200, 1000, "model"))
    meta = session_store.session_summary(key)
    assert (meta["ctx_tokens"], meta["total_tokens"], meta["last_in"], meta["last_cache"]) == (200, 50, 20, 30)
    assert events[-1]["type"] == "usage"
    event_count = len(events)
    session_store._conn().execute("DELETE FROM turns WHERE id=?", (tid,))
    session_store._conn().commit()
    await shared.observe(ContextUsage(900, 1000, "model"))
    assert session_store.session_summary(key)["ctx_tokens"] == 200
    assert len(events) == event_count
