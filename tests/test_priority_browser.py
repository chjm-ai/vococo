"""真实临时 WebAdapter 整页验证 GPT 优先档，绝不读取或写入生产设置。"""
import asyncio
import json
from pathlib import Path

import pytest

from vococo import config, providers
from vococo.core import tasks
from vococo.gateway import settings_store
from vococo.gateway.adapters.web import WebAdapter
from test_library_integration import _browser_environment


GPT = "gpt-6.1-sol"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def priority_browser_server(isolated, monkeypatch):
    for name in ("IMAGES_DIR", "AUDIO_DIR", "FILES_DIR", "VIDEOS_DIR", "PUBLISHED_DIR"):
        monkeypatch.setattr(config, name, config.DATA_DIR / name.lower())
    for name in ("CRON_JOBS_PATH", "SUGGESTIONS_PATH", "HEARTBEAT_PATH", "WATCHDOG_LOG_PATH"):
        monkeypatch.setattr(config, name, config.DATA_DIR / name.lower())
    monkeypatch.setattr(config, "OBSIDIAN_VAULT_DIR", isolated / "vault")
    monkeypatch.setattr(config, "WEB_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "WEB_PORT", 0)
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "priority-browser-test")
    monkeypatch.setattr(config, "VOICE_ENABLED", False)
    monkeypatch.setattr(config, "MODEL", GPT)
    monkeypatch.setattr(settings_store, "_PATH", isolated / "settings.json")
    monkeypatch.setattr(tasks, "_DB", None)
    settings_store.upsert_web_provider("codex-gpt", {
        "base_url": "http://127.0.0.1:8317", "api_key": "fake",
        "mgmt_key": "fake-management", "model": GPT,
    })
    choices = [(GPT, "GPT Sol", "codex"),
               ("claude-sonnet-5-5", "Sonnet", "anthropic"),
               ("deepseek-flash", "DeepSeek", "api")]
    monkeypatch.setattr(providers, "available_models", lambda *a, **kw: choices)

    async def fake_usage(*args, **kwargs):
        # 模型面板会查额度；不能让假凭据请求本机正在运行的真实代理。
        return {"available": False}, 200

    monkeypatch.setattr(providers, "usage_for_model", fake_usage)
    adapter = WebAdapter()
    try:
        await adapter._start_server()
        yield f"http://127.0.0.1:{adapter._runner.addresses[0][1]}"
    finally:
        if adapter._runner:
            await adapter._runner.cleanup()
        if tasks._DB is not None:
            tasks._DB.close()
            tasks._DB = None


@pytest.mark.anyio
@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
async def test_priority_switch_persists_hides_rolls_back_and_fits(priority_browser_server, width):
    process = await asyncio.create_subprocess_exec(
        "node", str(Path(__file__).with_name("priority_tier_browser.cjs")),
        priority_browser_server, config.WEB_AUTH_TOKEN, str(width),
        env=_browser_environment(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
        assert process.returncode == 0, stderr.decode() + stdout.decode()
        report = json.loads(stdout)
        assert report == {"ok": True, "width": width, "successfulPosts": 2, "failedPosts": 2}
        saved = json.loads(settings_store._PATH.read_text(encoding="utf-8"))
        assert settings_store.get_web_service_tier(GPT) == ""
        assert saved.get("web_service_tiers", {}).get(GPT, "") == ""
        print(f"优先档整页浏览器验证通过：{report}")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
