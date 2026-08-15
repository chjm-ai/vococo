"""「定时」Tab 本机系统任务区块的接口:/system/tasks、/system/tasks/detail。

只读展示,数据源是 vococo/cron/system_tasks.py(已在 test_system_tasks.py 单测过
解析逻辑),这里只验证 web 层的接线:hostname 透出、404、以及 detail 带出脚本/日志。

2026-08-15 事故回归测试:真机上 com.linxai.xhs.daily 的脚本落在 iCloud 同步路径
(~/Desktop)下,open() 卡住 184 秒,直接冻结了整条事件循环,看门狗判定假死自杀重启。
根因是 system_tasks 的文件 IO 是同步阻塞的,又直接在 async handler 里跑。修复用
asyncio.to_thread 挪进程池 + wait_for 兜超时——下面 test_slow_read_does_not_block_event_loop
就是复现"另一个请求会不会被拖下水"这件事本身,不只测超时报错。
"""
from __future__ import annotations

import asyncio
import plistlib
import time

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


@pytest.mark.anyio
async def test_detail_times_out_gracefully_on_hanging_read(system_tasks_app, monkeypatch):
    """模拟 iCloud 路径 open() 卡死:task_detail 一直不返回,handler 得在超时后
    给个 504,而不是无限期挂住这个请求。"""
    monkeypatch.setattr(WebAdapter, "_SYSTEM_TASKS_TIMEOUT_SEC", 0.2)
    monkeypatch.setattr(system_tasks, "task_detail", lambda task_id: time.sleep(2) or {})
    async with TestClient(TestServer(system_tasks_app)) as client:
        started = time.monotonic()
        resp = await client.get("/system/tasks/detail?id=launchd:com.wesley.daily")
        elapsed = time.monotonic() - started
        assert resp.status == 504
        assert elapsed < 1.5  # 远小于卡死函数的 2s,证明真的是超时放弃,不是等它跑完


@pytest.mark.anyio
async def test_slow_read_does_not_block_event_loop(system_tasks_app, monkeypatch):
    """核心回归测试:一个请求卡在阻塞 IO 里时,同一进程的另一个请求不能被拖下水
    ——这正是 2026-08-15 事故的本体(卡的不是这一个请求,是整条事件循环)。"""
    monkeypatch.setattr(WebAdapter, "_SYSTEM_TASKS_TIMEOUT_SEC", 5)
    monkeypatch.setattr(system_tasks, "task_detail", lambda task_id: time.sleep(1) or {})
    async with TestClient(TestServer(system_tasks_app)) as client:
        slow = asyncio.ensure_future(client.get("/system/tasks/detail?id=launchd:com.wesley.daily"))
        await asyncio.sleep(0.1)  # 让 slow 先真正进入阻塞调用
        started = time.monotonic()
        fast = await client.get("/system/tasks")  # 换一个完全不同的接口,不共享同一个 task_detail 阻塞
        fast_elapsed = time.monotonic() - started
        assert fast.status == 200
        assert fast_elapsed < 0.5  # 没被卡住的那 1s 拖累,证明事件循环仍然自由
        await slow
