"""定时任务管理界面的接口:/cron/sidebar、/cron/jobs/create|update|enable|delete。

只测新接口本身(数据模型见 vococo/cron/scheduler.py 的 job 结构注释:
每个任务一条专属会话 conv=task:<id>(job_id 复用为 task_id),创建/启停/删除都不经过 danger 审批——
这是用户在管理界面上的直接操作,不是 agent 代为执行,见与用户的设计讨论)。
"""
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
def cron_web_app(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    data = isolated / "data"
    data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "CRON_JOBS_PATH", data / "cron_jobs.json")
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes(
        [
            web.get("/cron/sidebar", adapter._handle_cron_sidebar),
            web.post("/cron/jobs/create", adapter._handle_cron_create),
            web.post("/cron/jobs/update", adapter._handle_cron_update),
            web.post("/cron/jobs/enable", adapter._handle_cron_set_enabled),
            web.post("/cron/jobs/delete", adapter._handle_cron_delete),
        ]
    )
    return app


@pytest.mark.anyio
async def test_sidebar_empty_then_create_shows_job(cron_web_app):
    async with TestClient(TestServer(cron_web_app)) as client:
        resp = await client.get("/cron/sidebar")
        assert (await resp.json())["jobs"] == []

        resp = await client.post(
            "/cron/jobs/create",
            json={
                "name": "晨间简报",
                "prompt": "汇总今天的安排",
                "schedule": {"kind": "cron", "expr": "0 8 * * *"},
            },
        )
        assert resp.status == 200
        job = (await resp.json())["job"]
        assert job["conv"] == f"task:{job['id']}"
        assert job["enabled"] is True

        resp = await client.get("/cron/sidebar")
        jobs = (await resp.json())["jobs"]
        assert len(jobs) == 1
        assert jobs[0]["conv"] == job["conv"]
        assert jobs[0]["title"] == "晨间简报"
        assert jobs[0]["schedule_desc"] == "0 8 * * *"
        assert jobs[0]["enabled"] is True


@pytest.mark.anyio
async def test_create_accepts_and_normalizes_cwd(cron_web_app, tmp_path):
    async with TestClient(TestServer(cron_web_app)) as client:
        cwd = tmp_path / "obsidian-project"
        cwd.mkdir()
        resp = await client.post(
            "/cron/jobs/create",
            json={
                "name": "整理笔记", "prompt": "整理项目文档",
                "schedule": {"kind": "cron", "expr": "0 8 * * *"}, "cwd": str(cwd),
            },
        )
        assert resp.status == 200
        assert (await resp.json())["job"]["cwd"] == str(cwd.resolve())


@pytest.mark.anyio
async def test_create_rejects_bad_cron_and_empty_fields(cron_web_app):
    async with TestClient(TestServer(cron_web_app)) as client:
        resp = await client.post(
            "/cron/jobs/create",
            json={"name": "坏任务", "prompt": "x", "schedule": {"kind": "cron", "expr": "not a cron"}},
        )
        assert resp.status == 400

        resp = await client.post(
            "/cron/jobs/create", json={"name": "", "prompt": "", "schedule": {}}
        )
        assert resp.status == 400


@pytest.mark.anyio
async def test_enable_and_delete(cron_web_app):
    async with TestClient(TestServer(cron_web_app)) as client:
        resp = await client.post(
            "/cron/jobs/create",
            json={
                "name": "每周复盘",
                "prompt": "回顾这周",
                "schedule": {"kind": "cron", "expr": "0 21 * * 0"},
            },
        )
        job = (await resp.json())["job"]

        resp = await client.post("/cron/jobs/enable", json={"id": job["id"], "enabled": False})
        assert resp.status == 200
        assert (await resp.json())["job"]["enabled"] is False

        resp = await client.get("/cron/sidebar")
        assert (await resp.json())["jobs"][0]["enabled"] is False

        # 运行结果落进专属会话后,删除任务应该把会话历史一起清掉
        session_store.append(job["conv"], "回顾这周", "本周完成了 A/B")
        resp = await client.post("/cron/jobs/delete", json={"id": job["id"]})
        assert resp.status == 200

        resp = await client.get("/cron/sidebar")
        assert (await resp.json())["jobs"] == []
        assert session_store.load_recent(job["conv"]) == []


@pytest.mark.anyio
async def test_sidebar_reports_pending_review_after_run(cron_web_app):
    """跑完一次任务(落库 + 标未读)后,侧边栏得看得到那颗灰点——session_summary()本身
    不带这个字段,是靠 list_sessions() 单独查回来合并的,回归见 web.py 里的注释。"""
    async with TestClient(TestServer(cron_web_app)) as client:
        resp = await client.post(
            "/cron/jobs/create",
            json={"name": "晨间简报", "prompt": "汇总", "schedule": {"kind": "cron", "expr": "0 8 * * *"}},
        )
        job = (await resp.json())["job"]

        session_store.append(job["conv"], "汇总", "今天没什么特别的")
        session_store.set_pending_review(job["conv"], True)

        resp = await client.get("/cron/sidebar")
        row = (await resp.json())["jobs"][0]
        assert row["pending_review"] is True


@pytest.mark.anyio
async def test_enable_unknown_id_404(cron_web_app):
    async with TestClient(TestServer(cron_web_app)) as client:
        resp = await client.post("/cron/jobs/enable", json={"id": "ghost", "enabled": True})
        assert resp.status == 404
        resp = await client.post("/cron/jobs/delete", json={"id": "ghost"})
        assert resp.status == 404


@pytest.mark.anyio
async def test_update_edits_fields_and_sidebar_reflects_it(cron_web_app):
    async with TestClient(TestServer(cron_web_app)) as client:
        resp = await client.post(
            "/cron/jobs/create",
            json={
                "name": "晨间简报",
                "prompt": "汇总今天的安排",
                "schedule": {"kind": "cron", "expr": "0 8 * * *"},
            },
        )
        job = (await resp.json())["job"]

        resp = await client.post(
            "/cron/jobs/update",
            json={
                "id": job["id"],
                "name": "晚间简报",
                "prompt": "汇总今天完成了什么",
                "schedule": {"kind": "cron", "expr": "0 21 * * *"},
                "target": {"platform": "web", "chat_id": "conv1"},
            },
        )
        assert resp.status == 200
        updated = (await resp.json())["job"]
        assert updated["name"] == "晚间简报"
        assert updated["prompt"] == "汇总今天完成了什么"
        assert updated["schedule"] == {"kind": "cron", "expr": "0 21 * * *"}
        assert updated["target"] == {"platform": "web", "chat_id": "conv1"}
        assert updated["cwd"] is None
        assert updated["id"] == job["id"]
        assert updated["conv"] == job["conv"]  # 编辑不改 id/conv

        resp = await client.get("/cron/sidebar")
        row = (await resp.json())["jobs"][0]
        assert row["title"] == "晚间简报"
        assert row["schedule_desc"] == "0 21 * * *"
        assert row["prompt"] == "汇总今天完成了什么"


@pytest.mark.anyio
async def test_update_rejects_bad_cron_and_unknown_id(cron_web_app):
    async with TestClient(TestServer(cron_web_app)) as client:
        resp = await client.post(
            "/cron/jobs/create",
            json={"name": "任务", "prompt": "x", "schedule": {"kind": "cron", "expr": "0 8 * * *"}},
        )
        job = (await resp.json())["job"]

        resp = await client.post(
            "/cron/jobs/update",
            json={"id": job["id"], "name": "任务", "prompt": "x", "schedule": {"kind": "cron", "expr": "bad"}},
        )
        assert resp.status == 400

        resp = await client.post(
            "/cron/jobs/update",
            json={"id": "ghost", "name": "a", "prompt": "b", "schedule": {"kind": "cron", "expr": "0 8 * * *"}},
        )
        assert resp.status == 404


@pytest.mark.anyio
async def test_create_script_job_and_expose_fields_in_sidebar(cron_web_app):
    """管理界面可完整创建脚本任务,侧栏回填编辑需要的执行字段。"""
    async with TestClient(TestServer(cron_web_app)) as client:
        payload = {
            "name": "固定巡检", "prompt": "检查状态", "mode": "script",
            "command": "python3 ~/scripts/check.py", "summarize_prompt": "有异常才说明",
            "schedule": {"kind": "cron", "expr": "0 8 * * *"},
        }
        resp = await client.post("/cron/jobs/create", json=payload)
        assert resp.status == 200
        job = (await resp.json())["job"]
        assert job["mode"] == "script"
        assert job["command"] == payload["command"]

        resp = await client.get("/cron/sidebar")
        row = (await resp.json())["jobs"][0]
        assert row["mode"] == "script"
        assert row["command"] == payload["command"]
        assert row["summarize_prompt"] == payload["summarize_prompt"]

        payload["command"] = ""
        resp = await client.post("/cron/jobs/create", json=payload)
        assert resp.status == 400
        assert "需要非空 command" in (await resp.json())["error"]
