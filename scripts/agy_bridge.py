"""本机 AGY 文本转发验收入口；不启动 vococo 生产路由。"""

import argparse
import hmac
import json
import os
from contextlib import aclosing, contextmanager, suppress
from tempfile import TemporaryDirectory

from aiohttp import web

from vococo.gateway.agy_trial import DEFAULT_MODEL, MAX_PROMPT, MODELS, UpstreamError, cli_binary, cli_config_is_clean, generate


def parse_input(data):
    if not isinstance(data, dict) or data.keys() - {"model", "prompt", "stream"}:
        raise ValueError
    prompt, model, stream = data.get("prompt"), data.get("model", DEFAULT_MODEL), data.get("stream", False)
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
        raise ValueError
    if model not in MODELS or not isinstance(stream, bool):
        raise ValueError
    return model, prompt, stream


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


async def stream_answer(request, events):
    response = web.StreamResponse(headers={
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
    })
    await response.prepare(request)
    try:
        await response.write(b": connected\n\n")
        async for event, data in events:
            await response.write(sse(event, data))
    except UpstreamError as exc:
        await response.write(sse("error", {"code": exc.code}))
    return response


async def json_answer(events, model):
    chunks = []
    try:
        async for event, data in events:
            if event == "delta":
                chunks.append(data["text"])
    except UpstreamError as exc:
        return web.json_response({"error": exc.code}, status=exc.status)
    return web.json_response({"model": model, "text": "".join(chunks)}, headers={"Cache-Control": "no-store"})


@contextmanager
def single_request(state):
    state["busy"] = True
    try:
        yield
    finally:
        state["busy"] = False


def make_app(key, command, cwd, timeout=90):
    app = web.Application(client_max_size=64 * 1024)
    state = {"busy": False}

    async def handle(request):
        header = request.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(header, f"Bearer {key}".encode()):
            return web.json_response({"error": "unauthorized"}, status=401)
        if request.content_type != "application/json":
            return web.json_response({"error": "expected_json"}, status=415)
        try:
            model, prompt, stream = parse_input(await request.json())
        except ValueError:
            return web.json_response({"error": "invalid_request"}, status=400)
        if state["busy"]:
            return web.json_response({"error": "busy"}, status=429)
        with single_request(state), suppress(ConnectionResetError):
            async with aclosing(generate(command, cwd, timeout, model, prompt)) as events:
                return await stream_answer(request, events) if stream else await json_answer(events, model)
        return web.Response(status=499)

    app.router.add_post("/v1/generate", handle)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()
    key = os.environ.get("AGY_BRIDGE_TOKEN", "")
    if len(key) < 32 or not key.isascii() or any(c.isspace() for c in key):
        parser.error("AGY_BRIDGE_TOKEN 必须是至少 32 字符的随机 ASCII 令牌")
    binary = cli_binary()
    if not os.access(binary, os.X_OK):
        parser.error(f"找不到可执行文件：{binary}")
    if not cli_config_is_clean():
        parser.error("Antigravity 存在全局自定义配置，不能启动纯文本试用")
    with TemporaryDirectory(prefix="vococo-agy-") as cwd:
        web.run_app(make_app(key, (str(binary),), cwd), host="127.0.0.1", port=args.port,
                    handler_cancellation=True, access_log=None, shutdown_timeout=5)


if __name__ == "__main__":
    main()
