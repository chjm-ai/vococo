"""真实浏览器验收定时任务的稍后处理入口及持久化。"""
import asyncio
from pathlib import Path

import pytest

from test_library_integration import _browser_environment
from vococo import config
from vococo.core import tasks
from vococo.cron import scheduler
from vococo.gateway import settings_store
from vococo.gateway.adapters.web import WebAdapter


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("mobile", [False, True], ids=["desktop", "mobile"])
async def test_cron_pinned_in_real_browser(isolated, monkeypatch, mobile):
    monkeypatch.setattr(config, "CRON_JOBS_PATH", isolated / "cron_jobs.json")
    monkeypatch.setattr(config, "WEB_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "WEB_PORT", 0)
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "cron-pinned-test")
    monkeypatch.setattr(config, "VOICE_ENABLED", False)
    monkeypatch.setattr(settings_store, "_PATH", isolated / "web_settings.json")
    monkeypatch.setattr(tasks, "_DB", None)
    job = scheduler.create_job(name="稍后处理验收" * 10, prompt="汇总", agent_id="general",
                               schedule={"kind": "cron", "expr": "0 8 * * *"})
    adapter, process = WebAdapter(), None
    try:
        await adapter._start_server()
        origin = f"http://127.0.0.1:{adapter._runner.addresses[0][1]}"
        process = await asyncio.create_subprocess_exec(
            "node", str(Path(__file__).with_name("cron_pinned_browser.cjs")), origin,
            config.WEB_AUTH_TOKEN, job["conv"], "mobile" if mobile else "desktop",
            env=_browser_environment(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
        assert process.returncode == 0, stderr.decode() + stdout.decode()
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        if adapter._runner:
            await adapter._runner.cleanup()
        if tasks._DB is not None:
            tasks._DB.close()
            tasks._DB = None
