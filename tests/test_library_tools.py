"""工具源头收录与受控发布；只操作隔离目录，不连接公网。"""
import asyncio
import base64
import io
import json

import pytest
from PIL import Image

from vococo.tools import builtin, publishing


def run(tool, args):
    result = asyncio.run(tool.handler(args))
    return result["content"][0]["text"]


def payload(text):
    return json.loads(text.split("\n", 1)[1])


def png():
    stream = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "image.png"
    path.write_bytes(png())
    return path


@pytest.fixture
def publish_dirs(isolated, monkeypatch):
    from vococo import config
    source = isolated / "site"
    source.mkdir()
    (source / "index.html").write_text("hello", encoding="utf-8")
    monkeypatch.setattr(config, "PUBLISHED_DIR", isolated / "data" / "published")
    return {"target": "local", "slug": "test-site", "source_dir": str(source),
            "title": "测试站点", "type": "static"}


@pytest.fixture
def approved(monkeypatch):
    calls = []
    async def approve(reason, detail, **kwargs):
        calls.append((reason, detail))
        return True
    monkeypatch.setattr(publishing.danger, "require_approval", approve)
    return calls


def test_collect_image_deduplicates_and_preserves_original(isolated, image):
    from vococo.memory import library
    first = payload(run(builtin.collect_library_item, {"kind": "image", "path": str(image)}))
    second = payload(run(builtin.collect_library_item, {"kind": "image", "path": str(image)}))
    assert first["library_id"] == second["library_id"]
    image.unlink()
    assert library.image_file(first["library_id"]).read_bytes() == png()
    assert len(library.list_items()) == 1


@pytest.mark.parametrize("url", ["file:///tmp/a", "https://user:pass@example.com", "javascript:alert(1)"])
def test_collect_website_rejects_non_http_or_credentials(isolated, url):
    assert "收藏失败" in run(builtin.collect_library_item, {"kind": "website", "url": url})


def test_collect_url_does_not_fetch(isolated, monkeypatch):
    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: pytest.fail("不能访问链接"))
    output = payload(run(builtin.collect_library_item, {"kind": "website", "url": "https://example.com/demo/"}))
    assert output["library_id"]


def test_collect_website_cover_is_validated_and_passed_separately(isolated, image, monkeypatch):
    from vococo.memory import library
    original = library.register_website
    calls = []
    def register(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(library, "register_website", register)
    monkeypatch.setattr(library, "register_image", lambda **kw: pytest.fail("网站封面不能另建图片作品"))
    output = payload(run(builtin.collect_library_item, {
        "kind": "website", "url": "https://example.com/demo/", "cover_path": str(image),
        "metadata": {"note": "网站截图"}}))
    assert output["library_id"]
    assert calls[0]["cover_path"] == str(image.resolve())
    assert calls[0]["metadata"] == {"note": "网站截图"}
    assert len(library.list_items(kind="website")) == 1
    assert library.get_item(output["library_id"])["cover_url"] == f"/library/cover?id={output['library_id']}"
    assert not library.list_items(kind="image")


@pytest.mark.parametrize("invalid", ["relative", "url", "missing", "directory", "symlink", "parent-symlink", "fake", "empty"])
def test_collect_website_new_event_rejects_invalid_cover(isolated, image, monkeypatch, invalid):
    from vococo.memory import library
    cover = str(image)
    if invalid == "relative":
        monkeypatch.chdir(image.parent)
        cover = image.name
    elif invalid == "url":
        cover = "https://example.com/image.png"
    elif invalid == "missing":
        cover = str(image.with_name("missing.png"))
    elif invalid == "directory":
        cover = str(image.parent)
    elif invalid == "symlink":
        link = isolated / "cover.png"
        link.symlink_to(image)
        cover = str(link)
    elif invalid == "parent-symlink":
        link = isolated / "cover-dir"
        link.symlink_to(image.parent, target_is_directory=True)
        cover = str(link / image.name)
    elif invalid == "fake":
        image.write_bytes(b"not an image")
    elif invalid == "empty":
        cover = ""
    receipt = payload(run(builtin.collect_library_item, {
        "kind": "website", "url": "https://example.com/demo/", "cover_path": cover,
        "event_key": "website:new-invalid-cover"}))
    assert receipt["library_status"] == "failed"
    assert receipt["library_error"]
    assert not library.list_items()


@pytest.mark.parametrize("format", ["PNG", "JPEG", "GIF", "WEBP"])
def test_collect_website_cover_accepts_supported_signatures(isolated, image, format):
    from vococo.memory import library
    stream = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(stream, format=format)
    image.write_bytes(stream.getvalue())
    receipt = payload(run(builtin.collect_library_item, {
        "kind": "website", "url": "https://example.com/", "cover_path": str(image)}))
    assert receipt["library_status"] == "collected"
    assert library.get_item(receipt["library_id"])["cover_url"]
    assert not library.list_items(kind="image")


def test_collect_website_cover_retry_preserves_top_level_path(isolated, image, monkeypatch):
    from vococo.memory import library
    monkeypatch.setattr(library, "register_website", lambda **kw: (_ for _ in ()).throw(OSError("disk")))
    receipt = payload(run(builtin.collect_library_item, {
        "kind": "website", "url": "https://example.com/", "cover_path": str(image)}))
    assert receipt["library_status"] == "failed"
    assert receipt["retry"]["cover_path"] == str(image.resolve())
    assert "cover_path" not in receipt["retry"].get("metadata", {})
    calls = []
    def register(**kwargs):
        calls.append(kwargs)
        return {"id": "website-test"}
    monkeypatch.setattr(library, "register_website", register)
    assert payload(run(builtin.collect_library_item, receipt["retry"]))["library_id"] == "website-test"
    assert calls[0]["cover_path"] == str(image.resolve())
    assert calls[0]["event_key"] == receipt["retry"]["event_key"]


def test_collect_website_same_event_retries_after_source_cover_deleted(isolated, image):
    from vococo.memory import library
    args = {"kind": "website", "url": "https://example.com/demo/",
            "cover_path": str(image), "event_key": "website:cover-retry"}
    first = payload(run(builtin.collect_library_item, args))
    assert first["library_status"] == "collected"
    image.unlink()
    second = payload(run(builtin.collect_library_item, args))
    assert second["library_status"] == "collected"
    assert second["library_id"] == first["library_id"]
    stored = library.get_item(first["library_id"])
    assert stored["cover_url"] == f"/library/cover?id={first['library_id']}"
    assert len(stored["sources"]) == 1
    assert len(library.list_items()) == 1
    assert not library.list_items(kind="image")
    new_event = payload(run(builtin.collect_library_item, {
        **args, "event_key": "website:new-event-missing-cover"}))
    assert new_event["library_status"] == "failed"
    assert len(library.get_item(first["library_id"])["sources"]) == 1


def test_collect_rejects_fake_image_and_symlink(isolated, image):
    link = isolated / "link.png"
    link.symlink_to(image)
    assert "软链接" in run(builtin.collect_library_item, {"kind": "image", "path": str(link)})
    image.write_bytes(b"not an image")
    assert "不是受支持的图片" in run(builtin.collect_library_item, {"kind": "image", "path": str(image)})


def test_image_failed_collection_receipt_can_retry_without_production(isolated, image, monkeypatch):
    from vococo.memory import library
    original = library.register_image
    monkeypatch.setattr(library, "register_image", lambda **kw: (_ for _ in ()).throw(OSError("disk")))
    receipt = builtin._library_receipt("image", {"path": str(image), "action": "generated", "event_key": "gen:test"})
    assert receipt["library_status"] == "failed"
    monkeypatch.setattr(library, "register_image", original)
    first = payload(run(builtin.collect_library_item, receipt["retry"]))
    second = payload(run(builtin.collect_library_item, receipt["retry"]))
    assert first["library_id"] == second["library_id"]
    assert len(library.get_item(first["library_id"])["sources"]) == 1


@pytest.mark.parametrize("purpose", [None, "output", "reference", "debug"])
def test_send_image_all_purposes_display_without_collection(isolated, image, monkeypatch, purpose):
    from vococo.gateway import clarify
    from vococo.gateway.adapters import web
    from vococo.memory import library
    calls = []
    class Adapter:
        async def send_image(self, *args):
            calls.append(args)
            return None
    monkeypatch.setattr(web, "WebAdapter", Adapter)
    monkeypatch.setattr(library, "register_image", lambda **kw: pytest.fail("普通发图不能收录"))
    token = clarify.set_current("web:test", Adapter(), "test")
    try:
        args = {"path": str(image), "caption": "截图"}
        if purpose is not None:
            args["purpose"] = purpose
        text = run(builtin.send_image, args)
    finally:
        clarify.reset_current(token)
    assert "已发送" in text
    assert calls == [("test", image.resolve(), "截图")]
    assert not library.list_items()


def test_send_image_failure_does_not_collect(isolated, image, monkeypatch):
    from vococo.gateway import clarify
    from vococo.gateway.adapters import web
    from vococo.memory import library
    class Adapter:
        async def send_image(self, *args):
            return "发送失败"
    monkeypatch.setattr(web, "WebAdapter", Adapter)
    token = clarify.set_current("web:test", Adapter(), "test")
    try:
        assert run(builtin.send_image, {"path": str(image)}) == "发送失败"
    finally:
        clarify.reset_current(token)
    assert not library.list_items()


@pytest.mark.parametrize("web_channel,fail_registration,fail_send", [(False, False, False), (True, False, False), (True, True, False), (True, False, True)])
def test_generate_registers_before_auto_send_and_never_repeats(isolated, monkeypatch, web_channel, fail_registration, fail_send):
    import aiohttp
    from vococo import config, providers
    from vococo.gateway import clarify
    from vococo.gateway.adapters import web
    from vococo.memory import library
    monkeypatch.setattr(config, "IMAGES_DIR", isolated / "images")
    monkeypatch.setattr(providers, "sidecar_env", lambda _: ("x", {"ANTHROPIC_BASE_URL": "http://fake", "ANTHROPIC_API_KEY": "fake"}))
    calls = []
    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self): return {"data": [{"b64_json": base64.b64encode(png()).decode()}]}
    class Session(Response):
        def post(self, *args, **kwargs):
            calls.append("generate")
            return Response()
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    original = library.register_image
    def register(**kwargs):
        calls.append("register")
        if fail_registration:
            raise OSError("disk")
        return original(**kwargs)
    monkeypatch.setattr(library, "register_image", register)
    class Adapter:
        async def send_image(self, *args):
            calls.append("send")
            assert "register" in calls
            if fail_send:
                raise OSError("display failed")
            return None
    monkeypatch.setattr(web, "WebAdapter", Adapter)
    token = clarify.set_current("web:test", Adapter(), "test") if web_channel else None
    try:
        text = run(builtin.generate_image, {"prompt": "猫"})
        receipt = payload(text)
        assert receipt["library_status"] == ("failed" if fail_registration else "collected")
        if fail_send:
            assert "图片已产出，不要重新生成" in text
        if web_channel and not fail_send:
            path = next(iter(clarify.current().auto_sent_image_paths))
            assert "禁止重复发送" in run(builtin.send_image, {"path": path})
        assert calls.count("generate") == 1
        assert calls.count("register") == 1
        assert calls.count("send") == int(web_channel)
        assert len(library.list_items(kind="image")) == int(not fail_registration)
        if not fail_registration:
            assert library.get_item(receipt["library_id"])["sources"][0]["action"] == "generated"
    finally:
        if token is not None:
            clarify.reset_current(token)


def test_publish_denied_never_writes(isolated, publish_dirs, monkeypatch):
    from vococo import config
    async def deny(*args, **kwargs): return False
    monkeypatch.setattr(publishing.danger, "require_approval", deny)
    assert payload(run(builtin.publish_website, publish_dirs))["activation"] == "denied"
    assert not config.PUBLISHED_DIR.exists()


def test_publish_local_updates_one_card_and_separates_activation(isolated, publish_dirs, approved):
    from vococo import config
    from vococo.memory import library
    first = payload(run(builtin.publish_website, publish_dirs))
    (config.PUBLISHED_DIR / "test-site" / "index.html").read_text() == "hello"
    assert first["metadata"]["http_verification"] == "not_checked"
    assert first["metadata"]["local_path"] == "/pub/test-site/"
    stored = library.get_item(first["library_id"])
    assert stored["sources"][0]["metadata"]["local_path"] == "/pub/test-site/"
    source = publishing.Path(publish_dirs["source_dir"])
    (source / "index.html").write_text("updated")
    second = payload(run(builtin.publish_website, publish_dirs))
    assert first["library_id"] == second["library_id"]
    assert first["event_key"] != second["event_key"]
    assert len(library.list_items()) == 1
    assert len(approved) == 2
    assert (config.PUBLISHED_DIR / "test-site" / "index.html").read_text() == "updated"
    assert list((config.DATA_DIR / "publish_backups").iterdir())


@pytest.mark.parametrize("cancel_count", [1, 2])
@pytest.mark.parametrize("activation_fails", [False, True])
def test_cancelled_local_publish_finishes_before_next_activation_and_registers(
        isolated, publish_dirs, approved, monkeypatch, cancel_count, activation_fails):
    import threading
    from vococo import config
    from vococo.memory import library

    original = publishing._activate
    release = threading.Event()
    calls = []
    second_source = isolated / "second-site"
    second_source.mkdir()
    (second_source / "index.html").write_text("second version")
    monkeypatch.setattr(publishing, "_LOCKS", {})

    async def scenario():
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        def delayed(source, slug, version, manifest):
            calls.append(version)
            if len(calls) == 1:
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise TimeoutError("测试未释放发布线程")
                if activation_fails:
                    raise OSError("模拟写入失败")
            return original(source, slug, version, manifest)
        monkeypatch.setattr(publishing, "_activate", delayed)
        first = asyncio.create_task(builtin.publish_website.handler(publish_dirs))
        second = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            for _ in range(cancel_count):
                first.cancel()
                await asyncio.sleep(0)
            second = asyncio.create_task(builtin.publish_website.handler(
                {**publish_dirs, "source_dir": str(second_source)}))
            await asyncio.sleep(0)
            assert not first.done()
            assert publishing._LOCKS["local:test-site"].locked()
            assert not second.done()
            assert len(calls) == 1
            release.set()
            results = await asyncio.wait_for(asyncio.gather(first, second), 5)
            return [result["content"][0]["text"] for result in results]
        finally:
            release.set()
            await asyncio.gather(first, *([second] if second else []), return_exceptions=True)

    first_text, second_text = asyncio.run(scenario())
    second = payload(second_text)
    assert second["activation"] == "succeeded"
    assert (config.PUBLISHED_DIR / "test-site" / "index.html").read_text() == "second version"
    record = config.DATA_DIR / "publish_manifests" / "test-site.json"
    assert json.loads(record.read_text()) == publishing._manifest(config.PUBLISHED_DIR / "test-site")
    stored = library.get_item(second["library_id"])
    versions = {source["metadata"]["version"] for source in stored["sources"]}
    assert len(calls) == 2
    if activation_fails:
        assert "发布未完成：模拟写入失败" in first_text
        assert versions == {second["metadata"]["version"]}
        return
    first = payload(first_text)
    assert first["activation"] == "succeeded"
    assert first["metadata"]["cancellation_requested"] is True
    assert first["library_id"] == second["library_id"]
    assert first["event_key"] != second["event_key"]
    assert versions == {first["metadata"]["version"], second["metadata"]["version"]}


def test_publish_refuses_unrecorded_destination_changes(isolated, publish_dirs, approved):
    from vococo import config
    run(builtin.publish_website, publish_dirs)
    entry = config.PUBLISHED_DIR / "test-site" / "index.html"
    entry.write_text("手工更新")
    assert "未登记的改动" in run(builtin.publish_website, publish_dirs)
    assert entry.read_text() == "手工更新"


@pytest.mark.parametrize("target", ["site"])
def test_unsupported_remote_never_requests_approval_or_runs_commands(isolated, publish_dirs, approved, target):
    assert "未支持" in run(builtin.publish_website, {**publish_dirs, "target": target})
    assert not approved


@pytest.mark.parametrize("slug", ["../escape", "-bad", "a;whoami", "a/child", "A"])
def test_publish_rejects_unsafe_slug(isolated, publish_dirs, approved, slug):
    assert "slug 必须" in run(builtin.publish_website, {**publish_dirs, "slug": slug})
    assert not approved


@pytest.mark.parametrize("name", [".env", ".env.prod", ".git", "private.pem"])
def test_publish_rejects_private_assets(isolated, publish_dirs, approved, name):
    source = publishing.Path(publish_dirs["source_dir"])
    (source / name).write_text("private")
    assert "发布未完成" in run(builtin.publish_website, publish_dirs)
    assert not approved


def test_publish_collection_failure_retry_does_not_reactivate(isolated, publish_dirs, approved, monkeypatch):
    from vococo.memory import library
    original = library.register_website
    monkeypatch.setattr(library, "register_website", lambda **kw: (_ for _ in ()).throw(OSError("disk")))
    receipt = payload(run(builtin.publish_website, publish_dirs))
    assert receipt["activation"] == "succeeded"
    assert receipt["library_status"] == "failed"
    monkeypatch.setattr(library, "register_website", original)
    monkeypatch.setattr(publishing, "_activate", lambda *args: pytest.fail("不得重新发布"))
    retry = payload(run(builtin.collect_library_item, receipt["retry"]))
    assert retry["library_id"]
    assert len(approved) == 1


@pytest.mark.parametrize("mutation", ["symlink", "plain-change"])
def test_publish_rejects_source_mutation_during_approval(isolated, publish_dirs, monkeypatch, mutation):
    from vococo import config
    source = publishing.Path(publish_dirs["source_dir"])
    secret = isolated / ".env"
    secret.write_text("SECRET=private")
    async def approve(*args, **kwargs):
        if mutation == "symlink":
            (source / "leaked.txt").symlink_to(secret)
        else:
            (source / "index.html").write_text("changed while waiting")
        return True
    monkeypatch.setattr(publishing.danger, "require_approval", approve)
    assert "发布未完成" in run(builtin.publish_website, publish_dirs)
    assert not (config.PUBLISHED_DIR / publish_dirs["slug"]).exists()
    assert not list((config.DATA_DIR / "publish_staging").iterdir())


@pytest.mark.parametrize("name,content", [("credentials.json", '{"value":"secret"}'),
    ("innocent.json", '{"private_key":"secret"}'), ("key.txt", "-----BEGIN PRIVATE KEY-----")])
def test_publish_rejects_credentials_in_public_extensions(isolated, publish_dirs, approved, name, content):
    source = publishing.Path(publish_dirs["source_dir"])
    (source / name).write_text(content)
    assert "发布未完成" in run(builtin.publish_website, publish_dirs)
    assert not approved


def test_search_get_and_background_source_contract(isolated, image):
    from vococo.gateway import clarify
    from vococo.memory import library
    from vococo.tools import danger
    task_token = danger.set_task_session("task:library-test")
    source_token = clarify.bind_turn("task:library-test", 41)
    try:
        context = builtin._library_context()
        assert context == {"session_key": "task:library-test", "turn_id": 41}
        item = payload(run(builtin.collect_library_item, {"kind": "image", "path": str(image), "title": "成果"}))
    finally:
        clarify.unbind_turn("task:library-test", 41, source_token)
        danger.reset_task_session(task_token)
    result = json.loads(run(builtin.search_library, {"search": "成果"}))
    assert result[0]["id"] == item["library_id"]
    detail = json.loads(run(builtin.get_library_item, {"id": item["library_id"]}))
    assert publishing.Path(detail["path"]).read_bytes() == png()
    source = library.get_item(item["library_id"])["sources"][0]
    assert source["turn_id"] == 41
    assert source["session_key"] == "task:library-test"
    assert run(builtin.get_library_item, {"id": "missing"}) == "作品不存在。"
