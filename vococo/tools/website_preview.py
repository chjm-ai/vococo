"""仅从发布器可信记录截图；浏览器全部断网，请求由受控代理代取。"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import signal
import tempfile

from .. import config
from ..deployment_settings import kimmy_target, require_target_binding
from ..memory import library

TIMEOUT = 35
_SCRIPT = Path(__file__).with_name("website_preview.cjs")


def preview_url(source_key: str) -> str:
    match = re.fullmatch(r"publish:(local|kimmy):([a-z0-9]+(?:-[a-z0-9]+)*)", source_key)
    if not match or len(match[2]) > 64:
        raise ValueError("只支持受控 local/kimmy 发布记录")
    origin = f"http://127.0.0.1:{config.WEB_PORT}/pub"
    if match[1] == 'kimmy':
        from .publishing import publication_state
        target = kimmy_target()
        state = publication_state(source_key)
        if not state:
            raise ValueError('截图缺少绑定当前部署目标的可信记录')
        require_target_binding(state, target)
        origin = target['public_origin']
    return f"{origin}/{match[2]}/"


def _runtime() -> tuple[str, dict]:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("缺少 Node，封面待生成；不会自动安装")
    # 不继承 .env、代理、令牌、Node 注入选项或登录状态。
    environment = {"PATH": os.defpath, "HOME": str(Path.home())}
    modules = Path(node).resolve().parent.parent / "lib/node_modules"
    if not (modules / "playwright/package.json").is_file():
        modules = Path(node).parent.parent / "lib/node_modules"
    if not (modules / "playwright/package.json").is_file():
        raise RuntimeError("缺少已有全局 Playwright，封面待生成；不会自动安装")
    environment["NODE_PATH"] = str(modules)
    return node, environment


async def _capture(url: str, output: Path) -> dict:
    node, environment = _runtime()
    browser_temporary = output.parent / 'browser-temp'
    browser_temporary.mkdir(mode=0o700, exist_ok=True)
    environment['TMPDIR'] = str(browser_temporary)
    process = await asyncio.create_subprocess_exec(
        node, str(_SCRIPT), url, str(output),
        kimmy_target()["public_origin"] if url.startswith("https://") else "", env=environment,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), TIMEOUT)
        if process.returncode:
            raise RuntimeError(stderr.decode(errors="replace")[-1000:] or "浏览器截图失败")
        return json.loads(stdout)
    finally:
        # 同组包含 Chrome 子进程；正常结束也清理，取消/超时不能遗留浏览器。
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def generate_cover(item_id: str, expected_version: str | None = None) -> dict:
    record = library.website_preview_record(item_id)
    if not record:
        return {"cover_status": "pending", "cover_error": "无可信发布记录，不能截图"}
    version = expected_version or record["version"]
    if version != record["version"]:
        return {"cover_status": "stale"}
    generation = library.begin_website_cover(item_id, version)
    if generation is None:
        return {"cover_status": "stale"}
    result = {"cover_status": "pending", "cover_retry": {"id": item_id}}
    try:
        with tempfile.TemporaryDirectory(prefix="vococo-cover-") as temporary:
            output = Path(temporary).resolve() / "cover.png"
            details = await _capture(preview_url(record["source_key"]), output)
            applied = await asyncio.to_thread(library.update_website_cover, item_id, version, output,
                                              "ready", details, expected_generation=generation)
            return {"cover_status": "ready" if applied else "stale", **details}
    except asyncio.CancelledError:
        await asyncio.to_thread(library.update_website_cover, item_id, version, None,
                                "pending", {"error": "截图已取消"}, expected_generation=generation)
        raise
    except Exception as exc:
        result["cover_error"] = str(exc)
        try:
            if not await asyncio.to_thread(library.update_website_cover, item_id, version, None,
                                           "pending", {"error": str(exc)}, expected_generation=generation):
                result["cover_status"] = "stale"
        except Exception as state_error:
            result["cover_state_error"] = str(state_error)
        return result
