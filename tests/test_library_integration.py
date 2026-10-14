"""启动完整 WebAdapter 的真实 localhost 服务；所有可写目录指向临时目录。"""
import asyncio
import base64
import json
import os
from pathlib import Path
import re
import shutil

from aiohttp import ClientSession
from PIL import Image
import pytest

from vococo import config
from vococo.core import tasks
from vococo.gateway import settings_store
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import library


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def running_library(isolated, monkeypatch):
    for name in ("IMAGES_DIR", "AUDIO_DIR", "FILES_DIR", "VIDEOS_DIR", "PUBLISHED_DIR"):
        monkeypatch.setattr(config, name, config.DATA_DIR / name.lower())
    for name in ("CRON_JOBS_PATH", "SUGGESTIONS_PATH", "HEARTBEAT_PATH", "WATCHDOG_LOG_PATH"):
        monkeypatch.setattr(config, name, config.DATA_DIR / name.lower())
    monkeypatch.setattr(config, "OBSIDIAN_VAULT_DIR", isolated / "vault")
    monkeypatch.setattr(config, "WEB_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "WEB_PORT", 0)
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "isolated-library-secret")
    monkeypatch.setattr(config, "VOICE_ENABLED", False)
    monkeypatch.setattr(settings_store, "_PATH", config.DATA_DIR / "web_settings.json")
    monkeypatch.setattr(tasks, "_DB", None)
    image_path = isolated / "output.png"
    Image.new("RGB", (4, 4), "blue").save(image_path)
    image = library.register_image(image_path, "隔离测试图片")
    published = config.PUBLISHED_DIR / "library-smoke"
    published.mkdir(parents=True)
    (published / "index.html").write_text('<meta charset="utf-8"><h1>隔离测试网站</h1>', encoding="utf-8")
    site = library.register_website(
        "http://127.0.0.1:8401/pub/library-smoke/", "隔离测试网站", "publish:local:library-smoke",
        metadata={"target": "local", "slug": "library-smoke"},
    )
    library.update_item(site["id"], pinned=True)
    adapter = WebAdapter()
    await adapter._start_server()
    port = next(iter(adapter._runner.sites))._server.sockets[0].getsockname()[1]
    try:
        yield adapter, f"http://127.0.0.1:{port}", image, site, image_path
    finally:
        await adapter._runner.cleanup()
        await asyncio.sleep(0.05)
        if tasks._DB is not None:
            tasks._DB.close()
            tasks._DB = None


@pytest.mark.anyio
async def test_real_server_static_versions_api_auth_and_send(running_library):
    adapter, origin, image, site, image_path = running_library
    auth = {"X-Auth-Token": config.WEB_AUTH_TOKEN}
    async with ClientSession() as client:
        async with client.get(origin + "/") as response:
            html = await response.text()
            assert response.status == 200
        for asset in ("library.js", "library.css"):
            url = re.search(rf'["\'](/' + re.escape(asset) + r'\?v=[^"\']+)', html).group(1)
            async with client.get(origin + url) as response:
                assert response.status == 200
                assert len(await response.read()) > 100
        async with client.get(origin + "/library/items") as response:
            assert response.status == 401
        async with client.get(origin + "/library/items?state=pinned", headers=auth) as response:
            assert [row["id"] for row in (await response.json())["items"]] == [site["id"]]
        async with client.get(origin + image["file_url"], headers=auth) as response:
            assert await response.read() == image_path.read_bytes()
        async with client.post(origin + "/send", headers=auth, json={
            "conv": "main", "text": "引用", "library_refs": [image["id"], site["id"]]
        }) as response:
            assert response.status == 200
    incoming = await adapter._inbox.get()
    assert base64.b64decode(incoming.images[0].data) == image_path.read_bytes()
    assert site["url"] in incoming.text


def _browser_environment():
    environment = dict(os.environ)
    if not environment.get("NODE_PATH"):
        modules = list((Path.home() / ".npm/_npx").glob("*/node_modules/playwright/package.json"))
        if modules:
            environment["NODE_PATH"] = str(modules[0].parent.parent)
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if chrome.exists() and not environment.get("CHROME_PATH"):
        environment["CHROME_PATH"] = str(chrome)
    return environment


@pytest.mark.anyio
async def test_real_browser_page_and_real_library_api(running_library, isolated):
    if not shutil.which("node"):
        pytest.skip("浏览器冒烟需要 Node")
    environment = _browser_environment()
    probe = await asyncio.create_subprocess_exec(
        "node", "-e", "require.resolve('playwright')", env=environment,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    if await probe.wait() != 0:
        pytest.skip("浏览器冒烟需要已安装的 Playwright，不自动安装依赖")
    adapter, origin, image, site, image_path = running_library
    script = Path(__file__).with_name("library_real_browser.cjs")
    screenshot = isolated / "library-real-smoke.png"
    process = await asyncio.create_subprocess_exec(
        "node", str(script), origin, config.WEB_AUTH_TOKEN, image["id"], site["id"], str(screenshot),
        env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
    assert process.returncode == 0, stderr.decode() + stdout.decode()
    report = json.loads(stdout)
    assert report["ok"] and screenshot.is_file()
    assert library.get_item(image["id"])["title"] == "真实本地接口改名"
    assert library.get_item(image["id"])["agent_id"] == ""
    incoming = await asyncio.wait_for(adapter._inbox.get(), timeout=3)
    assert base64.b64decode(incoming.images[0].data) == image_path.read_bytes()
    assert site["url"] in incoming.text
    print(f"真实页面截图：{screenshot}")
