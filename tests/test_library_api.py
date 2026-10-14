"""鉴权、真实作品引用和只读历史候选清单。"""
import base64

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
import pytest

from vococo import config
from vococo.gateway import library_api
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import library, session_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def env(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "test-library-token")
    monkeypatch.setattr(config, "IMAGES_DIR", config.DATA_DIR / "images")
    monkeypatch.setattr(config, "PUBLISHED_DIR", config.DATA_DIR / "published")
    image = isolated / "output.png"
    Image.new("RGB", (3, 3), "blue").save(image)
    return image


def app_for(adapter=None):
    app = web.Application()
    library_api.register_routes(app)
    if adapter:
        app.router.add_post("/send", adapter._handle_send)
    return app


AUTH = {"X-Auth-Token": "test-library-token"}


@pytest.mark.anyio
async def test_api_list_detail_patch_file_and_errors(env):
    row = library.register_image(env, "蓝色图片")
    async with TestClient(TestServer(app_for())) as client:
        listed = await client.get("/library/items?kind=image", headers=AUTH)
        assert listed.status == 200
        body = await listed.json()
        assert body["items"][0]["id"] == row["id"] and "agents" in body
        detail = await client.get(f"/library/item?id={row['id']}", headers=AUTH)
        expected = {**row, "sources": [{**source, "conv": "", "can_open": False} for source in row["sources"]]}
        assert (await detail.json()) == expected
        patched = await client.patch(f"/library/item?id={row['id']}",
                                     json={"title": "新标题", "pinned": True}, headers=AUTH)
        assert (await patched.json())["pinned"] is True
        image = await client.get(row["file_url"], headers=AUTH)
        assert await image.read() == env.read_bytes()
        assert image.headers["Cache-Control"] == "private, no-store"
        missing = await client.get("/library/item?id=missing", headers=AUTH)
        assert missing.status == 404
        bad = await client.patch(f"/library/item?id={row['id']}", json={"url": "x"}, headers=AUTH)
        assert bad.status == 400
        invalid = await client.get("/library/items?limit=bad", headers=AUTH)
        assert invalid.status == 400


@pytest.mark.anyio
async def test_all_library_routes_require_header_auth(env):
    row = library.register_image(env)
    async with TestClient(TestServer(app_for(WebAdapter()))) as client:
        for method, url in [("get", "/library/items"), ("get", "/library/item"),
                            ("patch", "/library/item"), ("get", row["file_url"]),
                            ("get", "/library/cover"),
                            ("post", "/library/history/preview"), ("post", "/send")]:
            response = await getattr(client, method)(url, json={})
            assert response.status == 401
        query_token = await client.get(row["file_url"] + "&token=test-library-token")
        assert query_token.status == 401


@pytest.mark.anyio
async def test_send_resolves_real_image_and_official_url(env):
    image = library.register_image(env)
    site = library.register_website("https://example.com/real", "网站", "site:one")
    adapter = WebAdapter()
    async with TestClient(TestServer(app_for(adapter))) as client:
        response = await client.post("/send", headers=AUTH,
                                     json={"text": "复用作品", "library_refs": [image["id"], site["id"]]})
        assert response.status == 200
    incoming = await adapter._inbox.get()
    assert base64.b64decode(incoming.images[0].data) == env.read_bytes()
    assert incoming.images[0].media_type == "image/png"
    assert "https://example.com/real" in incoming.text
    assert str(env) not in incoming.text


@pytest.mark.anyio
@pytest.mark.parametrize("refs,status", [(["missing"], 404), (["/etc/passwd"], 404),
                                         ([{"path": "/etc/passwd"}], 400), (None, 400), ("x", 400)])
async def test_send_bad_refs_do_not_queue(env, refs, status):
    adapter = WebAdapter()
    async with TestClient(TestServer(app_for(adapter))) as client:
        response = await client.post("/send", headers=AUTH, json={"text": "x", "library_refs": refs})
        assert response.status == status
        assert (await response.json())["error"]
    assert adapter._inbox.empty()


@pytest.mark.anyio
async def test_missing_and_corrupted_originals_fail_without_queueing(env):
    row = library.register_image(env)
    path = library.image_file(row["id"])
    adapter = WebAdapter()
    async with TestClient(TestServer(app_for(adapter))) as client:
        path.write_bytes(b"<script>bad</script>")
        response = await client.post("/send", headers=AUTH, json={"library_refs": [row["id"]]})
        assert response.status == 400
        file_response = await client.get(row["file_url"], headers=AUTH)
        assert file_response.status == 400
        path.unlink()
        missing = await client.post("/send", headers=AUTH, json={"library_refs": [row["id"]]})
        assert missing.status == 404
    assert adapter._inbox.empty()


@pytest.mark.anyio
async def test_history_preview_deduplicates_and_never_imports(env):
    config.IMAGES_DIR.mkdir(parents=True)
    for name in ["gen_1.png", "ai_1.png", "12_0.png"]:
        (config.IMAGES_DIR / name).write_bytes(env.read_bytes())
    (config.IMAGES_DIR / "gen_link.png").symlink_to(env)
    config.PUBLISHED_DIR.mkdir()
    (config.PUBLISHED_DIR / "one").mkdir()
    (config.PUBLISHED_DIR / "one/index.html").write_text("<html>site</html>")
    (config.PUBLISHED_DIR / "one/inner.html").write_text("ignored")
    (config.PUBLISHED_DIR / "audio.wav").write_bytes(b"ignored")
    (config.PUBLISHED_DIR / "redirect.html").write_text("<html>redirect</html>")
    (config.PUBLISHED_DIR / "bad.html").symlink_to(env)
    async with TestClient(TestServer(app_for())) as client:
        response = await client.post("/library/history/preview", headers=AUTH)
        candidates = (await response.json())["candidates"]
    images = [row for row in candidates if row["kind"] == "image"]
    sites = [row for row in candidates if row["kind"] == "website"]
    assert len(images) == 1 and images[0]["files"] == ["ai_1.png", "gen_1.png"]
    assert {row["file"] for row in sites} == {"one/index.html", "redirect.html"}
    assert all(row["agent_id"] == "" for row in candidates)
    assert not (config.DATA_DIR / "library.db").exists()
    assert not (config.DATA_DIR / "library").exists()


@pytest.mark.anyio
async def test_deleted_source_is_not_openable_but_work_survives(env):
    key = "web:library-source"
    session_store.set_title(key, "来源会话")
    row = library.register_image(env, session_key=key)
    async with TestClient(TestServer(app_for())) as client:
        response = await client.get(f"/library/item?id={row['id']}", headers=AUTH)
        source = (await response.json())["sources"][0]
        assert source["can_open"] and source["conv"] == "library-source"
        session_store.delete_session(key)
        response = await client.get(f"/library/item?id={row['id']}", headers=AUTH)
        source = (await response.json())["sources"][0]
        assert source["deleted"] and not source["can_open"]
        assert library.image_file(row["id"]).is_file()


@pytest.mark.anyio
@pytest.mark.parametrize("url,target,expected", [
    ("http://127.0.0.1:8401/pub/demo/?v=1#top", "local", "/pub/demo/?v=1#top"),
    ("http://localhost:8401/pub/demo/", "local", "/pub/demo/"),
    ("http://[::1]:8401/pub/demo/", "local", "/pub/demo/"),
    ("https://example.com/pub/demo/", "local", None),
    ("http://127.0.0.1:8401/pub/demo/", "kimmy", None),
    ("https://demo.kimmy.site/", "kimmy", None),
    ("http://127.0.0.1:8401/pub/demo/", "", None),
    ("http://127.0.0.1:8401/other/demo/", "local", None),
    ("http://127.0.0.1:8401/pub/%2e%2e/private", "local", None),
    ("http://127.0.0.1:8401/pub/a%5cb", "local", None),
])
async def test_only_explicit_local_pub_urls_get_same_origin_open_url(env, url, target, expected):
    row = library.register_website(url, "网站", "url-test", metadata={"target": target})
    async with TestClient(TestServer(app_for())) as client:
        for endpoint in [f"/library/item?id={row['id']}", "/library/items"]:
            response = await client.get(endpoint, headers=AUTH)
            body = await response.json()
            item = body["items"][0] if "items" in body else body
            assert item.get("open_url") == expected
            assert item["url"] == url
    assert library.get_item(row["id"])["url"] == url


@pytest.mark.anyio
async def test_website_cover_auth_list_detail_and_link_only_reference(env):
    site = library.register_website("https://example.com", "网站", "covered-site", cover_path=env)
    data = env.read_bytes()
    env.unlink()
    async with TestClient(TestServer(app_for())) as client:
        for headers in [{}, {"X-Auth-Token": "wrong"}]:
            response = await client.get(site["cover_url"], headers=headers)
            assert response.status == 401
        response = await client.get(site["cover_url"] + "&token=test-library-token")
        assert response.status == 401
        response = await client.get(site["cover_url"], headers=AUTH)
        assert response.status == 200 and await response.read() == data
        assert response.headers["Content-Type"] == "image/png"
        assert response.headers["Cache-Control"] == "private, no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        for endpoint in ["/library/items", f"/library/item?id={site['id']}"]:
            body = await (await client.get(endpoint, headers=AUTH)).json()
            row = body["items"][0] if "items" in body else body
            assert row["cover_url"] == site["cover_url"] and row["file_url"] == ""
        response = await client.get(f"/library/file?id={site['id']}", headers=AUTH)
        assert response.status == 400
    assert library_api.resolve_refs([site["id"]]) == ([], [site["url"]])


@pytest.mark.anyio
async def test_cover_errors_for_old_items_bad_paths_and_corrupted_files(env):
    old = library.register_website("https://example.com", "旧网站", "old-site")
    site = library.register_website("https://example.com", "网站", "site", cover_path=env)
    image = library.register_image(env)
    async with TestClient(TestServer(app_for())) as client:
        for item_id, status in [(old["id"], 404), ("missing", 404), ("/etc/passwd", 404),
                                (image["id"], 400)]:
            response = await client.get("/library/cover", params={"id": item_id}, headers=AUTH)
            assert response.status == status
        response = await client.get("/library/cover", headers=AUTH)
        assert response.status == 400
        path = library.cover_file(site["id"])
        path.write_bytes(b"<script>bad</script>")
        response = await client.get(site["cover_url"], headers=AUTH)
        assert response.status == 400
        path.unlink()
        response = await client.get(site["cover_url"], headers=AUTH)
        assert response.status == 404
        path.symlink_to(env)
        response = await client.get(site["cover_url"], headers=AUTH)
        assert response.status == 400


def test_refs_deduplicate_and_enforce_total_size(env, monkeypatch):
    first = library.register_image(env)
    Image.new("RGB", (4, 4), "red").save(env)
    second = library.register_image(env)
    assert len(library_api.resolve_refs([first["id"], first["id"]])[0]) == 1
    max_size = max(library.image_file(row["id"]).stat().st_size for row in [first, second])
    monkeypatch.setattr(library, "IMAGE_MAX_BYTES", max_size)
    with pytest.raises(ValueError, match="总大小"):
        library_api.resolve_refs([first["id"], second["id"]])
