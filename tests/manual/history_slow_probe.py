"""隔离实例的真实浏览器慢历史对照,不访问生产服务。

uv run python tests/manual/history_slow_probe.py --slow-ms 400 --out /tmp/history.json
加 --baseline 时浏览器加载 HEAD 的三个前端资源,其余测试条件相同。
慢接口串行发送 JSON 字节以模拟受限链路;这是可控压力模型,不是生产根因断言。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


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


def slow_history(handler, delay_ms, metrics):
    from aiohttp import web

    lock = asyncio.Lock()

    async def handle(request):
        response = await handler(request)
        if response.status != 200:
            return response
        transport = request.transport
        row = {"conv": request.query.get("conv"), "cancelled": False}
        metrics.append(row)
        # 排队期间也检查连接,取消能释放等待项,不必等整条响应发送完成。
        while lock.locked():
            if transport is None or transport.is_closing():
                row["cancelled"] = True
                return response
            await asyncio.sleep(0.01)
        async with lock:
            stream = web.StreamResponse(headers={"Content-Type": "application/json", "Cache-Control": "no-store"})
            try:
                await stream.prepare(request)
                steps = max(1, delay_ms // 20)
                for _ in range(steps):
                    await asyncio.sleep(delay_ms / steps / 1000)
                    await stream.write(b" ")
                await stream.write(response.body)
                await stream.write_eof()
            except (ConnectionResetError, RuntimeError):
                if transport is None or transport.is_closing():
                    row["cancelled"] = True
                else:
                    raise
            return stream
    return handle


async def main(arguments):
    os.environ.setdefault("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
    from vococo import config
    from vococo.gateway import settings_store
    from vococo.gateway.adapters.web import WebAdapter
    from vococo.memory import _db, session_store
    from vococo.core import tasks

    with tempfile.TemporaryDirectory(prefix="history-slow-") as temporary:
        sandbox = Path(temporary)
        config.DATA_DIR = sandbox / "data"
        config.AGENT_WORKSPACES_DIR = sandbox / "workspaces"
        config.AI_BRAIN_DIR = sandbox / "brain"
        config.OBSIDIAN_VAULT_DIR = sandbox / "vault"
        for name in ("IMAGES_DIR", "AUDIO_DIR", "FILES_DIR", "VIDEOS_DIR", "PUBLISHED_DIR"):
            setattr(config, name, config.DATA_DIR / name.lower())
        for name in ("CRON_JOBS_PATH", "SUGGESTIONS_PATH", "HEARTBEAT_PATH", "WATCHDOG_LOG_PATH"):
            setattr(config, name, config.DATA_DIR / name.lower())
        config.WEB_HOST = "127.0.0.1"
        config.WEB_PORT = 0
        config.WEB_AUTH_TOKEN = "history-slow-probe"
        config.VOICE_ENABLED = False
        settings_store._PATH = config.DATA_DIR / "web_settings.json"
        _db._DB = None
        tasks._DB = None
        for i in range(5):
            key = config.resolve_session_key("web", f"probe-{i}")
            session_store.new_session(key)
            for n in range(60):
                session_store.append(key, f"问题 {i}-{n} " + "细节" * 80, f"回答 {i}-{n} " + "过程" * 120)
        key = config.resolve_session_key("web", "probe-live")
        session_store.new_session(key)
        session_store.append(key, "先在跑着的会话", "已经落库的部分")
        turn = session_store.start_turn(key, "还在跑的这条")
        session_store.flush_draft(turn, "进行中的草稿正文")
        environment = _browser_environment()
        if arguments.baseline:
            for name in ("index.html", "app-core.js", "sidebar.js"):
                content = subprocess.check_output(["git", "show", f"{arguments.baseline_ref}:vococo/gateway/adapters/web_static/{name}"], cwd=ROOT)
                (sandbox / name).write_bytes(content)
            environment["BASELINE_DIR"] = str(sandbox)
        adapter = WebAdapter()
        metrics = []
        adapter._handle_history = slow_history(adapter._handle_history, arguments.slow_ms, metrics)
        await adapter._start_server()
        origin = f"http://127.0.0.1:{adapter._runner.addresses[0][1]}"
        try:
            process = await asyncio.create_subprocess_exec(
                "node", str(Path(__file__).with_suffix(".cjs")), origin, config.WEB_AUTH_TOKEN, arguments.tag,
                env={**environment, "SLOW_MS": str(arguments.slow_ms)},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
            except TimeoutError:
                process.kill()
                await process.wait()
                raise SystemExit("探测超时(180s)")
            if process.returncode:
                raise SystemExit(stderr.decode() + stdout.decode())
            result = json.loads(stdout.decode())
            result["server"] = {"started": len(metrics), "cancelled": sum(row["cancelled"] for row in metrics)}
            text = json.dumps(result, ensure_ascii=False, indent=2)
            if arguments.out:
                Path(arguments.out).write_text(text, encoding="utf-8")
            print(text)
            return result
        finally:
            await adapter._runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="run")
    parser.add_argument("--slow-ms", type=int, default=1200)
    parser.add_argument("--out", default="")
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--baseline-ref", default="HEAD")
    asyncio.run(main(parser.parse_args()))
