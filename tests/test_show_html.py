"""真实工具、回合快照、路由与文件读取边界，不调用模型。"""
import os

import pytest

from vococo import config
from vococo.gateway import clarify
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import session_store
from vococo.tools import builtin, danger, html_preview


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def context(isolated, tmp_path):
    adapter = WebAdapter()
    key = config.resolve_session_key("web", "html-test")
    tid = session_store.start_turn(key, "生成网页")
    route = clarify.set_current(key, adapter, "html-test", tid)
    cwd = danger.set_cwd(str(tmp_path))
    events = []
    adapter._emit = events.append
    try:
        yield adapter, key, tid, events, tmp_path
    finally:
        danger.reset_cwd(cwd)
        clarify.reset_current(route)


async def call(path, **kwargs):
    result = await builtin.show_html.handler({"path": str(path), **kwargs})
    return result["content"][0]["text"]


@pytest.mark.anyio
async def test_snapshot_tool_midturn_done_history_and_duplicate(context):
    adapter, key, tid, events, root = context
    file = root / "page.html"
    source = "<h1>快照</h1>" + " " * 31000
    file.write_text(source)
    assert "已展示" in await call("page.html", title="结果")
    assert len(events) == 1 and events[0]["mid_turn"]
    entry = events[0]["html_previews"][0]
    assert entry["html"] == source
    assert events[0]["turn_id"] == tid
    assert "不重复" in await call(file)
    assert len(events) == 1
    file.unlink()
    session_store.finish_turn(tid, "已生成")
    from vococo.core.agent import AgentReply
    await adapter.make_sink("html-test").done(AgentReply(text="已生成", tool_calls=[], cost_usd=0, is_error=False), tid)
    assert events[-1]["type"] == "done"
    assert events[-1]["html_previews"] == [entry]
    assert session_store.load_history(key)[0]["html_previews"] == [entry]
    assert "html_previews" not in session_store.load_history("web:other")
    session_store.duplicate_session(key, "web:copy-html", "副本")
    assert session_store.load_history("web:copy-html")[0]["html_previews"] == [entry]


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["missing", "extension", "directory", "symlink", "parent-symlink", "escape", "hidden", "hardlink", "fifo", "large", "utf8", "binary", "empty", "secret", "credential-name", "url"])
async def test_unsafe_files_never_emit_or_persist(context, kind):
    _, key, _, events, root = context
    file = root / "bad.html"
    if kind == "missing":
        pass
    elif kind == "url":
        file = "https://example.com/a.html"
    elif kind == "escape":
        file = root / ".." / "escape.html"
    elif kind == "directory":
        file.mkdir()
    elif kind == "fifo":
        os.mkfifo(file)
    elif kind == "parent-symlink":
        (root / "actual").mkdir()
        (root / "actual" / "bad.html").write_text("<h1>safe</h1>")
        (root / "link").symlink_to(root / "actual", target_is_directory=True)
        file = root / "link" / "bad.html"
    else:
        content = {"large": b"x" * 200001, "utf8": b"\xff", "binary": b"\x00", "empty": b" ",
                   "secret": b"<p>ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ12345</p>"}.get(kind, b"<h1>test</h1>")
        file.write_bytes(content)
        if kind == "extension":
            file = file.rename(root / "bad.txt")
        if kind == "credential-name":
            file = file.rename(root / "credentials.html")
        if kind == "hidden":
            file = file.rename(root / ".env.html")
        if kind == "symlink":
            (root / "link.html").symlink_to(file)
            file = root / "link.html"
        if kind == "hardlink":
            os.link(file, root / "hard.html")
    assert "未展示" in await call(file)
    assert events == []
    assert not session_store.turn_html_previews(key, context[2])


@pytest.mark.anyio
async def test_outside_directory_requires_existing_approval(context, monkeypatch):
    _, _, _, events, root = context
    outside = root.parent / "approved.html"
    outside.write_text("<p>批准后</p>")
    decisions = []
    async def approve(reason, detail):
        decisions.append((reason, detail))
        return len(decisions) > 1
    monkeypatch.setattr(danger, "require_approval", approve)
    assert "未展示" in await call(outside)
    assert not events
    assert "已展示" in await call(outside)
    assert len(decisions) == 2


@pytest.mark.anyio
async def test_exact_limit_and_later_turn_gets_own_snapshot(context):
    _, key, tid, events, root = context
    (root / "exact.HTM").write_bytes(b"a" * 200000)
    assert "已展示" in await call("exact.HTM")
    # 新一轮已占坑时，旧上下文仍不能写进最新轮。
    next_id = session_store.start_turn(key, "下一轮")
    (root / "two.html").write_text("<p>old-bound</p>")
    assert "已展示" in await call("two.html")
    assert len(session_store.turn_html_previews(key, tid)) == 2
    assert not session_store.turn_html_previews(key, next_id)
    session_store.finish_turn(tid, "结束")
    assert "已结束" in await call("two.html")


@pytest.mark.anyio
async def test_wrong_session_missing_turn_and_nonweb_do_not_display(context):
    adapter, key, tid, events, root = context
    (root / "ok.html").write_text("<h1>OK</h1>")
    for route_key, chat, turn, target in [(key, "other", tid, adapter), ("web:other", "other", tid, adapter),
                                          (key, "html-test", None, adapter), (key, "html-test", tid, object())]:
        token = clarify.set_current(route_key, target, chat, turn)
        try:
            result = await call("ok.html")
            assert any(word in result for word in ["未展示", "不属于"])
        finally:
            clarify.reset_current(token)
    assert not events


@pytest.mark.anyio
async def test_failed_persistence_no_success_event(context, monkeypatch):
    _, _, _, events, root = context
    (root / "ok.html").write_text("<p>ok</p>")
    def fail(*args):
        raise OSError("database failure")
    monkeypatch.setattr(session_store, "append_html_preview", fail)
    assert "未展示" in await call("ok.html")
    assert not events


@pytest.mark.anyio
async def test_no_cwd_and_per_turn_count_limit(context):
    _, key, tid, events, root = context
    token = danger.set_cwd(None)
    try:
        assert "工作目录" in await call("ok.html")
    finally:
        danger.reset_cwd(token)
    file = root / "ok.html"
    for index in range(10):
        file.write_text(f"<p>{index}</p>")
        assert "已展示" in await call(file)
    file.write_text("<p>11</p>")
    assert "最多" in await call(file)
    assert len(events) == len(session_store.turn_html_previews(key, tid)) == 10
    assert "title" in await call(file, title="x" * 121)


@pytest.mark.anyio
async def test_bound_turn_follows_current_source_not_stale_route(context):
    _, key, tid, events, root = context
    file = root / "ok.html"
    file.write_text("<p>first</p>")
    first = clarify.bind_turn(key, tid)
    try:
        assert "已展示" in await call(file)
    finally:
        clarify.unbind_turn(key, tid, first)
    session_store.finish_turn(tid, "第一轮结束")
    second_id = session_store.start_turn(key, "第二轮")
    second = clarify.bind_turn(key, second_id)
    try:
        assert "已展示" in await call(file)
        assert events[-1]["turn_id"] == second_id
        assert len(session_store.turn_html_previews(key, second_id)) == 1
    finally:
        clarify.unbind_turn(key, second_id, second)


def test_prompt_registration_disabled_tools_and_child_isolation(monkeypatch):
    from vococo.core import prompt, subagent
    captured = []
    def server(name, tools):
        captured.extend(tools)
        return object()
    monkeypatch.setattr(builtin, "create_sdk_mcp_server", server)
    builtin.build_mcp_servers()
    assert [t.name for t in captured].count("show_html") == 1
    assert "show_html" in prompt.PERSONA
    assert "show_html" not in subagent._NATIVE_TOOLS
    assert "脚本不执行" in builtin.show_html.description
