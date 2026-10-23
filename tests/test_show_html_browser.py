"""运行真实 Web 页面和工具/流事件链路；仅模型输出由确定性输入代替。"""
import asyncio
import os
from pathlib import Path

import pytest

from vococo import config
from vococo.core.agent import AgentReply
from vococo.gateway import clarify
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import session_store
from vococo.tools import builtin, danger
from test_library_integration import _browser_environment


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_show_html_real_tool_browser(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "WEB_PORT", 0)
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "html-browser-test-auth")
    monkeypatch.setattr(config, "VOICE_ENABLED", False)
    adapter = WebAdapter()
    screenshot_dir = Path(os.environ.get("PREVIEW_SCREENSHOTS", str(isolated)))
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    source = ('<!doctype html><h1>工具 HTML 快照</h1><p>```不是围栏</p>'
              '<script>parent.HTML_PREVIEW_ATTACK=1</script>'
              '<img src="https://html-preview-blocked.invalid/image">'
              '<style>@import "https://html-preview-blocked.invalid/style";</style>' + ' ' * 32000)
    file = isolated / "page.html"
    file.write_text(source)

    async def drive_tool():
        message = await adapter._inbox.get()
        assert message.text == "preview"
        key = config.resolve_session_key("web", "html-e2e")
        tid = session_store.start_turn(key, message.text)
        route = clarify.set_current(key, adapter, "html-e2e")
        bound = clarify.bind_turn(key, tid)
        cwd = danger.set_cwd(str(isolated))
        try:
            sink = adapter.make_sink("html-e2e")
            await sink.tool_started("show_html", "preview-tool")
            args = {"path": str(file), "title": "工具快照"}
            await sink.tool_input("show_html", "preview-tool", args)
            result = await builtin.show_html.handler(args)
            assert "已展示" in result["content"][0]["text"]
            assert source not in str(result) and len(str(result)) < 500
            await builtin.show_html.handler(args)  # 真正从工具端防重。
            await sink.tool_finished("show_html", True, result["content"][0]["text"], "preview-tool")
            file.write_text("<p>文件已修改，但历史仍是旧快照</p>")
            finish = await adapter._inbox.get()
            assert finish.text == "finish"
            await sink.text("HTML 已生成，无需再贴代码。")
            session_store.finish_turn(tid, "HTML 已生成，无需再贴代码。")
            reply = AgentReply(text="HTML 已生成，无需再贴代码。", tool_calls=[],
                               cost_usd=0, is_error=False)
            await sink.done(reply, tid)
            late = await adapter._inbox.get()
            assert late.text == "late-media"
            # 模拟重连中排在已完成历史之后送达的旧中途帧，走真实 SSE。
            for attachment in [
                {"html_previews": session_store.turn_html_previews(key, tid)},
                {"images": ["data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"]},
                {"videos": [{"url": "data:video/mp4;base64,", "filename": "late.mp4"}]},
            ]:
                adapter._emit({"conv": "html-e2e", "type": "message", "mid_turn": True,
                               "turn_id": tid, **attachment})
            finish_late = await adapter._inbox.get()
            assert finish_late.text == "finish-late"
            await sink.done(reply, tid)
        finally:
            danger.reset_cwd(cwd)
            clarify.unbind_turn(key, tid, bound)
            clarify.reset_current(route)

    task = None
    process = None
    try:
        await adapter._start_server()
        origin = f"http://127.0.0.1:{adapter._runner.addresses[0][1]}"
        task = asyncio.create_task(drive_tool())
        process = await asyncio.create_subprocess_exec(
            "node", str(Path(__file__).with_name("show_html_browser.cjs")),
            origin, config.WEB_AUTH_TOKEN, str(screenshot_dir), env=_browser_environment(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 120)
        if task.done():
            await task
        assert process.returncode == 0, stderr.decode() + stdout.decode()
        await asyncio.wait_for(task, 5)
        print(stdout.decode())
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if adapter._runner:
            await adapter._runner.cleanup()
