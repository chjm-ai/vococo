"""文件成果真实工具、临时 SQLite、认证下载与来源边界。"""
import io
import os
import zipfile
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.gateway import clarify
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import _db, session_store
from vococo.tools import artifacts, builtin, danger


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def context(isolated):
    root = isolated / "project"
    root.mkdir()
    adapter = WebAdapter()
    key = config.resolve_session_key("web", "files-test")
    tid = session_store.start_turn(key, "生成文件")
    route = clarify.set_current(key, adapter, "files-test", tid)
    cwd = danger.set_cwd(str(root))
    events = []
    adapter._emit = events.append
    try:
        yield adapter, key, tid, events, root
    finally:
        danger.reset_cwd(cwd)
        clarify.reset_current(route)


async def call(path, **kwargs):
    result = await builtin.send_file.handler({"path": str(path), **kwargs})
    text = result["content"][0]["text"]
    assert bool(result.get("isError")) == ("已交付" not in text)
    return text


def pptx_bytes():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
        archive.writestr("ppt/slides/slide1.xml", "<slide>可编辑内容</slide>")
    return stream.getvalue()


@pytest.mark.anyio
async def test_private_snapshot_auth_headers_session_history_and_cleanup(context, monkeypatch):
    adapter, key, tid, events, root = context
    content = pptx_bytes()
    file = root / "成果 演示.pptx"
    file.write_bytes(content)
    assert "已交付" in await call(file, title="可编辑演示")
    entry = events[0]["artifacts"][0]
    assert len(entry["id"]) == 43 and "path" not in entry
    assert events[0]["mid_turn"] and events[0]["turn_id"] == tid
    assert session_store.load_history(key)[0]["artifacts"] == [entry]
    file.unlink()
    session_store.finish_turn(tid, "完成")
    from vococo.core.agent import AgentReply
    await adapter.make_sink("files-test").done(AgentReply(text="完成", tool_calls=[], cost_usd=0, is_error=False), tid)
    assert events[-1]["artifacts"] == [entry]
    # 断开再打开临时 SQLite，验证不是内存或临时工具事件。
    _db.reset()
    assert session_store.load_history(key)[0]["artifacts"] == [entry]
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "artifact-test-auth")
    app = web.Application()
    app.router.add_get("/artifacts/{artifact_id}/download", adapter._handle_artifact_download)
    url = f'/artifacts/{entry["id"]}/download?conv=files-test'
    async with TestClient(TestServer(app)) as client:
        assert (await client.get(url)).status == 401
        headers = {"X-Auth-Token": config.WEB_AUTH_TOKEN}
        response = await client.get(url, headers=headers)
        assert response.status == 200 and await response.read() == content
        assert response.headers["Content-Disposition"].startswith("attachment;")
        assert "filename*=UTF-8''" in response.headers["Content-Disposition"]
        assert response.headers["Cache-Control"] == "private, no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert (await client.get(url.replace("files-test", "other"), headers=headers)).status == 404
        assert (await client.get(url.replace(entry["id"], "not-an-id"), headers=headers)).status == 404
        session_store.delete_session(key)
        assert (await client.get(url, headers=headers)).status == 404
    assert _db.conn().execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


@pytest.mark.anyio
async def test_duplicate_session_clones_artifacts_with_independent_ids_and_lifetime(context):
    _, source, _, _, root = context
    file = root / "演示.pptx"
    data = pptx_bytes()
    file.write_bytes(data)
    assert "已交付" in await call(file)
    destination = config.resolve_session_key("web", "copied-files")
    original = session_store.load_history(source)[0]["artifacts"][0]
    session_store.duplicate_session(source, destination, "副本")
    duplicate = session_store.load_history(destination)[0]["artifacts"][0]
    assert original["id"] != duplicate["id"]
    assert duplicate["filename"] == original["filename"]
    assert session_store.artifact_content(destination, original["id"]) is None
    assert session_store.artifact_content(source, duplicate["id"]) is None
    session_store.delete_session(source)
    assert session_store.artifact_content(destination, duplicate["id"]) == (file.name, data)
    session_store.delete_session(destination)
    assert _db.conn().execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["missing", "symlink", "directory-link", "hardlink", "fifo", "directory", "escape",
                                  "outside", "hidden", "credentials", "secret", "key", "large", "empty", "url", "runtime"])
async def test_rejects_unsafe_source_without_event_or_snapshot(context, kind):
    _, key, tid, events, root = context
    file = root / "result.txt"
    if kind == "missing":
        pass
    elif kind == "fifo":
        os.mkfifo(file)
    elif kind == "directory":
        file.mkdir()
    elif kind == "directory-link":
        (root / "actual").mkdir()
        (root / "actual" / "result.txt").write_text("content")
        (root / "link").symlink_to(root / "actual", target_is_directory=True)
        file = root / "link" / "result.txt"
    elif kind == "url":
        file = "https://example.com/file.txt"
    elif kind == "runtime":
        file = config.DATA_DIR / "other.txt"
        file.write_text("runtime content")
    else:
        file.write_text({"secret": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ12345", "key": "-----BEGIN PRIVATE KEY-----abc"}.get(kind, "content"))
        if kind == "symlink":
            (root / "link.txt").symlink_to(file)
            file = root / "link.txt"
        elif kind == "hardlink":
            os.link(file, root / "hard.txt")
        elif kind == "escape":
            file = root / ".." / "project" / "result.txt"
        elif kind == "outside":
            file = file.rename(root.parent / "result.txt")
        elif kind == "hidden":
            file = file.rename(root / ".env.txt")
        elif kind == "credentials":
            file = file.rename(root / "credentials.txt")
        elif kind == "extension":
            file = file.rename(root / "state.db")
        elif kind == "large":
            with file.open("wb") as stream:
                stream.truncate(artifacts.MAX_BYTES + 1)
        elif kind == "empty":
            file.write_bytes(b"")
    assert "未交付" in await call(file)
    assert events == [] and not session_store.turn_artifacts(key, tid)


@pytest.mark.anyio
@pytest.mark.parametrize("entry,payload", [("credentials.txt", "content"), ("safe.txt", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ12345"),
                                           ("../escape.txt", "content"), (".env", "content"), ("nested.zip", "PK\x03\x04fake")])
async def test_rejects_unsafe_archive(context, entry, payload):
    root = context[-1]
    file = root / "bundle.zip"
    with zipfile.ZipFile(file, "w") as archive:
        archive.writestr(entry, payload)
    assert "未交付" in await call(file)
    assert not context[3]


@pytest.mark.anyio
async def test_arbitrary_format_and_archive_are_delivered(context):
    root = context[-1]
    project = root / "project.mkcd"
    project.write_bytes(b"binary project content")
    assert "已交付" in await call(project)
    bundle = root / "bundle.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("project.mkcd", b"binary project content")
    assert "已交付" in await call(bundle)


@pytest.mark.anyio
async def test_code_requires_review_of_exact_snapshot(context):
    root = context[-1]
    source = root / "script.py"
    source.write_text("print('hello')")
    result = await call(source)
    assert "审核" in result and "未交付" in result
    import hashlib
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert digest in result
    source.write_text("print('changed')")
    assert "未交付" in await call(source, reviewed_sha256=digest)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert "已交付" in await call(source, reviewed_sha256=digest)


@pytest.mark.anyio
async def test_archive_code_requires_review(context):
    import hashlib
    file = context[-1] / "bundle.zip"
    with zipfile.ZipFile(file, "w") as archive:
        archive.writestr("src/app.js", "console.log('hello')")
    result = await call(file)
    assert "未交付" in result and "src/app.js" in result
    assert "已交付" in await call(file, reviewed_sha256=hashlib.sha256(file.read_bytes()).hexdigest())


@pytest.mark.anyio
async def test_nested_zip_code_and_secret_checks_ignore_extension(context):
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as archive:
        archive.writestr("script.py", "print('hello')")
    file = context[-1] / "bundle.mcpack"
    with zipfile.ZipFile(file, "w") as archive:
        archive.writestr("nested.zip", nested.getvalue())
    result = await call(file)
    assert "未交付" in result and "nested.zip → script.py" in result
    with zipfile.ZipFile(file, "w") as archive:
        archive.writestr("secret.py", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ12345")
    assert "未交付" in await call(file)


@pytest.mark.anyio
async def test_prefixed_zip_and_unsupported_compression_are_checked(context):
    import gzip
    root = context[-1]
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("credentials.txt", "sensitive")
    file = root / "file.bin"
    file.write_bytes(b"prefix" + archive.getvalue())
    assert "未交付" in await call(file)
    compressed = gzip.compress(b"ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ12345")
    file.write_bytes(compressed)
    assert "未交付" in await call(file)
    safe_zip = io.BytesIO()
    with zipfile.ZipFile(safe_zip, "w") as bundle:
        bundle.writestr("safe.txt", "safe")
    polyglot = compressed + safe_zip.getvalue()
    file.write_bytes(polyglot)
    assert "压缩格式" in await call(file)
    with zipfile.ZipFile(file, "w") as bundle:
        bundle.writestr("nested.bin", polyglot)
    assert "压缩格式" in await call(file)
    source = root / "app.mjs"
    source.write_text("console.log('hello')")
    assert "审核" in await call(source)
    source = root / "script.bat"
    source.write_text("@echo off")
    assert "审核" in await call(source)
    file.write_bytes("-----BEGIN PRIVATE KEY-----abc".encode("utf-16"))
    assert "未交付" in await call(file)
    import tarfile
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        info = tarfile.TarInfo("credentials.txt")
        info.size = 6
        tar.addfile(info, io.BytesIO(b"secret"))
    old_tar = bytearray(archive.getvalue())
    old_tar[257:265] = b"\0" * 8
    old_tar[148:156] = b" " * 8
    old_tar[148:156] = f"{sum(old_tar[:512]):06o}\0 ".encode()
    file.write_bytes(old_tar)
    assert "未交付" in await call(file)


@pytest.mark.anyio
async def test_damaged_compressed_archive_fails_with_clear_reason(context):
    file = context[-1] / "broken.zip"
    with zipfile.ZipFile(file, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("report.txt", "hello" * 100)
    data = bytearray(file.read_bytes())
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entry = archive.infolist()[0]
        offset = entry.header_offset + 30 + len(entry.filename.encode()) + len(entry.extra)
    data[offset] = 0xff
    file.write_bytes(data)
    assert "压缩包无法安全检查" in await call(file)


@pytest.mark.anyio
async def test_key_containers_and_lz4_are_rejected(context):
    root = context[-1]
    key = root / "identity.der"
    key.write_bytes(b"binary key")
    assert "密钥容器" in await call(key)
    bundle = root / "bundle.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("keys/identity.der", b"binary key")
    assert "密钥容器" in await call(bundle)
    data = root / "renamed.bin"
    data.write_bytes(b"\x04\x22\x4d\x18" + b"compressed data")
    assert "压缩格式" in await call(data)
    data.write_bytes(b"\x50\x2a\x4d\x18" + b"compressed data")
    assert "压缩格式" in await call(data)
    data.write_bytes(b"\x30\x03\x02\x01\x00")
    assert "DER" in await call(data)
    data.write_bytes(b"SQLite format 3\x00" + b"database")
    assert "数据库" in await call(data)
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("renamed.bin", data.read_bytes())
    assert "数据库" in await call(bundle)


@pytest.mark.anyio
async def test_invalid_lzma_zip_reports_error(context):
    file = context[-1] / "broken-lzma.zip"
    with zipfile.ZipFile(file, "w", compression=zipfile.ZIP_LZMA) as archive:
        archive.writestr("report.txt", "hello" * 100)
    data = bytearray(file.read_bytes())
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entry = archive.infolist()[0]
        offset = entry.header_offset + 30 + len(entry.filename.encode()) + len(entry.extra)
    data[offset + 4] = 0xff
    file.write_bytes(data)
    assert "压缩包无法安全检查" in await call(file)


@pytest.mark.anyio
async def test_standard_office_relationships_and_embedded_workbooks(context):
    workbook = io.BytesIO()
    with zipfile.ZipFile(workbook, "w") as archive:
        archive.writestr("_rels/.rels", "<Relationships/>")
        archive.writestr("xl/worksheets/sheet1.xml", "<sheet><cell>42</cell></sheet>")
    file = context[-1] / "chart.pptx"
    with zipfile.ZipFile(file, "w") as archive:
        archive.writestr("_rels/.rels", "<Relationships/>")
        archive.writestr("ppt/embeddings/Microsoft_Excel_Sheet1.xlsx", workbook.getvalue())
    assert "已交付" in await call(file)
    workbook = io.BytesIO()
    with zipfile.ZipFile(workbook, "w") as archive:
        archive.writestr("credentials.txt", "sensitive")
    with zipfile.ZipFile(file, "w") as archive:
        archive.writestr("ppt/embeddings/Microsoft_Excel_Sheet1.xlsx", workbook.getvalue())
    assert "未交付" in await call(file)


@pytest.mark.anyio
async def test_business_output_and_workspace_not_source_or_other_links(context, monkeypatch):
    from vococo.core import execution
    _, _, _, events, root = context
    workspace, output, source = (root.parent / name for name in ("workspace", "output", "source"))
    for folder in (workspace, output, source):
        folder.mkdir()
        (folder / "report.txt").write_text("成果")
    policy = SimpleNamespace(workspace=workspace, output_dir=output, source_dir=source)
    monkeypatch.setattr(execution, "policy_for_session", lambda *args: policy)
    token = danger.set_cwd(str(workspace))
    try:
        assert "已交付" in await call("report.txt")
        assert "已交付" in await call(output / "report.txt")
        assert "未交付" in await call(source / "report.txt")
        assert len(events) == 2
    finally:
        danger.reset_cwd(token)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["workspace", "project"])
async def test_writable_links_share_write_and_private_delivery_authorization(context, mode):
    from vococo.memory import agents

    adapter, _, _, events, root = context
    writable, readonly = root.parent / "writable", root.parent / "readonly"
    for folder in (writable, readonly):
        folder.mkdir()
        (folder / "report.txt").write_text("授权目录的成果")
    agent = agents.create("交付授权测试", str(root))
    meta = agents._read_meta(agent["id"])
    meta["execution_mode"] = mode
    agents._write_meta(agent["id"], meta)
    agents.update(agent["id"], links=[{"path": str(writable), "writable": True},
                                     {"path": str(readonly), "writable": False}])
    key = agents.main_session_key(agent)
    tid = session_store.start_turn(key, "生成并交付")
    route = clarify.set_current(key, adapter, agent["main_conv"], tid)
    try:
        assert danger.classify("Write", {"file_path": str(writable / "report.txt")}, str(root))[0] == "allow"
        assert "已交付" in await call(writable / "report.txt")
        entry = session_store.turn_artifacts(key, tid)[0]
        assert session_store.artifact_content(key, entry["id"]) == ("report.txt", "授权目录的成果".encode())
        assert "未交付" in await call(readonly / "report.txt")
        (writable / "credentials.txt").write_text("content")
        assert "未交付" in await call(writable / "credentials.txt")
        (writable / "link.txt").symlink_to(writable / "report.txt")
        assert "未交付" in await call(writable / "link.txt")
        assert len(events) == 1
        agents.update(agent["id"], links=[])
        assert "未交付" in await call(writable / "report.txt")
        assert len(events) == 1
    finally:
        clarify.reset_current(route)
    # 同一文件不能借其他会话交付。
    assert "未交付" in await call(writable / "report.txt")


@pytest.mark.anyio
async def test_unsafe_link_does_not_block_normal_delivery_or_expose_runtime(context):
    from vococo.core import execution
    from vococo.memory import agents

    adapter, _, _, _, root = context
    agent = agents.create("关联目录测试", str(root))
    agents.update(agent["id"], links=[{"path": str(config.DATA_DIR), "writable": True},
                                     {"path": str(root.parent / ".ssh"), "writable": True}])
    key = agents.main_session_key(agent)
    assert config.DATA_DIR.resolve() in execution.linked_file_roots(key)
    assert artifacts._delivery_links(key) == ()
    file = root / "normal.txt"
    file.write_text("普通成果")
    runtime = config.DATA_DIR / "other.txt"
    runtime.write_text("其他会话文件")
    tid = session_store.start_turn(key, "交付")
    route = clarify.set_current(key, adapter, agent["main_conv"], tid)
    try:
        assert "已交付" in await call(file)
        assert "未交付" in await call(runtime)
    finally:
        clarify.reset_current(route)


@pytest.mark.anyio
async def test_context_closed_turn_failure_and_no_home_fallback(context, monkeypatch):
    adapter, key, tid, events, root = context
    file = root / "report.txt"
    file.write_text("成果")
    token = clarify.set_current(key, object(), "files-test", tid)
    try:
        assert "仅 Web" in await call(file)
    finally:
        clarify.reset_current(token)
    for route_key, chat, turn in [(key, "other", tid), ("web:other", "other", tid), (key, "files-test", None)]:
        token = clarify.set_current(route_key, adapter, chat, turn)
        try:
            assert "未交付" in await call(file)
        finally:
            clarify.reset_current(token)
    assert "未交付" in await call(file, title="a" * 121)
    token = danger.set_cwd(None)
    try:
        assert "工作目录" in await call(file)
    finally:
        danger.reset_cwd(token)
    with monkeypatch.context() as scoped:
        scoped.setattr("pathlib.Path.home", lambda: root)
        assert "未交付" in await call(file)
    session_store.finish_turn(tid, "done")
    assert "已结束" in await call(file)
    assert not events


@pytest.mark.anyio
async def test_development_worktree_and_sibling_session_isolation(context):
    root = context[-1] / ".claude" / "worktrees" / "current"
    sibling = root.parent / "other"
    for folder in (root, sibling):
        folder.mkdir(parents=True)
        (folder / "result.txt").write_text("成果")
    token = danger.set_cwd(str(root))
    try:
        assert "已交付" in await call("result.txt")
        assert "未交付" in await call(sibling / "result.txt")
    finally:
        danger.reset_cwd(token)
    hidden = context[-1] / ".ssh"
    hidden.mkdir()
    (hidden / "result.txt").write_text("sensitive")
    token = danger.set_cwd(str(hidden))
    try:
        assert "未交付" in await call("result.txt")
    finally:
        danger.reset_cwd(token)


@pytest.mark.anyio
async def test_db_failure_no_success_or_event(context, monkeypatch):
    (context[-1] / "file.txt").write_text("成果")
    def fail(*args):
        raise OSError("db failure")
    monkeypatch.setattr(session_store, "append_artifact", fail)
    assert "未交付" in await call("file.txt")
    assert not context[3]


@pytest.mark.anyio
async def test_exact_64mb_limit_and_per_turn_count(context):
    _, key, tid, events, root = context
    file = root / "large.txt"
    with file.open("wb") as stream:
        stream.truncate(artifacts.MAX_BYTES)
    assert "已交付" in await call(file)
    assert session_store.turn_artifacts(key, tid)[0]["size"] == artifacts.MAX_BYTES
    for index in range(9):
        file.write_text(f"small {index}")
        assert "已交付" in await call(file)
    file.write_text("eleventh")
    assert "最多" in await call(file)
    assert len(events) == 10


def test_send_file_is_registered_once(context, monkeypatch):
    tools = []
    monkeypatch.setattr(builtin, "create_sdk_mcp_server", lambda name, tools: tools)
    servers = builtin.build_mcp_servers()
    assert sum(tool.name == "send_file" for tool in servers["vococo"]) == 1
