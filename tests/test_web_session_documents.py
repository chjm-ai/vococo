"""会话详情「文档」入口：只聚合文档，不把代码文件混进列表。"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import session_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def documents_app(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([web.get("/conv/documents", adapter._handle_conv_documents)])
    return app


@pytest.mark.anyio
async def test_session_documents_collects_document_mentions_and_file_changes(documents_app):
    turn_id = session_store.start_turn(
        "web:docs", "请查看 docs/brief.md、https://example.com/spec.pdf 和 core/app.py"
    )
    session_store.finish_turn(
        turn_id,
        "已更新 reports/summary.xlsx，也检查了 settings.py。",
        events=[
            {"type": "tool", "name": "Write", "input": {"file_path": "/tmp/plan.md"}},
            {"type": "tool", "name": "Edit", "input": {"file_path": "reports/summary.xlsx"}},
            {"type": "tool", "name": "Edit", "input": {"file_path": "core/settings.py"}},
        ],
    )

    async with TestClient(TestServer(documents_app)) as client:
        resp = await client.get("/conv/documents?conv=docs")
        assert resp.status == 200
        documents = (await resp.json())["documents"]

    by_path = {item["path"]: item for item in documents}
    assert by_path["/tmp/plan.md"]["actions"] == ["创建"]
    assert by_path["reports/summary.xlsx"]["actions"] == ["编辑", "提及"]
    assert by_path["docs/brief.md"]["actions"] == ["提及"]
    assert by_path["https://example.com/spec.pdf"]["actions"] == ["提及"]
    assert "core/app.py" not in by_path
    assert "core/settings.py" not in by_path
    # 创建/编辑的文档优先显示，方便从会话详情直接打开成果。
    assert [item["path"] for item in documents[:2]] == ["/tmp/plan.md", "reports/summary.xlsx"]
