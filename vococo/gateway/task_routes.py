"""通用后台任务 HTTP/SSE 路由。

任务不属于 voice 或 chat;输入来源只影响创建时的元数据,查询和状态流走中性接口。
旧 /voice/tasks* 路由在兼容期内继续由 voice.routes 转发到这里。
"""
from __future__ import annotations

import asyncio

from aiohttp import web

from ..core import task_events, task_runner, tasks
from .web_auth import check_web_auth


def _guard(request: web.Request, *, allow_query_token: bool = False) -> web.Response | None:
    """薄委托:规则实现收在 web_auth.check_web_auth(与 web.py 共用一份)。"""
    return check_web_auth(request, allow_query_token=allow_query_token)


def _dispatch_chat_id(request: web.Request) -> str | None:
    value = (request.query.get("session_key") or request.query.get("conv") or "").strip()
    if value.startswith("web:"):
        return value[4:]
    return value or None


async def handle_tasks_list(request: web.Request) -> web.Response:
    if (guard := _guard(request)) is not None:
        return guard
    source = (request.query.get("source") or "conversation").strip()
    if source not in {"conversation", "cron", "all"}:
        return web.json_response({"error": "source 不支持"}, status=400)
    try:
        limit = max(1, min(int(request.query.get("limit", "20")), 100))
    except ValueError:
        return web.json_response({"error": "limit 必须是数字"}, status=400)
    rows = tasks.list_recent_for_source(
        limit=limit,
        source=source,
        dispatch_chat_id=_dispatch_chat_id(request),
    )
    return web.json_response(rows)


async def handle_task_detail(request: web.Request) -> web.Response:
    if (guard := _guard(request)) is not None:
        return guard
    task = tasks.get(request.match_info["task_id"])
    if task is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(task)


async def handle_task_stop(request: web.Request) -> web.Response:
    if (guard := _guard(request)) is not None:
        return guard
    return web.json_response({"ok": task_runner.cancel(request.match_info["task_id"])})


async def _write_sse(resp: web.StreamResponse, event: str, payload: dict) -> None:
    import json

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    await resp.write(f"event: {event}\ndata: {body}\n\n".encode())


async def handle_tasks_stream(request: web.Request) -> web.StreamResponse:
    if (guard := _guard(request, allow_query_token=True)) is not None:
        return guard
    resp = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )
    await resp.prepare(request)
    queue = task_events.subscribe()
    try:
        while True:
            try:
                event, payload = await asyncio.wait_for(queue.get(), timeout=25)
            except asyncio.TimeoutError:
                await resp.write(b": keep-alive\n\n")
                continue
            await _write_sse(resp, event, payload)
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        task_events.unsubscribe(queue)
    return resp


def register_routes(app: web.Application) -> None:
    """注册中性任务 API,不依赖 VOICE_ENABLED。"""
    app.add_routes(
        [
            web.get("/tasks", handle_tasks_list),
            web.get("/tasks/stream", handle_tasks_stream),
            web.get("/tasks/{task_id}", handle_task_detail),
            web.post("/tasks/{task_id}/stop", handle_task_stop),
        ]
    )
