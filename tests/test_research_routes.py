"""研究入口使用真实临时数据库，模型执行由独立核心测试覆盖。"""
from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.core import research, research_store, task_runner, tasks
from vococo.gateway import clarify, task_routes
from vococo.tools import research_tools


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


def _payload():
    return {"title": "技术选型", "prompt": "比较 Python 的任务队列，引用官方资料",
            "model": "claude-sonnet-5-5"}


@pytest.mark.anyio
async def test_start_detail_and_pending_report(client, monkeypatch):
    monkeypatch.setattr(config, "SESSION_KEY", "shared-session")
    response = await client.post("/research?session_key=main", json=_payload())
    assert response.status == 202
    row = await response.json()
    assert row["kind"] == "research" and row["status"] == "queued"
    assert row["dispatch_chat_id"] == "main"
    assert row["source_session_key"] == "shared-session"
    state = await (await client.get(f"/tasks/{row['id']}/research")).json()
    assert state["stage"] == "plan" and state["budget"]["searches"] == 12
    pending = await client.get(f"/tasks/{row['id']}/report")
    assert pending.status == 409
    assert (await client.get("/tasks/missing/research")).status == 404
    assert (await client.get("/tasks/missing/report")).status == 404


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
@pytest.mark.parametrize("patch", [
    {"prompt": ""}, {"title": []}, {"model": 1}, {"budget": {"tokens": 0}},
    {"budget": {"seconds": True}}, {"budget": {"unknown": 1}},
])
async def test_invalid_start_does_not_queue(client, patch):
    response = await client.post("/research", json={**_payload(), **patch})
    assert response.status == 400
    assert tasks.list_queued() == []


@pytest.mark.anyio
async def test_progress_is_slim_and_compatible_with_old_state(client):
    row = await (await client.post("/research", json=_payload())).json()
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
    assert (await client.get("/tasks/missing/research/progress")).status == 404
    monkey_task = tasks.create("普通任务", "问题")
    assert (await client.get(f"/tasks/{monkey_task['id']}/research/progress")).status == 404


@pytest.mark.anyio
async def test_invalid_json_and_nested_dispatch(client):
    response = await client.post("/research", data="not-json", headers={"Content-Type": "application/json"})
    assert response.status == 400
    response = await client.post("/research?session_key=task:parent", json=_payload())
    assert response.status == 400
    assert tasks.list_queued() == []


@pytest.mark.anyio
async def test_reports_download_and_resume_preserves_usage(client):
    row = await (await client.post("/research", json=_payload())).json()
    task_id = row["id"]
    tasks.set_status(task_id, "running")
    tasks.finish(task_id, "failed", "", "中断")
    research_store.update(task_id, lambda state: state.update(
        status="interrupted", usage={"searches": 2, "fetches": 1, "tokens": 500}))
    response = await client.post(f"/tasks/{task_id}/resume")
    assert response.status == 202
    assert research.get(task_id)["usage"]["tokens"] == 500
    response = await client.post(f"/tasks/{task_id}/resume")
    assert response.status == 409
    research_store.update(task_id, lambda state: state.update(status="done", report="# 测试报告\n引用 [S1]"))
    response = await client.get(f"/tasks/{task_id}/report")
    assert response.status == 200
    assert "attachment" in response.headers["Content-Disposition"]
    assert "测试报告" in await response.text()
    assert response.headers["Content-Type"].startswith("text/markdown")


@pytest.mark.anyio
async def test_expired_research_cannot_resume(client):
    row = await (await client.post("/research", json=_payload())).json()
    tasks.set_status(row["id"], "running")
    tasks.finish(row["id"], "failed", "", "中断")
    research_store.update(row["id"], lambda state: state.update(deadline=0))
    response = await client.post(f"/tasks/{row['id']}/resume")
    assert response.status == 409
    assert tasks.get(row["id"])["status"] == "failed"


@pytest.mark.anyio
async def test_all_research_routes_require_auth(client, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "secret")
    for method, path in (("post", "/research"), ("get", "/tasks/id/research"),
                         ("get", "/tasks/id/research/progress"),
                         ("post", "/tasks/id/resume"), ("get", "/tasks/id/report")):
        response = await getattr(client, method)(path)
        assert response.status == 401
    assert tasks.list_queued() == []


@pytest.mark.anyio
async def test_tools_launch_query_and_reject_nested_dispatch(db):
    result = await research_tools.start_research.handler(_payload())
    assert not result["isError"]
    data = json.loads(result["content"][0]["text"])
    state = await research_tools.get_research.handler({"id": data["id"]})
    assert json.loads(state["content"][0]["text"])["stage"] == "plan"
    result = await research_tools.resume_research.handler({"id": data["id"]})
    assert result["isError"]
    token = clarify.bind_turn("task:parent", 1)
    try:
        result = await research_tools.start_research.handler(_payload())
        assert result["isError"]
        assert len(tasks.list_queued()) == 1
    finally:
        clarify.unbind_turn("task:parent", 1, token)


@pytest.mark.parametrize("data", [None, [], {"title": "a"}, {"title": "x" * 81, "prompt": "b"}])
def test_tool_input_validation(data):
    with pytest.raises(ValueError):
        research_tools.request_options(data)
