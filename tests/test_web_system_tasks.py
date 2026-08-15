"""「定时」Tab 本机系统任务区块的接口:/system/tasks、/system/tasks/detail。

只读展示,数据源是 vococo/cron/system_tasks.py(已在 test_system_tasks.py 单测过
解析逻辑),这里只验证 web 层的接线:hostname 透出、404、以及 detail 带出脚本/日志。
"""
from __future__ import annotations

import plistlib

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.cron import system_tasks
from vococo.gateway.adapters.web import WebAdapter


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def system_tasks_app(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    home = isolated / "home"
    launch_dir = home / "Library" / "LaunchAgents"
    launch_dir.mkdir(parents=True)
    monkeypatch.setattr(system_tasks, "_HOME", home)
    monkeypatch.setattr(system_tasks, "_LAUNCH_AGENTS_DIR", launch_dir)
    monkeypatch.setattr(system_tasks, "_read_launchctl_text", lambda: "")
    monkeypatch.setattr(system_tasks, "_read_crontab_text", lambda: "")
    monkeypatch.setattr(system_tasks, "hostname", lambda: "test-host")

    script = home / "scripts" / "daily.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\necho hi\n")
    with (launch_dir / "com.wesley.daily.plist").open("wb") as f:
        plistlib.dump({
            "Label": "com.wesley.daily",
            "ProgramArguments": [str(script)],
            "StartCalendarInterval": {"Hour": 10, "Minute": 10},
        }, f)

    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([
        web.get("/system/tasks", adapter._handle_system_tasks),
        web.get("/system/tasks/detail", adapter._handle_system_task_detail),
    ])
    return app


@pytest.mark.anyio
async def test_list_returns_hostname_and_task(system_tasks_app):
    async with TestClient(TestServer(system_tasks_app)) as client:
        resp = await client.get("/system/tasks")
        assert resp.status == 200
        data = await resp.json()
        assert data["hostname"] == "test-host"
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["id"] == "launchd:com.wesley.daily"
        assert "script_content" not in data["tasks"][0]  # 列表不带脚本内容,详情才带


@pytest.mark.anyio
async def test_detail_returns_script_content(system_tasks_app):
    async with TestClient(TestServer(system_tasks_app)) as client:
        resp = await client.get("/system/tasks/detail?id=launchd:com.wesley.daily")
        assert resp.status == 200
        task = (await resp.json())["task"]
        assert task["script_content"] == "#!/bin/bash\necho hi\n"


@pytest.mark.anyio
async def test_detail_unknown_id_404(system_tasks_app):
    async with TestClient(TestServer(system_tasks_app)) as client:
        resp = await client.get("/system/tasks/detail?id=launchd:ghost")
        assert resp.status == 404
