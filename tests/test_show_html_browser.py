"""运行真实 Web 页面和工具/流事件链路；仅模型输出由确定性输入代替。"""
import asyncio
import base64
import io
import json
import os

from PIL import Image
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


def image_layout_fixture():
    images = []
    for kind in ('PNG', 'JPEG', 'WEBP'):
        output = io.BytesIO()
        Image.new('RGB', (24, 16), '#d83850').save(output, format=kind)
        uri = f'data:image/{kind.lower()};base64,' + base64.b64encode(output.getvalue()).decode()
        images.append(f'<img class="raster" src="{uri}" width="24" height="16">')
    svg_uri = 'data:image/svg+xml;base64,' + base64.b64encode(
        b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="20" height="20"><animate attributeName="fill" values="red;blue" dur="0.1s" repeatCount="indefinite"/></rect></svg>'
    ).decode()
    css = (f'<style>#css-block{{background-image:url({svg_uri})}}</style><div id="css-block">CSS</div>'
           f'<div id="css-inline" style="background-image:url({svg_uri})">CSS</div>'
           '<style>#css-escaped{background-image:url("d\\\\61ta:image/svg+xml,test")}</style><div id="css-escaped">CSS</div>')
    return (css + '<div id="layout" style="position:relative;width:300px;height:100px">'
            '<div id="positioned" style="position:absolute;left:80px;top:30px;width:24px;height:16px;background:red"></div></div>'
            + ''.join(images) + '<svg id="drawing" viewBox="0 0 300 100" width="300" height="100">'
            '<rect x="4" y="5" width="30" height="20" fill="red"/>'
            '<text x="10" y="50">第一行</text><text x="10" y="80">第二行</text>'
            f'<image href="{uri}" x="60" y="5" width="24" height="16"/>'
            '</svg><img id="unsafe-data" src="data:image/svg+xml,%3Csvg onload=alert(1)%3E">'
            '<img id="unsafe-url" src="javascript:alert(1)" onerror="parent.HTML_PREVIEW_ATTACK=1">')


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
              '<style>@import "https://html-preview-blocked.invalid/style";</style>'
              + image_layout_fixture() + '<p>可滚动的文件内容</p>' * 30 + ' ' * 32000)
    file = isolated / "page.html"
    file.write_text(source)

    async def drive_tool():
        message = await adapter._inbox.get()
        assert message.text == "preview"
        key = config.resolve_session_key("web", "html-e2e")
        tid = session_store.start_turn(key, message.text)
        route = clarify.set_current(key, adapter, "html-e2e")
        bound = clarify.bind_turn(key, tid)
        workdir = isolated / "workspace"
        workdir.mkdir()
        cwd = danger.set_cwd(str(workdir))
        async def approve(*args, **kwargs):
            pytest.fail("目录外 HTML 展示不得触发审批")
        monkeypatch.setattr(danger, "require_approval", approve)
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
        for name in ('card', 'share'):
            with Image.open(screenshot_dir / f'show-html-{name}-svg.png') as image:
                red, green, blue = image.convert('RGB').getpixel((70, 10))
                assert red > 180 and green < 100 and blue < 120, 'SVG 内嵌位图必须实际绘制，不只保留 href'
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if adapter._runner:
            await adapter._runner.cleanup()


@pytest.mark.anyio
@pytest.mark.skipif(not os.environ.get('HTML_PREVIEW_FILES'), reason='仅按需验收本机文件，不收录业务资料')
async def test_local_html_files_real_preview(isolated, monkeypatch):
    monkeypatch.setattr(config, 'WEB_HOST', '127.0.0.1')
    monkeypatch.setattr(config, 'WEB_PORT', 0)
    monkeypatch.setattr(config, 'WEB_AUTH_TOKEN', 'local-file-test-auth')
    monkeypatch.setattr(config, 'VOICE_ENABLED', False)
    adapter = WebAdapter()
    key = config.resolve_session_key('web', 'file-validation')
    try:
        await adapter._start_server()
        for index, source_path in enumerate(json.loads(os.environ['HTML_PREVIEW_FILES'])):
            source = Path(source_path).read_text()
            file = isolated / f'input-{index}.html'
            file.write_text(source)
            tid = session_store.start_turn(key, f'本机验收 {index + 1}')
            route = clarify.set_current(key, adapter, 'file-validation')
            bound = clarify.bind_turn(key, tid)
            cwd = danger.set_cwd(str(isolated))
            try:
                result = await builtin.show_html.handler({'path': str(file), 'title': f'本机验收 {index + 1}'})
                assert '已展示' in result['content'][0]['text']
                assert session_store.turn_html_previews(key, tid)[0]['html'] == source
                session_store.finish_turn(tid, '本机文件静态验收')
            finally:
                danger.reset_cwd(cwd)
                clarify.unbind_turn(key, tid, bound)
                clarify.reset_current(route)
        output = isolated / 'screenshots'
        output.mkdir()
        origin = f'http://127.0.0.1:{adapter._runner.addresses[0][1]}'
        process = await asyncio.create_subprocess_exec(
            'node', str(Path(__file__).with_name('html_preview_files_browser.cjs')),
            origin, config.WEB_AUTH_TOKEN, str(output), env=_browser_environment(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 120)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        assert process.returncode == 0, stderr.decode() + stdout.decode()
        screenshots = list(output.glob('*.png'))
        assert screenshots
        for screenshot in screenshots:
            with Image.open(screenshot) as image:
                assert len(image.convert('RGB').getcolors(image.width * image.height)) > 20, '图片必须实际绘制，不能只有空白'
        print(stdout.decode())
        print(f'已验证 {len(screenshots)} 张位图截图非空白；资料仅保留在本机测试临时目录。')
    finally:
        if adapter._runner:
            await adapter._runner.cleanup()
