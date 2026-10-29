"""研究入口移除后，历史数据仍通过受认证保护的接口读取。"""
from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from claude_agent_sdk._internal.sdk_mcp_bridge import SdkMcpBridge

from vococo import config
from vococo.core import research, research_store, task_runner, tasks
from vococo.gateway import task_routes
from vococo.tools import builtin


@pytest.fixture
def db(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(tasks, "_DB", None)
    monkeypatch.setattr(task_runner, "_maybe_start_next", lambda: None)
    yield
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None


@pytest.fixture
async def client(db):
    app = web.Application()
    task_routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        yield client


def _historical_research():
    row = tasks.create("技术选型", "比较 Python 的任务队列", kind="research")
    research.initialize(row["id"], row["prompt"], "claude-sonnet-5-5")
    return row


@pytest.mark.anyio
async def test_historical_detail_and_pending_report(client):
    row = _historical_research()
    state = await (await client.get(f"/tasks/{row['id']}/research")).json()
    assert state["stage"] == "plan" and state["budget"]["searches"] == 12
    pending = await client.get(f"/tasks/{row['id']}/report")
    assert pending.status == 409
    assert (await client.get("/tasks/missing/research")).status == 404
    assert (await client.get("/tasks/missing/report")).status == 404
    detail = await (await client.get(f"/tasks/{row['id']}")).json()
    assert detail["kind"] == "research"


@pytest.mark.anyio
@pytest.mark.parametrize(("source", "conv"), [("web:source", "source"), ("shared-session", "main")])
async def test_task_summary_and_activity_share_normalized_conversation(client, monkeypatch, source, conv):
    from vococo.core import task_events

    monkeypatch.setattr(config, "SESSION_KEY", "shared-session")
    row = tasks.create("研究摘要", "问题", kind="research", source_session_key=source)
    rows = await (await client.get("/tasks?source=all")).json()
    summary = next(item for item in rows if item["id"] == row["id"])
    assert summary["source_session_key"] == source
    assert summary["conversation_id"] == conv
    queue = task_events.subscribe()
    try:
        task_events.on_task_activity(row)
        event, payload = queue.get_nowait()
        assert event == "task_update"
        assert payload["source_session_key"] == source
        assert payload["conversation_id"] == conv
    finally:
        task_events.unsubscribe(queue)


@pytest.mark.anyio
@pytest.mark.parametrize("body", [
    {"title": "研究", "prompt": "问题"}, {}, {"budget": {"tokens": 0}}, "not-json",
])
async def test_removed_start_never_dispatches(client, monkeypatch, body):
    def forbidden(*args, **kwargs):
        pytest.fail("移除后的 HTTP 入口不能派发研究")
    monkeypatch.setattr(task_runner, "dispatch_research", forbidden)
    response = await client.post("/research?session_key=main", data=json.dumps(body),
                                 headers={"Content-Type": "application/json"})
    assert response.status == 410
    assert "已移除" in (await response.json())["error"]
    assert tasks.list_research() == []
    assert not list(config.DATA_DIR.glob("research*"))


@pytest.mark.anyio
async def test_progress_is_slim_and_compatible_with_old_state(client):
    row = _historical_research()
    task_id = row["id"]
    def legacy(state):
        state.pop("activity", None)
        state.update(checkpoints={"plan": "公开计划", "report": "报告草稿"},
                     report="完整报告", search_results=[{"detail": "搜索全文"}],
                     sources=[{"id": "S1", "title": "标题", "url": "https://example.com/",
                               "extracts": [{"text": "网页全文", "fetched_at": 123}],
                               "claims": [{"quote": "秘密摘录", "claim": "待验证"}]}])
    research_store.update(task_id, legacy)
    response = await client.get(f"/tasks/{task_id}/research/progress")
    assert response.status == 200
    progress = await response.json()
    assert progress["id"] == task_id and progress["title"] == row["title"]
    assert progress["task_status"] == "queued"
    assert progress["checkpoints"] == {"plan": "公开计划"}
    assert progress["activity"] == [] and progress["limited_stages"] == []
    assert progress["sources"] == [{"id": "S1", "title": "标题", "url": "https://example.com/",
                                    "fetched_at": 123, "claims_count": 1}]
    assert not {"report", "search_results", "question", "error"} & progress.keys()
    assert "网页全文" not in json.dumps(progress, ensure_ascii=False)
    detail = await (await client.get(f"/tasks/{task_id}/research")).json()
    assert detail["sources"][0]["extracts"][0]["text"] == "网页全文"
    assert (await client.get("/tasks/missing/research/progress")).status == 404
    ordinary = tasks.create("普通任务", "问题")
    assert (await client.get(f"/tasks/{ordinary['id']}/research/progress")).status == 404


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled", "done", "missing"])
async def test_removed_resume_does_not_change_historical_data(client, monkeypatch, status):
    row = _historical_research()
    task_id = row["id"]
    tasks.set_status(task_id, status if status != "missing" else "failed")
    research_store.update(task_id, lambda state: state.update(
        status="interrupted", usage={"searches": 2, "fetches": 1, "tokens": 500}))
    before_task, before_state = tasks.get(task_id), research.get(task_id)
    async def forbidden(*args, **kwargs):
        pytest.fail("移除后的 HTTP 入口不能恢复研究")
    monkeypatch.setattr(task_runner, "resume_research", forbidden)
    target = "missing" if status == "missing" else task_id
    response = await client.post(f"/tasks/{target}/resume")
    assert response.status == 410
    assert "已移除" in (await response.json())["error"]
    assert tasks.get(task_id) == before_task
    assert research.get(task_id) == before_state


@pytest.mark.anyio
async def test_historical_report_download_and_cancel(client):
    row = _historical_research()
    task_id = row["id"]
    research_store.update(task_id, lambda state: state.update(status="done", report="# 测试报告\n引用 [S1]"))
    response = await client.get(f"/tasks/{task_id}/report")
    assert response.status == 200
    assert "attachment" in response.headers["Content-Disposition"]
    assert "测试报告" in await response.text()
    assert response.headers["Content-Type"].startswith("text/markdown")
    assert (await (await client.post(f"/tasks/{task_id}/stop")).json())["ok"]
    assert tasks.get(task_id)["status"] == "cancelled"
    assert research.get(task_id)["report"] == "# 测试报告\n引用 [S1]"


@pytest.mark.anyio
async def test_all_research_routes_require_auth(client, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "secret")
    for method, path in (("post", "/research"), ("get", "/tasks/id/research"),
                         ("get", "/tasks/id/research/progress"), ("post", "/tasks/id/stop"),
                         ("post", "/tasks/id/resume"), ("get", "/tasks/id/report")):
        response = await getattr(client, method)(path)
        assert response.status == 401
    assert tasks.list_queued() == []


@pytest.mark.anyio
async def test_mcp_list_excludes_removed_research_tools(db):
    server = builtin.build_mcp_servers()["vococo"]["instance"]
    bridge = SdkMcpBridge("vococo", server)
    try:
        initialized = await bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"}}})
        assert "result" in initialized
        await bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        result = await bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in result["result"]["tools"]}
    finally:
        await bridge.aclose()
    assert not {"start_research", "get_research", "resume_research"} & names
    assert {"dispatch_session", "recall_past", "send_message"} <= names
