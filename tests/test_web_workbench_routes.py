"""工作台管理接口:/workbench、/workbench/projects/*、/workbench/tasks/*。

只测新接口本身(数据模型见 vococo/memory/workbench.py 模块头说明)。项目/任务的创建、
更新、删除、图片上传全走 HTTP,不经过 danger 审批——用户在工作台界面上的直接操作,
跟 tools/builtin.py 里挂给 AI 的 MCP 工具是两条不同的入口(那条走 danger.require_approval)。
"""
from __future__ import annotations

import base64

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.gateway.adapters.web import WebAdapter


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def workbench_web_app(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(config, "IMAGES_DIR", isolated / "data" / "images")
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes(
        [
            web.get("/workbench", adapter._handle_workbench),
            web.post("/workbench/projects/create", adapter._handle_workbench_project_create),
            web.post("/workbench/projects/rename", adapter._handle_workbench_project_rename),
            web.post("/workbench/projects/archive", adapter._handle_workbench_project_archive),
            web.post("/workbench/projects/reorder", adapter._handle_workbench_project_reorder),
            web.post("/workbench/tasks/create", adapter._handle_workbench_task_create),
            web.post("/workbench/tasks/update", adapter._handle_workbench_task_update),
            web.post("/workbench/tasks/delete", adapter._handle_workbench_task_delete),
            web.get("/workbench/trash", adapter._handle_workbench_trash),
            web.post("/workbench/tasks/restore", adapter._handle_workbench_task_restore),
            web.post("/workbench/tasks/purge", adapter._handle_workbench_task_purge),
            web.post("/workbench/tasks/image/add", adapter._handle_workbench_task_image_add),
            web.post("/workbench/tasks/image/remove", adapter._handle_workbench_task_image_remove),
        ]
    )
    return app


@pytest.mark.anyio
async def test_workbench_bundle_is_seeded_on_first_load(workbench_web_app):
    async with TestClient(TestServer(workbench_web_app)) as client:
        resp = await client.get("/workbench")
        data = await resp.json()
        assert {p["name"] for p in data["projects"]} == {"AI 咨询", "VocoTrade", "面料外贸", "离职过渡"}
        assert len(data["tasks"]) == 12
        assert data["sources"]


@pytest.mark.anyio
async def test_create_project_then_create_task_in_it(workbench_web_app):
    async with TestClient(TestServer(workbench_web_app)) as client:
        resp = await client.post("/workbench/projects/create", json={"name": "新副业"})
        assert resp.status == 200
        project = (await resp.json())["project"]
        assert project["name"] == "新副业"

        resp = await client.post(
            "/workbench/tasks/create",
            json={"project": project["id"], "title": "第一条任务", "date": "2026-08-25"},
        )
        assert resp.status == 200
        task = (await resp.json())["task"]
        assert task["project"] == project["id"]
        assert task["status"] == "todo"

        resp = await client.get("/workbench")
        tasks = (await resp.json())["tasks"]
        assert any(t["id"] == task["id"] for t in tasks)


@pytest.mark.anyio
async def test_create_project_rejects_empty_name(workbench_web_app):
    async with TestClient(TestServer(workbench_web_app)) as client:
        resp = await client.post("/workbench/projects/create", json={"name": "  "})
        assert resp.status == 400


@pytest.mark.anyio
async def test_rename_and_archive_project_via_http(workbench_web_app):
    async with TestClient(TestServer(workbench_web_app)) as client:
        resp = await client.post("/workbench/projects/create", json={"name": "待改名"})
        project = (await resp.json())["project"]

        resp = await client.post(
            "/workbench/projects/rename", json={"id": project["id"], "name": "已改名"}
        )
        assert (await resp.json())["project"]["name"] == "已改名"

        resp = await client.post("/workbench/projects/archive", json={"id": project["id"]})
        assert resp.status == 200

        resp = await client.get("/workbench")
        names = {p["name"] for p in (await resp.json())["projects"]}
        assert "已改名" not in names


@pytest.mark.anyio
async def test_update_and_delete_task_via_http(workbench_web_app):
    async with TestClient(TestServer(workbench_web_app)) as client:
        resp = await client.get("/workbench")
        project_id = (await resp.json())["projects"][0]["id"]

        resp = await client.post(
            "/workbench/tasks/create", json={"project": project_id, "title": "待更新任务"}
        )
        task_id = (await resp.json())["task"]["id"]

        resp = await client.post(
            "/workbench/tasks/update", json={"id": task_id, "status": "focus"}
        )
        assert (await resp.json())["task"]["status"] == "focus"

        resp = await client.post("/workbench/tasks/delete", json={"id": task_id})
        assert resp.status == 200

        resp = await client.get("/workbench")
        assert task_id not in {t["id"] for t in (await resp.json())["tasks"]}


@pytest.mark.anyio
async def test_trash_lifecycle_via_http(workbench_web_app):
    async with TestClient(TestServer(workbench_web_app)) as client:
        resp = await client.get("/workbench")
        project_id = (await resp.json())["projects"][0]["id"]

        resp = await client.post(
            "/workbench/tasks/create", json={"project": project_id, "title": "要删的任务"}
        )
        task_id = (await resp.json())["task"]["id"]

        resp = await client.post("/workbench/tasks/delete", json={"id": task_id})
        assert resp.status == 200

        # 主列表不再包含它,回收站里能看到
        resp = await client.get("/workbench")
        assert task_id not in {t["id"] for t in (await resp.json())["tasks"]}
        resp = await client.get("/workbench/trash")
        trashed = (await resp.json())["tasks"]
        assert task_id in {t["id"] for t in trashed}

        # 恢复:回到主列表,不在回收站
        resp = await client.post("/workbench/tasks/restore", json={"id": task_id})
        assert resp.status == 200
        assert (await resp.json())["task"]["deletedAt"] is None
        resp = await client.get("/workbench")
        assert task_id in {t["id"] for t in (await resp.json())["tasks"]}
        resp = await client.get("/workbench/trash")
        assert task_id not in {t["id"] for t in (await resp.json())["tasks"]}

        # 还没删,彻底删除应该被拒绝
        resp = await client.post("/workbench/tasks/purge", json={"id": task_id})
        assert resp.status == 404

        # 删了之后彻底删除才成功,且回收站里也没了
        await client.post("/workbench/tasks/delete", json={"id": task_id})
        resp = await client.post("/workbench/tasks/purge", json={"id": task_id})
        assert resp.status == 200
        resp = await client.get("/workbench/trash")
        assert task_id not in {t["id"] for t in (await resp.json())["tasks"]}


@pytest.mark.anyio
async def test_task_image_add_and_remove_via_http(workbench_web_app):
    async with TestClient(TestServer(workbench_web_app)) as client:
        resp = await client.get("/workbench")
        project_id = (await resp.json())["projects"][0]["id"]
        resp = await client.post(
            "/workbench/tasks/create", json={"project": project_id, "title": "带图任务"}
        )
        task_id = (await resp.json())["task"]["id"]

        data = base64.b64encode(b"fake-png-bytes").decode()
        resp = await client.post(
            "/workbench/tasks/image/add",
            json={"id": task_id, "data": data, "mediaType": "image/png"},
        )
        assert resp.status == 200
        name = (await resp.json())["name"]
        assert (config.IMAGES_DIR / name).is_file()

        resp = await client.post(
            "/workbench/tasks/image/remove", json={"id": task_id, "name": name}
        )
        assert (await resp.json())["ok"] is True
        assert not (config.IMAGES_DIR / name).is_file()
