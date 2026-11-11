"""首访按需模块的路由、版本号和常驻依赖回归。浏览器路径见 bench_web_first_visit.py。"""
from __future__ import annotations

from html.parser import HTMLParser
import json

import pytest
from aiohttp import ClientSession

from vococo.gateway.adapters import web as web_mod


@pytest.fixture
def anyio_backend():
    return "asyncio"


class ScriptParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.assets = ""
        self.in_assets = False

    def handle_starttag(self, tag, attrs):
        if tag != "script":
            return
        attrs = dict(attrs)
        self.in_assets = attrs.get("id") == "lazyAssets"
        if attrs.get("src"):
            self.scripts.append(attrs["src"])

    def handle_data(self, data):
        if self.in_assets:
            self.assets += data

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_assets = False


@pytest.mark.anyio
async def test_lazy_assets_use_real_routes_and_immutable_versions(isolated, monkeypatch):
    from vococo import config
    from vococo.core import task_runner
    from vococo.memory import session_store

    async def no_recovery():
        pass
    monkeypatch.setattr(config, "WEB_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "WEB_PORT", 0)
    monkeypatch.setattr(config, "VOICE_ENABLED", False)
    monkeypatch.setattr(task_runner, "heal_after_restart", no_recovery)
    monkeypatch.setattr(session_store, "backfill_thumbs", lambda: None)
    adapter = web_mod.WebAdapter()
    try:
        await adapter._start_server()
        port = adapter._runner.addresses[0][1]
        async with ClientSession(base_url=f"http://127.0.0.1:{port}") as client:
            page = await client.get("/")
            parser = ScriptParser()
            parser.feed(await page.text())
            assets = json.loads(parser.assets)
            assert set(assets) == {"workbench", "settings", "library"}
            assert any(url.startswith("/voice.js?v=") for url in parser.scripts)
            assert any(url.startswith("/lazy.js?v=") for url in parser.scripts)
            assert not any(url.split("?")[0] in {f"/{name}.js" for name in assets} for url in parser.scripts)
            for name, url in {**assets, "lazy": next(u for u in parser.scripts if u.startswith("/lazy.js?"))}.items():
                assert "?v=" in url
                response = await client.get(url)
                assert response.status == 200
                assert response.headers["Content-Type"].startswith("text/javascript")
                assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
                assert await response.read() == (web_mod._STATIC / f"{name}.js").read_bytes()
                etag = response.headers["ETag"]
                response = await client.get(url, headers={"If-None-Match": etag})
                assert response.status == 304
    finally:
        if adapter._runner:
            await adapter._runner.cleanup()


def test_home_dependencies_stay_eager_and_module_execution_stays_plain():
    static = web_mod._STATIC
    app = (static / "app-core.js").read_text()
    sidebar = (static / "sidebar.js").read_text()
    settings = (static / "settings.js").read_text()
    for declaration in ("const PUSH =", "async function initPush()", "function closeWorkbench()"):
        assert declaration in app
        assert declaration not in settings
    for declaration in ("function findConv(", "function openConvMenu(", "function bindSwipe(", "async function delConv("):
        assert declaration in sidebar
        assert declaration not in settings
    assert '"/lazy.js"' in (static / "sw.js").read_text()
    assert "blob:" not in web_mod._CSP.split("script-src", 1)[1].split(";", 1)[0]
    for name in ("settings", "workbench", "library"):
        source = (static / f"{name}.js").read_text()
        assert source.rstrip().endswith(f'vococoLazy.ready("{name}");')
        assert "window.vococoLazy.load(" not in source
